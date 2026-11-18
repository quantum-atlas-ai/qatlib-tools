"""Classify exact Git trees using a repository's build workflow."""
from pathlib import PurePosixPath
import re
import subprocess

import yaml

from .pr_evidence import exact_sha
from .repo_api import RepoError


def _git(project, *args, allow_difference=False):
    result = subprocess.run(["git", *args], cwd=project, capture_output=True, timeout=60)
    if result.returncode and not (allow_difference and result.returncode == 1):
        raise RepoError(f"git {args[0]} failed for the supplied repository and revisions")
    return result


def _pattern(pattern):
    # Handle the common GitHub/Gitea subset. Refuse a verdict when a
    # workflow uses syntax this matcher does not understand.
    if not re.fullmatch(r"[A-Za-z0-9_.* /-]+", pattern) or "***" in pattern:
        raise RepoError(f"unsupported workflow path pattern: {pattern!r}")
    pieces = re.split(r"(\*\*/|\*\*|\*)", pattern)
    tokens = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*"}
    return re.compile("".join(tokens.get(piece, re.escape(piece)) for piece in pieces), re.DOTALL)


def _rules(raw):
    try:
        workflow = yaml.safe_load(raw)
    except yaml.YAMLError:
        raise RepoError("cannot parse workflow YAML") from None
    # PyYAML's YAML 1.1 loader parses the unquoted GitHub 'on' key as True.
    events = workflow.get("on", workflow.get(True)) if isinstance(workflow, dict) else None
    if events == "pull_request" or isinstance(events, list) and "pull_request" in events:
        return "paths-ignore", []
    if not isinstance(events, dict) or "pull_request" not in events:
        raise RepoError("build workflow has no pull_request trigger")
    config = events["pull_request"]
    if config is None:
        return "paths-ignore", []
    if not isinstance(config, dict) or {"paths", "paths-ignore"} <= config.keys():
        raise RepoError("invalid pull_request path filters")
    mode = "paths" if "paths" in config else "paths-ignore"
    patterns = config.get(mode, [])
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise RepoError("invalid workflow path list")
    if mode == "paths" and not any(not p.startswith("!") for p in patterns):
        raise RepoError("paths requires a positive pattern")
    return mode, [(p.startswith("!") and mode == "paths", _pattern(p.removeprefix("!") if mode == "paths" else p))
                  for p in patterns]


def _matched(paths, rules):
    mode, entries = rules
    names = []
    for path in paths:
        include = mode == "paths-ignore"
        for negative, regex in entries:
            if regex.fullmatch(path):
                include = mode == "paths" and not negative
        if include:
            names.append(path)
    return names


def _docs(paths):
    return all(PurePosixPath(path).suffix.lower() in {".md", ".rst"} and
               not path.startswith((".github/", ".gitea/")) for path in paths)


def _paths(project, base, head):
    return sorted(set(_git(project, "diff", "--no-renames", "--name-only", "-z",
                           base, head, "--").stdout.decode(errors="surrogateescape").split("\0")) - {""})


def classify(project, *, base, head, workflow, tested_base=None, tested_head=None, ci_success=False):
    """Evaluate local trees; ci_success is a caller attestation, not an API lookup."""
    base, head = exact_sha(base), exact_sha(head)
    path = PurePosixPath(workflow)
    if (path.parent.as_posix() not in {".github/workflows", ".gitea/workflows"}
            or path.suffix not in {".yml", ".yaml"}):
        raise RepoError("use a tracked workflow path under .github/workflows or .gitea/workflows")
    bases = _git(project, "merge-base", "--all", base, head).stdout.decode().splitlines()
    if len(bases) != 1:
        raise RepoError("expected one merge base")
    ancestor = bases[0]
    base_rules = _rules(_git(project, "show", f"{base}:{path}").stdout)
    head_rules = _rules(_git(project, "show", f"{head}:{path}").stdout)
    paths = _paths(project, ancestor, head)
    matching = sorted(set(_matched(paths, base_rules) + _matched(paths, head_rules)))
    static_only = bool(paths) and not matching and _docs(paths)
    result = {"head_sha": head, "base_sha": base, "merge_base": ancestor, "workflow": str(path),
              "changed_paths": paths, "build_paths": matching,
              "classification": "static-only" if static_only else "full",
              "reuse": False}
    if tested_base is None and tested_head is None:
        return result
    tested_base, tested_head = exact_sha(tested_base), exact_sha(tested_head)
    result.update(tested_base=tested_base, tested_head=tested_head)
    reasons = []
    if not ci_success or tested_head != head:
        reasons.append("requires successful applicable jobs for the exact current head")
    if ancestor != tested_base and base != tested_base:
        reasons.append("tested base is not the current merge base")
    if _git(project, "merge-base", "--is-ancestor", tested_base, base,
            allow_difference=True).returncode:
        reasons.append("current base does not descend from the tested base")
    advance = _paths(project, tested_base, base)
    # Include both sides of renames and directory/file replacements.
    pr_paths = set(paths)
    parents = {str(parent) for item in paths for parent in PurePosixPath(item).parents}
    overlap = [item for item in advance if item in pr_paths or item in parents
               or any(str(parent) in pr_paths for parent in PurePosixPath(item).parents)]
    if overlap:
        reasons.append("base advance intersects PR paths")
    if not _docs(advance) or _matched(advance, base_rules) or _matched(advance, head_rules):
        reasons.append("base advance is not limited to documentation outside build paths")
    result.update(base_advance_paths=advance, overlap=overlap, reuse=not reasons, reasons=reasons)
    return result
