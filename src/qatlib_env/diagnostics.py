"""Initial worktree state and elapsed time for a managed invocation."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .envvars import env_value

PHASES = ("elan_resolve_ms", "lake_config_ms", "cache_check_ms", "build_ms")
_current: ContextVar[dict | None] = ContextVar("env_diagnostics", default=None)


def toolchain_prefix(toolchain: str, env) -> Path:
    executable = env_value(env, "QATLIB_REAL_LEAN", None)
    if executable:
        return Path(executable).parent.parent
    explicit = env_value(env, "QATLIB_REAL_SYSROOT", None) or env.get("LEAN_SYSROOT")
    if explicit:
        return Path(explicit)
    home = Path(env.get("USERPROFILE" if os.name == "nt" else "HOME", str(Path.home())))
    elan = Path(env.get("ELAN_HOME", str(home / ".elan"))).expanduser()
    return elan / "toolchains" / toolchain.replace("/", "--").replace(":", "---")


def classify(project: Path, env) -> dict:
    from . import budget, workspace

    with measure("elan_resolve_ms"):
        toolchain = (project / "lean-toolchain").read_text(encoding="utf-8").strip()
        prefix = toolchain_prefix(toolchain, env)
        lean = prefix / "bin" / ("lean.exe" if os.name == "nt" else "lean")
        installed = lean.is_file() and (prefix / "lib/lean/Init.olean").is_file()
    with measure("cache_check_ms"):
        git_dirs = budget.git(project, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
        dirs = git_dirs.splitlines() if git_dirs else []
        linked = len(dirs) == 2 and Path(dirs[0]).resolve() != Path(dirs[1]).resolve()
        output = project / ".lake/build/lib/lean"
        materialized = next(output.rglob("*.olean"), None) is not None
        cache = workspace.cache_directory(project, env)
        shared = (cache / "artifacts").is_dir() and (cache / "outputs").is_dir()
    if not installed:
        classification = "cold_toolchain_missing"
    elif materialized:
        classification = "warm_worktree"
    elif linked:
        classification = "warm_toolchain_new_worktree"
    else:
        classification = "warm_toolchain_unbuilt_checkout"
    return {"classification": classification, "toolchain": toolchain, "toolchain_installed": installed,
            "linked_worktree": linked, "local_oleans_present": materialized,
            "shared_cache_layout_present": shared, "lake_cache_dir": str(cache)}


@contextmanager
def collect(project: Path, *, enabled: bool):
    if not enabled:
        yield None
        return
    report = {"schema_version": 1, "project": str(project), "status": "ok", "cache": None,
              "phase_timings": dict.fromkeys(PHASES)}
    token = _current.set({"timings": report["phase_timings"], "active": None})
    try:
        report["cache"] = classify(project, os.environ)
        yield report
    except BaseException as error:
        report.update(status="error", error_type=type(error).__name__)
        raise
    finally:
        _current.reset(token)
        report["phase_timings"] = {name: round(ns / 1_000_000, 3) if ns is not None else None
                                   for name, ns in report["phase_timings"].items()}
        print(json.dumps(report, indent=2, ensure_ascii=False))


@contextmanager
def measure(name: str):
    state = _current.get()
    if state is None:
        yield
        return
    parent = state["active"]
    timings = state["timings"]

    def accrue(now):
        if state["active"] is not None:
            phase, started = state["active"]
            timings[phase] = (timings[phase] or 0) + now - started

    now = time.monotonic_ns()
    accrue(now)
    state["active"] = (name, now)
    try:
        yield
    finally:
        now = time.monotonic_ns()
        accrue(now)
        state["active"] = (parent[0], now) if parent else None


def command_stdout():
    # Leave stdout to the JSON report while preserving streamed child output.
    return 2 if _current.get() is not None else None
