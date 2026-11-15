#!/usr/bin/env python3
"""B08: the 200-item retrieval contract testset (frozen gold) and its evaluator.

Two modes:

`--generate` builds `verification/retrieval-testset.json` from real repository data only —
the enriched declaration index (`.qatlas/index/enriched.jsonl`) and the links of
`metadata/papers/*.json`. Every gold entry is a real enriched FQN and
every item records `gold_method`, the mechanical way its gold was determined:

  direct_reuse   46 proposition-definition/theorem pairs (the answer file's own
                 `example : <claim> := <proof>` line is the EXACT witness; a sample is
                 re-verified through a real `check_goal` Lean run) + lexical first-hit
                 items across --fqn / --text (English and CJK) / --paper-id /
                 --theorem-id queries, each verified by running the real searcher.
  bridge         candidate theorems whose explicit premises are closed propositions
                 witnessed by other indexed theorems (`normalized_plus`,
                 `hadamard_unitary`, ...). The goal Prop is derived by parsing the
                 candidate's indexed `lean_type` (binder prefix + top-level arrow body)
                 and substituting the bridge witnesses; EVERY bridge item is verified by
                 a real `check_goal --bridge` run reaching VIA_BRIDGE (non-compiling
                 combos are dropped, never shipped).
  conditional    same mechanical goal construction, no bridges supplied: the candidate's
                 conclusion matches the goal but its premises are missing (CONDITIONAL).
                 A sample is verified through a real `check_goal` run.
  incompatible   (a) papers-metadata result labels with empty `covered_by` — the open
                 targets (e.g. BHMT "Theorem 12") — verified to retrieve NO_MATCH;
                 (b) cross-paper semantic mismatches: goal = one paper's claim, candidate
                 = a different paper's answer theorem (sample verified RELATED_ONLY);
                 (c) vocabulary genuinely absent from the library (verified NO_MATCH).

Gold is FROZEN once generated: do not edit `gold`/`allow_alternates` to make numbers
pass. A newly discovered equivalent correct proof is registered in the item's
`allow_alternates` list together with a note naming the PR that proved equivalence; the
candidate FQN itself must exist in the then-current enriched index.

`--output REPORT.json` (default mode) evaluates the frozen testset. The fast path runs
only `query_library` searchers (lexical + citation neighborhood; no lake, no docker).
Because the B06 protocol forbids applicability claims from the search stage, the fast
path never predicts EXACT/VIA_BRIDGE — an "可直接使用" (direct-use) claim can only come
from a real `check_goal` Lean run. Slow spot checks (`QATLAS_TEST_SLOW=1`, or
`--bridge-sample N` etc.) run `check_goal` on a deterministic sample per category and
make the false-direct-use gate non-trivial.

Metrics: per-category accuracy, Recall@10 over the 120 items with compatible candidates
(80 direct + 40 bridge), the false direct-use count (target 0), and a 4-way confusion
matrix. Applicability (the goal items probed by a real `check_goal` Lean run) and
retrieval (all 200 items) are reported over separate denominators: the frozen V1 testset
carries 125 goals (30 direct / 40 bridge / 40 conditional / 15 incompatible) and 75
retrieval-only items.

The evaluator is a gate: a failed threshold, a missing/erroneous probe, a timeout, an
invalid check_goal report or an outcome mismatch makes the process exit non-zero. Failed
probes are recorded per item, never silently dropped. A closing outcome (EXACT/
VIA_BRIDGE) only counts when the report's actual application/axiom witness is present
(`application_closed` + `axiom_check_passed`); a sorry-diagnostic probe is never
positive evidence. Negative items pass only by genuine rejection (no closing outcome).

Stdlib only. Run from the repository root. `--generate` additionally needs lake on PATH
(elan) for the check_goal verifications; evaluation needs it for the probe samples
(default none; `QATLAS_TEST_SLOW=1` spot-checks, `--full-goals` probes all 125).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search import query_library

ROOT = query_library.ROOT
DEFAULT_INDEX = query_library.DEFAULT_INDEX
DEFAULT_TESTSET = ROOT / "verification" / "retrieval-testset.json"
CATEGORIES = ("direct_reuse", "bridge", "conditional", "incompatible")
CATEGORY_COUNTS = {"direct_reuse": 80, "bridge": 40, "conditional": 40, "incompatible": 40}
EXPECTED_OUTCOME = {"direct_reuse": "EXACT", "bridge": "VIA_BRIDGE",
                    "conditional": "CONDITIONAL", "incompatible": "NOT_COMPATIBLE"}
RECALL_DENOMINATOR = CATEGORY_COUNTS["direct_reuse"] + CATEGORY_COUNTS["bridge"]
RECALL_THRESHOLD = 0.90  # E03 gate; enforced by the evaluator's exit code

GOLD_FREEZE_NOTE = (
    "Gold frozen at generation time (see generator_env). Do not edit gold to make the "
    "evaluator pass. To register a newly proven equivalent candidate, append its FQN to "
    "the item's allow_alternates (it must exist in the current enriched index) and name "
    "the PR that proved equivalence in the item's notes."
)

UNITARY_DISPLAY_RE = re.compile(r"𝐔\[([^[\]]*)\]")

QUERY_GENERIC = {"quantum", "algorithm", "nat", "ofnat", "bool", "fin", "matrix",
                 "one", "self", "eq"}


def de_notate(prop: str) -> str:
    """Rewrite the delaborated `𝐔[I]` display back to the underlying term
    `Matrix.unitaryGroup (I) ℂ` so goal files parse without scoped notation. The index
    is parenthesized — `Bool × Bool` must stay one argument."""
    return UNITARY_DISPLAY_RE.sub(r"Matrix.unitaryGroup (\1) ℂ", prop)


def goal_query(goal_prop: str) -> str:
    """User-side retrieval query derived mechanically from the goal proposition: the
    distinctive identifier vocabulary of the (de-notated) goal, lowercased, namespace
    filler and numeric/boolean noise dropped. Never uses the candidate's name."""
    text = de_notate(goal_prop)
    words = [w for w in split_identifier(text).split()
             if w.isascii() and w.isalnum() and len(w) > 2 and not w.isdigit()
             and w.lower() not in query_library.STOPWORDS
             and w.lower() not in QUERY_GENERIC]
    return " ".join(list(dict.fromkeys(w.lower() for w in words))[:6])

OPEN_BRACKETS = {"(": ")", "{": "}", "[": "]", "⟨": "⟩"}
IDENT_RE = r"[A-Za-z_αβγδεζηθικλμνξοπρστυφχψω][\w']*"


# --------------------------------------------------------------------------- type parsing

def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def split_binder_groups(prefix: str) -> list[str] | None:
    """Split a ∀-prefix into its top-level bracket groups. Returns None when the prefix
    contains unbracketed body binders (`∀ x ∈ s, …` display form) — such candidates are
    skipped, not guessed at."""
    groups: list[str] = []
    depth = 0
    cur = ""
    for ch in prefix:
        if ch in OPEN_BRACKETS:
            depth += 1
        elif ch in ")]}⟩":
            depth -= 1
        if depth == 0 and ch in " \t":
            if cur.strip():
                groups.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        groups.append(cur.strip())
    merged: list[str] = []
    for group in groups:
        if merged and group[0] not in OPEN_BRACKETS:
            merged[-1] += " " + group
        else:
            merged.append(group)
    if any(group[0] not in OPEN_BRACKETS for group in merged):
        return None
    return merged


def parse_forall_type(lean_type: str) -> tuple[list[tuple[str, str | None, str]], str] | None:
    """`∀ {x : T} [inst], body` → ([(kind, name, type)], body). Whitespace-normalized.
    Returns None for shapes the parser does not fully understand."""
    text = normalize_ws(lean_type)
    if not text.startswith("∀"):
        return [], text
    depth = 0
    idx = None
    for i, ch in enumerate(text):
        if ch in OPEN_BRACKETS:
            depth += 1
        elif ch in ")]}⟩":
            depth -= 1
        elif ch == "," and depth == 0:
            idx = i
            break
    if idx is None:
        return None
    prefix, body = text[1:idx].strip(), text[idx + 1:].strip()
    groups = split_binder_groups(prefix)
    if groups is None:
        return None
    binders: list[tuple[str, str | None, str]] = []
    for group in groups:
        closer = OPEN_BRACKETS[group[0]]
        if not group.endswith(closer):
            return None
        kind = {"{": "implicit", "[": "inst", "(": "explicit"}[group[0]]
        inner = group[1:-1].strip()
        colon = re.fullmatch(r"(.+?)\s*:\s*(.+)", inner)
        if colon:
            names = [n for n in colon.group(1).split()
                     if n and not n.startswith("inst")]
            for name in names or [None]:
                binders.append((kind, name, colon.group(2).strip()))
        else:
            binders.append((kind, None, inner))
    return binders, body


