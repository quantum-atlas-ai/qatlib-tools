"""Read-only workspace facts, review warnings and explicit Trash operations.

No environment/bootstrap/cache-lock helpers are used here. Harvesting and
migration belong to the operator, not to this inventory.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from qatlib_env import trash as system_trash

PRUNE = {".git", ".lake", "node_modules", ".venv", "__pycache__", "Trash",
         "$RECYCLE.BIN", ".Trash", "docker", "overlay2", "containers", "_work"}
SEMANTIC = {"questions", "research"}


def git(path, *args, timeout=15, input=None):
    """Invoke Git without optional index writes, lazy fetching or prompting."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
               GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes "
                   "-o UpdateHostKeys=no -o ControlMaster=no")
    return subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
                          capture_output=True, text=True, errors="replace", env=env,
                          timeout=timeout, check=False, input=input)


def _read_git(path, *args):
    result = git(path, *args)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git observation failed")
    return result.stdout


def _identity(path):
    s = path.lstat()
    return [s.st_dev, s.st_ino, s.st_mtime_ns, s.st_ctime_ns]


def discover(roots, *, max_directories=100000, semantic=None):
    """Triple index: Git, Lake/Lean, and semantic research markers.

    Do not follow symlinks or implicitly enter other mounts. Explicit roots
    allow inspecting those mounts. Skips/errors are facts, not empty results.
    """
    semantic = SEMANTIC if semantic is None else set(semantic)
    repos, markers, errors, skipped, seen = set(), [], [], [], set()
    count = 0
    for root in roots:
        root = Path(root).expanduser().absolute()
        if root.is_symlink():
            errors.append(f"symlink root requires its explicit real path: {root}")
            continue
        try:
            device = root.stat().st_dev
        except OSError as error:
            errors.append(f"{root}: {error}")
            continue
        for directory, dirs, files in os.walk(root, followlinks=False,
                                             onerror=lambda e: errors.append(str(e))):
            path = Path(directory)
            if str(path) in seen:
                dirs[:] = []
                continue
            seen.add(str(path))
            count += 1
            if count > max_directories:
                errors.append(f"directory limit {max_directories} reached at {path}; narrow roots")
                return sorted(repos), markers, errors, skipped
            names = set(dirs) | set(files)
            if ".git" in names:
                repos.add(str(path))
            kinds = sorted((semantic & names) | ({".lake"} & names) |
                           ({"lakefile.lean", "lakefile.toml"} & names))
            if not kinds and any(f.endswith(".lean") for f in files):
                kinds = ["lean-source"]
            if kinds:
                markers.append({"path": str(path), "markers": kinds})
            keep = []
            for name in dirs:
                child = path / name
                if name in PRUNE or name.startswith((".Trash-", ".Trash.")):
                    continue
                try:
                    if child.is_symlink():
                        skipped.append({"path": str(child), "reason": "symlink"})
                    elif child.stat().st_dev != device:
                        skipped.append({"path": str(child), "reason": "other filesystem"})
                    else:
                        keep.append(name)
                except OSError as error:
                    errors.append(f"{child}: {error}")
            dirs[:] = keep
    return sorted(repos), markers, errors, skipped


def processes():
    """Observe native build processes on Linux, macOS and Windows."""
    import psutil
    found, errors = [], []
    names = {"lean", "leanir", "lake", "make", "gmake", "ninja", "cmake",
             "cc", "c++", "gcc", "g++", "clang", "clang++", "ld", "lld", "ar"}
    for process in psutil.process_iter(["pid", "name"]):
        if (process.info.get("name") or "").lower().removesuffix(".exe") not in names:
            continue
        try:
            found.append({"pid": process.pid, "cwd": process.cwd(), "command": process.cmdline()})
        except psutil.NoSuchProcess:
            continue
        except (psutil.AccessDenied, OSError) as error:
            errors.append(f"cannot inspect build process {process.pid}: {error}")
    return found, errors


