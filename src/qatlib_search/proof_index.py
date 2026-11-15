"""On-demand source/doc index complementing the verified query_library index.

Literal and bounded regex search locate source; natural-language doc search is
local lexical ranking, not semantic/Lean applicability verification. No network,
embeddings or compilation. Module sources/documents and owned Markdown docs are
searched locally; explicit cross-repository scope also resolves local Qatlib sources.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import qatlib_env
from qatlib_search.paths import ROOT
from qatlib_search import index_identity

SCHEMA = "qatlas-proof-source-git-2"
ROOTS = index_identity.AREAS
SOURCE_DIRS = (*index_identity.MODULE_ROOTS, "docs")
SOURCE_SCOPES = {"source-and-docs", "qatlib-lean"}


def scope_path(relative: str, source_scope: str) -> bool:
    if source_scope not in SOURCE_SCOPES:
        raise ValueError("unsupported proof source scope")
    return (source_scope == "source-and-docs"
            or relative == "Qatlib.lean"
            or relative.startswith("Qatlib/") and relative.endswith(".lean"))


def admitted_path(relative: str) -> bool:
    path = Path(relative)
    return (relative == "README.md"
            or relative in {name + ".lean" for name in index_identity.MODULE_ROOTS}
            or any(relative.startswith(area + "/") for area in index_identity.MODULE_ROOTS)
            and path.suffix in {".lean", ".md"}
            or relative.startswith("docs/") and path.suffix == ".md")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_hash(data: bytes) -> str:
    """Hash the exact consumed bytes using Git's blob object representation."""
    result = hashlib.sha1(usedforsecurity=False)
    result.update(b"blob " + str(len(data)).encode("ascii") + b"\0")
    result.update(data)
    return result.hexdigest()


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def source_files(root: Path, source_scope: str = "source-and-docs") -> list[Path]:
    scope_path("", source_scope)
    found = []
    for area in (("Qatlib",) if source_scope == "qatlib-lean" else SOURCE_DIRS):
        directory = root / area
        if not directory.exists():
            continue
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f"source root invalid or symlinked: {area}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"symlink in index scope: {path.relative_to(root)}")
            if (path.is_file() and admitted_path(path.relative_to(root).as_posix())
                    and scope_path(path.relative_to(root).as_posix(), source_scope)):
                found.append(path)
    for name in ["README.md", *(name + ".lean" for name in index_identity.MODULE_ROOTS)]:
        path = root / name
        if path.is_symlink():
            raise ValueError("symlink in index scope: " + name)
        if path.is_file() and scope_path(name, source_scope):
            found.append(path)
    if not any(p.suffix == ".lean" for p in found):
        raise ValueError("no Lean source files in index scope")
    return sorted(found)


def git_head(root: Path) -> str | None:
    try:
        top = index_identity.git_output(root, ["rev-parse", "--show-toplevel"]).decode('utf-8', 'replace')
        if Path(top.strip()).resolve() != root.resolve():
            return None
        return index_identity.git_output(root, ["rev-parse", "HEAD"]).decode('utf-8', 'replace').strip()
    except (OSError, subprocess.SubprocessError):
        return None


def doc_comments(text: str) -> list[tuple[int, str]]:
    """Extract nested Lean doc comments, skipping ordinary comments and strings.

    This is a lexical location aid, not a declaration parser. It does not infer
    the declaration attached to a comment or names produced by Lean macros.
    """
    if "/--" not in text and "/-!" not in text:
        return []
    docs = []
    i, line = 0, 1
    while i < len(text):
        if text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
        elif text[i] == '"':
            i += 1
            while i < len(text):
                if text[i] == "\\":
                    if i + 1 < len(text):
                        line += text[i + 1] == "\n"
                    i += 2
                elif text[i] == '"':
                    i += 1
                    break
                else:
                    line += text[i] == "\n"
                    i += 1
        elif text.startswith("/-", i):
            start, start_line = i, line
            doc = text.startswith(("/--", "/-!"), i)
            depth, i = 1, i + 2
            while i < len(text) and depth:
                if text.startswith("/-", i):
                    depth += 1; i += 2
                elif text.startswith("-/", i):
                    depth -= 1; i += 2
                else:
                    line += text[i] == "\n"; i += 1
            if depth:
                raise ValueError(f"unterminated block comment at line {start_line}")
            if doc:
                docs.append((start_line, text[start + 3:i - 2]))
        else:
            line += text[i] == "\n"
            i += 1
    return docs


