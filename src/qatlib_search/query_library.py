#!/usr/bin/env python3
"""Local hybrid retrieval CLI over the enriched declaration index (B06). No network.

`search` answers five query kinds — `--text` (Chinese/English/symbols), `--paper-id`
(arXiv id, DOI, paper directory, or paper alias), `--theorem-id` (papers-metadata
result_label), `--fqn` — and `inspect --fqn FQN` dumps one full enriched record.

Retrieval strategy: unambiguous citation neighborhood first, then the whole library. A query that
resolves to a paper (through aliases / arXiv id / paper dir / result_label) returns that
paper's domain entries first, marked `neighborhood: citation`, followed by a global
lexical pass over every declaration, marked `neighborhood: global`. Lexical scoring is
idf-weighted token counting (no BM25 machinery beyond weighting) over fqn / short name /
lean_type / expanded_prop / aliases, plus the type skeleton tokens of lean_type; ties
break deterministically. Chinese queries are bridged through the built-in CJK_TERM_TABLE
below — a terminology bridge (e.g. 连通性 → walk/mixing/spectral vocabulary), NOT a
synonym rewrite; hits stay RELATED_ONLY. No embedding or external service is involved.

`--json` output strictly follows the package-bundled tools/retrieval-report.schema.json.
The search stage performs no applicability check (that is B07), so `outcome` is only
RELATED_ONLY or NO_MATCH and every result carries `status: "RELATED_ONLY"` (the schema's
status enum has no retrieval-stage values); the retrieval-stage markers
"lexical_match"/"citation_neighborhood" live in `results[].scope.scope_note`
("neighborhood: …; stage: …") and in the human-readable output. Fields the schema does
not allow to be null get honest empty values (arrays stay [], `validation` and
`semantic_review` get explicit not-checked objects). `snapshot` comes from the index
summary plus real `git rev-parse HEAD` / `git status --porcelain`.
Per-source metadata coverage and notes are retained in `scope.scope_note` and the
human summary, attributed by paper/version/result label. Link-level notes use
`result_note`, separately from each covered declaration's `note`; these are source claims,
not applicability checks. Unmapped coverage such as `partial_support` keeps the
conservative `unknown` relation rather than being promoted to a different category.
Theorem selectors choose exact labels before parenthetical-core matches across
covered and open entries alike. Open targets in other papers remain obligations
even when a matching covered declaration is returned. Open-target source notes
remain attributed in search_scope.limitations.

Failure diagnostics: a missing index exits 2 with a clear stderr message; an unresolved
paper yields NO_MATCH with the searched alias tables listed in `search_scope.limitations`.

Stdlib only. Run from the repository root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search.paths import ROOT
from qatlib_search.lean_doc_source import parse_module


DEFAULT_INDEX = ROOT / ".qatlas" / "index"
DEFAULT_METADATA = ROOT / "metadata"
DEFAULT_LIMIT = 20
ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}
STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "for", "to", "is", "with", "via",
             "using", "based", "from"}
SYMBOL_CHARS = set("√∀∃→↔¬∧∨ℂℕℝℤℚπθφχψΩΣ")
CJK_RUN_RE = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]+")

# Curated cross-lingual terminology bridge for CJK queries (longest match wins). These are
# vocabulary pointers into the library's English/Lean identifier space, not synonyms.
CJK_TERM_TABLE = {
    "振幅放大": ["amplitude", "amplification"],
    "幅度放大": ["amplitude", "amplification"],
    "振幅估计": ["amplitude", "estimation"],
    "二次加速": ["quadratic", "speedup"],
    "量子行走": ["quantum", "walk"],
    "量子游走": ["quantum", "walk"],
    "马尔可夫": ["markov"],
    "相位估计": ["phase", "estimation"],
    "线性系统": ["linear", "system"],
    "线性组合": ["linear", "combination"],
    "蒙特卡洛": ["monte", "carlo"],
    "奇异值": ["singular", "value"],
    "连通性": ["connectivity", "irreducible", "mixing", "spectral"],
    "行走": ["walk"],
    "搜索": ["search"],
    "计数": ["counting"],
    "查询": ["query", "oracle"],
    "测量": ["measurement"],
    "影子": ["shadow"],
    "哈密顿": ["hamiltonian"],
    "模拟": ["simulation"],
    "估计": ["estimation"],
    "放大": ["amplification"],
    "噪声": ["noise", "channel"],
    "信道": ["channel"],
    "混合态": ["mixed", "state"],
    "纯态": ["pure", "state"],
    "概率": ["probability"],
    "加速": ["speedup"],
    "下界": ["lower", "bound"],
    "自適應": ["adaptive"],
    "自适应": ["adaptive"],
    "反射": ["reflection"],
    "相位": ["phase"],
    "量子": ["quantum"],
    "图": ["graph", "walk", "markov"],
    "酉": ["unitary"],
    "矩阵": ["matrix"],
    "期望": ["expectation", "mean"],
}

FIELD_WEIGHTS = (("short_name", 6.0), ("aliases", 4.0), ("fqn", 3.0),
                 ("module", 2.0), ("skeleton", 1.5), ("lean_type", 1.0),
                 ("expanded_prop", 0.5))
KIND_ORDER = {"theorem": 0, "lemma": 1, "def": 2, "abbrev": 3, "instance": 4,
              "structure": 5, "inductive": 6, "ctor": 7, "rec": 8}
COVERAGE_TO_RELATION = {
    "equivalent": "equivalent_reformulation",
    "generalization": "generalization",
    "restricted": "restricted",
    "corrected": "corrected",
    "as_in_paper": "as_in_paper",
    "supporting_lemma": "supporting_lemma",
}


def run_git(args: list[str], cwd: Path) -> str | None:
    from qatlib_search.index_identity import git_output
    try:
        return git_output(cwd, args).decode('utf-8', 'replace').strip()
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- tokenization

def split_identifier(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    text = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", text)
    return text.replace("_", " ").replace(".", " ")


def tokenize(text: str) -> list[str]:
    """ASCII word tokens (lowercased, camelCase/snake_case split) + symbol characters."""
    if not text:
        return []
    tokens = [ch for ch in text if ch in SYMBOL_CHARS]
    for word in re.findall(r"[A-Za-z0-9]+", split_identifier(text)):
        token = word.lower()
        if token not in STOPWORDS and len(token) > 1 or token.isdigit():
            tokens.append(token)
    return tokens


def cjk_bridge(text: str) -> tuple[list[str], list[str]]:
    """Map CJK runs to English vocabulary through CJK_TERM_TABLE (longest match first).

    Returns (english_tokens, untranslated_cjk_runs)."""
    tokens: list[str] = []
    remaining = text
    for key in sorted(CJK_TERM_TABLE, key=len, reverse=True):
        while key in remaining:
            tokens.extend(CJK_TERM_TABLE[key])
            remaining = remaining.replace(key, " ", 1)
    untranslated = [run for run in CJK_RUN_RE.findall(remaining) if run.strip()]
    return tokens, untranslated


def query_tokens(text: str) -> tuple[list[str], list[str]]:
    bridged, untranslated = cjk_bridge(text)
    return tokenize(text) + tokenize(" ".join(bridged)), untranslated


def normalize_phrase(text: str) -> str:
    return re.sub(r"[\s_]+", " ", text.strip().lower())


# --------------------------------------------------------------------------- index

class LibraryIndex:
    def __init__(self, index_dir: Path, metadata_dir: Path | None = None):
        self.index_dir = index_dir
        self.metadata_dir = metadata_dir
        enriched_path = index_dir / "enriched.jsonl"
        summary_path = index_dir / "summary.json"
        if not enriched_path.is_file() or not summary_path.is_file():
            raise FileNotFoundError(
                f"index not found under {index_dir}: need enriched.jsonl and summary.json "
                f"(run qatlib_search.build_search_index then enrich_search_index)")
        # Bind validation to the bytes actually loaded, not a replacement index
        # that happens to be current by the time admission/output is checked.
        enriched_bytes = enriched_path.read_bytes()
        summary_bytes = summary_path.read_bytes()
        provenance_path = index_dir / "enriched-summary.json"
        self._loaded_index_bytes = {
            "enriched.jsonl": enriched_bytes,
            "summary.json": summary_bytes,
            "enriched-summary.json": (provenance_path.read_bytes()
                                      if provenance_path.is_file() else None),
        }
        self.records = [json.loads(line) for line in enriched_bytes.decode("utf-8").splitlines()
                        if line.strip()]
        self.records.sort(key=lambda r: r.get("fqn", ""))
        self.summary = json.loads(summary_bytes)
        self.by_fqn = {r["fqn"]: r for r in self.records}
        self._build_search()
        self._load_link_metadata()

    def validate_current(self, root: Path) -> None:
        from qatlib_search.build_search_index import provenance_errors
        from qatlib_search.enrich_search_index import enrichment_errors
        errors = []
        for name, loaded in self._loaded_index_bytes.items():
            path = self.index_dir / name
            try:
                current = path.read_bytes() if path.is_file() else None
            except OSError as error:
                errors.append(f"cannot verify loaded index {name}: {error}")
                continue
            if current != loaded:
                errors.append(f"loaded index changed: {name}")
        papers_dir = self.metadata_dir / "papers" if self.metadata_dir else None
        current_metadata = set(papers_dir.glob("*.json")) if papers_dir and papers_dir.is_dir() else set()
        if current_metadata != set(self._loaded_metadata_bytes):
            errors.append("loaded claim metadata file set changed")
        for path, loaded in self._loaded_metadata_bytes.items():
            try:
                current = path.read_bytes()
            except OSError as error:
                errors.append(f"cannot verify loaded claim metadata {path.name}: {error}")
                continue
            if current != loaded:
                errors.append(f"loaded claim metadata changed: {path.name}")
        errors += provenance_errors(root, self.summary, self.index_dir / "declarations.jsonl")
        errors += enrichment_errors(root, self.index_dir, self.metadata_dir or root / "metadata")
        if errors:
            raise ValueError("index is stale or unverified; rebuild/export/enrich from the current clean checkout: "
                             + "; ".join(errors))

    # ---- lexical machinery -------------------------------------------------
    def _build_search(self) -> None:
        self.field_tokens: dict[str, dict[str, list[str]]] = {}
        df: dict[str, int] = {}
        for record in self.records:
            fqn = record.get("fqn", "")
            short = fqn.rsplit(".", 1)[-1]
            identity = record.get("paper_identity") or {}
            lean_type = record.get("lean_type") or ""
            fields = {
                "short_name": short,
                "aliases": " ".join(record.get("aliases") or []),
                "fqn": fqn,
                "module": record.get("module", ""),
                "skeleton": " ".join(tokenize(lean_type))
                            + (" →" if "→" in lean_type else "")
                            + (" prop" if "Prop" in lean_type else ""),
                "lean_type": lean_type,
                "expanded_prop": record.get("expanded_prop") or "",
                "paper": " ".join(filter(None, [record.get("paper") or "",
                                                identity.get("venue") or ""])),
            }
            self.field_tokens[fqn] = {field: tokenize(value) for field, value in fields.items()}
            for token in {t for tokens in self.field_tokens[fqn].values() for t in tokens}:
                df[token] = df.get(token, 0) + 1
        total = max(len(self.records), 1)
        self.idf = {token: math.log(1 + (total - count + 0.5) / (count + 0.5))
                    for token, count in df.items()}

    def score(self, fqn: str, tokens: list[str]) -> float:
        field_tokens = self.field_tokens[fqn]
        score = 0.0
        for token in tokens:
            idf = self.idf.get(token)
            if not idf:
                continue
            weighted = 0.0
            for field, weight in FIELD_WEIGHTS:
                tf = field_tokens[field].count(token)
                if tf:
                    weighted += weight * (1 + math.log(tf))
            score += idf * weighted
        return score

    def phrase_bonus(self, record: dict, phrase: str) -> bool:
        needle = normalize_phrase(phrase)
        if not needle:
            return False
        identity = record.get("paper_identity") or {}
        haystacks = [record.get("fqn", ""), " ".join(record.get("aliases") or []),
                     record.get("module", ""), identity.get("venue") or ""]
        return any(needle in normalize_phrase(h) for h in haystacks if h)

    def lexical_rank(self, tokens: list[str], records: list[dict] | None = None,
                     phrase: str = "") -> list[tuple[float, dict]]:
        pool = self.records if records is None else records
        ranked = []
        for record in pool:
            score = self.score(record["fqn"], tokens)
            if phrase and self.phrase_bonus(record, phrase):
                score += 10.0
            if score > 0:
                ranked.append((score, record))
        ranked.sort(key=lambda pair: (-pair[0], not pair[1].get("is_prop_definition", False),
                                      KIND_ORDER.get(pair[1].get("kind", ""), 9), pair[1]["fqn"]))
        return ranked

    # ---- metadata ----------------------------------------------------------
    def _load_link_metadata(self) -> None:
        self._loaded_metadata_bytes: dict[Path, bytes] = {}
        self.paper_metadata: dict[str, dict] = {}
        self.label_pairs: dict[str, list[tuple[str, str | None]]] = {}
        self.linked_papers: dict[str, set[str]] = {}
        self.source_refs: dict[str, set[tuple[str, str | None, str, str | None]]] = {}
        self.source_coverage: dict[str, set[str]] = {}
        self.uncovered_labels: dict[str, list[dict]] = {}
        papers_dir = self.metadata_dir / "papers" if self.metadata_dir else None
        if not papers_dir or not papers_dir.is_dir():
            return
        for path in sorted(papers_dir.glob("*.json")):
            contents = path.read_bytes()
            self._loaded_metadata_bytes[path] = contents
            data = json.loads(contents)
            paper = data["id"]
            self.paper_metadata[paper] = data
            identity = data.get("identity") or {}
            paper_id = identity.get("arxiv_id") or identity.get("qatlas_paper_id") or paper
            for link in data.get("claims", []):
                if link.get("status", "active") != "active":
                    continue
                label = link.get("label")
                if not isinstance(label, str):
                    continue
                # These describe the source claim and its functional scope. They
                # do not certify that this particular declaration proves it.
                claim_context = {
                    target: link[source] for source, target in (
                        ("id", "claim_id"), ("content", "content"),
                        ("claim-rank", "claim_rank"), ("binding_note", "binding_note"),
                        ("binds", "binds")) if source in link
                }
                if not link.get("covered_by"):
                    self.uncovered_labels.setdefault(label, []).append({
                        "paper": paper, "label": label, "paper_id": paper_id,
                        "version": identity.get("arxiv_version_used"), **claim_context,
                    })
                for covered in link.get("covered_by", []):
                    fqn = covered.get("fqn")
                    if isinstance(fqn, str):
                        self.label_pairs.setdefault(fqn, []).append((label, covered.get("coverage")))
                        self.linked_papers.setdefault(fqn, set()).add(paper)
                        self.source_refs.setdefault(fqn, set()).add((
                            paper_id, identity.get("arxiv_version_used"), label,
                            covered.get("coverage")))
                        # Preserve each source's exact coverage and applicability note.
                        # In particular partial_support has no equivalent relation enum;
                        # retaining the metadata avoids silently losing its restrictions.
                        self.source_coverage.setdefault(fqn, set()).add(json.dumps({
                            "paper": paper, "paper_id": paper_id,
                            "version": identity.get("arxiv_version_used"),
                            "result_label": label, "coverage": covered.get("coverage"),
                            "note": covered.get("note"),
                            **claim_context,
                        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    # ---- paper resolution ----------------------------------------------------
    def paper_domain(self, paper: str) -> list[dict]:
        # Source membership survives promotion to Foundations/Concepts. Only return
        # declarations actually present in the verified index; metadata links
        # do not establish importability or applicability on their own.
        return [r for r in self.records if r.get("paper") == paper
                or paper in self.linked_papers.get(r["fqn"], set())]

    def paper_facts(self) -> dict[str, dict]:
        from qatlib_search.enrich_search_index import build_aliases

        facts: dict[str, dict] = {}
        for record in self.records:
            paper = record.get("paper")
            if not paper:
                continue
            fact = facts.setdefault(paper, {"dir": paper, "identity": {}, "aliases": set()})
            if record.get("paper_identity"):
                fact["identity"] = record["paper_identity"]
            fact["aliases"].update(record.get("aliases") or [])
        # Paper identity is independent of where its declarations live. A paper
        # with only Foundations/Concepts results has no record carrying `paper_identity`.
        # Metadata resolves the identity; paper_domain still admits index rows only.
        for paper, metadata in self.paper_metadata.items():
            fact = facts.setdefault(paper, {"dir": paper, "identity": {}, "aliases": set()})
            identity = metadata.get("identity") or {}
            fact["identity"] = {**identity, **fact["identity"]}
            labels = [claim["label"] for claim in metadata.get("claims", [])
                      if claim.get("status", "active") == "active" and isinstance(claim.get("label"), str)]
            fact["aliases"].update(build_aliases("", labels, identity))
        return facts

    def resolve_paper(self, paper_id: str) -> tuple[str | None, list[str]]:
        """Resolve --paper-id against dir names, arXiv ids, DOIs, titles and aliases.

        Returns (paper_dir_or_None, tables_searched)."""
        tables = ["paper directories", "QAtlas paper ids (qa_…)",
                  "arXiv ids (arxiv:…, quant-ph/…, bare numbers)",
                  "DOI substrings of paper_identity.venue", "paper titles",
                  "paper aliases (initials, initials+year, initials+label)"]
        raw = paper_id.strip()
        if not raw or not any(char.isalnum() for char in raw):
            return None, tables
        facts = self.paper_facts()
        lowered = raw.lower()
        arxiv_norm = re.sub(r"^arxiv:\s*", "", lowered, flags=re.IGNORECASE)
        arxiv_norm = re.sub(r"v\d+$", "", arxiv_norm).strip()
        # Resolve across the whole table before choosing: an earlier paper's
        # author/label alias must not shadow another paper's exact identity.
        matches: dict[int, set[str]] = {0: set(), 1: set(), 2: set()}
        for dir_name, fact in facts.items():
            identity = fact["identity"]
            if raw == dir_name or lowered == dir_name.lower():
                matches[0].add(dir_name)
            qa_id = (identity.get("qatlas_paper_id") or "").lower()
            if qa_id and lowered == qa_id:
                matches[0].add(dir_name)
            paper_arxiv = (identity.get("arxiv_id") or "").lower()
            if paper_arxiv and (arxiv_norm == paper_arxiv
                                or paper_arxiv.endswith("/" + arxiv_norm)
                                or arxiv_norm == paper_arxiv.split("/")[-1]):
                matches[0].add(dir_name)
            venue = (identity.get("venue") or "").lower()
            title = normalize_phrase(identity.get("title") or "")
            if title and normalize_phrase(raw) == title:
                matches[1].add(dir_name)
            if raw.lower().startswith("10.") and raw.lower() in venue:
                matches[1].add(dir_name)
            if normalize_phrase(raw) and normalize_phrase(raw) in normalize_phrase(venue):
                matches[2].add(dir_name)
            if raw in fact["aliases"] or lowered in {a.lower() for a in fact["aliases"]}:
                matches[2].add(dir_name)
        for candidates in matches.values():
            if len(candidates) == 1:
                return next(iter(candidates)), tables
            if candidates:
                return None, tables + ["ambiguous paper selector: " + ", ".join(sorted(candidates))]
        return None, tables

    def resolve_theorem(self, label: str) -> tuple[list[dict], list[dict]]:
        """Records covered by this result_label (exact, then label-core match), plus every
        papers-metadata entry whose label matches but has an empty covered_by list."""
        target = label.strip()
        core = re.sub(r"\s*\([^)]*\)\s*$", "", target).strip().lower()

        def _core(value: str) -> str:
            return re.sub(r"\s*\([^)]*\)\s*$", "", value).strip().lower()

        # Choose the label tier across both covered and open metadata entries.
        # An exact open target must not be replaced by another label's core match.
        labels = set(self.uncovered_labels)
        labels.update(label for pairs in self.label_pairs.values() for label, _ in pairs)
        labels.update(label for r in self.records for label in r.get("result_labels") or [])
        selected = {target} if target in labels else {lbl for lbl in labels if core and _core(lbl) == core}
        matched = [r for r in self.records if selected.intersection(r.get("result_labels") or [])]
        uncovered = [info for lbl, infos in self.uncovered_labels.items()
                     if lbl in selected for info in infos]
        return matched, uncovered


# --------------------------------------------------------------------------- report pieces

def upstream_snapshots(metadata_dir: Path | None) -> list[dict]:
    if not metadata_dir:
        return []
    path = metadata_dir / "upstream-relations.json"
    if not path.is_file():
        return []
    try:
        locked = json.loads(path.read_text(encoding="utf-8")).get("locked", {})
    except (OSError, json.JSONDecodeError):
        return []
    snapshots = []
    for name, info in sorted(locked.items()):
        commit = re.search(r"\b([0-9a-f]{40})\b", info)
        toolchain = re.search(r"(leanprover/lean4:v[0-9.]+)", info)
        snapshots.append({"repository": name,
                          "commit": commit.group(1) if commit else "",
                          "toolchain": toolchain.group(1) if toolchain else ""})
    return snapshots


def snapshot_block(index: LibraryIndex) -> dict:
    index_dir = index.index_dir.resolve()
    head = run_git(["rev-parse", "HEAD"], cwd=index_dir)
    dirty = run_git(["status", "--porcelain"], cwd=index_dir)
    remote = run_git(["remote", "get-url", "origin"], cwd=index_dir)
    lockfile = index.summary.get("lake_manifest_git_blob")
    if not lockfile and head:
        # A lexical fixture or exported index can live outside a Git checkout.
        # Only attach a lock blob from the actual repository containing it;
        # genuine dirty-source failures from blob() still propagate.
        top = run_git(["rev-parse", "--show-toplevel"], cwd=index_dir)
        manifest = Path(top) / "lake-manifest.json" if top else None
        if manifest and manifest.is_file():
            from . import index_identity
            lockfile = index_identity.blob(manifest.parent, "lake-manifest.json")
    repository = remote or (index_dir.parents[1].name if len(index_dir.parents) > 1 else "unknown")
    return {
        "repository": repository,
        "commit": head or index.summary.get("git_head") or "0" * 40,
        "dirty": bool(dirty),
        "lean_toolchain": index.summary.get("lean_toolchain") or "unknown",
        "lockfile_git_blob": lockfile,
        "import_modules": index.summary.get("modules")
                          or sorted({r.get("module", "") for r in index.records}),
    }


def best_effort_source(root: Path, record: dict) -> dict:
    module = record.get("module", "")
    path = root / (module.replace(".", "/") + ".lean")
    # Reuse the bounded source catalogue, including namespace and private-name rules.
    # An unsupported/generated declaration has no reliable source line in this index:
    # point to the module start rather than a same-spelled declaration or a comment.
    line = 1
    try:
        matches = [decl.line for decl in parse_module(path, root=root).decls
                   if decl.fqn == record["fqn"]]
        if len(matches) == 1:
            line = matches[0]
    except (OSError, ValueError):
        pass
    return {"path": str(path), "start_line": line, "end_line": line}


def relation_for(coverage: str | None) -> str:
    return COVERAGE_TO_RELATION.get(coverage or "", "unknown")


def result_entry(index: LibraryIndex, root: Path, record: dict, stage: str,
                 rank: int, score: float) -> dict:
    identity = record.get("paper_identity") or {}
    paper_refs = []
    source_refs = index.source_refs.get(record["fqn"])
    if source_refs:
        for paper_id, version, label, coverage in sorted(
                source_refs, key=lambda ref: tuple(value or "" for value in ref)):
            paper_refs.append({"paper_id": paper_id, "version": version,
                               "locator": label, "relation": relation_for(coverage)})
    elif record.get("paper"):
        paper_id = identity.get("arxiv_id") or record["paper"]
        paper_refs.append({"paper_id": paper_id,
                           "version": identity.get("arxiv_version_used"),
                           "locator": "", "relation": "supporting_lemma"})
    labels = record.get("model_labels") or {}
    scope_note = (f"neighborhood: {'citation' if stage == 'citation_neighborhood' else 'global'}; "
                  f"stage: {stage}; score: {score:.3f}; rank: {rank}")
    coverage = sorted(index.source_coverage.get(record["fqn"], set()))
    if coverage:
        # JSON keeps source notes distinct and escapes embedded newlines. These are
        # attributed metadata, not newly established applicability or review evidence.
        scope_note += "\nmetadata coverage: [" + ",".join(coverage) + "]"
    return {
        "status": "RELATED_ONLY",
        "fqn": record["fqn"],
        "module": record.get("module", ""),
        "elaborated_type": record.get("lean_type") or "Prop",
        "unfolded_contract": record.get("expanded_prop"),
        "source": best_effort_source(root, record),
        "paper_refs": paper_refs,
        "scope": {
            "model": json.dumps(labels, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "error_metric": "unknown (no applicability check; B07 pending)",
            "cost_model": "|".join(labels.get("cost_unit") or ["unknown"]),
            "scope_note": scope_note,
        },
        "missing_premises": [],
        "bridges": [],
        "remaining_goals": [],
        "application_snippet": None,
        "validation": {
            "elaborated": True,
            "application_closed": False,
            "exit_code": None,
            "axiom_check_passed": set(record.get("axioms") or []) <= ALLOWED_AXIOMS,
            "log_path": None,
        },
        "semantic_review": {"status": "not_reviewed", "reviewer": None, "evidence_path": None},
    }


def assemble_report(index: LibraryIndex, root: Path, query: dict,
                    entries: list[tuple[dict, str, float]],
                    limitations: list[str], obligations: list[str]) -> dict:
    results = [result_entry(index, root, record, stage, rank + 1, score)
               for rank, (record, stage, score) in enumerate(entries)]
    return {
        "schema_version": "1.0",
        "outcome": "RELATED_ONLY" if results else "NO_MATCH",
        "snapshot": snapshot_block(index),
        "query": query,
        "search_scope": {
            "searched_modules": index.summary.get("modules")
                               or sorted({r.get("module", "") for r in index.records}),
            "excluded_declarations": [],
            "upstream_snapshots": upstream_snapshots(index.metadata_dir),
            "limitations": limitations,
        },
        "results": results,
        "next_obligations": obligations or ["run the B07 applicability check before any "
                                            "EXACT/VIA_BRIDGE/CONDITIONAL claim"],
    }


BASE_LIMITATIONS = [
    "lexical retrieval only; no applicability check (B07 pending): outcome can only be RELATED_ONLY or NO_MATCH",
    "CJK query terms are bridged through a built-in terminology table (no external embedding service)",
    "source start_line/end_line use a limited explicit-declaration parser; unsupported/generated "
    "or ambiguous declarations fall back to module line 1 (the index has no line data)",
]


# --------------------------------------------------------------------------- searches

def _merge_stages(entries: list[tuple[dict, str, float]],
                  ranked: list[tuple[float, dict]], stage: str) -> list[tuple[dict, str, float]]:
    seen = {record["fqn"] for record, _stage, _score in entries}
    for score, record in ranked:
        if record["fqn"] not in seen:
            entries.append((record, stage, score))
            seen.add(record["fqn"])
    return entries


def _paper_neighborhood(index: LibraryIndex, paper: str, tokens: list[str],
                        phrase: str, first: list[dict] | None = None) -> list[tuple[dict, str, float]]:
    domain = index.paper_domain(paper)
    ranked_list = index.lexical_rank(tokens, domain, phrase)
    ranked = {record["fqn"]: (score, record) for score, record in ranked_list}
    entries: list[tuple[dict, str, float]] = []
    for record in first or []:
        score = ranked[record["fqn"]][0] if record["fqn"] in ranked else 0.0
        entries.append((record, "citation_neighborhood", score))
    entries = _merge_stages(entries, ranked_list, "citation_neighborhood")
    zero_ranked = [(0.0, record) for record in domain if record["fqn"] not in ranked]
    return _merge_stages(entries, zero_ranked, "citation_neighborhood")


def search_text(index: LibraryIndex, root: Path, text: str, limit: int) -> tuple[dict, list[str]]:
    tokens, untranslated = query_tokens(text)
    limitations = list(BASE_LIMITATIONS)
    if untranslated:
        limitations.append("untranslated CJK query tokens dropped (no terminology entry): "
                           + ", ".join(untranslated))
    obligations = []
    if not tokens:
        limitations.append("no usable query tokens after normalization")
        return assemble_report(index, root,
                               {"kind": "natural_language", "text": text,
                                "paper_id": None, "goal_fingerprint": None},
                               [], limitations, obligations), ["no usable query tokens"]
    # Citation neighborhood: a paper whose title/alias/domain contains every significant token.
    paper = _paper_hit_for_text(index, tokens, text)
    entries: list[tuple[dict, str, float]] = []
    if paper:
        entries.extend(_paper_neighborhood(index, paper, tokens, text))
    entries = _merge_stages(entries, index.lexical_rank(tokens, None, text), "lexical_match")
    query = {"kind": "natural_language", "text": text,
             "paper_id": paper, "goal_fingerprint": None}
    human = [f"query tokens: {' '.join(tokens)}"
             + (f" (citation neighborhood: {paper})" if paper else "")]
    return assemble_report(index, root, query, entries[:limit], limitations, obligations), human


def _paper_hit_for_text(index: LibraryIndex, tokens: list[str], text: str) -> str | None:
    """Use a citation neighborhood only when all tokens identify one paper.

    Multiple matching vocabularies do not establish source intent. Keep the global
    lexical ranking in that case instead of privileging the first metadata entry.
    This also makes repeated single-token queries behave like single-token queries.
    """
    if not tokens:
        return None
    candidates = []
    for paper, fact in index.paper_facts().items():
        identity = fact["identity"]
        blob_tokens: set[str] = set()
        for piece in (paper, " ".join(sorted(fact["aliases"])), identity.get("venue") or ""):
            blob_tokens.update(tokenize(piece))
        if all(token in blob_tokens for token in tokens):
            candidates.append(paper)
    return candidates[0] if len(candidates) == 1 else None


def search_paper(index: LibraryIndex, root: Path, paper_id: str, limit: int) -> tuple[dict, list[str]]:
    limitations = list(BASE_LIMITATIONS)
    paper, tables = index.resolve_paper(paper_id)
    if paper is None:
        limitations.append(f"paper id {paper_id!r} did not resolve to a unique paper; searched tables: "
                           + "; ".join(tables))
        report = assemble_report(index, root,
                                 {"kind": "paper", "text": paper_id,
                                  "paper_id": None, "goal_fingerprint": None},
                                 [], limitations,
                                 ["verify the arXiv id/DOI against metadata/papers/*.json identity"])
        return report, [f"paper id {paper_id!r} unresolved"]
    fact = index.paper_facts()[paper]
    identity = fact["identity"]
    phrase = identity.get("venue") or paper
    tokens = tokenize(paper + " " + " ".join(fact["aliases"]) + " " + phrase)
    entries = _paper_neighborhood(index, paper, tokens, paper)
    entries = _merge_stages(entries, index.lexical_rank(tokens, None, paper), "lexical_match")
    query = {"kind": "paper", "text": paper_id, "paper_id": paper, "goal_fingerprint": None}
    human = [f"paper {paper} (arXiv {identity.get('arxiv_id')}): "
             f"{len(index.paper_domain(paper))} declarations in the citation neighborhood"]
    return assemble_report(index, root, query, entries[:limit], limitations, []), human


def search_theorem(index: LibraryIndex, root: Path, label: str, limit: int) -> tuple[dict, list[str]]:
    limitations = list(BASE_LIMITATIONS)
    matched, uncovered = index.resolve_theorem(label)
    obligations = []
    human: list[str] = []
    if uncovered:
        papers = ", ".join(sorted({info["paper"] for info in uncovered}))
        limitations.append(f"matching result labels have empty covered_by in paper(s) {papers}; "
                           "coverage in another paper does not discharge these open targets")
        for info in sorted(uncovered, key=lambda i: (i["paper"], i["label"])):
            obligations.append(
                f"result_label {info['label']!r} of paper {info['paper']} is an open target"
                ": formalize and link it before retrieval can return it")
        human.append(f"matching labels remain open targets of paper(s) {papers}")
        source_notes = sorted({json.dumps(info, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":"))
                               for info in uncovered if info.get("binding_note") or info.get("content")})
        if source_notes:
            limitations.append("open-target metadata notes: [" + ",".join(source_notes) + "]")
    if not matched:
        if not uncovered:
            limitations.append(f"result_label {label!r} matched no indexed declaration "
                               "(searched: enriched result_labels and metadata/papers/*.json active claims)")
        report = assemble_report(index, root,
                                 {"kind": "theorem", "text": label,
                                  "paper_id": None, "goal_fingerprint": None},
                                 [], limitations, obligations)
        return report, human
    tokens = tokenize(label)
    # An exact metadata label is a citation hit irrespective of the declaration's
    # physical layer. Seed every match before any neighborhood/global lexical
    # candidates, including when all covered declarations live in Foundations/Concepts.
    entries = [(record, "citation_neighborhood", index.score(record["fqn"], tokens))
               for record in matched]
    papers = {record["paper"] for record in matched if record.get("paper")}
    for record in matched:
        papers.update(index.linked_papers.get(record["fqn"], set()))
    for source_paper in sorted(papers):
        neighborhood = _paper_neighborhood(index, source_paper, tokens, label)
        entries = _merge_stages(entries, [(score, record) for record, _stage, score in neighborhood],
                               "citation_neighborhood")
    entries = _merge_stages(entries, index.lexical_rank(tokens, None, label), "lexical_match")
    papers.update(info["paper"] for info in uncovered)
    paper = next(iter(papers)) if len(papers) == 1 else None
    query = {"kind": "theorem", "text": label, "paper_id": paper, "goal_fingerprint": None}
    human.append(f"result_label {label!r} covered by "
                 + ", ".join(r["fqn"] for r in matched))
    return assemble_report(index, root, query, entries[:limit], limitations, obligations), human


def search_fqn(index: LibraryIndex, root: Path, fqn: str, limit: int) -> tuple[dict, list[str]]:
    limitations = list(BASE_LIMITATIONS)
    exact = index.by_fqn.get(fqn)
    tokens = tokenize(fqn)
    short = fqn.rsplit(".", 1)[-1]
    pool = [exact] if exact else [r for r in index.records
                                  if short.lower() in r["fqn"].lower()]
    ranked = index.lexical_rank(tokens, pool, fqn)
    entries = [(record, "lexical_match", score) for score, record in ranked]
    if exact and not entries:
        entries = [(exact, "lexical_match", 0.0)]
    if not entries:
        limitations.append(f"fqn {fqn!r} matched nothing (exact and short-name substring lookup)")
    query = {"kind": "lean_goal", "text": fqn, "paper_id": None, "goal_fingerprint": None}
    human = [f"fqn lookup {fqn!r}: "
             + ("exact declaration" if exact else f"{len(pool)} substring candidates")]
    return assemble_report(index, root, query, entries[:limit], limitations, []), human


SEARCHERS = {"text": ("--text", search_text), "paper_id": ("--paper-id", search_paper),
             "theorem_id": ("--theorem-id", search_theorem), "fqn": ("--fqn", search_fqn)}


def human_summary(report: dict, extra: list[str]) -> str:
    lines = [f"outcome: {report['outcome']}"] + [f"  {line}" for line in extra]
    snapshot = report["snapshot"]
    lines.append(f"snapshot: {snapshot['repository']} @ {snapshot['commit'][:12]}"
                 f" (dirty={snapshot['dirty']}, {snapshot['lean_toolchain']})")
    for index, result in enumerate(report["results"], 1):
        note = result["scope"]["scope_note"]
        ranking_note, _, coverage_note = note.partition("\nmetadata coverage: ")
        stage = "citation" if "stage: citation_neighborhood" in ranking_note else "global"
        score = ranking_note.split("score: ")[-1].split(";")[0] if "score: " in ranking_note else "?"
        lines.append(f"{index}. [{stage}] {result['fqn']}  (score {score})")
        lines.append(f"     {result['module']}")
        if coverage_note:
            lines.append(f"     metadata coverage: {coverage_note}")
    for limitation in report["search_scope"]["limitations"]:
        lines.append(f"limitation: {limitation}")
    for obligation in report["next_obligations"]:
        lines.append(f"next: {obligation}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("search", "inspect"):
        child = sub.add_parser(command)
        child.add_argument("--text", help="free-text query (Chinese/English/symbols)")
        child.add_argument("--paper-id", help="arXiv id / DOI / paper directory / alias")
        child.add_argument("--theorem-id", help="papers-metadata result_label, e.g. 'Theorem 12'")
        child.add_argument("--fqn", help="declaration name (full FQN for inspect)")
        child.add_argument("--json", action="store_true", help="emit the retrieval report JSON")
        child.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                           help=f"max results (default {DEFAULT_LIMIT})")
        child.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX,
                           help=f"index directory (default {DEFAULT_INDEX})")
        child.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA,
                           help=f"metadata directory (default {DEFAULT_METADATA})")
        child.add_argument("--root", type=Path, default=ROOT,
                           help="repository root used for source paths (tests)")
    args = parser.parse_args(argv)

    try:
        index = LibraryIndex(args.index_dir.resolve(), args.metadata_dir.resolve()
                             if args.metadata_dir else None)
        index.validate_current(args.root.resolve())
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.command == "inspect":
        if not args.fqn:
            print("error: inspect requires --fqn <FQN>", file=sys.stderr)
            return 2
        record = index.by_fqn.get(args.fqn)
        try:
            index.validate_current(args.root.resolve())
        except (FileNotFoundError, ValueError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        if record is None:
            print(f"error: no declaration with fqn {args.fqn!r} in "
                  f"{args.index_dir / 'enriched.jsonl'}", file=sys.stderr)
            return 1
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    provided = {key: value for key, (_flag, _fn) in SEARCHERS.items()
                if (value := getattr(args, key)) is not None}
    if len(provided) != 1:
        flags = " ".join(flag for flag, _fn in SEARCHERS.values())
        print(f"error: search needs exactly one of {flags}", file=sys.stderr)
        return 2
    (key, value), = provided.items()
    if args.limit < 1:
        print("error: --limit must be >= 1", file=sys.stderr)
        return 2
    report, extra = SEARCHERS[key][1](index, args.root, value, args.limit)
    try:
        index.validate_current(args.root.resolve())
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(human_summary(report, extra))
    return 0


if __name__ == "__main__":
    # GBK consoles crash printing Lean/UTF-8 output; never fail on encoding.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
