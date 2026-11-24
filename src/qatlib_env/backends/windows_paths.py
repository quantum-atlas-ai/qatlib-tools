"""Windows filesystem handles for coordinating shared cache access."""
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def locked_file(path: Path):
    """Wait for an exclusive byte-range lock shared by Windows processes."""
    import msvcrt
    import pywintypes
    import win32con
    import win32file

    with path.open("a+b") as file:
        handle = msvcrt.get_osfhandle(file.fileno())
        position = pywintypes.OVERLAPPED()
        win32file.LockFileEx(handle, win32con.LOCKFILE_EXCLUSIVE_LOCK, 1, 0, position)
        try:
            yield
        finally:
            win32file.UnlockFileEx(handle, 1, 0, position)


@contextmanager
def locked_directory(path: Path, *, create: bool = False):
    """Open every ancestor without following reparse points, top down.

    Omitting FILE_SHARE_DELETE pins each directory name until all handles close.
    Omitting FILE_SHARE_WRITE excludes writers that could set a reparse point
    in place. OPEN_REPARSE_POINT makes the attribute check inspect the link
    itself. Incompatible existing handles fail closed. This uses Python 3.11+
    and the project's Windows dependency, pywin32.
    """
    import win32file

    path = Path(path).absolute()
    if ".." in path.parts:
        raise ValueError(f"cache directory contains a parent traversal: {path}")
    with ExitStack() as handles:
        for directory in (*reversed(path.parents), path):
            if create and directory == path:
                directory.mkdir(exist_ok=True)
            handle = win32file.CreateFile(
                str(directory), 0x80, 0x1, None, 3,
                0x02000000 | 0x00200000, None,
                # FILE_READ_ATTRIBUTES, FILE_SHARE_READ, OPEN_EXISTING,
                # FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT.
            )
            handles.callback(handle.Close)
            attributes = win32file.GetFileInformationByHandle(handle)[0]
            if attributes & 0x400 or not attributes & 0x10:
                raise ValueError(f"cache directory is not regular or is symlinked/reparse-point: {directory}")
        yield handle


def replace_mapping(source: Path, directory, name: str, expected: bytes) -> None:
    """Rename a verified source into the pinned destination directory."""
    import win32file

    # GENERIC_READ | DELETE; permit readers, exclude writers and replacement.
    handle = win32file.CreateFile(str(source), 0x80010000, 0x1, None, 3, 0x00200000, None)
    try:
        attributes = win32file.GetFileInformationByHandle(handle)[0]
        if attributes & (0x400 | 0x10):
            raise ValueError(f"cache mapping is not a regular file: {source}")
        _status, content = win32file.ReadFile(handle, len(expected) + 1)
        if content != expected:
            raise ValueError(f"cache mapping changed after validation: {source}")
        # FileRenameInfo rejects the relative form on some Win32 runtimes.
        # Resolve the name from the held directory handle. Its ancestor
        # handles prevent reparse or rename while this path is used.
        destination = Path(win32file.GetFinalPathNameByHandle(directory, 0)) / name
        win32file.SetFileInformationByHandle(handle, win32file.FileRenameInfo, {
            "ReplaceIfExists": True, "RootDirectory": None, "FileName": str(destination),
        })
    finally:
        handle.Close()