def split_arrows(body: str) -> list[str] | None:
    """Split `A → B → C` at top-level arrows (right-associated). None when unbalanced."""
    parts: list[str] = []
    depth = 0
    cur = ""
    for ch in body:
        if ch in OPEN_BRACKETS:
            depth += 1
        elif ch in ")]}⟩":
            depth -= 1
        if depth == 0 and ch == "→":
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    if depth != 0:
        return None
    parts.append(cur.strip())
    return parts


def split_identifier(text: str) -> str:
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", text)
    text = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", text)
    return text.replace("_", " ").replace(".", " ")


# --------------------------------------------------------------------------- generation

def index_probe_args(index: query_library.LibraryIndex | None) -> list[str]:
    """Keep applicability probes on the same artifacts used for retrieval."""
    if index is None:
        return []
    args = ["--index-dir", str(index.index_dir.resolve())]
    if index.metadata_dir is not None:
        args += ["--metadata-dir", str(index.metadata_dir.resolve())]
    return args


class Generator:
    def __init__(self, index: query_library.LibraryIndex, verify: bool, verify_limit: int,
                 log=lambda msg: print(msg, file=sys.stderr)):
        self.index = index
        self.by_fqn = index.by_fqn
        self.verify = verify
        self.verify_budget = verify_limit
        self.log = log
        self.items: list[dict] = []

    # ---- check_goal verification -------------------------------------------------
    def run_check(self, goal_text: str, candidate: str, bridges: list[str]) -> dict | None:
        """Run the check_goal module on a temp goal file; return its JSON report or None
        (verification unavailable). Spends one unit of the verify budget."""
        if not self.verify or self.verify_budget <= 0:
            return None
        self.verify_budget -= 1
        import tempfile
        with tempfile.TemporaryDirectory(prefix="qatlas-gen-") as tmp:
            goal = Path(tmp) / "goal.lean"
            goal.write_text(goal_text, encoding="utf-8")
            cmd = [sys.executable, "-B", "-m", "qatlib_search.check_goal", "check",
                   "--goal-file", str(goal), "--candidate", candidate, "--json",
                   *index_probe_args(self.index)]
            for bridge in bridges:
                cmd += ["--bridge", bridge]
            proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True,
                                  encoding="utf-8", errors="replace", timeout=900)
            if proc.returncode != 0:
                self.log(f"check_goal failed for {candidate} (exit {proc.returncode}): {proc.stderr[:200]}")
                return None
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError:
                self.log(f"check_goal produced no JSON for {candidate}: {proc.stderr[:200]}")
                return None

    # ---- search probes ------------------------------------------------------------
    def search_top(self, kind: str, value: str, limit: int = 10) -> tuple[str, list[str]]:
        report, _human = {
            "text": query_library.search_text,
            "theorem_id": query_library.search_theorem,
            "paper_id": query_library.search_paper,
            "fqn": query_library.search_fqn,
        }[kind](self.index, ROOT, value, limit)
        return report["outcome"], [r["fqn"] for r in report["results"][:limit]]

    # ---- direct_reuse ---------------------------------------------------------------
    def claim_answer_pairs(self) -> list[tuple[dict, dict]]:
        """(claim def, answer theorem) pairs: the answer theorem's indexed type IS the
        claim FQN — that type identity is the mechanical EXACT witness. For the frozen
        QatlasContract family the answer file additionally closes with an explicit
        `example : <claim> := <proof>`, recorded as extra provenance when present."""
        claims = {r["fqn"]: r for r in self.index.records if r.get("is_prop_definition")}
        pairs = []
        for record in self.index.records:
            if record["kind"] == "theorem" and record.get("lean_type") in claims:
                claim = claims[record["lean_type"]]
                witness = self.answer_example_line(claim, record)
                pairs.append((claim, record | {"example_witness": witness}))
        pairs.sort(key=lambda p: p[1]["fqn"])
        return pairs

    def answer_example_line(self, claim: dict, answer: dict) -> str | None:
        """The answer file's `example : <claim FQN> := <proof>` line, if present —
        a compiled EXACT witness living in the repository itself (the proof term may
        sit on its own line after the `:=`)."""
        source = ROOT / Path(*answer["module"].split(".")).with_suffix(".lean")
        proof = answer["fqn"].rsplit(".", 1)[-1]
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            return None
        pattern = (rf"^example\s+:\s*{re.escape(claim['fqn'])}\s*:=\s*"
                   rf"{re.escape(proof)}\s*$")
        match = re.search(pattern, text, re.M | re.S)
        if match:
            return f"{source}:{text[:match.start()].count(chr(10)) + 1}"
        return None

    def add(self, **item) -> dict:
        item.setdefault("allow_alternates", [])
        item.setdefault("goal", None)
        item.setdefault("accept_rank", 10)
        self.items.append({"id": None, **item})
        return self.items[-1]

    @staticmethod
    def claim_goal_file(claim: dict) -> str:
        """Goal file naming a claim def. Universe-polymorphic claims must re-bind their
        universe levels (`universe u` + `Claim.{u}`) — Lean rejects a def whose body has
        universe metavariables, which check_goal reports as a goal-file violation."""
        header = f"import {claim['module']}\n\n"
        if claim.get("universes"):
            levels = [f"u{i + 1}" for i, _u in enumerate(claim["universes"])]
            return (header + f"universe {' '.join(levels)}\n\n"
                    + f"def QatlasGoal.goal : Prop := "
                      f"{claim['fqn']}.{{{', '.join(levels)}}}\n")
        return header + f"def QatlasGoal.goal : Prop := {claim['fqn']}\n"

    def build_direct(self) -> None:
        pairs = self.claim_answer_pairs()

        # (1) up to 31 frozen-contract claim→answer items. Query = the answer short
        # name's vocabulary (measured at generation); the answer file's own closing
        # `example` line is the EXACT witness, re-verified by check_goal on a sample.
        verified = 0
        used_pairs = 0
        for claim, answer in pairs:
            if used_pairs >= 31:
                break
            short = answer["fqn"].rsplit(".", 1)[-1]
            query = " ".join(w for w in split_identifier(short).split()
                             if w.lower() not in query_library.STOPWORDS and len(w) > 2)
            outcome, top = self.search_top("text", query)
            if not (set(top[:10]) & {answer["fqn"], claim["fqn"]}):
                continue
            goal_text = self.claim_goal_file(claim)
            method = (f"answer theorem type identity: the indexed lean_type of "
                      f"{answer['fqn']} IS the claim def (exact application is a "
                      f"typecheck by construction)")
            if answer.get("example_witness"):
                method += f"; answer-file example witness at {answer['example_witness']}"
            if verified < 3:
                report = self.run_check(goal_text, answer["fqn"], [])
                if report and report.get("outcome") == "EXACT":
                    method += (f"; re-verified EXACT by check_goal (lean exit "
                               f"{report['results'][0]['validation']['exit_code']})")
                    verified += 1
            self.add(category="direct_reuse", expected="EXACT",
                     query={"kind": "text", "text": query},
                     gold=[answer["fqn"]], allow_alternates=[claim["fqn"]],
                     goal={"text": goal_text, "candidate": answer["fqn"], "bridges": []},
                     accept_rank=10, gold_method=method,
                     notes=f"frozen-contract pair of {claim.get('paper')}; retrieval may "
                           f"surface the proposition definition or the answer theorem "
                           f"(allow_alternates)")
            used_pairs += 1

        # (2) papers-metadata covered labels: search --theorem-id must surface the
        # covered declarations first. label_pairs maps fqn → [(label, coverage)];
        # invert it into label → covered fqns.
        label_to_fqns: dict[str, list[str]] = {}
        for fqn, entries in self.index.label_pairs.items():
            for label, _coverage in entries:
                label_to_fqns.setdefault(label, [])
                if fqn not in label_to_fqns[label]:
                    label_to_fqns[label].append(fqn)
        covered_labels = sorted((label, sorted(fqns))
                                for label, fqns in label_to_fqns.items())
        random.Random(20260926).shuffle(covered_labels)
        used_labels = 0
        for label, fqns in covered_labels:
            if used_labels >= 8:
                break
            outcome, top = self.search_top("theorem_id", label)
            if not (set(top[:10]) & set(fqns)):
                continue
            self.add(category="direct_reuse", expected="EXACT",
                     query={"kind": "theorem_id", "text": label},
                     gold=fqns, allow_alternates=[],
                     goal=None, accept_rank=10,
                     gold_method=f"papers-metadata covered label; search --theorem-id "
                                 f"{label!r} surfaces covered_by in top-10 (measured "
                                 f"at generation)",
                     notes="label→covered_by lookup through metadata/papers links")
            used_labels += 1


        # (4) text queries (English aliases + CJK terminology-bridge queries).
        text_queries: list[tuple[str, dict]] = []
        for record in self.index.records:
            if record.get("paper") and record["kind"] == "theorem":
                for alias in record.get("aliases") or []:
                    if " " in alias and alias.replace(" ", "").isascii():
                        text_queries.append((alias, record))
        text_queries.sort(key=lambda t: t[1]["fqn"])
        for text, record in text_queries:
            if len([i for i in self.items if i["query"]["kind"] == "text"]) >= 8:
                break
            outcome, top = self.search_top("text", text)
            if top and record["fqn"] in top[:10]:
                self.add(category="direct_reuse", expected="EXACT",
                         query={"kind": "text", "text": text},
                         gold=[record["fqn"]], allow_alternates=[],
                         goal=None, accept_rank=10,
                         gold_method=f"lexical_first_hit: query_library search --text "
                                     f"{text!r} top-10 (measured at generation)",
                         notes=f"alias-derived English text query for {record['fqn']}")
        cjk_queries = [("振幅估计", "QuantumAmplitudeAmplificationAndEstimation2000"),
                       ("量子行走 搜索", "SearchViaQuantumWalk2006"),
                       ("相位估计", "QuantumMeasurementsAndTheAbelianStabilizerProblem1995"),
                       ("马尔可夫 加速", "QuantumSpeedUpOfMarkovChainBasedAlgorithms2004"),
                       ("量子 搜索", "AFastQuantumMechanicalAlgorithmForDatabaseSearch1996"),
                       ("蒙特卡洛 加速", "MontanaroQuantumSpeedupOfMonteCarlo2015")]
        for text, paper in cjk_queries:
            if len([i for i in self.items if i["query"]["kind"] == "text"]) >= 14:
                break
            outcome, top = self.search_top("text", text)
            hit = [f for f in top[:10] if self.by_fqn[f].get("paper") == paper]
            if not hit:
                continue
            self.add(category="direct_reuse", expected="EXACT",
                     query={"kind": "text", "text": text},
                     gold=sorted(hit), allow_alternates=[],
                     goal=None, accept_rank=10,
                     gold_method=f"lexical_first_hit via CJK terminology bridge: "
                                 f"search --text {text!r} hits the {paper} domain top-10 "
                                 f"(measured at generation)",
                     notes=f"CJK query bridged to the {paper} citation neighborhood")

        # (5) paper-id queries: any theorem of the paper's domain in the top 10.
        facts = self.index.paper_facts()
        wanted = sorted(facts)
        random.Random(20260926).shuffle(wanted)
        for paper in wanted:
            if len([i for i in self.items if i["query"]["kind"] == "paper_id"]) >= 15:
                break
            arxiv = (facts[paper]["identity"] or {}).get("arxiv_id")
            if not arxiv:
                continue
            query = f"arxiv:{arxiv}"
            outcome, top = self.search_top("paper_id", query)
            domain = {r["fqn"] for r in self.index.paper_domain(paper)
                      if r["kind"] == "theorem"}
            if not domain or not (set(top[:10]) & domain):
                continue
            self.add(category="direct_reuse", expected="EXACT",
                     query={"kind": "paper_id", "text": query},
                     gold=sorted(domain), allow_alternates=[],
                     goal=None, accept_rank=10,
                     gold_method=f"citation_neighborhood: query_library search "
                                 f"--paper-id '{query}' surfaces the {paper} theorem "
                                 f"domain top-10 (measured at generation)",
                     notes=f"paper-domain query; any theorem of {paper} counts as gold")

        # (3) fqn lookups across Foundations: exact name must be rank 1. This bucket
        # runs last and tops the category up to exactly 80 (pairs/labels/queries that
        # failed their probe are skipped, never shipped with an unmeasured gold).
        fqn_pool = [r for r in self.index.records
                    if r["kind"] in ("theorem", "def")
                    and r.get("paper") is None
                    and not r.get("is_prop_definition")
                    and not r["fqn"].startswith("QatlasContract_")]
        fqn_pool.sort(key=lambda r: r["fqn"])
        random.Random(20260926).shuffle(fqn_pool)
        for record in fqn_pool:
            if len(self.items) >= CATEGORY_COUNTS["direct_reuse"]:
                break
            outcome, top = self.search_top("fqn", record["fqn"])
            if outcome == "NO_MATCH" or not top or top[0] != record["fqn"]:
                continue
            self.add(category="direct_reuse", expected="EXACT",
                     query={"kind": "fqn", "text": record["fqn"]},
                     gold=[record["fqn"]], allow_alternates=[],
                     goal=None, accept_rank=1,
                     gold_method=f"lexical_first_hit: query_library search --fqn "
                                 f"'{record['fqn']}' rank 1 (measured at generation)",
                     notes=f"Foundations {record['kind']} looked up by exact FQN")

        got = len(self.items)
        if got < CATEGORY_COUNTS["direct_reuse"]:
            raise SystemExit(f"direct_reuse: built only {got}, need "
                             f"{CATEGORY_COUNTS['direct_reuse']}")
        self.items = self.items[:CATEGORY_COUNTS["direct_reuse"]]

    # ---- bridge & conditional machinery ----------------------------------------------
    def bridge_witnesses(self) -> dict[str, list[dict]]:
        """Closed indexed theorems grouped by the premise shape they can close."""
        witnesses: dict[str, list[dict]] = {"normalized": [], "unitary": []}
        for record in self.index.records:
            if record["kind"] != "theorem" or record.get("binders") or record.get("universes"):
                continue
            lt = normalize_ws(record["lean_type"])
            if re.fullmatch(r"QuantumAlgorithm\.Normalized\s+\S+", lt):
                witnesses["normalized"].append(record)
            elif re.fullmatch(r".+?\s+∈\s+𝐔\[.+?\]", lt):
                witnesses["unitary"].append(record)
        for family in witnesses.values():
            family.sort(key=lambda r: r["fqn"])
        return witnesses

    @staticmethod
    def unify_premise(premise: str, binder_names: set[str],
                      witness_type: str) -> dict[str, str] | None:
        """Assignment dict unifying a premise (over named binder variables) with a closed
        witness theorem's type. Handles `Pred var` and `var ∈ 𝐔[var2]` shapes."""
        m_norm = re.fullmatch(r"(QuantumAlgorithm\.Normalized)\s+(\S+)", premise)
        if m_norm:
            var = m_norm.group(2)
            if var not in binder_names:
                return None
            m_wit = re.fullmatch(r"(QuantumAlgorithm\.Normalized)\s+(\S+)", witness_type)
            if not m_wit:
                return None
            return {var: m_wit.group(2)}
        m_mem = re.fullmatch(rf"({IDENT_RE})\s+∈\s+𝐔\[({IDENT_RE})\]", premise)
        if m_mem:
            u_var, i_var = m_mem.group(1), m_mem.group(2)
            if u_var not in binder_names or i_var not in binder_names:
                return None
            m_wit = re.fullmatch(r"(.+?)\s+∈\s+𝐔\[(.+?)\]", witness_type)
            if not m_wit:
                return None
            return {u_var: m_wit.group(1), i_var: m_wit.group(2)}
        return None

    def propagate(self, assignments: dict[str, str],
                  binder_types: dict[str, str]) -> dict[str, str]:
        """When `v := term` and v's binder type is `T a b…` with a/b binder variables,
        look up term's indexed type `T a' b'…` and assign a := a', b := b'."""
        for _ in range(4):
            changed = False
            for var, term in list(assignments.items()):
                btype = binder_types.get(var)
                if not btype:
                    continue
                m_var = re.fullmatch(rf"(\S+)\s+((?:{IDENT_RE}\s*)+)", btype)
                m_term_record = self.by_fqn.get(term)
                if not m_var or not m_term_record:
                    continue
                head = m_var.group(1)
                args = m_var.group(2).split()
                m_term = re.fullmatch(rf"({re.escape(head)})\s+((?:\S+\s*)+)",
                                      normalize_ws(m_term_record["lean_type"]))
                if not m_term:
                    continue
                term_args = m_term.group(2).split()
                if len(term_args) != len(args):
                    continue
                for arg, term_arg in zip(args, term_args):
                    if arg in binder_types and arg not in assignments:
                        assignments[arg] = term_arg
                        changed = True
            if not changed:
                break
        return assignments

    @staticmethod
    def substitute(text: str, assignments: dict[str, str]) -> str:
        for var, term in sorted(assignments.items(), key=lambda kv: -len(kv[0])):
            replacement = f"({term})" if " " in term else term
            text = re.sub(rf"(?<![\w'.]){re.escape(var)}(?![\w'])", replacement, text)
        return text

    def flatten_body(self, binders, body):
        """Flatten `prem₁ → ∀ {implicit…}, prem₂ → … → conclusion` into the premise
        list + conclusion: peel one top-level arrow at a time, absorbing nested
        implicit ∀ segments (right-associated, so their scope extends to the end).
        Explicit nested binders are rejected — they break the
        `exact candidate bridge…` leading-argument shape."""
        premises: list[str] = []
        while True:
            body = body.strip()
            if body.startswith("∀"):
                parsed = parse_forall_type(body)
                if not parsed:
                    return None
                more, rest = parsed
                if any(kind == "explicit" for kind, _n, _t in more):
                    # explicit binders here cannot be filled by theorem bridges, so
                    # this ∀ segment must be the conclusion, not a premise carrier
                    premises.append(body)
                    return binders, premises
                binders = binders + more
                body = rest
                continue
            split = split_arrows(body)
            if len(split) < 2:
                if len(split) != 1 or not split[0]:
                    return None
                if not premises:
                    # no premises at all — not a bridge candidate
                    return None
                premises.append(split[0])  # the conclusion
                return binders, premises
            premises.append(split[0])
            body = " → ".join(split[1:])

    def bridge_candidates(self) -> list[dict]:
        """Theorems of shape `∀ {implicit/instance binders}, prem → … → conclusion`
        (nested implicit ∀ segments flattened) where every premise is closable by a
        closed witness family."""
        out = []
        for record in sorted(self.index.records, key=lambda r: r["fqn"]):
            if record["kind"] != "theorem":
                continue
            parsed = parse_forall_type(record["lean_type"])
            if not parsed:
                continue
            binders, body = parsed
            if not binders or any(kind == "explicit" for kind, _n, _t in binders):
                continue
            flat = self.flatten_body(list(binders), body)
            if flat is None:
                continue
            binders, segments = flat
            premises, conclusion = segments[:-1], segments[-1]
            binder_names = {name for _k, name, _t in binders if name}
            shapes = []
            ok = True
            for premise in premises:
                if re.fullmatch(rf"QuantumAlgorithm\.Normalized\s+{IDENT_RE}", premise) \
                        and premise.split()[-1] in binder_names:
                    shapes.append("normalized")
                elif re.fullmatch(rf"{IDENT_RE}\s+∈\s+𝐔\[{IDENT_RE}\]", premise) \
                        and premise.split()[0] in binder_names:
                    shapes.append("unitary")
                else:
                    ok = False
                    break
            if ok:
                out.append({"record": record, "binders": binders, "premises": premises,
                            "conclusion": conclusion, "shapes": shapes})
        return out

    def build_goals(self) -> list[dict]:
        """All (candidate, bridges, goal) constructions that close every premise."""
        witnesses = self.bridge_witnesses()
        combos = []
        for cand in self.bridge_candidates():
            binder_types = {name: btype for _k, name, btype in cand["binders"] if name}
            binder_names = set(binder_types)
            per_premise = []
            for premise, shape in zip(cand["premises"], cand["shapes"]):
                options = []
                for witness in witnesses[shape]:
                    assign = self.unify_premise(premise, binder_names,
                                                normalize_ws(witness["lean_type"]))
                    if assign is not None:
                        options.append((witness, assign))
                per_premise.append(options)
            if any(not options for options in per_premise):
                continue
            indices = [0] * len(per_premise)
            while True:
                chosen = [per_premise[i][indices[i]] for i in range(len(per_premise))]
                assignments: dict[str, str] = {}
                for _witness, assign in chosen:
                    assignments.update(assign)
                assignments = self.propagate(assignments, binder_types)
                goal_prop = self.substitute(cand["conclusion"], assignments)
                leftover = [name for name in binder_names
                            if name not in assignments
                            and re.search(rf"(?<![\w'.]){re.escape(name)}(?![\w'])", goal_prop)]
                if not leftover:
                    combos.append({
                        "candidate": cand["record"],
                        "bridges": [w["fqn"] for w, _a in chosen],
                        "goal_prop": goal_prop,
                        "premises": [self.substitute(p, assignments) for p in cand["premises"]],
                        "assignments": assignments,
                    })
                for i in range(len(per_premise) - 1, -1, -1):
                    indices[i] += 1
                    if indices[i] < len(per_premise[i]):
                        break
                    indices[i] = 0
                else:
                    break
        return combos

    def goal_file(self, candidate: dict, bridges: list[str], goal_prop: str) -> str:
        modules = sorted({candidate["module"]}
                         | {self.by_fqn[f]["module"] for f in bridges})
        return ("".join(f"import {m}\n" for m in modules)
                + "\nopen QuantumAlgorithm in\n"
                + f"def QatlasGoal.goal : Prop := {de_notate(goal_prop)}\n")

    def conclusion_query(self, goal_prop: str) -> str:
        return goal_query(goal_prop)

    def build_bridge_and_conditional(self) -> None:
        combos = self.build_goals()
        if len(combos) < CATEGORY_COUNTS["bridge"]:
            raise SystemExit(f"need ≥{CATEGORY_COUNTS['bridge']} goal constructions, "
                             f"found {len(combos)}")
        # deterministic spread: sort by (candidate fqn, bridges), verify greedily.
        combos.sort(key=lambda c: (c["candidate"]["fqn"], tuple(c["bridges"])))
        used: set[tuple] = set()

        def emit(combo: dict, category: str) -> tuple[dict, tuple] | None:
            candidate, bridges = combo["candidate"], combo["bridges"]
            goal_text = self.goal_file(candidate, bridges, combo["goal_prop"])
            query = self.conclusion_query(combo["goal_prop"])
            # bridge and conditional items may draw on the same construction: they are
            # different tasks (bridges supplied vs missing); dedupe within a category.
            key = (category, candidate["fqn"], tuple(bridges), combo["goal_prop"])
            if key in used:
                return None
            used.add(key)
            item = {
                "id": None,
                "category": category,
                "expected": EXPECTED_OUTCOME[category],
                "query": {"kind": "text", "text": query},
                "gold": [candidate["fqn"]],
                "allow_alternates": [],
                "goal": {"text": goal_text, "candidate": candidate["fqn"],
                         "bridges": bridges if category == "bridge" else []},
                "accept_rank": 10,
                "gold_method": None,
                "notes": f"goal derived by parsing the indexed lean_type of "
                         f"{candidate['fqn']} and substituting witnesses "
                         f"{', '.join(bridges)}; premises: "
                         f"{'; '.join(combo['premises'])}",
            }
            return item, key

        # bridge: verify every item through a real check_goal VIA_BRIDGE run.
        built = 0
        for combo in combos:
            if built >= CATEGORY_COUNTS["bridge"]:
                break
            made = emit(combo, "bridge")
            if made is None:
                continue
            item, key = made
            report = self.run_check(item["goal"]["text"], item["goal"]["candidate"],
                                    item["goal"]["bridges"])
            if not report or report.get("outcome") != "VIA_BRIDGE":
                self.log(f"bridge combo rejected ({(report or {}).get('outcome')}): "
                         f"{item['goal']['candidate']} + {item['goal']['bridges']}")
                used.discard(key)
                continue
            exit_code = report["results"][0]["validation"]["exit_code"]
            item["gold_method"] = (f"check_goal VIA_BRIDGE verified at generation "
                                   f"(exact {item['goal']['candidate']} "
                                   f"+{' +'.join(item['goal']['bridges'])}, lean exit "
                                   f"{exit_code}, log "
                                   f"{report['results'][0]['validation']['log_path']})")
            item["id"] = f"bridge-{built + 1:03d}"
            self.items.append(item)
            built += 1
        if built != CATEGORY_COUNTS["bridge"]:
            raise SystemExit(f"bridge: verified {built}, need {CATEGORY_COUNTS['bridge']}")

        # conditional: same machinery, premises deliberately unsupplied; sample verified.
        # Prefer constructions the bridge items did not use; fall back to reusing bridge
        # constructions (a different task: the same goal with bridges missing).
        def combo_key(c: dict) -> tuple:
            return (c["candidate"]["fqn"], tuple(c["bridges"]), c["goal_prop"])

        cond_pool = [c for c in combos if ("bridge",) + combo_key(c) not in used]
        cond_pool += [c for c in reversed(combos) if ("bridge",) + combo_key(c) in used]
        built = 0
        verified = 0
        for combo in cond_pool:
            if built >= CATEGORY_COUNTS["conditional"]:
                break
            made = emit(combo, "conditional")
            if made is None:
                continue
            item, key = made
            report = self.run_check(item["goal"]["text"], item["goal"]["candidate"], [])
            if report and report.get("outcome") != "CONDITIONAL":
                self.log(f"conditional combo rejected ({report.get('outcome')}): "
                         f"{item['goal']['candidate']}")
                used.discard(key)
                continue
            if report is not None:
                verified += 1
            item["gold_method"] = (
                "goal construction identical to the verified bridge items (lean_type "
                "parse + witness substitution); bridges deliberately unsupplied"
                + (f"; check_goal CONDITIONAL verified at generation (log "
                   f"{report['results'][0]['validation']['log_path']})" if report else ""))
            item["id"] = f"cond-{built + 1:03d}"
            self.items.append(item)
            built += 1
        if built != CATEGORY_COUNTS["conditional"]:
            raise SystemExit(f"conditional: built {built}, need "
                             f"{CATEGORY_COUNTS['conditional']}")
        if verified < 5:
            raise SystemExit(f"conditional: only {verified} items verified by check_goal, "
                             f"need ≥5 (raise --verify-limit)")

    # ---- incompatible ---------------------------------------------------------------
    def open_targets(self) -> list[tuple[str, str]]:
        # A label can be open in several papers, including when another source
        # covers it. Preserve each source; repeated links are not extra targets.
        return sorted({(info["paper"], label)
                       for label, infos in self.index.uncovered_labels.items()
                       for info in infos})

    def build_incompatible(self) -> None:
        # (a) open targets → NO_MATCH (real open goals, e.g. BHMT Theorem 12).
        targets = list(self.open_targets())
        targets.sort(key=lambda t: (t[0], t[1]))
        picked, papers_seen = 0, {}
        for paper, label in targets:
            if picked >= 15:
                break
            if papers_seen.get(paper, 0) >= 3:
                continue
            outcome, top = self.search_top("theorem_id", label)
            if outcome != "NO_MATCH" or top:
                continue
            papers_seen[paper] = papers_seen.get(paper, 0) + 1
            source_notes = [info for info in self.index.uncovered_labels[label]
                            if info["paper"] == paper and info.get("result_note")]
            self.items.append({
                "id": None, "category": "incompatible", "expected": "NOT_COMPATIBLE",
                "query": {"kind": "theorem_id", "text": label},
                "gold": [], "allow_alternates": [], "goal": None,
                "accept_rank": None, "expect_search": "NO_MATCH",
                "gold_method": f"papers-metadata open target of {paper} "
                               f"(covered_by empty); search --theorem-id {label!r} "
                               f"verified NO_MATCH at generation",
                "notes": "metadata open target; no candidate retrieved in the searched "
                         "index, not a claim of semantic impossibility"
                         + ("; source notes: " + json.dumps(source_notes, ensure_ascii=False,
                                                           sort_keys=True)
                            if source_notes else ""),
            })
            picked += 1
        if picked < 15:
            raise SystemExit(f"incompatible open targets: found {picked}, need 15")

        # (b) cross-paper semantic mismatch: goal = claim A, candidate = answer of B.
        pairs = self.claim_answer_pairs()
        verified = 0
        idx = 0
        made = 0
        while made < 15 and idx < len(pairs) * len(pairs):
            claim_a, answer_a = pairs[idx % len(pairs)]
            claim_b, answer_b = pairs[(idx // len(pairs) + 1 + idx % 7) % len(pairs)]
            idx += 1
            if claim_a["fqn"] == claim_b["fqn"] or answer_a["fqn"] == answer_b["fqn"]:
                continue
            goal_text = self.claim_goal_file(claim_a)
            query_vocab = " ".join(
                w for w in split_identifier(answer_b["fqn"]).split()
                if w.lower() not in query_library.STOPWORDS and len(w) > 2)[:60]
            item = {
                "id": None, "category": "incompatible", "expected": "NOT_COMPATIBLE",
                "query": {"kind": "text", "text": query_vocab},
                "gold": [], "allow_alternates": [],
                "goal": {"text": goal_text, "candidate": answer_b["fqn"], "bridges": []},
                "accept_rank": None, "expect_search": "distractor_ok",
                "gold_method": f"cross-claim mismatch: goal is {claim_a['fqn']} "
                               f"({claim_a.get('paper')}), candidate is "
                               f"{answer_b['fqn']} ({answer_b.get('paper')}) — distinct "
                               f"contract defs, never defeq",
                "notes": "semantic-mismatch subclass: retrieval may surface the "
                         "distractor; it must never be claimed directly usable",
            }
            if verified < 5:
                report = self.run_check(goal_text, answer_b["fqn"], [])
                if report and report.get("outcome") not in ("RELATED_ONLY", "CONDITIONAL"):
                    self.log(f"mismatch combo rejected: {report.get('outcome')}")
                    continue
                if report is not None:
                    verified += 1
                    item["gold_method"] += (f"; check_goal {report['outcome']} verified "
                                            f"at generation")
            self.items.append(item)
            made += 1
        if made < 15:
            raise SystemExit(f"incompatible mismatches: built {made}, need 15")

        # (c) queries whose targets do not exist at all → NO_MATCH: nonexistent theorem
        # numbers, unresolved paper ids, and vocabulary genuinely absent from the
        # library (each verified at generation; anything that accidentally hits a real
        # declaration is dropped).
        absent = [
            ("theorem_id", "Theorem 99"),
            ("theorem_id", "Lemma 42"),
            ("theorem_id", "Proposition 13"),
            ("theorem_id", "Corollary 7"),
            ("theorem_id", "Theorem 3.5"),
            ("theorem_id", "Eq. (99)"),
            ("paper_id", "arxiv:2401.99999"),
            ("paper_id", "doi:10.9999/fake-paper"),
            ("paper_id", "NonexistentQuantumAlgorithmPaper2031"),
            ("text", "qumode squeezing synthesis"),
            ("text", "匿名网络领导者选举精确终止"),
        ]
        made = 0
        for kind, query in absent:
            if made >= 10:
                break
            outcome, top = self.search_top(kind, query)
            if outcome != "NO_MATCH" or top:
                self.log(f"absent-target query unexpectedly hit: {query!r} → {top[:3]}")
                continue
            self.items.append({
                "id": None, "category": "incompatible", "expected": "NOT_COMPATIBLE",
                "query": {"kind": kind, "text": query},
                "gold": [], "allow_alternates": [], "goal": None,
                "accept_rank": None, "expect_search": "NO_MATCH",
                "gold_method": f"absent target: search --{kind.replace('_', '-')} "
                               f"{query!r} verified NO_MATCH at generation",
                "notes": "NO_MATCH subclass: 所检索范围内未找到 (searched range only; "
                         "no claim that the result does not exist mathematically)",
            })
            made += 1
        if made < 10:
            raise SystemExit(f"incompatible absent-target: built {made}, need 10")

    # ---- driver -----------------------------------------------------------------
    def run(self) -> dict:
        started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self.build_direct()
        self.build_bridge_and_conditional()
        self.build_incompatible()
        # finalize ids per category
        counters = {c: 0 for c in CATEGORIES}
        prefixes = {"direct_reuse": "direct", "bridge": "bridge",
                    "conditional": "cond", "incompatible": "incompat"}
        for item in self.items:
            cat = item["category"]
            counters[cat] += 1
            item["id"] = f"{prefixes[cat]}-{counters[cat]:03d}"
            item["allow_alternates"] = item.get("allow_alternates") or []
            item["goal"] = item.get("goal")
            item["accept_rank"] = item.get("accept_rank")
        return self.render(started)

    def render(self, started: str) -> dict:
        counts = {c: len([i for i in self.items if i["category"] == c]) for c in CATEGORIES}
        return {
            "schema_version": "1.0",
            "generated_at": started,
            "generator": "python -m qatlib_search.eval_retrieval --generate",
            "generator_env": {
                "git_head": query_library.run_git(["rev-parse", "HEAD"], cwd=ROOT),
                "declaration_count": len(self.index.records),
            },
            "gold_freeze": GOLD_FREEZE_NOTE,
            "categories": counts,
            "items": self.items,
        }


# --------------------------------------------------------------------------- testset validation

def validate_testset(data: dict, known_fqns: set[str]) -> list[str]:
    """Structural self-check used by both --generate (pre-write) and the unit tests."""
    errors: list[str] = []
    items = data.get("items")
    if not isinstance(items, list):
        return ["items is not a list"]
    if len(items) != 200:
        errors.append(f"expected 200 items, found {len(items)}")
    counts = {c: 0 for c in CATEGORIES}
    ids = set()
    for item in items:
        where = item.get("id", "<no id>")
        for field in ("id", "category", "expected", "query", "gold", "allow_alternates",
                      "gold_method", "notes"):
            if field not in item:
                errors.append(f"{where}: missing field {field}")
        if item.get("category") not in CATEGORIES:
            errors.append(f"{where}: bad category {item.get('category')!r}")
            continue
        counts[item["category"]] += 1
        if item["id"] in ids:
            errors.append(f"duplicate id {item['id']}")
        ids.add(item["id"])
        if item.get("expected") != EXPECTED_OUTCOME[item["category"]]:
            errors.append(f"{where}: expected outcome {item.get('expected')!r} does not "
                          f"match category {item['category']}")
        query = item.get("query") or {}
        if query.get("kind") not in ("text", "theorem_id", "paper_id", "fqn"):
            errors.append(f"{where}: bad query kind {query.get('kind')!r}")
        if not isinstance(query.get("text"), str) or not query["text"]:
            errors.append(f"{where}: empty query text")
        for fqn in list(item.get("gold") or []) + list(item.get("allow_alternates") or []):
            if fqn not in known_fqns:
                errors.append(f"{where}: fqn {fqn} not in the enriched index")
        if item["category"] != "incompatible" and not (item.get("gold")):
            errors.append(f"{where}: non-incompatible item needs a gold candidate")
        if item["category"] == "incompatible":
            if item.get("gold"):
                errors.append(f"{where}: incompatible item must have empty gold")
            if item.get("expect_search") not in ("NO_MATCH", "distractor_ok"):
                errors.append(f"{where}: incompatible item needs expect_search "
                              f"NO_MATCH|distractor_ok")
        goal = item.get("goal")
        if goal is not None:
            for field in ("text", "candidate", "bridges"):
                if field not in goal:
                    errors.append(f"{where}: goal missing {field}")
            if goal.get("candidate") and goal["candidate"] not in known_fqns:
                errors.append(f"{where}: goal candidate not in the index")
            for bridge in goal.get("bridges") or []:
                if bridge not in known_fqns:
                    errors.append(f"{where}: bridge {bridge} not in the index")
            if "def QatlasGoal.goal" not in (goal.get("text") or ""):
                errors.append(f"{where}: goal text does not define QatlasGoal.goal")
        if item["category"] in ("direct_reuse", "bridge", "conditional") \
                and not item.get("gold_method"):
            errors.append(f"{where}: missing gold_method provenance")
    for category, want in CATEGORY_COUNTS.items():
        if counts[category] != want:
            errors.append(f"category {category}: {counts[category]} items, want {want}")
    return errors


# --------------------------------------------------------------------------- evaluation

SEARCHERS = {"text": query_library.search_text, "theorem_id": query_library.search_theorem,
             "paper_id": query_library.search_paper, "fqn": query_library.search_fqn}

CLOSING_OUTCOMES = ("EXACT", "VIA_BRIDGE")
PROBE_TIMEOUT_DEFAULT = 900


def build_probe_plan(data: dict, samples: dict[str, int],
                     full_goals: bool = False) -> list[dict]:
    """Goal items to probe, in testset order (all of them, or per-category caps)."""
    if full_goals:
        return [i for i in data["items"] if i.get("goal")]
    plan: list[dict] = []
    for category, want in samples.items():
        pool = [i for i in data["items"] if i["category"] == category and i.get("goal")]
        plan.extend(pool[:want])
    return plan


def run_goal_probe(item: dict, timeout: int,
                   index: query_library.LibraryIndex | None = None) -> dict:
    """One real check_goal run; returns the per-item check entry. Never raises and never
    silently drops a failure: every error mode is recorded with a non-null `error` and
    `matches_expected=False`, so the gate fails on it."""
    goal = item["goal"]
    import tempfile
    with tempfile.TemporaryDirectory(prefix="qatlas-eval-") as tmp:
        goal_path = Path(tmp) / "goal.lean"
        goal_path.write_text(goal["text"], encoding="utf-8")
        cmd = [sys.executable, "-B", "-m", "qatlib_search.check_goal", "check",
               "--goal-file", str(goal_path), "--candidate", goal["candidate"],
               "--json", *index_probe_args(index)]
        for bridge in goal.get("bridges") or []:
            cmd += ["--bridge", bridge]
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), text=True, encoding='utf-8', errors='replace', capture_output=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"id": item["id"], "outcome": None, "exit_code": None,
                    "application_closed": None, "axiom_check_passed": None,
                    "log_path": None, "matches_expected": False,
                    "error": f"check_goal timed out after {timeout}s"}
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {"id": item["id"], "outcome": None, "exit_code": proc.returncode,
                    "application_closed": None, "axiom_check_passed": None,
                    "log_path": None, "matches_expected": False,
                    "error": "invalid check_goal report "
                             f"(exit {proc.returncode}): {proc.stdout[:160]!r} "
                             f"{proc.stderr[:160]}"}
    results = report.get("results") or []
    validation = results[0].get("validation") if results else None
    if not isinstance(validation, dict):
        return {"id": item["id"], "outcome": report.get("outcome"),
                "exit_code": proc.returncode, "application_closed": None,
                "axiom_check_passed": None, "log_path": None,
                "matches_expected": False,
                "error": "check_goal report carries no result validation"}
    outcome = report.get("outcome")
    closed = outcome in CLOSING_OUTCOMES
    verified = (validation.get("application_closed") is True
                and validation.get("axiom_check_passed") is True)
    error = None
    if proc.returncode != 0:
        error = f"check_goal failed (exit {proc.returncode}): {proc.stderr[:160]}"
        matches = False
    elif outcome not in (*CLOSING_OUTCOMES, "CONDITIONAL", "RELATED_ONLY", "NO_MATCH"):
        error = f"invalid check_goal outcome: {outcome!r}"
        matches = False
    elif closed and not verified:
        # A closing claim without its actual application/axiom witness is not
        # evidence (a sorry-diagnostic probe can still print outcome=EXACT).
        error = ("closing outcome without application_closed/axiom_check_passed "
                 "witness")
        matches = False
    elif item["category"] == "incompatible":
        matches = not closed  # a negative passes only by genuine rejection
    else:
        matches = outcome == item["expected"] and (not closed or verified)
    return {"id": item["id"], "outcome": outcome,
            "exit_code": (proc.returncode if proc.returncode != 0
                          else validation.get("exit_code", proc.returncode)),
            "application_closed": validation.get("application_closed"),
            "axiom_check_passed": validation.get("axiom_check_passed"),
            "log_path": validation.get("log_path"),
            "matches_expected": matches, "error": error}