def _related(a, b):
    return a == b or a in b.parents or b in a.parents


def _active_for(path, active):
    found = []
    for process in active:
        cwd = Path(process["cwd"]).resolve() if process.get("cwd") else None
        if cwd and (path == cwd or path in cwd.parents):
            found.append(process)
            continue
        for argument in process.get("command", [])[1:]:
            if argument.startswith(("-I", "-L")):
                argument = argument[2:]
            elif argument.startswith("-"):
                continue
            candidate = Path(argument)
            if not candidate.is_absolute():
                if cwd is None:
                    continue
                candidate = cwd / candidate
            candidate = candidate.resolve()
            if candidate == path or path in candidate.parents:
                found.append(process)
                break
    return found


def checkout_facts(path, *, inspect_ignored=False, semantic=None):
    path = Path(path).absolute()
    row = {"path": str(path), "errors": [], "warnings": []}
    try:
        if not (path / ".git").exists() or (path / ".git").is_symlink():
            raise ValueError("an exact independent Git checkout root is required")
        if Path(_read_git(path, "rev-parse", "--show-toplevel").strip()).resolve() != path.resolve():
            raise ValueError("path is not the Git checkout root")
        row["identity"] = _identity(path)
        row["head"] = _read_git(path, "rev-parse", "HEAD").strip()
        row["branch"] = _read_git(path, "branch", "--show-current").strip() or None
        row["git_dir"] = _read_git(path, "rev-parse", "--absolute-git-dir").strip()
        common = _read_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
        row["common_dir"] = common
        row["linked_worktree"] = Path(row["git_dir"]) != Path(common)
        flags = _read_git(path, "ls-files", "-v", "-z").split("\0")
        if any(item and (item[0].islower() or item[0] == "S") for item in flags):
            row["warnings"].append("assume-unchanged/skip-worktree files may hide modifications")
        status = _read_git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                           "--ignore-submodules=none").split("\0")
        states = []
        skip = False
        for item in status:
            if skip:
                skip = False
                continue
            if item:
                states.append(item)
                skip = "R" in item[:2] or "C" in item[:2]
        row["dirty_count"] = len(states)
        row["untracked_count"] = sum(s.startswith("??") for s in states)
        row["dirty_sample"] = states[:20]
        if inspect_ignored:
            row["dirty_entries"] = states
        row["ignored_count"] = None
        if inspect_ignored:
            row["ignored_paths"] = [p for p in _read_git(path, "ls-files", "--others", "--ignored",
                                   "--exclude-standard", "--directory", "-z", "--", ".",
                                   ":(exclude).lake").split("\0")
                                    if p and Path(p).parts[0] != ".lake"]
            row["ignored_count"] = len(row["ignored_paths"])
        recent = [path, Path(row["git_dir"]), Path(row["git_dir"]) / "HEAD",
                  Path(row["git_dir"]) / "index", Path(row["git_dir"]) / "logs/HEAD",
                  path / ".lake", path / ".lake/build"]
        row["last_activity"] = max([float(_read_git(path, "log", "-1", "--format=%ct").strip())] +
                                   [p.lstat().st_mtime for p in recent if p.exists()])
        row["semantic_markers"] = sorted(m for m in (SEMANTIC if semantic is None else semantic) if (path / m).exists())
        origin = git(path, "remote", "get-url", "origin")
        row["origin"] = origin.stdout.strip() if origin.returncode == 0 else None
        if _read_git(path, "rev-parse", "HEAD").strip() != row["head"]:
            row["errors"].append("HEAD changed during observation")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        row["errors"].append(str(error))
    return row