def fingerprint(root: Path, source_scope: str = "source-and-docs") -> dict[str, str]:
    paths = [p.relative_to(root).as_posix() for p in source_files(root, source_scope)]
    tracked = index_identity.git_output(root, ["ls-tree", "-rz", "--name-only", "HEAD"]).decode('utf-8').split("\0")
    paths = sorted(set(paths) | {p for p in tracked if admitted_path(p) and scope_path(p, source_scope)})
    result = index_identity.snapshot(root, paths)
    if set(result) != set(paths):
        raise ValueError("source index requires committed sources")
    return result


def build(root: Path = ROOT, *, source_scope: str = "source-and-docs") -> dict:
    root = root.resolve()
    head = git_head(root)
    bindings, records, warnings = fingerprint(root, source_scope), [], []
    for path in source_files(root, source_scope):
        raw = path.read_bytes()
        relative = path.relative_to(root).as_posix()
        if git_blob_hash(raw) != bindings.get(relative):
            raise ValueError("consumed source bytes differ from committed Git blob: " + relative)
        text = raw.decode("utf-8")
        if path.suffix == ".lean":
            records.append({"path": relative, "line": 1, "kind": "source", "text": text})
            try:
                docs = doc_comments(text)
            except ValueError as error:
                warnings.append(f"{relative}: {error}")
                docs = []
            for line, body in docs:
                records.append({"path": relative, "line": line, "kind": "doc", "text": body})
        else:
            records.append({"path": relative, "line": 1, "kind": "doc", "text": text})
    if bindings != fingerprint(root, source_scope) or head != git_head(root):
        raise ValueError("source changed during indexing; rebuild from a stable checkout")
    return {"schema": SCHEMA, "git_head": head, "source_git_blob": bindings,
            "source_scope": source_scope,
            "engine_version": "git-blob-source-index-v2",
            "records_sha256": digest(json_bytes(records)), "records": records,
            "warnings": warnings, "validation": "source-only; Lean not run"}


def save(index: dict, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        prior = json.loads(output.read_text(encoding="utf-8"))
        if not isinstance(prior, dict) or prior.get("schema") != SCHEMA:
            raise ValueError("refusing to replace a file that is not a proof source index")
    name = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".proof-index-", delete=False) as handle:
            name = Path(handle.name)
            handle.write(json_bytes(index) + b"\n")
            handle.flush(); os.fsync(handle.fileno())
        os.replace(name, output)
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def load(path: Path, root: Path = ROOT) -> dict:
    index = json.loads(path.read_text(encoding="utf-8"))
    return admit(index, root)


def admit(index: dict, root: Path | None = ROOT) -> dict:
    """Validate a local or release-supplied index against its actual Git sources."""
    if not isinstance(index, dict) or index.get("schema") != SCHEMA:
        raise ValueError("unsupported proof source index")
    if index.get("engine_version") != "git-blob-source-index-v2":
        raise ValueError("indexing engine changed; run build")
    source_scope = index.get("source_scope", "source-and-docs")
    scope_path("", source_scope)
    records = index.get("records")
    if not isinstance(records, list) or digest(json_bytes(records)) != index.get("records_sha256"):
        raise ValueError("index records are malformed or differ from their recorded hash")
    bindings = index.get("source_git_blob")
    if (not isinstance(bindings, dict) or not bindings
            or any(not isinstance(p, str) or not admitted_path(p) or not scope_path(p, source_scope) or ".." in Path(p).parts
                   or Path(p).is_absolute() or "\\" in p
                   or not isinstance(blob, str) or not re.fullmatch(r"[0-9a-f]{40}", blob)
                   for p, blob in bindings.items())):
        raise ValueError("invalid index source manifest")
    for record in records:
        if (not isinstance(record, dict) or not isinstance(record.get("path"), str)
                or record["path"] not in bindings or not isinstance(record.get("text"), str)
                or record.get("kind") not in {"source", "doc"}
                or type(record.get("line")) is not int or record["line"] < 1):
            raise ValueError("invalid index record")
    validate_record_sources(records, bindings)
    if root is not None:
        _validate_current(index, root)
    return index