def evaluate(index: query_library.LibraryIndex, data: dict, samples: dict[str, int],
             log=lambda msg: print(msg, file=sys.stderr),
             probe_timeout: int = PROBE_TIMEOUT_DEFAULT,
             full_goals: bool = False) -> dict:
    compat_classes = ("direct_reuse", "bridge", "conditional")
    recall_classes = ("direct_reuse", "bridge")  # the 120 with usable candidates
    per_item: list[dict] = []
    confusion = {c: {cls: 0 for cls in (*compat_classes, "incompatible", "miss")}
                 for c in CATEGORIES}
    recall_hits = 0
    recall_total = 0
    false_direct_use = 0
    for item in data["items"]:
        kind = item["query"]["kind"]
        report, _human = SEARCHERS[kind](index, ROOT, item["query"]["text"], 10)
        outcome = report["outcome"]
        top10 = [r["fqn"] for r in report["results"][:10]]
        gold = list(item.get("gold") or [])
        alternates = list(item.get("allow_alternates") or [])
        wanted = set(gold) | set(alternates)
        rank = next((i + 1 for i, fqn in enumerate(top10) if fqn in wanted), None)
        entry = {
            "id": item["id"], "category": item["category"],
            "query": item["query"], "outcome_search": outcome,
            "top10": top10, "gold": gold, "gold_rank": rank,
            "has_goal": bool(item.get("goal")),
            "check": None,
        }
        # fast-path predicted class: the search stage may surface candidates but can
        # never claim direct usability (EXACT only from a real check_goal Lean run).
        if item["category"] in compat_classes:
            if item["category"] in recall_classes:
                recall_total += 1
                if rank is not None and rank <= 10:
                    recall_hits += 1
            ok = rank is not None and rank <= (item.get("accept_rank") or 10)
            predicted = item["category"]
            if not ok:
                predicted = "miss"
            confusion[item["category"]][predicted] += 1
            entry["predicted_class"] = predicted
            entry["category_correct"] = ok
        else:
            # incompatible: two honest subclasses. open-target / absent-vocab queries
            # must retrieve NO_MATCH; mismatch items may surface a distractor, which the
            # protocol already forbids claiming as directly usable.
            if item.get("expect_search") == "NO_MATCH":
                correct = outcome == "NO_MATCH"
            else:
                correct = outcome in ("NO_MATCH", "RELATED_ONLY") and not wanted
            predicted = "incompatible" if correct else "miss"
            confusion["incompatible"][predicted] += 1
            entry["predicted_class"] = predicted
            entry["category_correct"] = correct
        entry["direct_use_claimed"] = False
        per_item.append(entry)

    # slow probes: real check_goal runs make the applicability gate non-trivial. Every
    # planned probe produces an entry; failures stay visible and fail the gate.
    slow_plan = build_probe_plan(data, samples, full_goals=full_goals)
    checks_by_id: dict[str, dict] = {}
    item_by_id = {i["id"]: i for i in data["items"]}
    for item in slow_plan:
        try:
            check_entry = run_goal_probe(item, probe_timeout, index)
        except Exception as error:  # never drop a probe silently
            check_entry = {"id": item["id"], "outcome": None, "exit_code": None,
                           "application_closed": None, "axiom_check_passed": None,
                           "log_path": None, "matches_expected": False,
                           "error": f"probe crashed: {error!r}"}
        checks_by_id[item["id"]] = check_entry
        if check_entry["outcome"] in CLOSING_OUTCOMES \
                and item["category"] in ("conditional", "incompatible"):
            false_direct_use += 1
            check_entry["false_direct_use"] = True
        if check_entry["error"]:
            log(f"probe error for {item['id']}: {check_entry['error']}")
        if not check_entry["matches_expected"]:
            log(f"probe mismatch for {item['id']}: outcome={check_entry['outcome']} "
                f"expected={item['expected']}")
    for entry in per_item:
        if entry["id"] in checks_by_id:
            entry["check"] = checks_by_id[entry["id"]]

    # ---- applicability and retrieval keep separate denominators -----------------
    goal_items = [i for i in data["items"] if i.get("goal")]
    retrieval_only = [i for i in data["items"] if not i.get("goal")]
    goal_by_category = {c: len([i for i in goal_items if i["category"] == c])
                        for c in CATEGORIES}
    retrieval_only_by_category = {c: len([i for i in retrieval_only if i["category"] == c])
                                  for c in CATEGORIES}
    planned_ids = [i["id"] for i in slow_plan]
    missing = [i for i in planned_ids if i not in checks_by_id]
    probe_errors = [{"id": cid, "error": c["error"]}
                    for cid, c in sorted(checks_by_id.items()) if c["error"]]
    probe_mismatches = [{"id": cid, "outcome": c["outcome"],
                          "expected": item_by_id[cid]["expected"]}
                         for cid, c in sorted(checks_by_id.items())
                         if not c["matches_expected"]]
    positive_probes = [c for cid, c in checks_by_id.items()
                       if item_by_id[cid]["category"] != "incompatible"]
    negative_probes = [c for cid, c in checks_by_id.items()
                       if item_by_id[cid]["category"] == "incompatible"]
    applicability = {
        "goal_items_total": len(goal_items),
        "goal_items_by_category": goal_by_category,
        "retrieval_only_items": len(retrieval_only),
        "retrieval_only_by_category": retrieval_only_by_category,
        "probes_planned": len(slow_plan),
        "probes_reported": len(checks_by_id),
        "probes_missing": missing,
        "probe_errors": probe_errors,
        "probe_mismatches": probe_mismatches,
        "positives_probed": len(positive_probes),
        "positives_closed_and_axiom_clean": sum(
            1 for c in positive_probes
            if c["outcome"] in CLOSING_OUTCOMES and c["matches_expected"]),
        "negatives_probed": len(negative_probes),
        "negatives_rejected": sum(1 for c in negative_probes if c["matches_expected"]),
    }

    def accuracy(category: str) -> float:
        rows = [e for e in per_item if e["category"] == category]
        return round(sum(1 for e in rows if e["category_correct"]) / len(rows), 4) if rows else 0.0

    recall = round(recall_hits / recall_total, 4) if recall_total else 0.0
    slow_checked = [e for e in per_item if e["check"]]
    bad_probes = len(missing) + len(probe_errors) + len(probe_mismatches)
    report = {
        "schema_version": "1.1",
        "mode": "lexical/citation retrieval (no lake) + check_goal probes"
                if slow_checked else "lexical/citation retrieval only (no lake)",
        "snapshot": {
            "git_head": query_library.run_git(["rev-parse", "HEAD"], cwd=ROOT),
            "declaration_count": len(index.records) if index is not None else None,
            "testset_categories": data.get("categories"),
        },
        "summary": {
            "items": len(per_item),
            "accuracy_by_category": {c: accuracy(c) for c in CATEGORIES},
            "recall_at_10": {"hits": recall_hits, "total": recall_total, "value": recall},
            "false_direct_use_claims": false_direct_use,
            "check_goal_spot_checks": len(slow_checked),
            "check_goal_matches_expected": sum(1 for e in slow_checked
                                               if e["check"]["matches_expected"]),
        },
        "applicability": applicability,
        "thresholds": {
            "recall_at_10_ge_0.90": {"target": RECALL_THRESHOLD, "measured": recall,
                                     "pass": recall >= RECALL_THRESHOLD},
            "false_direct_use_eq_0": {"target": 0, "measured": false_direct_use,
                                      "pass": false_direct_use == 0},
            "goal_probes_complete": {"target": 0,
                                     "measured": len(missing) + len(probe_errors),
                                     "pass": not missing and not probe_errors},
            "goal_probe_mismatches_eq_0": {"target": 0, "measured": len(probe_mismatches),
                                           "pass": not probe_mismatches},
        },
        "confusion_matrix_rows_gold_cols_predicted": confusion,
        "protocol_note": (
            "EXACT/VIA_BRIDGE can only be produced by a real check_goal Lean run; the "
            "fast path therefore never claims direct usability — its false-direct-use "
            "count is 0 by protocol construction and re-tested on the probed items. "
            "Applicability is measured over the goal items only; retrieval over all "
            f"{len(per_item)} items ({applicability['retrieval_only_items']} of them "
            "retrieval-only)."),
        "items": per_item,
    }
    return report