def common_facts(path, *, check_remote=False, remote_cache=None):
    """One observation for all branches/stashes/worktrees in a common-dir."""
    data = {"errors": [], "remote_verified": False, "unpushed_branches": [], "unknown_branches": []}
    try:
        data["stash_count"] = len(_read_git(path, "stash", "list", "--format=%H").splitlines())
        raw = _read_git(path, "worktree", "list", "--porcelain", "-z")
        data["worktrees"] = [x.removeprefix("worktree ") for x in raw.split("\0") if x.startswith("worktree ")]
        data["locked_worktrees"] = []
        local = _read_git(path, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads").splitlines()
        origin = _read_git(path, "remote", "get-url", "origin").strip()
        local_origin = origin.startswith(("/", "./", "../", "file:", "~", "\\\\")) or ("://" not in origin and ":" not in origin)
        data["origin_is_local"] = local_origin
        if check_remote:
            cache = {} if remote_cache is None else remote_cache
            if origin not in cache:
                cache[origin] = git(path, "ls-remote", "origin", timeout=30)
            remote = cache[origin]
            if remote.returncode:
                raise ValueError("ls-remote failed: " + remote.stderr.strip())
            tips = {line.split()[0] for line in remote.stdout.splitlines() if line.split()}
            data["remote_basis"] = "live origin ls-remote (no fetch)"
        else:
            tips = set(_read_git(path, "for-each-ref", "--format=%(objectname)", "refs/remotes/origin").splitlines())
            data["remote_basis"] = "local origin refs; may be stale"
        checked = git(path, "cat-file", "--batch-check=%(objectname) %(objecttype)",
                      input="".join(s + "^{commit}\n" for s in sorted(tips)))
        if checked.returncode:
            raise ValueError("cannot inspect remote objects")
        known = {line.split()[0] for line in checked.stdout.splitlines() if line.endswith(" commit")}
        missing = bool(tips - known) or _read_git(path, "rev-parse", "--is-shallow-repository").strip() == "true"
        reachable = set()
        if known:
            graph = git(path, "rev-list", "--stdin", input="\n".join(sorted(known)) + "\n", timeout=30)
            if graph.returncode:
                raise ValueError("cannot read remote ancestor graph: " + graph.stderr.strip())
            reachable = set(graph.stdout.splitlines())
        heads = []
        current = ""
        for token in raw.split("\0"):
            if token.startswith("worktree "):
                current = token.removeprefix("worktree ")
            elif token.startswith("HEAD "):
                heads.append(token.removeprefix("HEAD ") + " worktree:" + current)
            elif token == "locked" or token.startswith("locked "):
                data["locked_worktrees"].append(current)
        for line in local + heads:
            sha, ref = line.split(" ", 1)
            if sha in tips or sha in reachable:
                continue
            target = "unknown_branches" if missing else "unpushed_branches"
            data[target].append({"ref": ref, "sha": sha})
        data["remote_verified"] = check_remote and not local_origin and not data["unknown_branches"]
        data["remote_tip_count"] = len(tips)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        data["errors"].append(str(error))
    return data


def scan(roots, *, measure=False, check_remote=False, hot_hours=24, protect=(), now=None,
         exact=False, semantic=None, max_directories=100000, history=None, protect_refs=()):
    if not math.isfinite(hot_hours) or hot_hours < 24:
        raise ValueError("hot-hours must be finite and at least 24")
    now = time.time() if now is None else now
    if exact:
        repos = sorted({str(Path(p).expanduser().absolute()) for p in roots})
        markers, errors, skipped = [], [], []
    else:
        if max_directories < 1:
            raise ValueError("max-directories must be positive")
        repos, markers, errors, skipped = discover(roots, max_directories=max_directories, semantic=semantic)
    active, process_errors = processes()
    # Git operations do not contend for shared write locks. Four workers bound load.
    with ThreadPoolExecutor(max_workers=4) as executor:
        def observe(path):
            return checkout_facts(path, inspect_ignored=history is not None, semantic=semantic)
        rows = list(executor.map(observe, repos))
    groups, remote_cache = {}, {}
    representatives = {}
    for row in rows:
        if row.get("common_dir"):
            representatives.setdefault(row["common_dir"], row["path"])
    def observe_common(item):
        common, path = item
        return common, common_facts(path, check_remote=check_remote, remote_cache=remote_cache)
    with ThreadPoolExecutor(max_workers=4) as executor:
        groups.update(executor.map(observe_common, representatives.items()))
    for row in rows:
        common = row.get("common_dir")
        row["warnings"] = list(row.get("warnings", [])) + row["errors"] + process_errors
        row["protection"] = []
        row["hot"] = now - row.get("last_activity", now) < hot_hours * 3600
        if row["hot"]:
            row["warnings"].append(f"activity within {hot_hours:g} hours")
        path = Path(row["path"])
        if any(_related(path.resolve(), Path(p).expanduser().resolve()) for p in protect):
            row["protection"].append("explicitly protected by operator")
        if row.get("branch") in protect_refs or row.get("head") in protect_refs:
            row["protection"].append("protected branch or HEAD")
        if row["path"] in groups.get(common, {}).get("locked_worktrees", []):
            row["protection"].append("Git worktree is locked")
        row["active_processes"] = _active_for(path, active)
        if row["active_processes"]:
            row["protection"].append("active Lean/Lake process")
        lake = path / ".lake"
        row["lake"] = None
        if lake.exists() or lake.is_symlink():
            row["lake"] = {"path": str(lake), "symlink": lake.is_symlink()}
            try:
                row["lake"]["identity"] = _identity(lake)
                if measure and not lake.is_symlink():
                    row["lake"].update(system_trash.measure(lake))
                row["lake"]["trash_probe"] = system_trash.probe(lake)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                row["lake"]["error"] = str(error)
    result = {"schema_version": 1, "captured_at": now, "roots": [str(p) for p in roots], "exact": exact,
            "checkouts": rows, "common_dirs": groups, "research_markers": markers,
            "errors": errors, "skipped": skipped, "process_errors": process_errors,
            "truncated": any(error.startswith(f"directory limit {max_directories} reached at ") for error in errors),
            "notes": ["No fetch/bootstrap/cache lock/config write; no proof that all research is harvested.",
                      "Sizes are optional; same-filesystem Trash does not reclaim physical space.",
                      "Processes visible to this user only; coordinate idle writers before apply."]}
    if history is not None:
        result["history_root"] = str(history)
        result["history_unclassified"] = []
        try:
            if history.is_dir() and not history.is_symlink():
                result["history_unclassified"] = [
                    str(entry) for entry in sorted(history.iterdir())
                    if not any(Path(repo).is_relative_to(entry) for repo in repos)]
        except OSError as error:
            errors.append(f"{history}: {error}")
    return result


def _warning(path, reason, *, recursive=False, **details):
    """Keep findings complete; directory metadata warnings share target totals."""
    path = Path(path)
    row = {"path": str(path), "reason": reason, **details}
    try:
        if recursive or not path.is_dir() or system_trash.is_link(path):
            row.update(system_trash.measure(path))
        else:
            row["size_basis"] = "included in target totals"
    except (OSError, RuntimeError) as error:
        row["measurement_error"] = str(error)
    return row


def _git_warnings(path, fact, group):
    warnings = [_warning(path, message) for message in fact["errors"] + fact.get("warnings", []) + group["errors"]]
    for entry in fact.get("dirty_entries", []):
        warnings.append(_warning(path / entry[3:], "modified/untracked Git content", status=entry[:2]))
    for name in fact.get("ignored_paths", []):
        warnings.append(_warning(path / name, "Git-ignored content", recursive=True))
    if group.get("stash_count"):
        warnings.append(_warning(path, "Git stash present", count=group["stash_count"]))
    for name in ("unpushed_branches", "unknown_branches"):
        if group.get(name):
            warnings.append(_warning(path, name.replace("_", " "), refs=group[name]))
    if not group.get("remote_verified"):
        warnings.append(_warning(path, "remote preservation is not verified", basis=group.get("remote_basis")))
    common = fact.get("common_dir")
    if common and _related(path, Path(common)) and group.get("worktrees"):
        warnings.append(_warning(path, "shared Git directory", common_dir=common,
                                 worktrees=group["worktrees"]))
    return warnings


def _lake_warnings(path, *, check_remote=False, remote_cache=None):
    """Describe nonstandard data, not a second permission system."""
    warnings = []
    try:
        children = sorted(path.iterdir())
    except OSError as error:
        return [_warning(path, f"Lake inspection incomplete: {error}")]
    standard = {"build", "packages", "lakefile.olean", "lakefile.ilean", "lakefile.olean.trace"}
    for item in children:
        if system_trash.is_link(item):
            warnings.append(_warning(item, "symbolic link inside Lake; only the link is moved"))
        elif item.name not in standard:
            warnings.append(_warning(item, "nonstandard Lake content", recursive=True))
        if item.is_mount():
            warnings.append(_warning(item, "nested mount; size and Trash behavior depend on the filesystem"))
    packages = path / "packages"
    if packages.is_dir() and not system_trash.is_link(packages):
        try:
            dependencies = sorted(packages.iterdir())
        except OSError as error:
            return warnings + [_warning(packages, f"dependency inspection incomplete: {error}")]
        for root in dependencies:
            if system_trash.is_link(root) or not (root / ".git").exists():
                warnings.append(_warning(root, "linked or unmanaged dependency", recursive=True))
                continue
            fact = checkout_facts(root, inspect_ignored=True)
            group = common_facts(root, check_remote=check_remote, remote_cache=remote_cache)
            warnings.extend(_git_warnings(root, fact, group))
            nested = root / ".lake"
            if nested.is_dir() and not system_trash.is_link(nested):
                warnings.extend(_lake_warnings(nested, check_remote=check_remote, remote_cache=remote_cache))
    return warnings


def _runtime_reasons(checkout, target, active, protect):
    reasons = []
    if checkout.parent == checkout or checkout == Path.home().resolve():
        reasons.append("a filesystem root or home directory is outside cleanup scope")
    if any(system_trash.is_link(p) for p in (target, *target.parents)):
        reasons.append("symlinked target or parent escapes explicit cleanup scope")
    if not target.is_dir() or not (checkout / ".git").exists():
        reasons.append("an existing directory in an exact Git checkout is required")
    if any(_related(checkout, Path(p).expanduser().resolve()) for p in protect):
        reasons.append("explicitly protected by operator")
    if _active_for(checkout, active):
        reasons.append("active Lean/Lake or build process")
    return reasons


def _merged_reasons(path, fact, group, merged_into, expected_common):
    """Prove that this linked checkout can be retired without losing local work."""
    if (not fact.get("linked_worktree") or not fact.get("common_dir")
            or Path(fact["common_dir"]).resolve() != expected_common):
        return ["auto-retirement requires a linked worktree of the current Git repository"]
    if (fact["errors"] or fact.get("warnings") or fact.get("dirty_count") or fact.get("ignored_count")
            or group.get("errors") or group.get("stash_count")):
        return ["Git inspection found changes, local material, a stash or an unreadable checkout"]
    if git(path, "merge-base", "--is-ancestor", fact["head"], merged_into).returncode:
        return ["HEAD is not verified as an ancestor of the supplied main SHA"]
    return []


def clean(checkouts, *, mode="caches", apply=False, hot_hours=24, protect=(), check_remote=False,
          prune=True, protect_refs=(), merged_into=None):
    """Preview complete warnings; --apply confirms them and uses native Trash."""
    if mode not in {"caches", "derived", "checkouts"}:
        raise ValueError("mode must be caches or checkouts")
    expected_common = None
    if merged_into is not None:
        if mode != "checkouts" or len(merged_into) not in {40, 64} or any(c not in "0123456789abcdef" for c in merged_into):
            raise ValueError("--merged-into requires trash-checkouts and a full commit SHA")
        expected_common = Path(_read_git(Path.cwd(), "rev-parse", "--path-format=absolute",
                                         "--git-common-dir").strip()).resolve()
    selected = [Path(p).expanduser().absolute() for p in checkouts]
    selected = [Path(os.path.abspath(p)) for p in selected]
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("select distinct explicit checkout paths")
    pairs = [(p, p / ".lake" if mode == "caches" else p) for p in selected]
    if mode == "derived":
        pairs = [(p, p / ".lake" / name) for p in selected for name in ("build", "packages")
                 if (p / ".lake" / name).exists() or (p / ".lake" / name).is_symlink()]
    targets = [target for _, target in pairs]
    if any(_related(a, b) for i, a in enumerate(targets) for b in targets[i + 1:]):
        raise ValueError("overlapping targets must be reviewed separately")
    active, process_errors = processes()
    rows, receipts, remote_cache = [], [], {}
    for path, target in pairs:
        reasons = _runtime_reasons(path, target, active, protect)
        fact = checkout_facts(path, inspect_ignored=True)
        group = common_facts(path, check_remote=check_remote, remote_cache=remote_cache)
        if fact.get("branch") in protect_refs or fact.get("head") in protect_refs:
            reasons.append("protected branch or HEAD")
        if str(path) in group.get("locked_worktrees", []):
            reasons.append("Git worktree is locked")
        if merged_into:
            reasons.extend(_merged_reasons(path, fact, group, merged_into, expected_common))
            if process_errors or time.time() - fact.get("last_activity", time.time()) < hot_hours * 3600:
                reasons.append("recent activity or incomplete process inspection")
        warnings = _git_warnings(path, fact, group)
        warnings.extend(_warning(path, message) for message in process_errors)
        if time.time() - fact.get("last_activity", time.time()) < hot_hours * 3600:
            warnings.append(_warning(path, "recent activity; coordinate idle writers before apply"))
        if not math.isfinite(hot_hours) or hot_hours < 24:
            raise ValueError("hot-hours must be finite and at least 24")
        lake = path / ".lake"
        if lake.is_dir() and not system_trash.is_link(lake) and not reasons:
            warnings.extend(_lake_warnings(lake, check_remote=check_remote, remote_cache=remote_cache))
            try:
                tracked = _read_git(path, "ls-files", "-z", "--", ".lake").split("\0")
                warnings.extend(_warning(path / p, "Lake content is tracked by the checkout") for p in tracked if p)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                warnings.append(_warning(lake, f"tracked-content inspection incomplete: {error}"))
        if merged_into and lake.exists() and any(
                Path(w["path"]).is_relative_to(lake) and w["reason"] != "remote preservation is not verified"
                for w in warnings):
            reasons.append("local or unverified material in .lake")
        for warning in warnings:
            common = warning.get("common_dir")
            if common and Path(common).is_relative_to(target) and any(
                    not any(Path(p).is_relative_to(t) for t in targets) for p in warning.get("worktrees", [])):
                reasons.append("Git directory serves worktrees outside the selected target")
        row = {"path": str(target), "checkout": str(path), "reasons": reasons, "warnings": warnings,
               "head": fact.get("head"), "common_dir": fact.get("common_dir"),
               "linked_worktree": fact.get("linked_worktree"),
               "active_processes": _active_for(path, active)}
        try:
            row["identity"] = _identity(target)
        except OSError as error:
            reasons.append(f"target identity unavailable: {error}")
        try:
            row.update(system_trash.measure(target))
            warnings.extend(_warning(p, "nested mount; included in target totals") for p in row.get("nested_mounts", []))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            warnings.append(_warning(target, f"size inspection incomplete: {error}"))
        if not reasons:
            try:
                row["trash_probe"] = system_trash.probe(target)
                if not row["trash_probe"].get("supported"):
                    reasons.append("Trash backend unavailable")
            except (OSError, ValueError, RuntimeError) as error:
                reasons.append(str(error))
        row["eligible"] = not reasons
        rows.append(row)
    result = {"applied": False, "mode": mode, "targets": rows, "receipts": receipts,
              "physical_bytes_freed": 0}
    if mode == "checkouts":
        result["worktree_prunes"] = []
    if apply:
        if any(not row["eligible"] for row in rows):
            result["error"] = "batch refused: active, out-of-scope or unavailable targets; nothing moved"
            return result
        def recheck(row):
            try:
                unchanged = _identity(Path(row["path"]))[:2] == row["identity"][:2]
            except OSError as error:
                raise ValueError(f"target unavailable since preflight: {error}") from error
            if not unchanged:
                raise ValueError("target changed since preflight; stopped")
            live, live_errors = processes()
            checkout = Path(row["checkout"])
            row["warnings"].extend(_warning(checkout, message) for message in live_errors)
            blockers = _runtime_reasons(checkout, Path(row["path"]), live, protect)
            if merged_into or protect_refs:
                fact = checkout_facts(checkout, inspect_ignored=bool(merged_into))
                if fact.get("branch") in protect_refs or fact.get("head") in protect_refs:
                    blockers.append("protected branch or HEAD")
                if merged_into:
                    group = common_facts(checkout)
                    blockers.extend(_merged_reasons(checkout, fact, group, merged_into, expected_common))
                    if (str(checkout) in group.get("locked_worktrees", []) or live_errors
                            or time.time() - fact.get("last_activity", time.time()) < hot_hours * 3600
                            or fact.get("head") != row["head"]):
                        blockers.append("worktree locked, active, changed or incompletely inspected")
                    lake = checkout / ".lake"
                    if lake.is_symlink():
                        blockers.append("Lake directory is a symbolic link")
                    elif lake.is_dir() and (any(
                            w["reason"] != "remote preservation is not verified"
                            for w in _lake_warnings(lake))
                            or _read_git(checkout, "ls-files", "-z", "--", ".lake")):
                        blockers.append("local or unverified material in .lake")
            if blockers:
                raise ValueError("; ".join(blockers))
            for warning in row["warnings"][:]:
                if warning.get("common_dir") and Path(warning["common_dir"]).is_relative_to(Path(row["path"])):
                    try:
                        raw = _read_git(warning["path"], "worktree", "list", "--porcelain", "-z")
                    except (OSError, ValueError, subprocess.SubprocessError) as error:
                        row["warnings"].append(_warning(warning["path"], f"worktree recheck incomplete: {error}"))
                        continue
                    worktrees = [x.removeprefix("worktree ") for x in raw.split("\0") if x.startswith("worktree ")]
                    if any(not any(Path(p).is_relative_to(t) for t in targets) for p in worktrees):
                        raise ValueError("Git directory now serves a worktree outside the selected targets")
        # Move linked trees before their selected primary Git directory.
        order = sorted(rows, key=lambda row: not row["linked_worktree"]) if mode == "checkouts" else rows
        moved = []
        for row in order:
            try:
                recheck(row)
                receipts.append(system_trash.trash(Path(row["path"])))
                moved.append(row)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result["error"] = str(error)
                break  # earlier receipts remain truthful; no fallback or rollback
        result["applied"] = "error" not in result
        if mode == "checkouts" and prune:
            moved_paths = [Path(row["path"]) for row in moved]
            common_dirs = {Path(row["common_dir"]).resolve() for row in moved
                           if row["linked_worktree"] and row["common_dir"]}
            for common in sorted(common_dirs):
                if any(common.is_relative_to(path) for path in moved_paths):
                    continue  # The main checkout and its Git metadata are already in Trash.
                receipt = {"common_dir": str(common)}
                result["worktree_prunes"].append(receipt)
                try:
                    _read_git(common, "--git-dir", str(common), "worktree", "prune", "--expire", "now")
                    receipt["pruned"] = True
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    receipt.update(pruned=False, error=str(error))
                    result.setdefault("error", f"checkout moved to Trash, but Git worktree prune failed: {common}")
    return result


def cli_main(argv=None):
    parser = argparse.ArgumentParser(prog="qatlib-env workspace", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("scan", description="Inspect Git and Lake state without preparing a workspace. A truncated scan reports its limit and exits 1.")
    inventory.add_argument("roots", type=Path, nargs="*")
    inventory.add_argument("--history", action="store_true",
                           help="scan ~/.legacy-worktrees/tmp first, including ignored Git files and unclassified entries")
    inventory.add_argument("--measure", action="store_true", help="measure .lake bytes (slower)")
    inventory.add_argument("--exact", action="store_true", help="inspect known checkout roots without recursive discovery")
    inventory.add_argument("--max-directories", type=int, default=100000, help="limit recursive discovery")
    inventory.add_argument("--marker", action="append", help="additional domain directory/file marker (repeatable)")
    inventory.add_argument("--check-remote", action="store_true", help="live ls-remote, never fetch")
    for name in ("clean-caches", "trash-checkouts"):
        command = commands.add_parser(name, description="Preview targets and warnings. With --apply, move eligible targets to the system Trash and report receipts.")
        command.add_argument("--checkout", action="append", type=Path, required=True)
        action = command.add_mutually_exclusive_group()
        action.add_argument("--apply", action="store_true", help="only after approval of the concrete list")
        action.add_argument("--dry-run", action="store_true", help="default; no filesystem writes")
        command.add_argument("--check-remote", action="store_true")
        if name == "clean-caches":
            command.add_argument("--derived-only", action="store_true",
                                 help="select existing .lake/build and .lake/packages only")
        else:
            command.add_argument("--no-prune", action="store_true",
                                 help="keep Git worktree registrations after moving linked checkouts")
            command.add_argument("--merged-into", metavar="SHA",
                                 help="only retire idle, clean linked trees of the current repository whose HEAD is an ancestor of this verified main commit")
    for command in (inventory, *[commands.choices[n] for n in ("clean-caches", "trash-checkouts")]):
        command.add_argument("--json", action="store_true")
        command.add_argument("--hot-hours", type=float, default=24)
        command.add_argument("--protect", action="append", type=Path, default=[])
        command.add_argument("--protect-ref", action="append", default=[], metavar="BRANCH_OR_SHA",
                             help="protect checkouts matching this branch or exact HEAD (repeat for open PRs)")
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            history = Path.home() / ".legacy-worktrees/tmp"
            roots = args.roots
            if args.history:
                if args.exact:
                    raise ValueError("--history requires recursive discovery")
                roots = ([history] if os.path.lexists(history) else []) + roots
            elif not roots:
                if args.exact:
                    roots = [Path.cwd()]
                else:
                    roots = ([history] if os.path.lexists(history) else []) + [Path.cwd().parent]
            result = scan(roots, measure=args.measure, check_remote=args.check_remote,
                          hot_hours=args.hot_hours, protect=args.protect, exact=args.exact,
                          semantic=SEMANTIC | set(args.marker) if args.marker else None,
                          max_directories=args.max_directories, history=history if args.history else None,
                          protect_refs=args.protect_ref)
        else:
            mode = "checkouts"
            if args.command == "clean-caches":
                mode = "derived" if args.derived_only else "caches"
            result = clean(args.checkout, mode=mode,
                           apply=args.apply, hot_hours=args.hot_hours, protect=args.protect,
                           check_remote=args.check_remote, prune=not getattr(args, "no_prune", False),
                           protect_refs=args.protect_ref, merged_into=getattr(args, "merged_into", None))
        # JSON is the stable agent interface, also the default readable output.
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("error") or result.get("truncated") else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error), "applied": False}), file=sys.stderr)
        return 1
