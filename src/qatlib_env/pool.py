from __future__ import annotations

from dataclasses import dataclass, asdict
import contextlib
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


def pool_state_dir() -> Path:
    from .locations import pool_paths
    return pool_paths()[2]


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        if os.name == "nt":
            import msvcrt
            try: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError: pass
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _identity(pid: int) -> str | None:
    """Return a process creation identity suitable for PID-reuse checks."""
    if sys.platform.startswith("linux"):
        try:
            text = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
            return text.rsplit(") ", 1)[1].split()[19]
        except (OSError, IndexError):
            return None
    if sys.platform == "darwin":
        try:
            return subprocess.check_output(["ps", "-p", str(pid), "-o", "lstart="], text=True, stderr=subprocess.DEVNULL).strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return None
            created = wintypes.FILETIME(); exited = wintypes.FILETIME(); kernel_time = wintypes.FILETIME(); user_time = wintypes.FILETIME()
            try:
                if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                return f"{created.dwHighDateTime:08x}{created.dwLowDateTime:08x}"
            finally:
                kernel.CloseHandle(handle)
        except (OSError, AttributeError, TypeError):
            return None
    return None


@dataclass
class Lease:
    token: str
    pid: int
    start_identity: str
    memory_mib: int
    cpu: int
    heartbeat: float
    owner: str

    def alive(self, now: float | None = None, ttl: float = 90.0) -> bool:
        now = time.time() if now is None else now
        if now - self.heartbeat > ttl:
            return False
        if self.pid == os.getpid():
            return True
        try:
            os.kill(self.pid, 0)
        except OSError:
            return False
        current = _identity(self.pid)
        return current is not None and current == self.start_identity


class Pool:
    schema_version = 1

    def __init__(self, *, budget_mib: int = 24576, budget_cpu: int = 4,
                 state_dir: Path | None = None, ttl: float = 90.0):
        if type(budget_mib) is not int or type(budget_cpu) is not int or min(budget_mib, budget_cpu) < 1:
            raise ValueError("pool limits must be positive integers")
        self.budget_mib, self.budget_cpu = budget_mib, budget_cpu
        self.state_dir = state_dir or pool_state_dir()
        self.ledger = self.state_dir / "ledger.json"
        self.lock_path = self.state_dir / "ledger.lock"
        self.ttl = ttl

    def _read(self) -> dict[str, object]:
        try:
            data = json.loads(self.ledger.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema_version", self.schema_version) != self.schema_version:
                raise ValueError("unsupported pool ledger schema")
            return data
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as error:
            raise ValueError("pool ledger is unreadable or corrupt; admission denied") from error

    def _reap(self, data: dict[str, object]) -> list[Lease]:
        rows = data.get("leases", [])
        try:
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("invalid leases")
            leases = [Lease(**item) for item in rows]
        except (TypeError, ValueError) as error:
            raise ValueError("corrupt pool lease entries; admission denied") from error
        kept = [lease for lease in leases if lease.alive(ttl=self.ttl)]
        data["leases"] = [asdict(lease) for lease in kept]
        return kept

    def acquire(self, memory_mib: int, cpu: int = 1, *, owner: str = "local") -> Lease:
        if type(memory_mib) is not int or type(cpu) is not int or min(memory_mib, cpu) < 1:
            raise ValueError("pool requests must be positive integers")
        memory_mib, cpu = max(2048, int(memory_mib)), max(1, int(cpu))
        if memory_mib > self.budget_mib or cpu > self.budget_cpu:
            raise PoolCapacityError(f"request {memory_mib}MiB/{cpu}cpu exceeds pool hard limit {self.budget_mib}MiB/{self.budget_cpu}cpu")
        with _locked(self.lock_path):
            data = self._read()
            data.setdefault("schema_version", self.schema_version)
            leases = self._reap(data)
            # Concurrent callers cannot enlarge the pool held by active leases.
            previous = data.get("limits", {}) if leases else {}
            budget_mib = min(self.budget_mib, previous.get("memory_mib", self.budget_mib))
            budget_cpu = min(self.budget_cpu, previous.get("cpu", self.budget_cpu))
            if memory_mib > budget_mib or cpu > budget_cpu:
                raise PoolCapacityError("request exceeds active pool hard limits")
            used_mib = sum(x.memory_mib for x in leases)
            used_cpu = sum(x.cpu for x in leases)
            if used_mib + memory_mib > budget_mib or used_cpu + cpu > budget_cpu:
                raise RuntimeError(f"pool admission denied: used={used_mib}MiB/{used_cpu}cpu")
            lease = Lease(uuid.uuid4().hex, os.getpid(), _identity(os.getpid()), memory_mib, cpu, time.time(), owner)
            data["leases"] = [asdict(x) for x in leases] + [asdict(lease)]
            data["schema_version"] = self.schema_version
            data["limits"] = {"memory_mib": budget_mib, "cpu": budget_cpu}
            self.ledger.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            return lease

    def heartbeat(self, lease: Lease) -> Lease:
        with _locked(self.lock_path):
            data = self._read(); leases = self._reap(data); found = False
            for item in leases:
                if item.token == lease.token:
                    item.heartbeat = time.time(); found = True
            if not found:
                raise RuntimeError("lease is no longer active")
            data["leases"] = [asdict(x) for x in leases]
            self.ledger.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        lease.heartbeat = time.time(); return lease

    def release(self, lease: Lease) -> None:
        with _locked(self.lock_path):
            data = self._read(); leases = [x for x in self._reap(data) if x.token != lease.token]
            data["leases"] = [asdict(x) for x in leases]
            self.ledger.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def status(self) -> dict[str, object]:
        with _locked(self.lock_path):
            data = self._read(); leases = self._reap(data)
            data["leases"] = [asdict(x) for x in leases]
            self.ledger.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            limits = data.get("limits", {}) if leases else {}
            return {"schema_version": self.schema_version, "budget_mib": min(self.budget_mib, limits.get("memory_mib", self.budget_mib)),
                    "budget_cpu": min(self.budget_cpu, limits.get("cpu", self.budget_cpu)), "used_mib": sum(x.memory_mib for x in leases),
                    "used_cpu": sum(x.cpu for x in leases), "leases": [asdict(x) for x in leases]}

    def prune(self) -> dict[str, object]:
        return self.status()


def lease_id(lease: Lease) -> str:
    return hashlib.sha256(lease.token.encode()).hexdigest()[:12]


class PoolCapacityError(RuntimeError):
    """An impossible request must fail immediately, without waiting for leases."""