# --------------------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--generate", action="store_true",
                        help="build and freeze verification/retrieval-testset.json")
    parser.add_argument("--refresh-queries", action="store_true",
                        help="recompute the goal-derived query text of every goal-based "
                             "item in place (gold, goals and verification provenance "
                             "are untouched — the query is a pure function of the goal)")
    parser.add_argument("--force", action="store_true",
                        help="with --generate: overwrite an existing frozen testset")
    parser.add_argument("--verify-limit", type=int, default=200,
                        help="generation: cap on real check_goal verification runs")
    parser.add_argument("--no-verify", action="store_true",
                        help="generation: skip check_goal verification (development only; "
                             "bridge items then fail to freeze)")
    parser.add_argument("--testset", type=Path, default=DEFAULT_TESTSET)
    parser.add_argument("--output", type=Path, default=None,
                        help="write the evaluation report JSON here")
    parser.add_argument("--bridge-sample", type=int, default=None,
                        help="check_goal spot-check count for bridge items (default 5 "
                             "when QATLAS_TEST_SLOW=1, else 0)")
    parser.add_argument("--cond-sample", type=int, default=None)
    parser.add_argument("--incompat-sample", type=int, default=None)
    parser.add_argument("--direct-sample", type=int, default=None)
    parser.add_argument("--full-goals", action="store_true",
                        help="probe every goal item with a real check_goal run (the "
                             "125-goal baseline mode; per-category samples are ignored)")
    parser.add_argument("--probe-timeout", type=int, default=PROBE_TIMEOUT_DEFAULT,
                        help="per-probe check_goal timeout in seconds")
    parser.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    args = parser.parse_args(argv)

    try:
        index = query_library.LibraryIndex(args.index_dir.resolve(),
                                           query_library.DEFAULT_METADATA.resolve())
    except FileNotFoundError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.generate:
        if args.testset.exists() and not args.force:
            print(f"error: {args.testset} already exists (frozen gold); use --force to "
                  f"regenerate", file=sys.stderr)
            return 2
        generator = Generator(index, verify=not args.no_verify,
                              verify_limit=args.verify_limit)
        data = generator.run()
        errors = validate_testset(data, set(index.by_fqn))
        if errors:
            print("testset self-check FAILED:", file=sys.stderr)
            for error in errors[:30]:
                print(f"  {error}", file=sys.stderr)
            return 1
        args.testset.parent.mkdir(parents=True, exist_ok=True)
        args.testset.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        print(f"wrote {args.testset}: 200 items, counts "
              f"{data['categories']}, self-check clean", file=sys.stderr)
        return 0

    if args.refresh_queries:
        if not args.testset.is_file():
            print(f"error: testset {args.testset} not found", file=sys.stderr)
            return 2
        data = json.loads(args.testset.read_text(encoding="utf-8"))
        changed = 0
        for item in data["items"]:
            goal = item.get("goal")
            if item["category"] not in ("bridge", "conditional"):
                continue
            if not goal or item["query"]["kind"] != "text":
                continue
            prop = goal["text"].split("def QatlasGoal.goal : Prop :=", 1)[1].strip()
            new_query = goal_query(prop)
            if new_query and new_query != item["query"]["text"]:
                item["query"]["text"] = new_query
                changed += 1
        errors = validate_testset(data, set(index.by_fqn))
        if errors:
            print(f"error: refreshed testset fails self-check ({errors[0]})",
                  file=sys.stderr)
            return 1
        data["generator"] += " ; queries refreshed in place by --refresh-queries"
        args.testset.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        print(f"refreshed {changed} goal-derived queries in {args.testset}",
              file=sys.stderr)
        return 0

    if not args.testset.is_file():
        print(f"error: testset {args.testset} not found (run --generate first)",
              file=sys.stderr)
        return 2
    data = json.loads(args.testset.read_text(encoding="utf-8"))
    errors = validate_testset(data, set(index.by_fqn))
    if errors:
        print(f"error: frozen testset fails self-check against the current index "
              f"({len(errors)} problems, first: {errors[0]})", file=sys.stderr)
        return 1
    try:
        index.validate_current(ROOT)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    slow = os.environ.get("QATLAS_TEST_SLOW") == "1"
    samples = {
        "bridge": args.bridge_sample if args.bridge_sample is not None else (5 if slow else 0),
        "conditional": args.cond_sample if args.cond_sample is not None else (2 if slow else 0),
        "incompatible": args.incompat_sample if args.incompat_sample is not None else (2 if slow else 0),
        "direct_reuse": args.direct_sample if args.direct_sample is not None else (2 if slow else 0),
    }
    report = evaluate(index, data, samples, probe_timeout=args.probe_timeout,
                      full_goals=args.full_goals)
    # Evaluation can include many long-running Lean checks. Do not publish
    # metrics under a snapshot that changed after initial index admission.
    try:
        index.validate_current(ROOT)
    except ValueError as error:
        print(f"error: evaluation inputs changed; no current report written: {error}",
              file=sys.stderr)
        return 2
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    summary = report["summary"]
    applicability = report["applicability"]
    lines = [
        f"mode: {report['mode']}",
        f"applicability: {applicability['probes_reported']}/"
        f"{applicability['probes_planned']} planned goal probes reported "
        f"({applicability['goal_items_total']} goal items, "
        f"{applicability['retrieval_only_items']} retrieval-only)",
        f"accuracy by category: {summary['accuracy_by_category']}",
        f"Recall@10 (compatible {summary['recall_at_10']['total']} items): "
        f"{summary['recall_at_10']['value']} "
        f"({summary['recall_at_10']['hits']} hits)",
        f"false direct-use claims: {summary['false_direct_use_claims']}",
        f"check_goal probes: {summary['check_goal_spot_checks']} "
        f"({summary['check_goal_matches_expected']} matching expected)",
        f"probe errors: {len(applicability['probe_errors'])}, "
        f"mismatches: {len(applicability['probe_mismatches'])}, "
        f"missing: {len(applicability['probes_missing'])}",
    ]
    print("\n".join(lines))
    failures = {name: gate for name, gate in report["thresholds"].items()
                if not gate["pass"]}
    if failures:
        for name, gate in failures.items():
            print(f"gate FAILED: {name} (measured {gate['measured']}, "
                  f"target {gate['target']})", file=sys.stderr)
        for error in applicability["probe_errors"][:10]:
            print(f"  probe error {error['id']}: {error['error']}", file=sys.stderr)
        for mismatch in applicability["probe_mismatches"][:10]:
            print(f"  probe mismatch {mismatch['id']}: outcome="
                  f"{mismatch['outcome']} expected={mismatch['expected']}",
                  file=sys.stderr)
        print(f"retrieval gate FAILED ({len(failures)} threshold(s)); report: "
              f"{args.output or 'stdout only'}", file=sys.stderr)
        return 1
    print(f"thresholds: {json.dumps(report['thresholds'])}")
    print("retrieval gate passed")
    return 0


if __name__ == "__main__":
    # GBK consoles crash printing Lean/UTF-8 output; never fail on encoding.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
