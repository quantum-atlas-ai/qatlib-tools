#!/usr/bin/env python3
"""Build the Lean-environment search index for configured Lean library roots.

One driver process elaborates the repo's `src/qatlib_search/ExportSearchIndex.lean`
(its `import` header stripped) against the FULL tracked module set — enumerated exactly like
`verification.check`, not just the
wide root import — and emits one NDJSON record per declaration (into `.qatlas/records.ndjson`
via QATLAS_INDEX_OUT; the exporter's default channel is stdout). The records land in
`.qatlas/index/declarations.jsonl` (sorted by fqn, one compact JSON object per line) next to
`summary.json` (counts, module coverage diagnostics, toolchain/manifest/HEAD provenance, the
exact lean command and its exit code). `--check` rebuilds and compares against the on-disk
index so CI can reject a stale one; only `generated_at` is volatile.

Stdlib only; no third-party dependencies. Run from the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search import paths
from qatlib_search import index_identity

ROOT = paths.ROOT
MODULE_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
INDEX_DIR = ROOT / ".qatlas" / "index"
DRIVER = ROOT / ".qatlas" / "driver.lean"
EXPORTER = Path("src/qatlib_search/ExportSearchIndex.lean")
VOLATILE_SUMMARY_FIELDS = ("generated_at",)


def modules_from_paths(paths: list[str]) -> list[str]:
    """Admitted modules only, using the configured module roots and excluding test trees."""
    modules = sorted({path[:-5].replace("/", ".") for path in paths if path and
        (any(path == name + ".lean" or path.startswith(name + "/")
             for name in index_identity.MODULE_ROOTS))})
    if not modules or any(not MODULE_RE.fullmatch(m) for m in modules):
        raise SystemExit("No valid tracked library modules, or unexpected module path")
    return modules


def tracked_modules(root: Path = ROOT) -> list[str]:
    paths = index_identity.git_output(root, ["ls-files", "-z", "*.lean"]).decode('utf-8', 'replace').split("\0")
    return modules_from_paths(paths)


def strip_imports(script: str) -> str:
    """Drop every `import ` line before rebuilding the configured audit header.
    Lean audit scripts before regenerating the import header."""
    return "\n".join(line for line in script.splitlines() if not line.startswith("import "))


def exporter_path(root: Path) -> Path:
    """Use a checkout exporter when present, otherwise the installed wheel asset."""
    source = root / EXPORTER
    return source if source.is_file() else Path(__file__).with_name("ExportSearchIndex.lean")


def write_driver(root: Path, modules: list[str], driver: Path) -> None:
    exporter = exporter_path(root)
    script = strip_imports(exporter.read_text(encoding="utf-8"))
    script = script.replace("def projectModules : List Name := []",
                            "def projectModules : List Name := ["
                            + ", ".join("`" + name for name in modules) + "]")
    driver.parent.mkdir(parents=True, exist_ok=True)
    driver.write_text("\n".join(["import Lean"] + [f"import {m}" for m in modules])
                      + "\n" + script + "\n", encoding="utf-8")


def runtime_environment(path_value: str | None = None, home: Path | None = None) -> dict[str, str]:
    """Environment for lake/lean, prepending ~/.elan/bin when lake is not already on PATH."""
    env = os.environ.copy()
    if path_value is not None:
        env["PATH"] = path_value
    if shutil.which("lake", path=env.get("PATH", "")) is not None:
        return env
    elan = (Path.home() if home is None else Path(home)) / ".elan" / "bin"
    # which() knows the platform suffix (lake.exe on Windows); a bare-name
    # existence probe would miss every real Windows elan install.
    if shutil.which("lake", path=str(elan)) is None:
        raise SystemExit("lake not found on PATH or in ~/.elan/bin")
    env["PATH"] = f"{elan}{os.pathsep}{env.get('PATH', '')}"
    return env


def parse_records(stdout: str) -> list[dict]:
    """Parse the driver's NDJSON; every line must be a JSON object with a string `fqn`."""
    records: list[dict] = []
    seen: set[str] = set()
    for lineno, line in enumerate(stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(f"driver emitted non-JSON on line {lineno}: {error}: {line[:200]}") from error
        if not isinstance(obj, dict) or not isinstance(obj.get("fqn"), str):
            raise SystemExit(f"driver line {lineno} is not a JSON object with a string fqn: {line[:200]}")
        if obj["fqn"] in seen:
            raise SystemExit(f"driver emitted duplicate fqn {obj['fqn']}")
        seen.add(obj["fqn"])
        records.append(obj)
    records.sort(key=lambda record: record["fqn"])
    return records


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head(root: Path) -> str:
    return index_identity.git_output(root, ["rev-parse", "HEAD"]).decode('utf-8', 'replace').strip()


def source_bindings(root: Path, modules: list[str]) -> dict[str, str]:
    paths = [module.replace(".", "/") + ".lean" for module in modules]
    bindings = index_identity.snapshot(root, paths)
    if set(bindings) != set(paths):
        raise ValueError("index module source is not a committed Git blob")
    return bindings


def records_bytes(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for r in records).encode("utf-8")


def input_provenance(root: Path, modules: list[str]) -> dict:
    """Identity of the source and configuration used by the exporter.

    Record absent alternative Lake configuration too: adding lakefile.lean can
    change what Lake compiles even while lakefile.toml remains untouched.
    """
    return {
        "git_head": git_head(root),
        "lean_toolchain": (root / "lean-toolchain").read_text(encoding="utf-8").strip(),
        "lake_manifest_git_blob": index_identity.blob(root, "lake-manifest.json"),
        "lake_config_git_blob": {name: index_identity.blob(root, name)
                               for name in ("lakefile.toml", "lakefile.lean")},
        "source_git_blob": source_bindings(root, modules),
        "exporter_git_blob": index_identity.blob(root, EXPORTER.as_posix()),
        "exporter_sha256": sha256_file(exporter_path(root)),
        "modules": modules,
    }


def provenance_errors(root: Path, summary: dict, declarations: Path | None = None) -> list[str]:
    """Check the entire admitted source snapshot, not merely the queried declaration."""
    errors = []
    try:
        modules = tracked_modules(root)
        checks = input_provenance(root, modules)
        for key, expected in checks.items():
            if summary.get(key) != expected:
                errors.append(f"stale/missing index provenance: {key}")
        if summary.get("lean_exit_code") != 0:
            errors.append("index was not exported by a successful Lean process")
        if declarations is not None and summary.get("declarations_sha256") != sha256_file(declarations):
            errors.append("declaration index content differs from its exported hash")
    except (OSError, ValueError, subprocess.CalledProcessError, SystemExit) as exc:
        errors.append(f"cannot verify index provenance: {exc}")
    return errors


def build_summary(records: list[dict], modules: list[str], lean_command: list[str],
                  lean_exit_code: int, root: Path = ROOT,
                  generated_at: str | None = None, *, provenance: dict | None = None) -> dict:
    kinds: dict[str, int] = {}
    for record in records:
        kinds[record.get("kind", "?")] = kinds.get(record.get("kind", "?"), 0) + 1
    seen = {record.get("module") for record in records if isinstance(record.get("module"), str)}
    tracked = set(modules)
    return {
        "schema_version": "2.0",
        "declaration_count": len(records),
        "declarations_by_kind": dict(sorted(kinds.items())),
        "modules": modules,
        "module_count": len(modules),
        "modules_in_index": sorted(seen),
        "modules_missing_from_index": [m for m in modules if m not in seen],
        "modules_unexpected_in_index": [m for m in sorted(seen) if m not in tracked],
        "prop_definition_count": sum(1 for r in records if r.get("is_prop_definition") is True),
        "expanded_prop_count": sum(1 for r in records if r.get("expanded_prop")),
        **(provenance if provenance is not None else input_provenance(root, modules)),
        "declarations_sha256": hashlib.sha256(records_bytes(records)).hexdigest(),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lean_command": lean_command,
        "lean_exit_code": lean_exit_code,
    }


def run_pipeline(root: Path = ROOT) -> tuple[list[dict], dict]:
    from qatlib_env.lean_runtime import require_prepared_dependencies, prepared_environment

    modules = tracked_modules(root)
    before = input_provenance(root, modules)

    def check_inputs() -> None:
        try:
            current = input_provenance(root, tracked_modules(root))
        except ValueError as error:
            raise SystemExit("index export inputs changed during build/export; retry from a stable checkout") from error
        if current != before:
            raise SystemExit("index export inputs changed during build/export; retry from a stable checkout")
        # Reuse the gate's lock and tracked-source checks, without cleaning builds
        # or installing/updating dependencies on behalf of a retrieval command.
        require_prepared_dependencies(root)

    check_inputs()
    write_driver(root, modules, root / ".qatlas" / "driver.lean")
    records_path = root / ".qatlas" / "records.ndjson"
    temporary = root / '.lake/qatlas-tmp'
    temporary.mkdir(parents=True, exist_ok=True)
    env = prepared_environment(root, runtime_environment(), temporary)
    if index_identity.git_output(root, ["status", "--porcelain", "--untracked-files=all"]).strip():
        raise SystemExit("index export requires a clean committed checkout (use an isolated clone)")
    records_path.unlink(missing_ok=True)
    built = subprocess.run(["lake", "--no-build", "build", *modules], cwd=root, env=env, text=True,
                           encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if built.returncode != 0:
        raise SystemExit(f"lake build failed (exit {built.returncode}):\n{built.stdout[-4000:]}")
    check_inputs()
    lean_command = ["lake", "--no-build", "lean", str(root / ".qatlas" / "driver.lean"),
                    "--", "--trust=0", "-DautoImplicit=false", f"--root={root / '.qatlas'}"]
    # The exporter writes NDJSON to QATLAS_INDEX_OUT instead of stdout: in run_cmd context
    # runtime panic spew (delaborator "loose bvar" on a few matrix-heavy theorem types) shares
    # fd 1 with println and cannot be suppressed, so the clean stream needs a separate file.
    run_env = dict(env, QATLAS_INDEX_OUT=str(records_path))
    result = subprocess.run(lean_command, cwd=root, env=run_env, text=True,
                            encoding="utf-8", errors="replace",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        raise SystemExit(f"lean driver failed (exit {result.returncode}):\n{result.stdout[-4000:]}")
    check_inputs()
    if not records_path.exists():
        raise SystemExit(f"lean driver wrote no records file {records_path}")
    records = parse_records(records_path.read_text(encoding="utf-8"))
    if not records:
        raise SystemExit("lean driver emitted no records")
    (root / ".qatlas/export.log").write_text(result.stdout, encoding="utf-8")
    return records, build_summary(records, modules, lean_command, result.returncode, root, provenance=before)


def write_index(index_dir: Path, records: list[dict], summary: dict) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    # No newline translation: the on-disk bytes must match the exported sha256
    # (the Lean exporter writes LF; Windows text mode would rewrite it).
    with (index_dir / "declarations.jsonl").open("w", encoding="utf-8", newline="") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    (index_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")


def load_index(index_dir: Path) -> tuple[list[dict], dict]:
    records = []
    with (index_dir / "declarations.jsonl").open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{index_dir / 'declarations.jsonl'}:{lineno}: invalid JSON: {error}")
    summary = json.loads((index_dir / "summary.json").read_text(encoding="utf-8"))
    return records, summary


def compare_index(index_dir: Path, records: list[dict], summary: dict) -> list[str]:
    """Differences between a fresh build and the on-disk index (volatile fields ignored)."""
    if not (index_dir / "declarations.jsonl").exists() or not (index_dir / "summary.json").exists():
        return [f"missing index files under {index_dir}"]
    disk_records, disk_summary = load_index(index_dir)
    errors: list[str] = []
    if disk_records != records:
        fresh = {r.get("fqn"): r for r in records}
        disk = {r.get("fqn"): r for r in disk_records}
        for fqn in sorted(set(disk) - set(fresh)):
            errors.append(f"on-disk record {fqn} absent from a fresh build")
        for fqn in sorted(set(fresh) - set(disk)):
            errors.append(f"fresh record {fqn} missing on disk")
        for fqn in sorted(set(fresh) & set(disk)):
            if fresh[fqn] != disk[fqn]:
                errors.append(f"record {fqn} differs from a fresh build")
    for key, value in summary.items():
        if key in VOLATILE_SUMMARY_FIELDS:
            continue
        if disk_summary.get(key) != value:
            errors.append(f"summary field {key} differs: disk={disk_summary.get(key)!r} fresh={value!r}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="rebuild and exit 1 if the on-disk index is stale (no rewrite)")
    parser.add_argument("--index-dir", type=Path, default=INDEX_DIR,
                        help=f"index directory (default {INDEX_DIR})")
    args = parser.parse_args()
    records, summary = run_pipeline()
    if args.check:
        errors = compare_index(args.index_dir, records, summary)
        if errors:
            for error in errors[:20]:
                print(error, file=sys.stderr)
            print(f"{args.index_dir} is stale; run `python3 -m qatlib_search.build_search_index`",
                  file=sys.stderr)
            return 1
        print(f"{args.index_dir} is up to date ({summary['declaration_count']} declarations, "
              f"{summary['module_count']} modules)")
        return 0
    write_index(args.index_dir, records, summary)
    print(f"wrote {args.index_dir / 'declarations.jsonl'} and summary.json: "
          f"{summary['declaration_count']} declarations over {summary['module_count']} modules, "
          f"kinds {summary['declarations_by_kind']}, "
          f"{summary['prop_definition_count']} proposition definitions, "
          f"{summary['expanded_prop_count']} expanded props")
    return 0


if __name__ == "__main__":
    # GBK consoles crash printing Lean/UTF-8 output; never fail on encoding.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
