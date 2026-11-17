from __future__ import annotations

import os
from fractions import Fraction
import shutil
from pathlib import Path


def systemd_available() -> bool:
    if not (shutil.which("systemd-run") and os.name == "posix" and os.environ.get("XDG_RUNTIME_DIR")):
        return False
    controllers = Path("/sys/fs/cgroup/cgroup.controllers")
    if controllers.is_file():
        try:
            available = set(controllers.read_text(encoding="ascii").split())
        except OSError:
            return False
        if not {"cpu", "memory"}.issubset(available):
            return False
    return True


def enclosing_cgroup_limits(*, memory_mib: int, cpu: int,
                           proc_root: Path = Path("/proc"),
                           cgroup_root: Path = Path("/sys/fs/cgroup")) -> dict | None:
    """Recognize existing v2 hard caps, checking every ancestor's headroom.

    No CI environment variable grants this capability. The kernel membership
    and actual controller files must prove both finite memory and CPU limits.
    Soft memory.high thresholds and affinity alone are not hard controllers.
    """
    try:
        memberships = (proc_root / "self/cgroup").read_text().splitlines()
        paths = [line[3:] for line in memberships if line.startswith("0::")]
        if len(paths) != 1 or not paths[0].startswith("/") or ".." in Path(paths[0]).parts:
            return None
        root = cgroup_root.resolve(strict=True)
        current = (root / paths[0].lstrip("/")).resolve(strict=True)
        if not current.is_relative_to(root) or not current.is_dir():
            return None
        memory_limits, headrooms, cpu_limits = [], [], []
        directory = current
        while True:
            memory_file = directory / "memory.max"
            if memory_file.is_file():
                raw = memory_file.read_text().strip()
                if raw != "max":
                    if not raw.isdigit() or int(raw) <= 0:
                        return None
                    used = (directory / "memory.current").read_text().strip()
                    if not used.isdigit():
                        return None
                    memory_limits.append(int(raw))
                    headrooms.append(max(0, int(raw) - int(used)))
            cpu_file = directory / "cpu.max"
            if cpu_file.is_file():
                quota, period = cpu_file.read_text().split()
                if not period.isdigit() or int(period) <= 0:
                    return None
                if quota != "max":
                    if not quota.isdigit() or int(quota) <= 0:
                        return None
                    cpu_limits.append(Fraction(int(quota), int(period)))
            if directory == root:
                break
            directory = directory.parent
    except (OSError, ValueError):
        return None
    if not memory_limits or not cpu_limits:
        return None
    memory_max, memory_remaining, cpu_quota = min(memory_limits), min(headrooms), min(cpu_limits)
    requested_memory = max(2048, int(memory_mib)) * 1024**2
    if requested_memory > memory_max or requested_memory > memory_remaining:
        raise ValueError(f"enclosing cgroup memory limit/headroom exceeded: requested={requested_memory} "
                         f"limit={memory_max} remaining={memory_remaining}")
    if max(1, int(cpu)) > cpu_quota:
        raise ValueError(f"enclosing cgroup CPU quota exceeded: requested={cpu} limit={float(cpu_quota)}")
    return {"kind": "enclosing_cgroup_v2", "path": str(current), "memory_max": memory_max,
            "memory_remaining": memory_remaining, "cpu_quota": float(cpu_quota)}


def linux_command(command: list[str], *, memory_mib: int, cpu: int, unit: str = "qatlib-build",
                  runtime_sec: float | None = None) -> list[str]:
    if not systemd_available():
        return list(command)
    memory_mib = max(2048, int(memory_mib))
    quota = max(1, int(cpu)) * 100
    runtime = [] if runtime_sec is None else ["-p", f"RuntimeMaxSec={runtime_sec}", "-p", "KillMode=control-group"]
    return ["systemd-run", "--user", "--scope", f"--unit={unit}", "--slice=qatlib-lean.slice",
            "-p", f"MemoryHigh={memory_mib}M", "-p", f"MemoryMax={memory_mib}M",
            "-p", "MemorySwapMax=0", "-p", f"CPUQuota={quota}%", *runtime, "--", *command]

