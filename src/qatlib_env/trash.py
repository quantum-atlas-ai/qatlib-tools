"""Thin Send2Trash adapter; no permanent-deletion fallback."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import json
import stat
import subprocess
import sys


class TrashError(RuntimeError):
    """The operating system could not safely accept an item into Trash."""


def _failure(message: str) -> TrashError:
    return TrashError(f"{message}; stop and ask the user how to proceed")


def _identity(path):
    info = Path(path).lstat()
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns


def _reparse(info):
    return bool(getattr(info, "st_file_attributes", 0) &
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def is_link(path):
    try:
        return Path(path).is_symlink() or _reparse(Path(path).lstat())
    except OSError:
        return Path(path).is_symlink()


def measure(path: Path) -> dict:
    """Count apparent and allocated bytes without following symbolic links.

    Hard links are counted once within this tree. Shared/reflink extents and
    links outside the tree mean the result is not physical space reclaimable.
    Errors propagate: a partial scan must never look like an exact result.
    """
    seen = set()
    apparent = allocated = 0
    allocation_known = True
    mounts = []
    stack = [(Path(path), None)]
    while stack:
        item, parent_device = stack.pop()
        info = item.lstat()
        if parent_device is not None and info.st_dev != parent_device:
            mounts.append(str(item))
        key = (info.st_dev, info.st_ino)
        if info.st_nlink > 1 and key in seen:
            continue
        seen.add(key)
        apparent += info.st_size
        if hasattr(info, "st_blocks"):
            allocated += info.st_blocks * 512
        else:
            allocation_known = False
        if stat.S_ISDIR(info.st_mode) and not _reparse(info):
            with os.scandir(item) as entries:
                stack.extend((Path(entry.path), info.st_dev) for entry in entries)
    return {"apparent_bytes": apparent,
            "allocated_bytes": allocated if allocation_known else None,
            **({"nested_mounts": mounts} if mounts else {})}


def _backend():
    if platform.system() == "Windows":
        # Never allow the library's legacy SHFileOperation fallback. pywin32 is
        # a Windows-only runtime dependency. Keep the actual operation in the
        # upstream IFileOperation backend, with its cancellation bug fixed:
        # https://github.com/arsenetar/send2trash/pull/115
        from send2trash.win import modern
        from send2trash.win.IFileOperationProgressSink import FileOperationProgressSink
        from win32com.server.exception import COMException
        from win32com.shell import shellcon
        import winerror

        def require_recycle(self, flags, item):
            if not flags & shellcon.TSF_DELETE_RECYCLE_IF_POSSIBLE:
                raise COMException(description="The item cannot be recycled", hresult=winerror.E_ABORT)
            return 0

        FileOperationProgressSink.PreDeleteItem = require_recycle
        return modern.send2trash
    from send2trash import send2trash
    return send2trash


def _windows_path_check(path):
    # Known Shell limitation: long descendants can be permanently deleted even
    # with recycling requested (upstream issue #117). Reject this unsupported
    # case until the native backend fixes it; ordinary Windows paths work.
    def check(item):
        if len(str(item).encode("utf-16le")) // 2 >= 260:
            raise _failure(f"Windows Recycle Bin cannot safely handle this long path: {item}")
    check(path)
    if path.is_dir() and not is_link(path):
        def failed(error):
            raise error
        for directory, dirs, files in os.walk(path, followlinks=False, onerror=failed):
            for name in dirs + files:
                check(Path(directory) / name)
            dirs[:] = [name for name in dirs if not is_link(Path(directory) / name)]


def probe(path: Path, env=None) -> dict:
    """Read-only availability check; filesystem support is confirmed on apply.

    Send2Trash has no dry-run API. Do not create Trash directories or call its
    move function to manufacture a positive capability result.
    """
    path = Path(os.path.abspath(os.fspath(path)))
    if not os.path.lexists(path):
        raise _failure(f"Trash target does not exist: {path}")
    if path.parent == path:
        raise _failure("a filesystem root cannot be sent to Trash")
    if platform.system() == "Windows":
        _windows_path_check(path)
    try:
        backend = _backend()
    except (ImportError, OSError) as error:
        raise _failure(f"Send2Trash backend unavailable: {error}") from error
    if hasattr(os, "statvfs") and os.statvfs(path.parent).f_flag & getattr(os, "ST_RDONLY", 1):
        raise _failure(f"read-only filesystem: {path}")
    return {"original_path": str(path), "backend": backend.__module__, "supported": True,
            "can_trash": None,
            "note": "Backend available; filesystem capability is confirmed only on apply. Preview performs no move."}


def trash(path: Path, env=None) -> dict:
    """Move one path to the system Trash and return an auditable receipt."""
    if env is not None and any(env.get(k, os.environ.get(k)) != os.environ.get(k)
                               for k in ("XDG_DATA_HOME", "HOME", "LOCALAPPDATA", "APPDATA")):
        # The library caches platform paths on import. A caller-specific
        # environment needs a fresh interpreter, not global os.environ edits.
        child = subprocess.run([sys.executable, "-B", "-m", __name__, os.fspath(path)],
                               env={**os.environ, **env}, text=True, capture_output=True)
        if child.returncode:
            raise _failure(f"Send2Trash failed: {child.stderr.strip()}")
        return json.loads(child.stdout)
    original = _identity(path)
    details = probe(path, env)
    path = Path(details["original_path"])
    try:
        sizes = measure(path)
    except OSError as error:
        sizes = {"measurement_error": str(error)}
    if original != _identity(path):
        raise _failure(f"Trash target changed during capability/size inspection: {path}")
    try:
        _backend()(str(path))
    except OSError as error:
        raise _failure(f"Send2Trash failed for {path}: {error}") from error
    return {"original_path": str(path), "backend": details["backend"], **sizes,
            "physical_space_note": "Moving to Trash may retain blocks on the same filesystem; no physical space freed is assumed."}


if __name__ == "__main__":
    print(json.dumps(trash(Path(sys.argv[1])), ensure_ascii=False))
