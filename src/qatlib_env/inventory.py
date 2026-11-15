"""Read-only workspace facts and explicit, conservative Trash operations.

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
    """Read Git without refreshing the index, lazy fetching or prompting."""
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
    """Visible Lean/Lake processes; unreadable/unsupported inspection blocks apply."""
    found, errors = [], []
    proc = Path("/proc")
    if proc.is_dir():
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                argv = (item / "cmdline").read_bytes().split(b"\0")
                name = Path(os.fsdecode(argv[0])).name.removesuffix(".exe") if argv[0] else ""
                if name not in {"lean", "leanir", "lake"}:
                    continue
                found.append({"pid": int(item.name), "cwd": str((item / "cwd").resolve(strict=True)),
                              "command": [os.fsdecode(x) for x in argv if x]})
            except FileNotFoundError:
                continue  # the process exited
            except (OSError, ValueError) as error:
                errors.append(f"process {item.name}: {error}")
        return found, errors
    # ps works on macOS; Windows requires a platform-specific process observer.
    if sys.platform == "darwin":
        result = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True,
                                text=True, timeout=10, check=False)
        if result.returncode:
            return [], ["cannot inspect active processes"]
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and Path(parts[1]).name in {"lean", "leanir", "lake"}:
                # No reliable cwd from ps: conservatively protect every checkout.
                found.append({"pid": int(parts[0]), "cwd": None, "command": [parts[1]]})
        return found, []
    return [], ["active process inspection unavailable on this platform; apply refused"]


def _related(a, b):
    return a == b or a in b.parents or b in a.parents


def checkout_facts(path, *, inspect_ignored=False, semantic=None):
    path = Path(path).absolute()
    row = {"path": str(path), "errors": []}
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
        status = _read_git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
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
        row["ignored_count"] = None
        if inspect_ignored:
            row["ignored_count"] = len(_read_git(path, "ls-files", "--others", "--ignored",
                                                "--exclude-standard", "-z").split("\0")) - 1
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


def scan(roots, *, measure=False, check_remote=False, hot_hours=24, protect=(), now=None, exact=False, semantic=None):
    if not math.isfinite(hot_hours) or hot_hours < 24:
        raise ValueError("hot-hours must be finite and at least 24")
    now = time.time() if now is None else now
    if exact:
        repos = sorted({str(Path(p).expanduser().absolute()) for p in roots})
        markers, errors, skipped = [], [], []
    else:
        repos, markers, errors, skipped = discover(roots) if semantic is None else discover(roots, semantic=semantic)
    active, process_errors = processes()
    # Git operations do not contend for shared write locks. Four workers bound load.
    with ThreadPoolExecutor(max_workers=4) as executor:
        observe = checkout_facts if semantic is None else lambda p: checkout_facts(p, semantic=semantic)
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
        row["protection"] = list(row["errors"])
        row["hot"] = now - row.get("last_activity", now) < hot_hours * 3600
        if row["hot"]:
            row["protection"].append(f"activity within {hot_hours:g} hours")
        path = Path(row["path"])
        if any(_related(path, Path(p).absolute()) for p in protect):
            row["protection"].append("explicitly protected by operator")
        row["active_processes"] = [p for p in active if p["cwd"] is None or _related(path, Path(p["cwd"])) or
                                   any(str(path) in a for a in p["command"][1:])]
        if row["active_processes"]:
            row["protection"].append("active Lean/Lake process")
        if process_errors:
            row["protection"].extend(process_errors)
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
    return {"schema_version": 1, "captured_at": now, "roots": [str(p) for p in roots], "exact": exact,
            "checkouts": rows, "common_dirs": groups, "research_markers": markers,
            "errors": errors, "skipped": skipped, "process_errors": process_errors,
            "notes": ["No fetch/bootstrap/cache lock/config write; no proof that all research is harvested.",
                      "Sizes are optional; same-filesystem Trash does not reclaim physical space.",
                      "Processes visible to this user only; coordinate idle writers before apply."]}


def _lake_guard(path, remote_cache=None):
    """Lake packages contain source, not just derived cache. Preserve uncertain data."""
    errors = []
    for directory, dirs, files in os.walk(path, followlinks=False, onerror=lambda e: errors.append(str(e))):
        root = Path(directory)
        for name in dirs + files:
            item = root / name
            if item.is_symlink():
                errors.append(f"shared/symlinked Lake content: {item}")
        if ".git" in dirs or ".git" in files:
            fact = checkout_facts(root)
            common = common_facts(root, check_remote=True, remote_cache=remote_cache)
            try:
                ignored_source = git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z",
                                     "--", "*.lean", "*.py", "*.md")
                if fact["errors"] or fact.get("dirty_count") or ignored_source.returncode or ignored_source.stdout:
                    errors.append(f"modified/ignored/unknown dependency source: {root}")
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(f"dependency source inspection incomplete: {root}: {error}")
            if (common["errors"] or common.get("stash_count") or not common["remote_verified"] or
                    common["unpushed_branches"] or common["unknown_branches"]):
                errors.append(f"unharvested/unknown dependency Git data: {root}")
            if not fact.get("linked_worktree") and any(Path(p) != root for p in common.get("worktrees", [])):
                errors.append(f"dependency common-dir serves other worktrees: {root}")
            dirs[:] = []
        else:
            # build outputs are derived; source outside a Git package is not.
            if any(f.endswith((".lean", ".py", ".md")) for f in files) and "build" not in root.relative_to(path).parts:
                errors.append(f"unclassified source inside Lake: {root}")
            dirs[:] = [d for d in dirs if d not in {".git", "build"} and not (root / d).is_symlink()]
    return errors


def clean(checkouts, *, mode="caches", apply=False, hot_hours=24, protect=(), check_remote=False):
    """Re-observe the exact selected list; refuse the whole batch before any move."""
    selected = [Path(p).expanduser().absolute() for p in checkouts]
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("select distinct explicit checkout paths")
    for p in selected:
        if any(parent.is_symlink() for parent in (p, *p.parents)):
            raise ValueError(f"symlinked checkout path: {p}")
    targets = [p / ".lake" if mode == "caches" else p for p in selected]
    if any(_related(a, b) for i, a in enumerate(targets) for b in targets[i + 1:]):
        raise ValueError("overlapping targets must be reviewed separately")
    # Scan only exact checkouts, not their descendants: discovery is a separate step.
    active, process_errors = processes()
    rows, receipts, remote_cache = [], [], {}
    for path, target in zip(selected, targets):
        fact = checkout_facts(path, inspect_ignored=mode == "checkouts")
        reasons = list(fact["errors"]) + process_errors
        if time.time() - fact.get("last_activity", time.time()) < hot_hours * 3600:
            reasons.append("hot checkout (<24h or configured protection window)")
        if not math.isfinite(hot_hours) or hot_hours < 24:
            reasons.append("hot-hours must be finite and at least 24")
        if any(_related(path, Path(p).absolute()) for p in protect):
            reasons.append("explicitly protected by operator")
        if any(p["cwd"] is None or _related(path, Path(p["cwd"])) or
               any(str(path) in a for a in p["command"][1:]) for p in active):
            reasons.append("active Lean/Lake process")
        if fact.get("dirty_count"):
            reasons.append("dirty/untracked checkout")
        group = common_facts(path, check_remote=check_remote or mode == "checkouts", remote_cache=remote_cache)
        if group["errors"] or group.get("stash_count"):
            reasons.append("stash or incomplete common-dir observation")
        if mode == "checkouts":
            if not group["remote_verified"] or group["unpushed_branches"] or group["unknown_branches"]:
                reasons.append("unpushed/unknown common-dir branches or HEAD")
            others = [p for p in group.get("worktrees", []) if Path(p) != path]
            if not fact.get("linked_worktree") and others:
                reasons.append("main common-dir still serves linked worktrees")
            if fact.get("ignored_count"):
                reasons.append("ignored files require agent harvest review")
            # Never silently include nested independent repositories/research directories.
            nested, markers, errors, _ = discover([path])
            if errors or any(Path(p) != path for p in nested):
                reasons.append("nested checkout or incomplete discovery; review separately")
        elif target.is_dir() and not target.is_symlink() and not reasons:
            reasons.extend(_lake_guard(target, remote_cache=remote_cache))
        if target.is_symlink() or not target.exists():
            reasons.append("missing or shared/symlinked target")
        row = {"path": str(target), "checkout": str(path), "reasons": reasons,
               "head": fact.get("head"), "common_dir": fact.get("common_dir")}
        try:
            row["identity"] = _identity(target)
            row.update(system_trash.measure(target))
            row["trash_probe"] = system_trash.probe(target)
            if not row["trash_probe"].get("supported"):
                reasons.append("official Trash unavailable: " + str(row["trash_probe"]))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            reasons.append(str(error))
        row["eligible"] = not reasons
        rows.append(row)
    result = {"applied": False, "mode": mode, "targets": rows, "receipts": receipts,
              "physical_bytes_freed": 0}
    if apply:
        if any(not row["eligible"] for row in rows):
            result["error"] = "batch refused: protected or unverified targets; nothing moved"
            return result
        for row in rows:
            if _identity(Path(row["path"])) != row["identity"]:
                result["error"] = "target changed since preflight; stopped"
                return result
            current = checkout_facts(row["checkout"], inspect_ignored=mode == "checkouts")
            live, live_errors = processes()
            if (current["errors"] or current.get("head") != row["head"] or current.get("dirty_count") or
                    time.time() - current.get("last_activity", time.time()) < hot_hours * 3600 or live_errors or
                    any(p["cwd"] is None or _related(Path(row["checkout"]), Path(p["cwd"])) or
                        any(row["checkout"] in a for a in p["command"][1:]) for p in live)):
                result["error"] = "checkout/process state changed since preflight; stopped"
                return result
            group = common_facts(row["checkout"], check_remote=check_remote or mode == "checkouts")
            if (group["errors"] or group.get("stash_count") or
                    (mode == "checkouts" and (current.get("ignored_count") or not group["remote_verified"] or
                                             group["unpushed_branches"] or group["unknown_branches"])) or
                    (mode == "caches" and _lake_guard(Path(row["path"])))):
                result["error"] = "source/common-dir state changed since preflight; stopped"
                return result
            try:
                receipts.append(system_trash.trash(Path(row["path"])))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result["error"] = str(error)
                return result  # earlier receipts remain truthful; no fallback or rollback
        result["applied"] = True
    return result


def cli_main(argv=None):
    parser = argparse.ArgumentParser(prog="qatlib-env workspace", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("scan")
    inventory.add_argument("roots", type=Path, nargs="*")
    inventory.add_argument("--measure", action="store_true", help="measure .lake bytes (slower)")
    inventory.add_argument("--exact", action="store_true", help="inspect known checkout roots without recursive discovery")
    inventory.add_argument("--marker", action="append", help="additional domain directory/file marker (repeatable)")
    inventory.add_argument("--check-remote", action="store_true", help="live ls-remote, never fetch")
    for name in ("clean-caches", "trash-checkouts"):
        command = commands.add_parser(name)
        command.add_argument("--checkout", action="append", type=Path, required=True)
        action = command.add_mutually_exclusive_group()
        action.add_argument("--apply", action="store_true", help="only after approval of the concrete list")
        action.add_argument("--dry-run", action="store_true", help="default; no filesystem writes")
        command.add_argument("--check-remote", action="store_true")
    for command in (inventory, *[commands.choices[n] for n in ("clean-caches", "trash-checkouts")]):
        command.add_argument("--json", action="store_true")
        command.add_argument("--hot-hours", type=float, default=24)
        command.add_argument("--protect", action="append", type=Path, default=[])
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            roots = args.roots or [Path.cwd().parent, Path.home() / ".legacy-worktrees/tmp"]
            result = scan(roots, measure=args.measure, check_remote=args.check_remote,
                          hot_hours=args.hot_hours, protect=args.protect, exact=args.exact,
                          semantic=SEMANTIC | set(args.marker) if args.marker else None)
        else:
            result = clean(args.checkout, mode="caches" if args.command == "clean-caches" else "checkouts",
                           apply=args.apply, hot_hours=args.hot_hours, protect=args.protect,
                           check_remote=args.check_remote)
        # JSON is the stable agent interface, also the default readable output.
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("error") else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error), "applied": False}), file=sys.stderr)
        return 1
