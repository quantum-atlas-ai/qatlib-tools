"""Read written Lean signatures and docstrings directly from local sources."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .retrieve import extract
from .retrieve_corpus import discover


def lookup(project=None, *, prefix=None, substring=None, module=None, kind=None, limit=100) -> dict:
    """Search public declaration headers in the project's local Lake TOML libraries."""
    if not any((prefix, substring, module)) or any(
        value is not None and not value.strip() for value in (prefix, substring, module)
    ):
        raise ValueError("provide a nonempty FQN prefix, substring or module")
    if limit < 1:
        raise ValueError("limit must be positive")
    corpus = discover(project, source_only=True)
    results = []
    warnings = list(corpus.warnings)
    for repo, relative, name in corpus.files():
        if module and name != module:
            continue
        root = Path(repo["root"])
        path = root / relative
        try:
            declarations, _, _, warning = extract(path.read_text(encoding="utf-8"), path, root)
        except (UnicodeError, ValueError) as error:
            warnings.append(f"{path}: {error}")
            continue
        if warning:
            warnings.append(warning)
        for decl in declarations:
            if ((prefix and not decl.fqn.startswith(prefix)) or
                    (substring and substring not in decl.fqn) or (kind and decl.kind != kind)):
                continue
            results.append({**asdict(decl), "module": name, "path": str(path), "repo": repo["repo"]})
    results.sort(key=lambda row: (row["fqn"], row["path"], row["line"]))
    return {"results": results[:limit], "total": len(results), "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qatlib-lean-tools retrieve", description=__doc__)
    parser.add_argument("query", nargs="?", help="case-sensitive FQN substring")
    parser.add_argument("--project", help="Lake TOML project (default: nearest Lake project)")
    parser.add_argument("--prefix", help="case-sensitive FQN prefix")
    parser.add_argument("--substring", help="case-sensitive FQN substring")
    parser.add_argument("--module", help="exact Lean module name")
    parser.add_argument("--kind", help="declaration kind, such as theorem or def")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.query is not None and args.substring is not None:
        parser.error("provide the positional query or --substring")
    try:
        result = lookup(args.project, prefix=args.prefix,
                        substring=args.substring if args.substring is not None else args.query,
                        module=args.module, kind=args.kind, limit=args.limit)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        for row in result["results"]:
            print(f"{row['fqn']} [{row['kind']}] {row['path']}:{row['line']}")
            print(row["signature"])
            if row["doc"]:
                print(row["doc"])
        print(f"{len(result['results'])} of {result['total']} declarations")
    return 0
