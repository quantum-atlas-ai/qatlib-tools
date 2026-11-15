from __future__ import annotations

import os
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


def linux_command(command: list[str], *, memory_mib: int, cpu: int, unit: str = "qatlib-build") -> list[str]:
    if not systemd_available():
        return list(command)
    memory_mib = max(2048, int(memory_mib))
    quota = max(1, int(cpu)) * 100
    return ["systemd-run", "--user", "--scope", f"--unit={unit}", "--slice=qatlib-lean.slice",
            "-p", f"MemoryHigh={memory_mib}M", "-p", f"MemoryMax={memory_mib}M",
            "-p", "MemorySwapMax=0", "-p", f"CPUQuota={quota}%", "--", *command]