def validate_record_sources(records: list[dict], bindings: dict[str, str]) -> None:
    """Bind the complete catalog and its derived doc snippets to Git blob bytes.

    The catalog's own SHA256 is a corruption check, not source authentication:
    an edited catalog can recompute it. Full source texts must independently
    reproduce the Git object IDs already checked against the actual checkout.
    """
    full = {}
    for record in records:
        relative = record["path"]
        expected_kind = "source" if relative.endswith(".lean") else "doc"
        if record["kind"] == expected_kind and record["line"] == 1:
            if relative in full:
                raise ValueError("duplicate full source record: " + relative)
            full[relative] = record
    if full.keys() != bindings.keys():
        raise ValueError("missing full source records for the committed source manifest")
    expected = []
    # Match source_files' Path ordering, including a leaf X.lean beside X/.
    for relative in sorted(bindings, key=Path):
        text = full[relative]["text"]
        if git_blob_hash(text.encode("utf-8")) != bindings[relative]:
            raise ValueError("index source text differs from committed Git blob: " + relative)
        lean = relative.endswith(".lean")
        expected.append({"path": relative, "line": 1,
                         "kind": "source" if lean else "doc", "text": text})
        if lean:
            try:
                docs = doc_comments(text)
            except ValueError:
                # build() records that lexical warning and emits no snippets.
                docs = []
            expected.extend({"path": relative, "line": line, "kind": "doc", "text": body}
                            for line, body in docs)
    if records != expected:
        raise ValueError("index document records differ from verified source derivation")


def _validate_current(index: dict, root: Path) -> None:
    """Check the admitted source snapshot, including after a bounded query."""
    if index.get("engine_version") != "git-blob-source-index-v2":
        raise ValueError("indexing engine changed; run build")
    try:
        changed = (fingerprint(root.resolve(), index.get("source_scope", "source-and-docs")) != index.get("source_git_blob")
                   or index.get("git_head") != git_head(root))
    except (ValueError, subprocess.SubprocessError) as error:
        raise ValueError("stale index: source changed or checkout unavailable; run build") from error
    if changed:
        raise ValueError("stale index: source set, contents or HEAD changed; run build")


def tokens(text: str) -> list[str]:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).casefold()
    words = re.findall(r"[a-z0-9]+", text)
    stop = {"the", "a", "an", "is", "of", "to", "and", "for", "in", "with"}
    result = [w for w in words if w not in stop]
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.extend([run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)])
    return result


def _regex_worker(payload: dict) -> list[dict]:
    pattern = re.compile(payload["query"], re.MULTILINE)
    hits = []
    for i, record in enumerate(payload["records"]):
        # finditer advances after empty matches as well; result and wall-clock
        # budgets still apply. The default remains one location per source file.
        for match in pattern.finditer(record["text"]):
            hits.append({"index": i, "offset": match.start()})
            if len(hits) >= payload["limit"]:
                return hits
            if not payload.get("all_matches", False):
                break
    return hits


def validate_query(query: str, mode: str, limit: int, regex_seconds: float) -> None:
    """Preflight all benchmark cases without launching a search worker."""
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise ValueError("query must contain 1–512 characters")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    if (type(regex_seconds) not in {int, float} or not math.isfinite(regex_seconds)
            or not 0 < regex_seconds <= 30):
        raise ValueError("regex budget must be finite and between 0 and 30 seconds")
    if not isinstance(mode, str) or mode not in {"literal", "regex", "doc"}:
        raise ValueError("mode must be literal, regex or doc")
    if mode == "regex":
        try:
            re.compile(query, re.MULTILINE)
        except re.error as error:
            raise ValueError(f"invalid regex: {error}") from error
    if mode == "doc" and not tokens(query):
        raise ValueError("doc query has no searchable terms")


