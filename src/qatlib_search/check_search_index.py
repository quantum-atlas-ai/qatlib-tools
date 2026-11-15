#!/usr/bin/env python3
"""Validate the on-disk Lean search index built by `qatlib_search.build_search_index`.

Checks, failing with a non-zero exit: every NDJSON line parses; every record carries the
full schema; every tracked configured module with public declarations appears;
no untracked module leaked in; proposition definitions carry their expanded bodies;
and every declaration's axiom closure stays within
{propext, Classical.choice, Quot.sound} (the ratio and the violation list are reported either
way). Stdlib only; no third-party dependencies.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search import paths
from qatlib_search.lean_source import mask_comments
from qatlib_search import build_search_index

ROOT = paths.ROOT
DEFAULT_INDEX_DIR = ROOT / ".qatlas" / "index"

REQUIRED_FIELDS = ("fqn", "module", "kind", "universes", "binders", "lean_type", "expanded_prop",
                   "deps_type", "deps_proof", "deps_instance", "axioms", "is_prop_definition")
KNOWN_KINDS = {"theorem", "def", "structure", "inductive", "axiom", "ctor", "rec", "opaque", "quot"}
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
DECL_RE = re.compile(
    r"^[ \t]*(?:(?:@\[[^\]]*\]\s*)*)"
    r"(?P<modifiers>(?:(?:private|protected|noncomputable|partial|unsafe|scoped|local)\s+)*)"
    r"(?:def|abbrev|theorem|lemma|structure|inductive|class|opaque|axiom|instance)\s",
    re.MULTILINE)



def module_source_path(root: Path, module: str) -> Path:
    return root / Path(*module.split(".")).with_suffix(".lean")


def source_declares_anything(root: Path, module: str) -> bool:
    """Require records for explicit public content, matching the exporter's visibility.

    Private-only modules legitimately have no exported records. Unreadable or
    malformed source remains a coverage error rather than receiving an exemption.
    """
    try:
        text = module_source_path(root, module).read_text(encoding="utf-8")
        text = mask_comments(text)
    except (OSError, ValueError):
        return True  # cannot exempt a source we cannot read
    return any('private' not in match.group('modifiers').split()
               for match in DECL_RE.finditer(text))


def load_records(path: Path) -> tuple[list[dict], list[str]]:
    records: list[dict] = []
    errors: list[str] = []
    if not path.exists():
        return records, [f"missing {path}"]
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(f"{path}:{lineno}: invalid JSON: {error}")
                continue
            if not isinstance(record, dict):
                errors.append(f"{path}:{lineno}: not a JSON object")
                continue
            records.append(record)
    return records, errors


def check_records(records: list[dict]) -> list[str]:
    errors: list[str] = []
    for record in records:
        fqn = record.get("fqn", "<missing fqn>")
        missing = [field for field in REQUIRED_FIELDS if field not in record]
        if missing:
            errors.append(f"{fqn}: missing required fields {missing}")
            continue
        if record["kind"] not in KNOWN_KINDS:
            errors.append(f"{fqn}: unknown kind {record['kind']!r}")
        if not isinstance(record["universes"], list):
            errors.append(f"{fqn}: universes is not a list")
        if not isinstance(record["binders"], list) or any(
                not isinstance(b, dict) or {"name", "type", "binder_kind"} - set(b)
                for b in record["binders"]):
            errors.append(f"{fqn}: binders is not a list of name/type/binder_kind objects")
        if not isinstance(record["lean_type"], str):
            errors.append(f"{fqn}: lean_type is not a string")
        for field in ("deps_type", "deps_proof", "deps_instance"):
            if not isinstance(record[field], list) or any(
                    not isinstance(d, dict) or "name" not in d or "in_project" not in d
                    for d in record[field]):
                errors.append(f"{fqn}: {field} is not a list of name/in_project objects")
        if not isinstance(record["axioms"], list) or any(not isinstance(a, str) for a in record["axioms"]):
            errors.append(f"{fqn}: axioms is not a list of strings")
        if not isinstance(record["is_prop_definition"], bool):
            errors.append(f"{fqn}: is_prop_definition is not a boolean")
        if record["is_prop_definition"] and not record["expanded_prop"]:
            errors.append(f"{fqn}: proposition definition without expanded_prop")
    return errors


def check_modules(records: list[dict], modules: list[str], root: Path) -> list[str]:
    errors: list[str] = []
    seen = {record.get("module") for record in records if isinstance(record.get("module"), str)}
    tracked = set(modules)
    for module in modules:
        if module in seen:
            continue
        if source_declares_anything(root, module):
            errors.append(f"tracked module {module} declares content but has no records in the index")
    for module in sorted(seen - tracked):
        errors.append(f"index contains untracked module {module}")
    return errors


def axiom_audit(records: list[dict]) -> tuple[float, list[tuple[str, list[str]]]]:
    violations: list[tuple[str, list[str]]] = []
    audited = 0
    for record in records:
        axioms = record.get("axioms")
        if not isinstance(axioms, list):
            continue
        audited += 1
        extra = sorted({a for a in axioms if a not in ALLOWED_AXIOMS})
        if extra:
            violations.append((record.get("fqn", "<missing fqn>"), extra))
    ratio = (audited - len(violations)) / audited if audited else 1.0
    return ratio, violations


def check_index(index_dir: Path, modules: list[str], root: Path = ROOT) -> dict:
    records, errors = load_records(index_dir / "declarations.jsonl")
    errors += check_records(records)
    errors += check_modules(records, modules, root)
    ratio, violations = axiom_audit(records)
    if violations:
        errors.append(f"{len(violations)} declarations use axioms outside {sorted(ALLOWED_AXIOMS)}")
    kinds: dict[str, int] = {}
    for record in records:
        kinds[record.get("kind", "?")] = kinds.get(record.get("kind", "?"), 0) + 1
    return {
        "errors": errors,
        "declaration_count": len(records),
        "declarations_by_kind": dict(sorted(kinds.items())),
        "modules_in_index": sorted({r.get("module") for r in records
                                    if isinstance(r.get("module"), str)}),
        "prop_definition_count": sum(1 for r in records if r.get("is_prop_definition") is True),
        "expanded_prop_count": sum(1 for r in records if r.get("expanded_prop")),
        "axiom_clean_ratio": ratio,
        "axiom_violations": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX_DIR,
                        help=f"index directory (default {DEFAULT_INDEX_DIR})")
    args = parser.parse_args()
    modules = build_search_index.tracked_modules(ROOT)
    report = check_index(args.index_dir, modules, ROOT)
    try:
        summary = json.loads((args.index_dir / "summary.json").read_text(encoding="utf-8"))
        report["errors"] += build_search_index.provenance_errors(ROOT, summary,
                                                                 args.index_dir / "declarations.jsonl")
    except (OSError, ValueError) as exc:
        report["errors"].append(f"missing/invalid index summary: {exc}")
    print(f"declarations: {report['declaration_count']} (kinds {report['declarations_by_kind']})")
    print(f"modules in index: {len(report['modules_in_index'])} of {len(modules)} tracked")
    print(f"proposition definitions: {report['prop_definition_count']} "
          f"(expanded_prop on {report['expanded_prop_count']} declarations)")
    print(f"axiom-clean declarations: {report['axiom_clean_ratio']:.4f} "
          f"({len(report['axiom_violations'])} violations)")
    for fqn, extra in report["axiom_violations"][:20]:
        print(f"  axiom violation {fqn}: {extra}")
    if report["errors"]:
        for error in report["errors"][:40]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(f"index check FAILED with {len(report['errors'])} error(s)", file=sys.stderr)
        return 1
    print("index check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
