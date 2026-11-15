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
    base = os.environ.get("XDG_STATE_HOME") or os.environ.get("XDG_CACHE_HOME")
    return Path(base or (Path.home() / ".cache")) / "qatlib-env" / "pool"


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
        self.budget_mib, self.budget_cpu = max(2048, budget_mib), max(1, budget_cpu)
        self.state_dir = state_dir or pool_state_dir()
        self.ledger = self.state_dir / "ledger.json"
        self.lock_path = self.state_dir / "ledger.lock"
        self.ttl = ttl

    def _read(self) -> dict[str, object]:
        try:
            data = json.loads(self.ledger.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _reap(self, data: dict[str, object]) -> list[Lease]:
        leases = [Lease(**item) for item in data.get("leases", []) if isinstance(item, dict)]
        kept = [lease for lease in leases if lease.alive(ttl=self.ttl)]
        data["leases"] = [asdict(lease) for lease in kept]
        return kept

    def acquire(self, memory_mib: int, cpu: int = 1, *, owner: str = "local") -> Lease:
        memory_mib, cpu = max(2048, int(memory_mib)), max(1, int(cpu))
        with _locked(self.lock_path):
            data = self._read()
            data.setdefault("schema_version", self.schema_version)
            leases = self._reap(data)
            used_mib = sum(x.memory_mib for x in leases)
            used_cpu = sum(x.cpu for x in leases)
            if used_mib + memory_mib > self.budget_mib or used_cpu + cpu > self.budget_cpu:
                raise RuntimeError(f"pool admission denied: used={used_mib}MiB/{used_cpu}cpu")
            lease = Lease(uuid.uuid4().hex, os.getpid(), _identity(os.getpid()), memory_mib, cpu, time.time(), owner)
            data["leases"] = [asdict(x) for x in leases] + [asdict(lease)]
            data["schema_version"] = self.schema_version
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
            return {"schema_version": self.schema_version, "budget_mib": self.budget_mib,
                    "budget_cpu": self.budget_cpu, "used_mib": sum(x.memory_mib for x in leases),
                    "used_cpu": sum(x.cpu for x in leases), "leases": [asdict(x) for x in leases]}

    def prune(self) -> dict[str, object]:
        return self.status()


def lease_id(lease: Lease) -> str:
    return hashlib.sha256(lease.token.encode()).hexdigest()[:12]

