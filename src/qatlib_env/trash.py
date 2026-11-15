"""System Trash adapters. No permanent-deletion fallback is provided."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess


class TrashError(RuntimeError):
    """The operating system could not safely accept an item into Trash."""


def _failure(message: str) -> TrashError:
    return TrashError(f"{message}; stop and ask the user how to proceed")


def _identity(path):
    info = Path(path).lstat()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def measure(path: Path) -> dict:
    """Count apparent and allocated bytes without following symbolic links.

    Hard links are counted once within this tree. Shared/reflink extents and
    links outside the tree mean the result is not physical space reclaimable.
    Errors propagate: a partial scan must never look like an exact result.
    """
    seen = set()
    apparent = allocated = 0
    allocation_known = True
    stack = [Path(path)]
    device = stack[0].lstat().st_dev
    while stack:
        item = stack.pop()
        info = item.lstat()
        if info.st_dev != device:
            raise _failure(f"nested filesystem requires separate review: {item}")
        key = (info.st_dev, info.st_ino)
        if info.st_nlink > 1 and key in seen:
            continue
        seen.add(key)
        apparent += info.st_size
        if hasattr(info, "st_blocks"):
            allocated += info.st_blocks * 512
        else:
            allocation_known = False
        if stat.S_ISDIR(info.st_mode):
            with os.scandir(item) as entries:
                stack.extend(Path(entry.path) for entry in entries)
    return {"apparent_bytes": apparent,
            "allocated_bytes": allocated if allocation_known else None}


def _environment(env):
    result = dict(os.environ if env is None else env)
    result.update(GIO_USE_VFS="local", LC_ALL="C")
    return result


def _run(command, *, env):
    try:
        result = subprocess.run(command, env=env, text=True, capture_output=True,
                                check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _failure(f"cannot run system Trash backend: {error}") from error
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise _failure(f"system Trash backend failed ({result.returncode}): {detail}")
    return result.stdout


def probe(path: Path, env=None) -> dict:
    """Read-only capability check; success is not a guarantee of a later move."""
    path = Path(os.path.abspath(os.fspath(path)))
    if not os.path.lexists(path):
        raise _failure(f"Trash target does not exist: {path}")
    environment = _environment(env)
    system = platform.system()
    capability = None
    if system == "Linux":
        gio = shutil.which("gio", path=environment.get("PATH"))
        if gio:
            output = _run([gio, "info", "--nofollow-symlinks", "-a", "access::can-trash", "--", str(path)], env=environment)
            found = re.findall(r"^\s*access::can-trash:\s*(TRUE|FALSE)\s*$", output, re.MULTILINE)
            if found != ["TRUE"]:
                raise _failure(f"gio reports Trash unsupported or unknown for {path}")
            backend, executable, capability = "gio", gio, True
        else:
            executable = shutil.which("trash-put", path=environment.get("PATH"))
            if not executable:
                raise _failure("install gio or trash-put to use the system Trash")
            backend = "trash-put"
    elif system == "Darwin":
        if os.path.isfile("/usr/bin/trash") and os.access("/usr/bin/trash", os.X_OK):
            backend, executable = "macos-trash", "/usr/bin/trash"
        else:
            executable = shutil.which("osascript", path=environment.get("PATH"))
            if not executable:
                raise _failure("macOS native Trash requires /usr/bin/trash or Finder and osascript")
            backend = "macos-finder"
    elif system == "Windows":
        # VisualBasic FileSystem ignores SendToRecycleBin when UserInteractive
        # is false. Its interactive Shell path also only requests FOF_ALLOWUNDO
        # (best effort). Neither path guarantees recoverability. Do not expose
        # it as an automatic Trash backend, even if PowerShell is available.
        raise _failure("automatic Windows recycling is unavailable: .NET SendToRecycleBin "
                       "can permanently delete; use the native Recycle Bin manually")
    else:
        raise _failure(f"no system Trash backend for {system}")
    return {"original_path": str(path), "backend": backend, "supported": True,
            "executable": executable, "can_trash": capability,
            "note": "A capability check does not guarantee that a later move will succeed."}


def trash(path: Path, env=None) -> dict:
    """Move one path to the system Trash and return an auditable receipt."""
    original = _identity(path)
    details = probe(path, env)
    path = Path(details["original_path"])
    sizes = measure(path)
    environment = _environment(env)
    executable, backend = details["executable"], details["backend"]
    if backend == "gio":
        command = [executable, "trash", "--", str(path)]
    elif backend == "trash-put":
        command = [executable, "--", str(path)]
    elif backend == "macos-trash":
        command = [executable, str(path)]
    elif backend == "macos-finder":
        # Pass the path as argv, never interpolate it into AppleScript.
        script = 'on run argv\n tell application "Finder" to delete POSIX file (item 1 of argv)\nend run'
        command = [executable, "-e", script, str(path)]
    else:
        raise _failure(f"unknown system Trash backend: {backend}")
    if original != _identity(path):
        raise _failure(f"Trash target changed during capability/size inspection: {path}")
    _run(command, env=environment)
    return {"original_path": str(path), "backend": backend, **sizes,
            "physical_space_note": "Moving to Trash may retain blocks on the same filesystem; no physical space freed is assumed."}
