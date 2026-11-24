"""Apply runtime Lean threads and heavy-module admission without changing depHash.

Lake 4.35 supports LAKE_OVERRIDE_LEAN + LEAN_SYSROOT. A workspace-local overlay
uses the *same* real compiler and githash, with runtime arguments supplied after
Lake has computed its trace. Neither lakefile.toml nor dependency sources change.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_env import supervisor
from qatlib_env.envvars import env_value


def compiler_command(command: list[str] | tuple[str, ...], *, jobs: int = 2,
                     memory_mib: int = 2048) -> list[str]:
    """Build a direct Lean command for callers that need an explicit overlay.

    Lake commands must remain the first executable; this helper only appends
    Lean runtime flags and never prepends a compiler path before Lake.
    """
    args = list(command)
    jobs = max(1, int(jobs)); memory_mib = max(2048, int(memory_mib))
    if "-j" in args:
        args[args.index("-j") + 1] = str(jobs)
    else:
        args.extend(["-j", str(jobs)])
    if "-M" in args:
        args[args.index("-M") + 1] = str(memory_mib)
    else:
        args.extend(["-M", str(memory_mib)])
    return args


def parse_dep_hash(text: str) -> str | None:
    match = __import__("re").search(r"depHash\s*[:=]\s*([0-9a-fA-F]+)", text)
    return match.group(1) if match else None


def _link(destination: Path, target: Path) -> None:
    if destination.is_symlink() and destination.resolve() == target.resolve():
        return
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"runtime overlay path already exists with a different identity: {destination}")
    destination.symlink_to(target, target_is_directory=target.is_dir())


def environment(project: Path, base_env: dict[str, str]) -> dict[str, str]:
    """Create an idempotent toolchain overlay; never change a real toolchain."""
    result = dict(base_env)
    if os.name != "posix":
        raise ValueError("runtime Lean thread/admission overlay currently requires POSIX")
    configured_lean = env_value(result, "QATLIB_REAL_LEAN", None)
    if configured_lean:
        real = Path(configured_lean)
        sysroot = real.parent.parent
    else:
        from qatlib_env.diagnostics import measure
        with measure("elan_resolve_ms"):
            probe = supervisor.run(["lean", "--print-prefix"], cwd=project, env=result,
                                   capture_output=True, text=True, check=True, timeout=30)
        sysroot = Path(probe.stdout.strip()).resolve()
        real = sysroot / "bin/lean"
    if not real.is_file() or not (sysroot / "lib/lean/Init.olean").is_file():
        raise ValueError(f"Lean runtime prefix does not contain a complete toolchain: {sysroot}")
    identity = hashlib.sha256(str(sysroot).encode()).hexdigest()[:16]
    overlay = Path(project).resolve() / ".lake/qatlas-runtime" / identity
    overlay.mkdir(mode=0o700, parents=True, exist_ok=True)
    (overlay / "bin").mkdir(mode=0o700, exist_ok=True)
    for item in sysroot.iterdir():
        if item.name != "bin":
            _link(overlay / item.name, item)
    for item in (sysroot / "bin").iterdir():
        if item.name != "lean":
            _link(overlay / "bin" / item.name, item)
    wrapper = overlay / "bin/lean"
    content = (f"#!{sys.executable}\nimport runpy\n"
               f"runpy.run_path({str(Path(__file__).resolve())!r}, run_name='__main__')\n")
    if wrapper.is_symlink():
        raise ValueError(f"runtime compiler wrapper must not be a symlink: {wrapper}")
    if not wrapper.exists() or wrapper.read_text() != content:
        temporary = wrapper.with_name(f"lean.{os.getpid()}.tmp")
        with temporary.open("x") as stream:
            stream.write(content)
        temporary.chmod(0o700)
        temporary.replace(wrapper)
    result.update(LAKE_OVERRIDE_LEAN="true", LEAN_SYSROOT=str(overlay), QATLIB_REAL_LEAN=str(real))
    # Preserve the real prefix in actual compiler calls. Lake still resolves its
    # compiler via the overlay, whose --githash is the real compiler's githash.
    result["QATLIB_REAL_SYSROOT"] = str(sysroot)
    default_lock_dir = str(Path(result.get("LAKE_CACHE_DIR", overlay)) / "qatlas-heavy-slots")
    result["QATLIB_HEAVY_LOCK_DIR"] = env_value(result, "QATLIB_HEAVY_LOCK_DIR", default_lock_dir)
    return result


def _acquire_slot(directory: Path, slots: int) -> int:
    import fcntl

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("heavy admission directory must not be a symlink")
    while True:
        for index in range(slots):
            fd = os.open(directory / f"{index}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.set_inheritable(fd, True)
                return fd
            except BlockingIOError:
                os.close(fd)
        time.sleep(0.05)


def compiler_command(command: list[str] | tuple[str, ...], *, jobs: int = 2,
                     memory_mib: int = 2048) -> list[str]:
    """Return a direct Lean invocation with explicit resource arguments."""
    args = list(command)
    jobs, memory_mib = max(1, int(jobs)), max(2048, int(memory_mib))
    if "-j" in args:
        args[args.index("-j") + 1] = str(jobs)
    else:
        args.extend(["-j", str(jobs)])
    if "-M" in args:
        args[args.index("-M") + 1] = str(memory_mib)
    else:
        args.extend(["-M", str(memory_mib)])
    return args


def main() -> int:
    args = sys.argv[1:]
    real = env_value(os.environ, "QATLIB_REAL_LEAN")
    sources = [value for value in args if value.endswith(".lean")]
    if sources:
        patterns = [item.strip() for item in env_value(os.environ, "QATLIB_HEAVY_PATTERNS", "BalancedGram").split(",") if item.strip()]
        if any(fnmatch.fnmatch(source, pattern) or pattern in source for source in sources for pattern in patterns):
            slots = int(env_value(os.environ, "QATLIB_HEAVY_JOBS", "1"))
            if slots < 1:
                raise ValueError("QATLIB_HEAVY_JOBS must be positive")
            _acquire_slot(Path(env_value(os.environ, "QATLIB_HEAVY_LOCK_DIR")), slots)
        threads = int(env_value(os.environ, "QATLIB_LEAN_THREADS", "2"))
        if threads < 1:
            raise ValueError("QATLIB_LEAN_THREADS must be positive")
        from qatlib_env.config import memory_bytes
        total = memory_bytes(env_value(os.environ, "QATLIB_MEMORY_BUDGET", str(4 * 1024**3)))
        processes = max(1, int(env_value(os.environ, "QATLIB_JOBS", "1")))
        memory_mib = max(2048, total // (1024**2) // processes)
        args = [f"-j{threads}", f"-M{memory_mib}", *args]
    actual = dict(os.environ)
    actual["LEAN_SYSROOT"] = env_value(actual, "QATLIB_REAL_SYSROOT")
    os.execve(real, [real, *args], actual)
    return 1  # unreachable


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"Lean runtime admission failed: {error}", file=sys.stderr)
        raise SystemExit(1)

