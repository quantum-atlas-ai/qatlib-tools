"""Generic dependency upgrade and promotion retirement checks for thin adapters."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import workspace
from .project import primary_library
from qatlib_search.promotion import check_duplicates, locked_revision


def merge_bibliography(project: Path, increment: Path, *, write: bool) -> dict:
    from qatlib_search.doc_references import merge_bibliographies, parse_bibliography_entries
    target = project / "docs/references.bib"
    baseline = target.read_bytes().decode("utf-8")
    entries, errors = merge_bibliographies(
        baseline, increment.read_bytes().decode("utf-8"),
        baseline_path=str(target), increment_path=str(increment),
    )
    result = {"action": "merge-bibliography", "target": str(target),
              "errors": [str(error) for error in errors]}
    if not errors:
        existing, _ = parse_bibliography_entries(baseline, str(target))
        added = [key for key in entries if key not in existing]
        if write and added:
            # Retain the target's bytes, including comments and final newlines.
            separator = "" if baseline.endswith("\n\n") else "\n" if baseline.endswith("\n") else "\n\n"
            addition = "\n\n".join(entries[key] for key in added)
            target.write_bytes((baseline + separator + addition + "\n").encode("utf-8"))
        result.update(added=added, total=len(entries), written=bool(write and added))
    return result


def single_root(project: Path, override: str | None) -> tuple[Path, str]:
    """Return a source base and one library root, including Lake srcDir."""
    if override:
        from .project import libraries
        matches = [lib for lib in libraries(project) if override in lib.roots]
        if len(matches) != 1:
            raise ValueError("explicit root must identify one Lake library: " + override)
        lib = matches[0]
    else:
        lib = primary_library(project)
        if len(lib.roots) != 1:
            raise ValueError("library has several roots; select one explicitly")
    root = override or lib.roots[0]
    # The shared duplicate checker operates on one top-level identifier root.
    if "." in root:
        raise ValueError("retirement checks require a single-component source root")
    return project / lib.src_dir, root


def dependency(project: Path, package: str, source: Path | None) -> Path:
    manifest = workspace.manifest(project)
    matches = [entry for entry in manifest["packages"] if entry.get("name") == package]
    if len(matches) != 1:
        raise ValueError("expected one dependency named " + package)
    if source is not None:
        return source.resolve()
    if Path(package).name != package or package in {".", ".."}:
        raise ValueError("unsafe dependency directory name")
    return (project / manifest.get("packagesDir", ".lake/packages") / package).resolve()


def execute(project: Path, args, *, resource_cli: dict | None = None) -> dict:
    project = project.resolve()
    if args.action == "merge-bibliography":
        return merge_bibliography(project, args.increment, write=args.write)
    if args.action == "smoke":
        from .smoke import run
        return run(project, args.probe, args.migration_map,
                   env=workspace.environment(project, cli=resource_cli))
    if args.action == "bump-lock":
        # Check that this is a direct locked dependency before allowing mutation.
        locked_revision(project, args.package)
        workspace.run(["lake", "update", args.package], cwd=project,
                      env=workspace.environment(project, cli=resource_cli))
    source = dependency(project, args.package, args.source)
    base, public_root = single_root(source, args.root)
    # Exact source identity is validated at the checkout root, even with srcDir.
    relative_root = (base / public_root).relative_to(source).as_posix()
    from qatlib_search.promotion import verify_source_revision
    revision = locked_revision(project, args.package)
    verify_source_revision(source, revision, relative_root)
    result = {"action": args.action, "package": args.package, "revision": revision,
              "source": str(source), "root": public_root}
    if args.action == "check-retired":
        local_base, local_root = single_root(project, args.source_root)
        errors, stats = check_duplicates(project, source,
            source_root=(local_base / local_root).relative_to(project).as_posix(),
            public_root=relative_root)
        result.update(errors=errors, stats=stats)
    return result


def add_parser(commands):
    parser = commands.add_parser("promotion", help=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("verify-lock", "bump-lock", "check-retired"):
        command = actions.add_parser(action)
        command.add_argument("--package", required=True, help="exact direct Lake dependency name")
        command.add_argument("--source", type=Path, help="explicit dependency checkout")
        command.add_argument("--root", "--public-root", dest="root", help="dependency library root")
        command.add_argument("--source-root", help="consumer library root (check-retired)")
    smoke = actions.add_parser("smoke", help="run small module-mode consumers without building dependencies")
    smoke.add_argument("--probe", type=Path, action="append", required=True)
    smoke.add_argument("--migration-map", type=Path, help="TOML [[rename]] old/new/reason records")
    bibliography = actions.add_parser("merge-bibliography", help="check and append new BibTeX entries")
    bibliography.add_argument("--increment", type=Path, required=True, help="source Dev bibliography")
    bibliography.add_argument("--write", action="store_true", help="update the destination checkout")


def cli(project: Path, args, *, resource_cli: dict | None = None) -> int:
    try:
        result = execute(project, args, resource_cli=resource_cli)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return int(bool(result.get("errors")))
