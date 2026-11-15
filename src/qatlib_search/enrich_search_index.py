#!/usr/bin/env python3
"""Enrich the B04 declaration index with provenance, semantic labels and staleness bindings.

Reads `.qatlas/index/declarations.jsonl` (B04 output; never modified here — public helpers
of `qatlib_search.build_search_index` are imported, not edited) plus `metadata/papers/*.json`
(identity + claims in one file per paper), and writes `.qatlas/index/enriched.jsonl`: one merged
record per declaration (all B04 fields plus the new ones below).

New fields per record
---------------------
- `paper`: paper directory name derived ONLY from the module path
  (`<LibraryRoot>.Papers.<Paper>.…` → `<Dir>`); `null` outside Papers. There is
  deliberately no second hand-maintained pairing list — the path IS the authority.
- `paper_identity`: `{arxiv_id, arxiv_version_used, venue}` from
  `metadata/papers/<paper>.json` (`null` when the record has no paper).
- `result_labels`: active Claim `label`s whose `covered_by` points at this fqn.
- `aliases`: mechanically joined strings: fqn short name, each result label, each label
  core (parenthetical stripped), `<INITIALS> <label core>` (e.g. "BHMT Theorem 12"),
  `<INITIALS>` and `<INITIALS><year>` (e.g. "BHMT2000"). INITIALS = first letters of the
  author surnames, in metadata order.
- `model_labels`: conservative derivation, one list per key; a value that cannot be
  honestly derived from the declaration's module/type text is `["unknown"]` — never a
  guessed default like "general". Rules (see MODEL_RULES / derive_model_labels):
    * finite_dimensional: `["arbitrary_hilbert_space"]` when the elaborated types mention
      `HilbertBasis` (statement quantifies over a general complete inner-product space);
      `["finite_dimensional"]` when they mention `Matrix`/`EuclideanSpace`/`Qubit`/`Fin`/
      `Finsupp` (this library's matrices and finite bases); else `["unknown"]`.
    * state_model: `["mixed_state"]` for modules about density matrices/channels/shadows
      (MixedState, Channel, Mixture, Shadow); `["pure_state"]` for ket-state modules
      (Qubit, Reflection, PhaseEstimation, Oracle, Circuit, Tensor); else `["unknown"]`.
    * query_model: `["classical_query"]` for ClassicalQuery modules; `["oracle_query"]`
      for modules named *Query*/*Oracle*/*BlockEncoding*; else `["unknown"]`.
    * adaptive: `["adaptive"]` for modules with interleaved measurement (*Observed*,
      *Adaptive*); else `["unknown"]`.
    * cost_unit: `["bit_machine_steps"]` (BitMachineTime), `["oracle_queries"]`
      (ObservedQueryBudget), `["expected_cost"]` (RandomizedCost); else `["unknown"]`.
- `coverage`: papers-metadata coverage of the alphabetically first result label covering the
  fqn; `null` when the declaration is not a linked result.
- `binding`: `{source_git_blob, deps_fqns, review_binding_version,
  review_snapshot_sha256, commit, built_at}` where `source_git_blob` is the
  committed Git blob ID of the declaration source (path derived from its module) and
  `deps_fqns` are the sorted in-project direct dependency fqns
  (deps_type + deps_proof + deps_instance). The semantic part reviewed evidence pins is
  `{source_git_blob, deps_fqns, review_binding_version, review_snapshot_sha256}`;
  `binding_semantic_sha256(binding)` is its canonical-JSON sha256 and is what
  `review-bindings.json` registers. Version 2 pins the whole tracked source snapshot,
  raw declaration index, toolchain, Lake configuration/lock and exporter. This
  deliberately invalidates unrelated reviews too: exported dependency edges can omit
  internal declarations and are not a complete semantic dependency graph.

`--check` recomputes everything and compares against on-disk `enriched.jsonl`. A
declaration is reported `stale` when its source file hash changed, its direct-dependency
fqn set changed, it disappeared/appeared, (transitively) any in-project dependency of
it is itself stale, or its conservative review snapshot changed. Entries of
`.qatlas/index/review-bindings.json` (a list, or an
object with a `bindings` list, of `{fqn, reviewed_at, binding_sha256}`) whose registered
hash no longer matches the current semantic binding are listed under `invalid`. Legacy
own-source-only bindings require renewed review; refreshing enrichment does not renew
or rewrite review evidence. Neither a matching digest nor this report establishes
semantic correctness or Lean applicability. Exit 1
when anything is stale/invalid, 0 when clean. `commit`/`built_at` drift is reported but
is not staleness.

Stdlib only; no third-party dependencies, no network. Run from the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search import paths
from qatlib_search import build_search_index as _builder

ROOT = paths.ROOT
INDEX_DIR = ROOT / ".qatlas" / "index"
PAPERS_DIR_PART = "Papers"
PAPERS_MODULE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\.Papers\.([A-Za-z_][A-Za-z0-9_]*)\.")

MODEL_KEYS = ("finite_dimensional", "state_model", "query_model", "adaptive", "cost_unit")

FINITE_EVIDENCE = ("HilbertBasis",)
FINITE_DIM_EVIDENCE = ("Matrix", "EuclideanSpace", "Qubit", "Fin", "Finsupp")
MIXED_STATE_MODULES = ("MixedState", "Channel", "Mixture", "Shadow")
PURE_STATE_MODULES = ("Qubit", "Reflection", "PhaseEstimation", "Oracle", "Circuit", "Tensor")
CLASSICAL_QUERY_MODULES = ("ClassicalQuery",)
ORACLE_QUERY_MODULES = ("Query", "Oracle", "BlockEncoding")
ADAPTIVE_MODULES = ("Observed", "Adaptive")
COST_UNIT_MODULES = {
    "BitMachineTime": "bit_machine_steps",
    "ObservedQueryBudget": "oracle_queries",
    "RandomizedCost": "expected_cost",
}
LABEL_CORE_RE = re.compile(r"\s*\([^)]*\)\s*$")
REVIEW_BINDING_VERSION = 3


sha256_file = _builder.sha256_file
git_head = _builder.git_head


def derive_paper(module: str) -> str | None:
    """Paper directory from the module path alone; None outside the Papers namespaces."""
    match = PAPERS_MODULE_PATTERN.match(module)
    return match.group(1) if match else None


def source_path(root: Path, module: str) -> Path:
    return root / (module.replace(".", "/") + ".lean")


def load_papers_metadata(metadata_dir: Path, consumed: dict[Path, bytes] | None = None) -> dict[str, dict]:
    papers: dict[str, dict] = {}
    if not metadata_dir.is_dir():
        return papers
    for path in sorted(metadata_dir.glob("*.json")):
        contents = path.read_bytes()
        if consumed is not None:
            consumed[path.resolve()] = contents
        data = json.loads(contents)
        directory = data["id"]
        papers[directory] = data
    return papers


def theorem_link_index(papers: dict[str, dict]) -> dict[str, dict]:
    """fqn → {pairs: [(result_label, coverage), …], paper} from active claims in metadata/papers/*.json."""
    index: dict[str, dict] = {}
    for paper, data in sorted(papers.items()):
        for link in data.get("claims", []):
            if link.get("status", "active") != "active":
                continue
            label = link.get("label")
            for covered in link.get("covered_by", []):
                fqn = covered.get("fqn")
                if not isinstance(fqn, str) or not isinstance(label, str):
                    continue
                entry = index.setdefault(fqn, {"pairs": [], "paper": paper})
                entry["pairs"].append((label, covered.get("coverage")))
    return index


def author_initials(identity: dict) -> str:
    """First letters of author surnames in metadata order, e.g. ["Gilles Brassard", …] → BHMT."""
    initials = []
    for author in identity.get("authors") or []:
        surname = re.split(r"\s+", author.strip())[-1]
        if surname:
            initials.append(surname[0].upper())
    return "".join(initials)


def label_core(label: str) -> str:
    return LABEL_CORE_RE.sub("", label).strip()


def build_aliases(fqn: str, result_labels: list[str], identity: dict | None) -> list[str]:
    aliases = [fqn.rsplit(".", 1)[-1]]
    for label in result_labels:
        aliases.extend([label, label_core(label)])
    if identity is not None:
        initials = author_initials(identity)
        if initials:
            aliases.extend([initials, f"{initials}{identity.get('year', '')}"])
            for label in result_labels:
                aliases.append(f"{initials} {label_core(label)}")
    seen: set[str] = set()
    return [a for a in aliases if a and not (a in seen or seen.add(a))]


def _declaration_text(record: dict) -> str:
    parts = [record.get("module", ""), record.get("lean_type") or "", record.get("expanded_prop") or ""]
    for binder in record.get("binders") or []:
        parts.append(binder.get("type") or "")
    return " ".join(parts)


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def derive_model_labels(record: dict) -> dict[str, list[str]]:
    module = record.get("module", "")
    text = _declaration_text(record)
    labels: dict[str, list[str]] = {}

    if _matches_any(text, FINITE_EVIDENCE):
        labels["finite_dimensional"] = ["arbitrary_hilbert_space"]
    elif _matches_any(text, FINITE_DIM_EVIDENCE):
        labels["finite_dimensional"] = ["finite_dimensional"]
    else:
        labels["finite_dimensional"] = ["unknown"]

    if _matches_any(module, MIXED_STATE_MODULES):
        labels["state_model"] = ["mixed_state"]
    elif _matches_any(module, PURE_STATE_MODULES):
        labels["state_model"] = ["pure_state"]
    else:
        labels["state_model"] = ["unknown"]

    if _matches_any(module, CLASSICAL_QUERY_MODULES):
        labels["query_model"] = ["classical_query"]
    elif _matches_any(module, ORACLE_QUERY_MODULES):
        labels["query_model"] = ["oracle_query"]
    else:
        labels["query_model"] = ["unknown"]

    labels["adaptive"] = ["adaptive"] if _matches_any(module, ADAPTIVE_MODULES) else ["unknown"]

    cost = ["unknown"]
    for keyword, unit in COST_UNIT_MODULES.items():
        if keyword in module:
            cost = [unit]
            break
    labels["cost_unit"] = cost
    return {key: labels[key] for key in MODEL_KEYS}


def direct_dependency_fqns(record: dict) -> list[str]:
    names: set[str] = set()
    for key in ("deps_type", "deps_proof", "deps_instance"):
        for dep in record.get(key) or []:
            if dep.get("in_project") and isinstance(dep.get("name"), str):
                names.add(dep["name"])
    return sorted(names)


def binding_semantic_payload(binding: dict) -> str:
    return json.dumps({"source_git_blob": binding["source_git_blob"], "deps_fqns": binding["deps_fqns"],
                       "review_binding_version": binding.get("review_binding_version"),
                       "review_snapshot_sha256": binding.get("review_snapshot_sha256")},
                      ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def binding_semantic_sha256(binding: dict) -> str:
    return hashlib.sha256(binding_semantic_payload(binding).encode("utf-8")).hexdigest()


def enrich_records(records: list[dict], root: Path, papers_dir: Path,
                   commit: str | None, built_at: str, *,
                   review_snapshot_sha256: str | None = None,
                   consumed_metadata: dict[Path, bytes] | None = None) -> tuple[list[dict], dict[str, int]]:
    """Join labels; only build_all supplies the full snapshot for review bindings.

    A direct caller without that snapshot can still enrich labels, but its records
    cannot validate persisted review evidence.
    """
    papers = load_papers_metadata(papers_dir, consumed_metadata)
    links = theorem_link_index(papers)
    source_blobs = _builder.source_bindings(root, sorted({r["module"] for r in records}))
    enriched: list[dict] = []
    stats = {"paper_records": 0, "foundations_or_root_records": 0, "missing_metadata_papers": 0}
    for record in records:
        paper = derive_paper(record.get("module", ""))
        identity = None
        if paper is not None:
            stats["paper_records"] += 1
            meta = papers.get(paper)
            if meta is None:
                stats["missing_metadata_papers"] += 1
            else:
                raw = meta.get("identity") or {}
                identity = {
                    "arxiv_id": raw.get("arxiv_id"),
                    "arxiv_version_used": raw.get("arxiv_version_used"),
                    "qatlas_paper_id": raw.get("qatlas_paper_id"),
                    "venue": raw.get("venue"),
                    "authors": raw.get("authors") or [],
                    "year": raw.get("year"),
                }
        else:
            stats["foundations_or_root_records"] += 1
        link = links.get(record["fqn"], {"pairs": [], "paper": paper})
        result_labels = sorted({label for label, _coverage in link["pairs"]})
        coverage = sorted(link["pairs"])[0][1] if link["pairs"] else None
        path = source_path(root, record["module"])
        enriched.append({
            **record,
            "paper": paper,
            "paper_identity": identity,
            "result_labels": result_labels,
            "aliases": build_aliases(record["fqn"], result_labels, identity),
            "model_labels": derive_model_labels(record),
            "coverage": coverage,
            "binding": {
                "source_git_blob": source_blobs.get(path.relative_to(root).as_posix()),
                "deps_fqns": direct_dependency_fqns(record),
                "review_binding_version": REVIEW_BINDING_VERSION,
                "review_snapshot_sha256": review_snapshot_sha256,
                "commit": commit,
                "built_at": built_at,
            },
        })
    return enriched, stats


def label_distribution(records: list[dict]) -> dict[str, dict[str, int]]:
    distribution: dict[str, dict[str, int]] = {}
    for record in records:
        for key, values in record.get("model_labels", {}).items():
            bucket = distribution.setdefault(key, {})
            for value in values:
                bucket[value] = bucket.get(value, 0) + 1
    return distribution


def write_enriched(index_dir: Path, enriched: list[dict]) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    with (index_dir / "enriched.jsonl").open("w", encoding="utf-8") as handle:
        for record in enriched:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_enriched(index_dir: Path) -> list[dict]:
    path = index_dir / "enriched.jsonl"
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise SystemExit(f"{path}:{lineno}: invalid JSON: {error}") from error
    return records


def _binding_map(records: list[dict]) -> dict[str, dict]:
    return {r["fqn"]: r.get("binding") or {} for r in records}


def staleness_report(fresh: list[dict], disk: list[dict]) -> dict:
    """Per-fqn staleness: own source hash / dependency set changed, added or removed
    declarations, plus transitive propagation through in-project dependencies."""
    fresh_map = _binding_map(fresh)
    disk_map = _binding_map(disk)
    stale: dict[str, list[str]] = {}
    for fqn in sorted(set(disk_map) - set(fresh_map)):
        stale.setdefault(fqn, []).append("removed_from_declarations")
    for fqn in sorted(set(fresh_map) - set(disk_map)):
        stale.setdefault(fqn, []).append("new_declaration")
    for fqn in sorted(set(fresh_map) & set(disk_map)):
        reasons = []
        if fresh_map[fqn].get("source_git_blob") != disk_map[fqn].get("source_git_blob"):
            reasons.append("source_changed")
        if fresh_map[fqn].get("deps_fqns") != disk_map[fqn].get("deps_fqns"):
            reasons.append("deps_changed")
        if (fresh_map[fqn].get("review_binding_version"),
                fresh_map[fqn].get("review_snapshot_sha256")) != (
                disk_map[fqn].get("review_binding_version"),
                disk_map[fqn].get("review_snapshot_sha256")):
            reasons.append("review_snapshot_changed")
        if reasons:
            stale.setdefault(fqn, []).extend(reasons)
    # Transitive closure: anything (transitively) depending on a stale declaration is stale.
    dependents: dict[str, set[str]] = {}
    for fqn, binding in fresh_map.items():
        for dep in binding.get("deps_fqns") or []:
            dependents.setdefault(dep, set()).add(fqn)
    frontier = list(stale)
    while frontier:
        fqn = frontier.pop()
        for dependent in dependents.get(fqn, ()):
            if "dependency_stale" not in stale.setdefault(dependent, []):
                stale[dependent].append("dependency_stale")
                frontier.append(dependent)
    commit_changed = sum(
        1 for fqn in set(fresh_map) & set(disk_map)
        if fresh_map[fqn].get("commit") != disk_map[fqn].get("commit"))
    return {"stale": dict(sorted(stale.items())), "commit_changed": commit_changed}


def review_binding_report(review_bindings: list[dict], fresh: list[dict],
                          stale: dict[str, list[str]]) -> list[dict]:
    fresh_map = _binding_map(fresh)
    invalid = []
    for entry in review_bindings:
        fqn = entry.get("fqn")
        current = fresh_map.get(fqn)
        if current is None:
            invalid.append({**entry, "reason": "fqn_absent_from_index"})
        elif not isinstance(current, dict):
            invalid.append({**entry, "reason": "review_binding_incomplete"})
        elif (type(current.get("review_binding_version")) is not int
              or current["review_binding_version"] != REVIEW_BINDING_VERSION):
            invalid.append({**entry, "reason": "review_binding_version_unsupported"})
        elif (any(not isinstance(current.get(key), str)
                  or not re.fullmatch(r"[0-9a-f]{64}", current[key])
                  for key in ("review_snapshot_sha256",))
              or not isinstance(current.get("source_git_blob"), str)
              or not re.fullmatch(r"[0-9a-f]{40,64}", current["source_git_blob"])
              or not isinstance(current.get("deps_fqns"), list)
              or not all(isinstance(dep, str) for dep in current["deps_fqns"])):
            invalid.append({**entry, "reason": "review_binding_incomplete"})
        elif binding_semantic_sha256(current) != entry.get("binding_sha256"):
            reason = "stale" if fqn in stale else "binding_changed"
            invalid.append({**entry, "reason": reason})
    return invalid


def load_review_bindings(index_dir: Path) -> list[dict]:
    path = index_dir / "review-bindings.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("bindings") or []
    return [entry for entry in data if isinstance(entry, dict) and entry.get("fqn")]


def metadata_bindings(metadata_dir: Path, consumed: dict[Path, bytes] | None = None) -> dict[str, str]:
    # Only public metadata actually consumed by enrichment/retrieval, never held-out eval gold.
    metadata_dir = metadata_dir.resolve()
    paths = sorted(metadata_dir.glob("papers/*.json"))
    metadata_blobs = {}
    if paths:
        if (metadata_dir / "papers").is_symlink() or any(path.is_symlink() for path in paths):
            raise ValueError("metadata index inputs must be regular tracked files, not symlinks")
        # Frozen consumes Dev's authoritative metadata through --metadata-dir.
        # Bind those files to their own clean Git objects, not to the Lean
        # package's separate checkout or to untracked external source text.
        metadata_root = Path(_builder.index_identity.git_output(
            metadata_dir, ["rev-parse", "--show-toplevel"]).decode('utf-8').strip()).resolve()
        blobs = _builder.index_identity.snapshot(
            metadata_root, [path.relative_to(metadata_root).as_posix() for path in paths])
        metadata_blobs = {
            path.relative_to(metadata_dir).as_posix():
                blobs[path.relative_to(metadata_root).as_posix()]
            for path in paths
        }
    if consumed is not None:
        if set(consumed) != {path.resolve() for path in paths}:
            raise ValueError("consumed claim metadata file set changed during enrichment")
        for path in paths:
            contents = consumed[path.resolve()]
            oid = metadata_blobs[path.relative_to(metadata_dir).as_posix()]
            if len(oid) not in {40, 64}:
                raise ValueError("unsupported Git object format for claim metadata")
            # Authenticate the bytes used by the join, not merely a clean file
            # that might have been restored after a transient metadata read.
            payload = b'blob ' + str(len(contents)).encode('ascii') + b'\0' + contents
            actual = hashlib.new('sha1' if len(oid) == 40 else 'sha256', payload).hexdigest()
            if actual != oid:
                raise ValueError(f"consumed claim metadata differs from committed Git blob: {path.name}")
    return metadata_blobs


def enrichment_provenance(root: Path, index_dir: Path, metadata_dir: Path, *,
                          consumed_metadata: dict[Path, bytes] | None = None) -> dict:
    return {
        "raw_index_sha256": sha256_file(index_dir / "declarations.jsonl"),
        "raw_summary_sha256": sha256_file(index_dir / "summary.json"),
        "enriched_sha256": sha256_file(index_dir / "enriched.jsonl"),
        "enricher_version": "git-blob-v3",
        "metadata_git_blob": metadata_bindings(metadata_dir, consumed_metadata),
    }


def enrichment_errors(root: Path, index_dir: Path, metadata_dir: Path) -> list[str]:
    try:
        recorded = json.loads((index_dir / "enriched-summary.json").read_text(encoding="utf-8"))
        current = enrichment_provenance(root, index_dir, metadata_dir)
        return [f"stale/missing enriched index provenance: {k}" for k, value in current.items()
                if recorded.get(k) != value]
    except (OSError, ValueError) as exc:
        return [f"cannot verify enriched index: {exc}"]


def review_snapshot_sha256(root: Path, index_dir: Path, modules: list[str]) -> str:
    """Conservative review context, independent of commit and build time.

    main validates the raw summary's complete tracked module list before calling
    build_all. Include source files without exported rows: generated/internal
    declarations can otherwise hide dependency paths. External declaration bodies
    remain outside this index; their pinned environment is represented by the lock
    and toolchain, not by a claim to inspect installed dependency contents.
    """
    required = ["lean-toolchain", "lake-manifest.json"]
    if any(not (root / name).is_file() for name in required):
        raise ValueError("missing committed review snapshot input")
    if not any((root / name).is_file() for name in ("lakefile.toml", "lakefile.lean")):
        raise ValueError("missing Lake configuration for review snapshot")
    payload = {
        "source_git_blob": _builder.source_bindings(root, modules),
        "inputs_git_blob": {path.as_posix(): _builder.index_identity.blob(root, path.as_posix()) for path in (
            Path("lean-toolchain"), Path("lake-manifest.json"), Path("lakefile.toml"),
            _builder.EXPORTER)},
        "lake_config_git_blob": {name: _builder.index_identity.blob(root, name)
                               for name in ("lakefile.toml", "lakefile.lean")},
        "declarations_sha256": sha256_file(index_dir / "declarations.jsonl"),
        "exporter_sha256": sha256_file(_builder.exporter_path(root)),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_all(root: Path, index_dir: Path | None = None, metadata_dir: Path | None = None, *,
              consumed_metadata: dict[Path, bytes] | None = None) -> list[dict]:
    index_dir = index_dir or root / ".qatlas" / "index"
    metadata_dir = metadata_dir or root / "metadata"
    records, summary = _builder.load_index(index_dir)
    snapshot = review_snapshot_sha256(root, index_dir, summary["modules"])
    try:
        commit: str | None = git_head(root)
    except (subprocess.CalledProcessError, OSError):
        commit = None
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    consumed = consumed_metadata if consumed_metadata is not None else {}
    enriched, _stats = enrich_records(records, root, metadata_dir / "papers", commit, built_at,
                                     review_snapshot_sha256=snapshot, consumed_metadata=consumed)
    metadata_bindings(metadata_dir, consumed)
    return enriched


def print_distribution(enriched: list[dict]) -> None:
    distribution = label_distribution(enriched)
    print("model_labels distribution (share with unknown in parentheses):")
    for key in MODEL_KEYS:
        bucket = distribution.get(key, {})
        total = sum(bucket.values())
        unknown = bucket.get("unknown", 0)
        share = f"{unknown / total:.1%}" if total else "n/a"
        detail = ", ".join(f"{value}={count}" for value, count in sorted(bucket.items(), key=lambda kv: -kv[1]))
        print(f"  {key}: {detail} (unknown {share})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="compare a fresh enrichment against the on-disk enriched.jsonl; "
                             "exit 1 if any declaration is stale (no rewrite)")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (tests)")
    parser.add_argument("--index-dir", type=Path, default=None,
                        help=f"index directory (default <root>/.qatlas/index)")
    parser.add_argument("--metadata-dir", type=Path, default=None,
                        help=f"metadata directory (default <root>/metadata)")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    index_dir = (args.index_dir or root / ".qatlas" / "index").resolve()
    metadata_dir = (args.metadata_dir or root / "metadata").resolve()

    try:
        _records, summary = _builder.load_index(index_dir)
        errors = _builder.provenance_errors(root, summary, index_dir / "declarations.jsonl")
    except (OSError, ValueError) as exc:
        errors = [str(exc)]
    if errors:
        print("cannot enrich a stale/unverified raw index: " + "; ".join(errors), file=sys.stderr)
        return 1
    try:
        consumed_metadata: dict[Path, bytes] = {}
        enriched = build_all(root, index_dir, metadata_dir, consumed_metadata=consumed_metadata)
    except (OSError, ValueError) as exc:
        print(f"cannot bind review snapshot: {exc}", file=sys.stderr)
        return 1
    paper_count = sum(1 for r in enriched if r["paper"] is not None)
    if args.check:
        disk_path = index_dir / "enriched.jsonl"
        if not disk_path.is_file():
            print(f"{disk_path} is missing; run `python3 -m qatlib_search.enrich_search_index` first",
                  file=sys.stderr)
            return 1
        disk = load_enriched(index_dir)
        report = staleness_report(enriched, disk)
        invalid = review_binding_report(load_review_bindings(index_dir), enriched, report["stale"])
        for fqn, reasons in list(report["stale"].items())[:40]:
            print(f"stale: {fqn} ({', '.join(reasons)})", file=sys.stderr)
        if len(report["stale"]) > 40:
            print(f"... and {len(report['stale']) - 40} more stale declarations", file=sys.stderr)
        for entry in invalid:
            print(f"invalid review binding: {entry['fqn']} reviewed_at={entry.get('reviewed_at')} "
                  f"({entry['reason']})", file=sys.stderr)
        if report["commit_changed"]:
            print(f"note: {report['commit_changed']} records have commit drift "
                  "(not staleness)", file=sys.stderr)
        provenance = enrichment_errors(root, index_dir, metadata_dir)
        try:
            metadata_bindings(metadata_dir, consumed_metadata)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            provenance.append(f"cannot verify consumed claim metadata: {error}")
        for error in provenance:
            print(error, file=sys.stderr)
        if report["stale"] or invalid or provenance:
            print(f"enriched index is stale: {len(report['stale'])} declarations, "
                  f"{len(invalid)} invalid review bindings; rerun without --check to refresh "
                  "enrichment; invalid reviews require renewed review",
                  file=sys.stderr)
            return 1
        print(f"enriched index up to date: {len(enriched)} declarations "
              f"({paper_count} in Papers)")
        return 0

    write_enriched(index_dir, enriched)
    (index_dir / "enriched-summary.json").write_text(json.dumps(
        enrichment_provenance(root, index_dir, metadata_dir, consumed_metadata=consumed_metadata), indent=2) + "\n", encoding="utf-8")
    identity_count = sum(1 for r in enriched if r["paper_identity"] is not None)
    linked = sum(1 for r in enriched if r["result_labels"])
    print(f"wrote {index_dir / 'enriched.jsonl'}: {len(enriched)} records, "
          f"{paper_count} in Papers, {identity_count} with paper identity, "
          f"{linked} carrying result labels, "
          f"{sum(1 for r in enriched if r['coverage'] is not None)} with coverage")
    print_distribution(enriched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