def search(index: dict, query: str, mode: str = "literal", limit: int = 10,
           regex_seconds: float = 2.0, *, all_matches: bool = False) -> dict:
    """Locate files by default, or inspect bounded non-overlapping occurrences.

    A limit is a display budget, not a completeness claim. Probe one extra match
    so exact-limit results and genuinely truncated results remain distinguishable.
    """
    validate_query(query, mode, limit, regex_seconds)
    if type(all_matches) is not bool or all_matches and mode == "doc":
        raise ValueError("all_matches must be boolean and is only available for literal/regex")
    stop = limit + 1
    records = [r for r in index["records"] if r["kind"] == ("doc" if mode == "doc" else "source")
               or mode != "doc" and r["path"].endswith(".md")]
    matches = []
    if mode == "literal":
        for i, record in enumerate(records):
            pos = record["text"].find(query)
            while pos >= 0:
                matches.append({"index": i, "offset": pos})
                if len(matches) >= stop or not all_matches:
                    break
                pos = record["text"].find(query, pos + len(query))
            if len(matches) >= stop:
                break
    elif mode == "regex":
        payload = {"query": query, "records": records, "limit": stop,
                   "all_matches": all_matches}
        try:
            # Use the same installed/snapshotted package as the parent, even
            # when a thin stdlib script added it to sys.path (not PYTHONPATH).
            # Isolated mode avoids importing a package from the searched cwd.
            worker = ("import sys; sys.path[0:0] = sys.argv[1:3]; "
                      "from qatlib_search.proof_index import main; "
                      "raise SystemExit(main(['_regex']))")
            child = subprocess.run([sys.executable, "-I", "-B", "-c", worker,
                                    str(Path(__file__).resolve().parents[1]),
                                    str(Path(qatlib_env.__file__).resolve().parents[1])],
                                   input=json.dumps(payload), capture_output=True, text=True,
                                   encoding="utf-8", timeout=regex_seconds, check=False)
        except subprocess.TimeoutExpired as error:
            raise ValueError("regex search exceeded its budget; worker killed and reaped") from error
        if child.returncode:
            raise ValueError(f"regex worker failed: {child.stderr.strip()}")
        matches = json.loads(child.stdout)
    else:
        terms = set(tokens(query))
        if not terms:
            raise ValueError("doc query has no searchable terms")
        bags = [Counter(tokens(r["text"])) for r in records]
        df = Counter(t for bag in bags for t in bag)
        for i, bag in enumerate(bags):
            score = sum(math.log(1 + len(bags) / (1 + df[t])) * bag[t] / (bag[t] + 0.5 + sum(bag.values()) / 200)
                        for t in terms if bag[t])
            if score:
                matches.append({"index": i, "offset": 0, "score": score})
        matches.sort(key=lambda hit: (-hit["score"], records[hit["index"]]["path"], records[hit["index"]]["line"]))
        matches = matches[:stop]
    hits = []
    for match in matches[:limit]:
        record = records[match["index"]]
        offset = match["offset"]
        line = record["line"] + record["text"].count("\n", 0, offset)
        start = record["text"].rfind("\n", 0, offset) + 1
        excerpt = "\n".join(record["text"][start:].splitlines()[:6])[:1200]
        hits.append({"path": record["path"], "line": line, "kind": record["kind"],
                     "excerpt": excerpt, "score": match.get("score"), "status": "SOURCE_MATCH"})
        if record["kind"] == "source":
            hits[-1]["column"] = offset - start + 1  # Unicode characters, not UTF-8 bytes.
        # Portable discovery may enrich records without changing the persisted
        # release schema or legacy result shape. Its adapter resolves this opaque
        # ID back to declaration/source provenance, never to executable code.
        if "record_id" in record:
            hits[-1]["record_id"] = record["record_id"]
    return {"mode": mode, "query": query, "git_head": index.get("git_head"), "results": hits,
            "truncated": len(matches) > limit,
            "result_unit": ("doc-record" if mode == "doc" else
                            "source-occurrence" if all_matches else "source-file"),
            "warnings": index.get("warnings", []), "validation": "not-checked",
            "limitations": ["Source/doc locations are not elaborated declarations or applicability proofs.",
                            "Doc ranking is lexical; no translation, embeddings or external service.",
                            "Only admitted module sources/documents and Markdown docs are indexed; absence is not proof of nonexistence."]}


def _unique_json(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ValueError(f"non-JSON numeric constant: {value}")


def evaluate(index: dict, dataset: dict) -> dict:
    """Opt-in retrieval measurement bound to a dataset and an explicit corpus.

    Missing positive gold is an incompatible corpus, not a retrieval miss. An
    expected-empty query tests only this corpus. Neither case proves absence from
    the whole repository, Mathlib, or the literature. All cases are checked before
    the first query runs; worker errors abort evaluation rather than pass negatives.
    """
    if not isinstance(dataset, dict) or dataset.get("schema") != "proof-index-bench-1":
        raise ValueError("unsupported retrieval benchmark")
    items = dataset.get("items")
    if not isinstance(items, list) or not items:
        raise ValueError("benchmark must contain items")
    manifest = index.get("source_git_blob")
    if (not isinstance(manifest, dict) or not manifest
            or any(not isinstance(p, str) or not isinstance(h, str)
                   or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", h) for p, h in manifest.items())):
        raise ValueError("benchmark needs the indexed source manifest")
    corpus_sha = digest(json_bytes(manifest))
    if "corpus_sha256" in dataset:
        pin = dataset["corpus_sha256"]
        if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
            raise ValueError("corpus_sha256 must be a complete lowercase SHA256")
        if pin != corpus_sha:
            raise ValueError("benchmark corpus fingerprint differs from the loaded index")
    ids = set()
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or not item["id"].strip() or item["id"] in ids):
            raise ValueError("benchmark ids must be unique nonempty strings")
        ids.add(item["id"])
        gold = item.get("gold_paths")
        if (not isinstance(gold, list)
                or any(not isinstance(p, str) or not p.strip() for p in gold)):
            raise ValueError("benchmark gold_paths must be a string array")
        if len(set(gold)) != len(gold):
            raise ValueError("duplicate benchmark gold_paths")
        if "expect_empty" in item and type(item["expect_empty"]) is not bool:
            raise ValueError("expect_empty must be boolean")
        if bool(gold) == (item.get("expect_empty") is True):
            raise ValueError("provide positive gold OR explicit expect_empty, never both")
        absent = sorted(set(gold) - manifest.keys())
        if absent:
            raise ValueError(f"{item['id']}: gold not in the indexed corpus: {absent}")
        validate_query(item.get("query"), item.get("mode"), item.get("limit", 10), 2.0)
    results, reciprocals, file_reciprocals = [], [], []
    by_mode = {}
    for item in items:
        gold = item["gold_paths"]
        limit = item.get("limit", 10)
        answer = search(index, item["query"], item["mode"], limit)
        hits = answer["results"]
        paths = list(dict.fromkeys(hit["path"] for hit in hits))
        rank = next((i for i, hit in enumerate(hits, 1) if hit["path"] in gold), None)
        file_rank = next((i for i, path in enumerate(paths, 1) if path in gold), None)
        passed = rank is not None if gold else not hits
        if gold:
            reciprocals.append(1 / rank if rank else 0)
            file_reciprocals.append(1 / file_rank if file_rank else 0)
        mode = by_mode.setdefault(item["mode"], {"total": 0, "passed": 0})
        mode["total"] += 1
        mode["passed"] += int(passed)
        results.append({"id": item["id"], "mode": item["mode"], "query": item["query"],
                        "limit": limit, "gold_paths": gold, "passed": passed,
                        "rank": rank, "file_rank": file_rank,
                        "retrieved": [{"path": hit["path"], "line": hit["line"]} for hit in hits]})
    negatives = [r for r in results if not r["gold_paths"]]
    return {"cases": results, "passed": sum(r["passed"] for r in results), "total": len(results),
            "positive_queries": len(reciprocals), "positive_hits": sum(x > 0 for x in reciprocals),
            "negative_queries": len(negatives), "negative_correct": sum(r["passed"] for r in negatives),
            "mrr": sum(reciprocals) / len(reciprocals) if reciprocals else None,
            "file_mrr": sum(file_reciprocals) / len(file_reciprocals) if file_reciprocals else None,
            "ranking_unit": "mrr: displayed record; file_mrr: first occurrence of each returned path",
            "by_mode": by_mode, "indexed_files": len(manifest),
            "dataset_sha256": digest(json_bytes(dataset)),
            "corpus": {"git_head": index.get("git_head"), "source_manifest_sha256": corpus_sha,
                       "engine_version": index.get("engine_version"),
                       "records_sha256": index.get("records_sha256"),
                       "files_by_root": {area: sum(p.startswith(area + "/") for p in manifest)
                                         for area in ROOTS},
                       "warnings": index.get("warnings", []), "completeness": "not-established"},
            "validation": "retrieval-only; no applicability or completeness claim"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["_regex"]:
        print(json.dumps(_regex_worker(json.load(sys.stdin))))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--index", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    bench = sub.add_parser("eval", help="run an opt-in retrieval benchmark, never a build gate")
    bench.add_argument("dataset", type=Path)
    find = sub.add_parser("search")
    find.add_argument("query")
    find.add_argument("--mode", choices=("literal", "regex", "doc"), default="literal")
    find.add_argument("--limit", type=int, default=10)
    find.add_argument("--regex-seconds", type=float, default=2.0)
    find.add_argument("--all-matches", action="store_true",
                      help="inspect non-overlapping literal/regex occurrences, not just one per file")
    find.add_argument("--scope", choices=("local", "all", "qatlib", "frozen", "dev-docs"), default="local",
                      help="all searches local Qatlib plus current sources/docs; frozen is a deprecated qatlib alias")
    find.add_argument("--public-repo", type=Path,
                      help="select an explicit local Qatlib checkout instead of the locked dependency")
    find.add_argument("--offline", action="store_true",
                      help="compatibility option; Qatlib source search always works locally")
    args = parser.parse_args(argv)
    output = args.index or args.root / ".qatlas/proof-source-index.json"
    try:
        if args.command == "build":
            resolved_output = output.resolve()
            if (resolved_output.is_relative_to(args.root.resolve())
                    and admitted_path(resolved_output.relative_to(args.root.resolve()).as_posix())):
                raise ValueError("index output cannot be a source file in index scope")
            value = build(args.root)
            save(value, output)
            result = {"index": str(output), "files": len(value["source_git_blob"]),
                      "records": len(value["records"]), "warnings": value["warnings"],
                      "validation": value["validation"]}
        elif args.command == "eval":
            raw = args.dataset.read_bytes()
            dataset = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json,
                                 parse_constant=_invalid_constant)
            admitted = load(output, args.root)
            result = evaluate(admitted, dataset)
            if args.dataset.read_bytes() != raw:
                raise ValueError("benchmark changed during evaluation; rerun with stable inputs")
            result["dataset_file_sha256"] = digest(raw)
        else:
            if args.scope != "local":
                if args.index is not None:
                    raise ValueError("--index selects a single local corpus; cross-repository scope manages separate versioned catalogs")
                from . import cross_proof_index
                result = cross_proof_index.search(args.root, args.query, mode=args.mode,
                    limit=args.limit, regex_seconds=args.regex_seconds, all_matches=args.all_matches,
                    scope=args.scope, offline=args.offline, public_repo=args.public_repo)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 2 if result["incomplete"] else 0
            admitted = load(output, args.root)
            result = search(admitted, args.query, args.mode, args.limit, args.regex_seconds,
                            all_matches=args.all_matches)
        if args.command != "build":
            _validate_current(admitted, args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, re.error, subprocess.SubprocessError) as error:
        print(f"proof_index: {error}", file=sys.stderr)
        return 2
    return 1 if args.command == "eval" and result["passed"] != result["total"] else 0


if __name__ == "__main__":
    # GBK consoles crash printing Lean/UTF-8 output; never fail on encoding.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
