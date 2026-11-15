#!/usr/bin/env python3
"""B07 applicability check: can an indexed candidate declaration close a concrete Lean goal?

`check --goal-file G.lean --candidate FQN [--bridge F1,F2] [--json]` elaborates the goal
against the prebuilt lake environment and classifies the candidate:

  EXACT        `example : QatlasGoal.goal := by exact <candidate>` compiles — `exact`
               admits no new hypotheses by construction, so this is a closed application.
  VIA_BRIDGE   the `--bridge` theorems, applied as the leading positional explicit
               arguments (`exact <candidate> <bridge1> <bridge2> ...`), close the goal.
               Every bridge must be an indexed *theorem* whose axioms stay inside the
               library's allowed set (checked up front; violations exit 2).
  CONDITIONAL  the conclusion matches once the candidate's explicit premises are
               supplied, but they are not in scope: `apply <candidate>` reports the
               leftover goals (parsed into `missing_premises`), and a sorry-based probe
               (never reported as verified, never emitted in the snippet) confirms the
               conclusion itself unifies. No hypothesis is silently added.
  RELATED_ONLY the candidate is in the library but no instantiation closes the goal
               (type mismatch) — the classic grid/oracle/norm/ancilla/entanglement/
               output-model mismatches land here.
  NO_MATCH     the candidate FQN is not in the enriched index: cross-version/unmigrated
               candidates are never claimed importable (`excluded_declarations` +
               limitation).

Goal-file convention: a trusted local Lean file defining `def QatlasGoal.goal : Prop := …`
(optional `noncomputable` prefix). It may `import` project modules; it must be
definitional (no `theorem`/`lemma`/`example`/`axiom`/`unsafe` declarations, no `sorry`,
no `#eval`-style commands). Module-mode goal files (`module`) are currently
unsupported and exit 2 before probing: the driver audit expects public legacy names.
The goal must also be universe-closed — a universe-polymorphic claim must be
re-quantified or instantiated in the goal file (Lean rejects a declaration body with
universe metavariables). Violations exit 2 with diagnostics before any Lean run.

Probe staging: each probe is one `lake --no-build lean DRIVER -- --trust=0 -DautoImplicit=false` run over
a one-shot driver in `<root>/.qatlas/scratch/check-goal-<id>/` (goal body first, probe
appended; errors on goal-body lines are goal-file errors). Probes run in order
P1 exact → P4 bridge → P2 apply → P3 sorry and stop at the first decisive one. Known direct heartbeat/recursion/kernel resource diagnostics are inconclusive and
exit 2 rather than classifying a mismatch, including when Lean exits 1. On
success the scratch directory is collapsed to a single retained log
(`.qatlas/scratch/check-goal-<id>.log`, the schema demands a non-empty `log_path` for
EXACT/VIA_BRIDGE); on failure the whole directory is kept for diagnosis.

`snapshot.dirty` counts tracked changes and untracked `.lean` sources (they change what
elaborates) but not untracked tooling/artifacts. The report otherwise follows
the package-bundled tools/retrieval-report.schema.json; the schema has no fields for
command/elapsed/scratch, so they ride in `results[].scope.scope_note`.

Why a standalone script instead of a `query_library.py` subcommand: B06 is pure
retrieval (no toolchain needed, no side effects), while this tool shells out to Lean,
writes scratch drivers and stages probes — a different runtime contract and failure
surface. Index/report helpers are reused by importing the tools package.

Stdlib only. Run from the repository root; no network, no docker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search import query_library
from qatlib_search.build_search_index import runtime_environment
from qatlib_search.lean_source import import_header, mask_comments

ROOT = query_library.ROOT
DEFAULT_INDEX = query_library.DEFAULT_INDEX
DEFAULT_METADATA = query_library.DEFAULT_METADATA
ALLOWED_AXIOMS = query_library.ALLOWED_AXIOMS

GOAL_DEF_RE = re.compile(r"^\s*(?:noncomputable\s+)?def\s+QatlasGoal\.goal\b", re.M)
FORBIDDEN_DECL_RE = re.compile(
    r"^\s*(?:private\s+|protected\s+|noncomputable\s+|partial\s+)*"
    r"(theorem|lemma|example|axiom|unsafe)\b", re.M)
FORBIDDEN_COMMAND_RE = re.compile(r"^\s*#(eval|check|print|reduce|exit)\b", re.M)
SORRY_TOKEN_RE = re.compile(r"\bsorry\b")
LEAN_MSG_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*"
    r"(?P<severity>error|warning|information)(?:\([\w.]+\))?:\s*(?P<head>.*)$")
INACCESSIBLE_NAME_RE = re.compile(r"✝")

# Lean 4: CoreM.throwMaxHeartbeat, Init.maxRecDepthErrorMessage and
# Kernel.Exception.toMessageData. Match complete located error *heads*, never
# words in a type, goal, note, warning or arbitrary process output. These known
# resource failures are inconclusive even when Lean exits its usual error code 1.
RESOURCE_ERROR_HEADS = frozenset({
    "maximum recursion depth has been reached",
    "(kernel) deterministic timeout",
    "(kernel) excessive memory consumption detected",
    "(kernel) deep recursion detected",
    "(kernel) interrupted",
})
HEARTBEAT_ERROR_HEAD_RE = re.compile(
    r"\(deterministic\) timeout(?: at `[^`\r\n]+`)?, "
    r"maximum number of heartbeats \([0-9]+\) has been reached")


def is_resource_error(block: dict) -> bool:
    """Recognize the pinned Lean runtime's direct resource diagnostics only."""
    return (block["severity"] == "error"
            and (block["head"] in RESOURCE_ERROR_HEADS
                 or HEARTBEAT_ERROR_HEAD_RE.fullmatch(block["head"]) is not None))


class GoalFileError(Exception):
    """The goal file violates the QatlasGoal convention (exit 2)."""


class ProbeError(Exception):
    """The Lean process did not produce a trustworthy probe result."""


class BridgeError(Exception):
    """A requested bridge is not an indexed, proved theorem (exit 2)."""


# --------------------------------------------------------------------------- goal file


def load_goal_file(path: Path) -> tuple[list[str], str, str]:
    """Validate the goal-file convention and split it into (imports, body, full text).

    The body is the unchanged suffix after the shared header parser; the canonical
    `def QatlasGoal.goal : Prop` must appear (a `namespace QatlasGoal` + `def goal`
    spelling is not accepted — the FQN form keeps validation textual and cheap)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise GoalFileError(f"cannot read goal file {path}: {error}") from error
    try:
        masked = mask_comments(text).lstrip("\ufeff")
        imports, header_end = import_header(text)
    except ValueError as error:
        raise GoalFileError(f"goal file {path}: {error}") from error
    if masked.lstrip("\ufeff").split()[:1] == ["module"]:
        raise GoalFileError(
            f"goal file {path}: module-mode goal files are not supported by this "
            "applicability driver; candidate applicability was not checked")
    if not GOAL_DEF_RE.search(masked):
        raise GoalFileError(
            f"goal file {path} does not define `QatlasGoal.goal`: expected a top-level "
            "`def QatlasGoal.goal : Prop := …' (universe-closed; re-quantify or "
            "instantiate polymorphic claims)")
    if FORBIDDEN_DECL_RE.search(masked):
        raise GoalFileError(
            f"goal file {path} must be definitional: theorem/lemma/example/axiom/unsafe "
            "declarations are not allowed (premises belong inside the Prop)")
    if FORBIDDEN_COMMAND_RE.search(masked):
        raise GoalFileError(f"goal file {path} contains #eval/#check-style commands")
    if re.search(r"\b(run_cmd|run_meta|run_tac|initialize|builtin_initialize|native_decide|implemented_by)\b", masked):
        raise GoalFileError(f"goal file {path} contains executable metaprogramming")
    if SORRY_TOKEN_RE.search(masked):
        raise GoalFileError(f"goal file {path} contains `sorry`")
    body = text[header_end:]
    return imports, body, text


# --------------------------------------------------------------------------- lean output parsing


def parse_lean_messages(output: str) -> list[dict]:
    """Split `lake lean` output into located message blocks.

    Lean prefixes every message with `file:line:col: severity: head`; everything until
    the next such prefix (goals `⊢ …`, `case …`, notes, hints) is that block's body."""
    blocks: list[dict] = []
    current: dict | None = None
    for line in output.splitlines():
        match = LEAN_MSG_RE.match(line)
        if match:
            if current is not None:
                blocks.append(current)
            current = {"line": int(match.group("line")), "col": int(match.group("col")),
                       "severity": match.group("severity"), "head": match.group("head"),
                       "body": []}
        elif current is not None:
            current["body"].append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def parse_unsolved_goals(block: dict) -> list[str]:
    """`error: unsolved goals` blocks → the leftover goals as missing premises.

    Body shape: `case hψ` / `⊢ Normalized Plus` pairs (case label optional)."""
    premises: list[str] = []
    case = ""
    for line in block["body"]:
        stripped = line.strip()
        case_match = re.match(r"^case\s+(\S+)$", stripped)
        if case_match:
            case = case_match.group(1)
            continue
        goal_match = re.match(r"^⊢\s*(.+)$", stripped)
        if goal_match:
            premises.append(f"{case}: {goal_match.group(1)}" if case else goal_match.group(1))
            case = ""
    return premises


def parse_synthesize_failure(block: dict) -> str | None:
    """`don't know how to synthesize …` → one missing (instance) premise."""
    joined = " ".join([block["head"]] + [line.strip() for line in block["body"]])
    match = re.search(r"don't know how to synthesize\s+(?:implicit argument\s+)?(.+)", joined)
    return match.group(1).strip() if match else None


def block_summary(blocks: list[dict], limit: int = 3) -> str:
    parts = []
    for block in blocks[:limit]:
        parts.append(f"{block['head']}" + (f" {' '.join(b.strip() for b in block['body'][:1])}"
                                           if block["body"] else ""))
    return " | ".join(parts)[:400]


# --------------------------------------------------------------------------- probe driver


class ProbeResult:
    def __init__(self, name: str, command: list[str], exit_code: int, output: str,
                 elapsed_ms: int, driver: Path):
        self.name = name
        self.command = command
        self.exit_code = exit_code
        self.output = output
        self.elapsed_ms = elapsed_ms
        self.driver = driver
        self.goal_lines = 0
        self.blocks: list[dict] = parse_lean_messages(output)

    def goal_errors(self) -> list[dict]:
        return [b for b in self.blocks
                if b["severity"] == "error" and b["line"] <= self.goal_lines]

    def probe_blocks(self) -> list[dict]:
        return [b for b in self.blocks if b["line"] > self.goal_lines]

    def probe_ok(self) -> bool:
        return self.exit_code == 0 and not [b for b in self.probe_blocks()
                                            if b["severity"] == "error"]

    def note(self) -> str:
        return (f"probe {self.name}: exit {self.exit_code}, {self.elapsed_ms} ms; "
                f"command: {' '.join(self.command)}")


def run_lean_probe(root: Path, driver: Path) -> tuple[int, str, int]:
    """Run one probe driver; the seam unit tests fake (no Lean needed there)."""
    from qatlib_env.lean_runtime import require_prepared_dependencies, prepared_environment

    # Applicability is against the installed environment, not the lockfile text
    # alone. Never let Lake prepare/update packages as a side effect of probing.
    require_prepared_dependencies(root)
    command = ["lake", "--no-build", "lean", str(driver), "--", "--trust=0", "-DautoImplicit=false", "--root=" + str(driver.parent)]
    started = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=str(root),
                              env=prepared_environment(root, runtime_environment(), driver.parent), text=True,
                              encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
    except FileNotFoundError as error:
        raise SystemExit(f"error: cannot run lake (is the elan toolchain installed?): {error}")
    except subprocess.TimeoutExpired as error:
        partial = error.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        return 124, partial + "\nlean probe timed out after 600s\n", \
            int((time.monotonic() - started) * 1000)
    require_prepared_dependencies(root)
    elapsed = int((time.monotonic() - started) * 1000)
    return proc.returncode, proc.stdout + (proc.stderr or ""), elapsed


GOAL_VALIDATION = """run_cmd Lean.Elab.Command.liftTermElabM do
  let goal ← Lean.getConstInfo `QatlasGoal.goal
  unless (← Lean.Meta.whnfD goal.type).isProp do
    throwError "QATLAS_GOAL_INVALID: goal must be a closed Prop definition"
"""

PROOF_AUDIT = """run_cmd Lean.Elab.Command.liftTermElabM do
  let goal ← Lean.getConstInfo `QatlasGoal.goal
  let proof ← Lean.getConstInfo `QatlasProbe.closed
  -- A universe-polymorphic goal is closed by a theorem whose type IS the goal const
  -- under the theorem's own auto-abstracted level params (u_1 vs u1 are distinct
  -- free params and isDefEq does not alpha-rename them): instantiate positionally.
  let goalLevels := goal.levelParams.map Lean.Level.param
  let proofType := proof.type.instantiateLevelParams proof.levelParams goalLevels
  unless (← Lean.Meta.isDefEq proofType (Lean.Expr.const `QatlasGoal.goal goalLevels)) do
    throwError "QATLAS_NOT_CLOSED: application theorem has additional parameters or premises"
  let axs ← Lean.collectAxioms `QatlasProbe.closed
  let allowed : List Lean.Name := [`propext, `Classical.choice, `Quot.sound]
  unless axs.all (fun ax => allowed.contains ax) do
    throwError "QATLAS_AXIOM_REJECTED: application uses unapproved axioms: {axs}"
  IO.println ("QATLAS_VERIFIED " ++ (Lean.Json.mkObj [
    ("application_closed", Lean.toJson true),
    ("axioms", Lean.toJson (axs.toList.map Lean.Name.toString))]).compress)
"""


def verified_application(result: ProbeResult) -> bool:
    if not result.probe_ok():
        return False
    for line in result.output.splitlines():
        if line.startswith("QATLAS_VERIFIED "):
            try:
                record = json.loads(line[len("QATLAS_VERIFIED "):])
            except ValueError:
                return False
            return (record.get("application_closed") is True and isinstance(record.get("axioms"), list)
                    and set(record["axioms"]) <= ALLOWED_AXIOMS)
    return False


class ProbeStage:
    """Writes one-shot drivers under a scratch dir and runs staged probes."""

    def __init__(self, root: Path, scratch: Path, imports: list[str], goal_body: str,
                 goal_header: str = ""):
        self.root = root
        self.scratch = scratch
        self.imports = list(dict.fromkeys(["Lean", *imports]))
        self.goal_body = goal_body
        self.goal_header = goal_header
        self.log_lines: list[str] = []
        self.results: list[ProbeResult] = []
        scratch.mkdir(parents=True, exist_ok=True)

    def run(self, name: str, probe_text: str) -> ProbeResult:
        # Keep the goal's actual legacy header (including prelude and comments).
        # `imports` here contains only the additional candidate/bridge dependencies.
        header = self.goal_header + ("\n" if self.goal_header else "")
        header += "".join(f"import {module}\n" for module in self.imports)
        driver = self.scratch / f"{name}.lean"
        prefix = header + "\n" + self.goal_body + "\n\n" + GOAL_VALIDATION + "\n"
        driver.write_text(prefix + probe_text + "\n", encoding="utf-8")
        goal_lines = len(prefix.splitlines())
        command = ["lake", "--no-build", "lean", str(driver), "--", "--trust=0", "-DautoImplicit=false", "--root=" + str(driver.parent)]
        exit_code, output, elapsed_ms = run_lean_probe(self.root, driver)
        result = ProbeResult(name, command, exit_code, output, elapsed_ms, driver)
        result.goal_lines = goal_lines
        self.results.append(result)
        self.log_lines.append(f"=== {result.note()} ===\n{output}")
        # Partial diagnostics do not make an interrupted process a completed
        # applicability check. Lean's ordinary elaboration failure exits 1;
        # timeout (124), signals and other launcher failures are infrastructure.
        if exit_code not in (0, 1):
            raise ProbeError(f"Lean probe {name} did not complete (exit {exit_code}): {output[-1000:]}")
        if any(is_resource_error(block) for block in result.blocks):
            raise ProbeError(f"Lean probe {name} exhausted resources or was interrupted "
                             f"(exit {exit_code}); applicability is inconclusive: {output[-1500:]}")
        trust_tags = ("QATLAS_AXIOM_REJECTED", "QATLAS_NOT_CLOSED")
        ordinary_errors = [b for b in result.blocks if b["severity"] == "error"
                           and not any(tag in b["head"] for tag in trust_tags)]
        # Lean inserts a sorry placeholder after an ordinary elaboration error;
        # its subsequent audit error is not an independently closed application.
        if any(tag in output for tag in trust_tags) and not ordinary_errors:
            raise ProbeError(f"untrusted/unclosed application: {output[-1500:]}")
        if exit_code != 0 and not any(b["severity"] == "error" for b in result.blocks):
            raise ProbeError(f"Lean probe {name} failed without a located goal/type diagnostic (exit {exit_code}): {output[-1000:]}")
        return result


def exact_probe(candidate: str) -> str:
    return ("/- probe P1: exact application — `exact` cannot introduce new hypotheses -/\n"
            f"theorem QatlasProbe.closed : QatlasGoal.goal := by\n  exact {candidate}\n" + PROOF_AUDIT)


def bridge_probe(candidate: str, bridges: list[str]) -> str:
    applied = " ".join(bridges)
    return ("/- probe P4: bridges applied as the leading positional explicit arguments -/\n"
            f"theorem QatlasProbe.closed : QatlasGoal.goal := by\n  exact {candidate} {applied}\n" + PROOF_AUDIT)


def apply_probe(candidate: str) -> str:
    return ("/- probe P2: apply diagnostic — leftover goals are the missing premises -/\n"
            f"example : QatlasGoal.goal := by\n  apply {candidate}")


def sorry_probe(candidate: str, binder_names: list[str]) -> str:
    named = " ".join(f"({name} := sorry)" for name in binder_names)
    return ("/- probe P3 (UNVERIFIED by construction): does the conclusion unify once every\n"
            "   explicit premise of the candidate is supplied? Only classifies CONDITIONAL;\n"
            "   the reported snippet never contains sorry. -/\n"
            f"example : QatlasGoal.goal := by\n  exact {candidate} {named}")


def explicit_premise_binders(record: dict) -> list[tuple[str, str]]:
    """Explicit binders of the enriched record (name, type) — candidates for P3."""
    out = []
    for binder in record.get("binders") or []:
        if binder.get("binder_kind") == "explicit":
            name = binder.get("name") or ""
            if name and not INACCESSIBLE_NAME_RE.search(name):
                out.append((name, binder.get("type") or ""))
    return out


# --------------------------------------------------------------------------- snapshot & metadata


def run_git_status(root: Path) -> str | None:
    try:
        proc = subprocess.run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                              cwd=str(root), encoding="utf-8", errors="surrogateescape",
                              capture_output=True)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def snapshot_dirty(root: Path) -> bool:
    """True when tracked files changed or untracked Lean sources exist (either can change
    what the probe elaborates); untracked tooling/artifacts cannot."""
    output = run_git_status(root)
    if output is None:
        return True  # Unknown Git state must not certify a clean snapshot.
    for entry in output.split("\0"):
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        if status == "??":
            if path.endswith(".lean"):
                return True
            continue
        # Any tracked change, including rename/copy, is dirty. Its second NUL
        # path need not be parsed because we return before reaching that entry.
        return True
    return False


def snapshot_block_for(root: Path, index: query_library.LibraryIndex) -> dict:
    head = query_library.run_git(["rev-parse", "HEAD"], cwd=root)
    remote = query_library.run_git(["remote", "get-url", "origin"], cwd=root)
    manifest = root / "lake-manifest.json"
    lockfile = None
    if manifest.is_file():
        from . import index_identity
        lockfile = index_identity.blob(root, "lake-manifest.json")
    if not lockfile:
        lockfile = index.summary.get("lake_manifest_git_blob")
    return {
        "repository": remote or root.name,
        "commit": head or index.summary.get("git_head") or "0" * 40,
        "dirty": snapshot_dirty(root),
        "lean_toolchain": index.summary.get("lean_toolchain")
                          or (root / "lean-toolchain").read_text(encoding="utf-8").strip(),
        "lockfile_git_blob": lockfile,
        "import_modules": index.summary.get("modules")
                          or sorted({r.get("module", "") for r in index.records}),
    }


def upstream_hit(metadata_dir: Path | None, fqn: str) -> dict | None:
    """Is this FQN a known upstream (non-library) declaration in upstream-relations?"""
    if not metadata_dir:
        return None
    path = metadata_dir / "upstream-relations.json"
    if not path.is_file():
        return None
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("entries", [])
    except (OSError, json.JSONDecodeError):
        return None
    for entry in entries:
        for cand in entry.get("upstream_candidates", []):
            decl = cand.get("decl") or ""
            if decl and (decl == fqn or fqn.endswith("." + decl)):
                return {"pkg": cand.get("pkg", "upstream"), "decl": decl,
                        "local_fqn": entry.get("local_fqn")}
    return None


# --------------------------------------------------------------------------- report


def check_result_entry(index: query_library.LibraryIndex, root: Path, record: dict,
                       status: str, *, missing_premises: list[str], bridges: list[dict],
                       remaining_goals: list[str], snippet: str | None,
                       validation: dict, scope_note: str) -> dict:
    entry = query_library.result_entry(index, root, record, "applicability_check", 1, 0.0)
    labels = record.get("model_labels") or {}
    entry["status"] = status
    entry["scope"] = {
        "model": json.dumps(labels, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")),
        "error_metric": "defeq applicability check (B07): exact application against "
                        "QatlasGoal.goal in the prebuilt lake environment",
        "cost_model": "|".join(labels.get("cost_unit") or ["unknown"]),
        "scope_note": scope_note,
    }
    entry["missing_premises"] = missing_premises
    entry["bridges"] = bridges
    entry["remaining_goals"] = remaining_goals
    entry["application_snippet"] = snippet
    entry["validation"] = validation
    return entry


def bridge_entries(index: query_library.LibraryIndex, bridge_fqns: list[str],
                   applied: bool) -> list[dict]:
    entries = []
    for fqn in bridge_fqns:
        record = index.by_fqn[fqn]
        entries.append({
            "fqn": fqn,
            "module": record.get("module", ""),
            "application_checked": applied,
            "cost_and_error_change": "propositional premise only: supplying this proved "
                                     "theorem changes neither the cost nor the error "
                                     "accounting of the candidate application",
        })
    return entries


# --------------------------------------------------------------------------- the check


def run_check(index: query_library.LibraryIndex, root: Path, goal_path: Path,
              candidate_fqn: str, bridge_fqns: list[str], keep_scratch: bool,
              human: list[str]) -> tuple[dict, list[str]]:
    record = index.by_fqn.get(candidate_fqn)
    goal_imports, goal_body, goal_text = load_goal_file(goal_path)
    goal_header = goal_text[:len(goal_text) - len(goal_body)]
    goal_fingerprint = hashlib.sha256(goal_text.encode("utf-8")).hexdigest()

    limitations = [
        "applicability = Lean defeq elaboration against the prebuilt lake environment "
        "(`lake --no-build lean DRIVER -- --trust=0 -DautoImplicit=false`); it says nothing about paper "
        "fidelity (independent semantic review still required)",
    ]

    if record is None:
        scratch = root / ".qatlas/scratch" / ("check-goal-" + uuid.uuid4().hex[:16])
        stage = ProbeStage(root, scratch, [], goal_body, goal_header)
        try:
            p0 = stage.run("p0-goal", "-- Candidate is outside the admitted index; validate the goal only.")
            if p0.goal_errors() or not p0.probe_ok():
                raise GoalFileError(f"goal file does not elaborate ({goal_path}): {p0.output[-1500:]}")
        finally:
            (scratch / "log.txt").write_text("\n".join(stage.log_lines), encoding="utf-8")
        limitations.append("goal elaborated by a real Lean probe; candidate applicability not checked; log: " + str(scratch / "log.txt"))
        hit = upstream_hit(index.metadata_dir, candidate_fqn)
        if hit:
            limitations.append(
                f"candidate {candidate_fqn} is the {hit['pkg']} upstream declaration "
                f"`{hit['decl']}` (upstream-relations, near local {hit['local_fqn']}); "
                "跨版本候选未迁移，未纳入当前索引；元数据不证明可导入性")
        else:
            limitations.append(
                f"candidate {candidate_fqn} is not in the enriched index "
                f"({len(index.records)} declarations); 跨版本/拼写候选未迁移，未纳入当前索引；不推断上游不存在")
        report = {
            "schema_version": "1.0",
            "outcome": "NO_MATCH",
            "snapshot": snapshot_block_for(root, index),
            "query": {"kind": "lean_goal", "text": f"{goal_path} :: {candidate_fqn}",
                      "paper_id": None, "goal_fingerprint": goal_fingerprint},
            "search_scope": {
                "searched_modules": sorted(set(goal_imports)),
                "excluded_declarations": [candidate_fqn],
                "upstream_snapshots": query_library.upstream_snapshots(index.metadata_dir),
                "limitations": limitations,
            },
            "results": [],
            "next_obligations": [
                f"migrate or alias {candidate_fqn} into the tracked library (V2/V3 per "
                "AGENTS.md) before an applicability check can run"],
        }
        human.append(f"candidate {candidate_fqn} not indexed: excluded as cross-version "
                     f"(upstream-relations hit: {bool(hit)})")
        return report, human

    bridge_records = []
    for fqn in bridge_fqns:
        bridge = index.by_fqn.get(fqn)
        if bridge is None:
            raise BridgeError(f"bridge {fqn} is not in the enriched index — bridges must "
                              "be real, proved library theorems")
        if bridge.get("kind") != "theorem":
            raise BridgeError(f"bridge {fqn} has kind {bridge.get('kind')!r} — bridges "
                              "must be theorems (unproved definitions do not bridge)")
        extra = set(bridge.get("axioms") or []) - ALLOWED_AXIOMS
        if extra:
            raise BridgeError(f"bridge {fqn} depends on axioms outside the allowed set: "
                              f"{sorted(extra)}")
        bridge_records.append(bridge)

    run_id = hashlib.sha256((goal_fingerprint + candidate_fqn).encode()).hexdigest()[:8] + "-" + uuid.uuid4().hex[:12]
    scratch_dir = root / ".qatlas" / "scratch" / f"check-goal-{run_id}"
    if scratch_dir.exists():
        shutil.rmtree(scratch_dir)
    additional_imports = [record.get("module", ""),
                          *[b.get("module", "") for b in bridge_records]]
    imports = [*goal_imports, *additional_imports]
    stage = ProbeStage(root, scratch_dir, [m for m in additional_imports if m],
                       goal_body, goal_header)

    outcome = "RELATED_ONLY"
    missing_premises: list[str] = []
    remaining_goals: list[str] = []
    snippet: str | None = None
    decisive: ProbeResult | None = None
    bridges_out: list[dict] = []
    note_bits: list[str] = []

    def dump_scratch_log() -> None:
        """Failure path: keep the whole scratch directory plus its log for diagnosis."""
        (scratch_dir / "log.txt").write_text("\n".join(stage.log_lines), encoding="utf-8")

    try:
        # P1 — exact application.
        p1 = stage.run("p1-exact", exact_probe(candidate_fqn))
        if p1.goal_errors():
            dump_scratch_log()
            raise GoalFileError(
                f"goal file does not elaborate ({goal_path}):\n"
                + "\n".join(f"  {b['head']}" for b in p1.goal_errors()))
        if p1.probe_ok() and not verified_application(p1):
            raise ProbeError("P1 exited zero without its actual application/axiom witness")
        if verified_application(p1):
            outcome, decisive, snippet = "EXACT", p1, f"by\n  exact {candidate_fqn}"
            if bridge_fqns:
                limitations.append(
                    "bridges were supplied but not needed: the candidate closed the goal "
                    "directly, so the report carries no bridge obligations")
        else:
            note_bits.append(f"P1 failed: {block_summary(p1.probe_blocks(), 1)}")

            # P4 — bridge application.
            if bridge_fqns:
                p4 = stage.run("p4-bridge", bridge_probe(candidate_fqn, bridge_fqns))
                if p4.goal_errors():
                    dump_scratch_log()
                    raise GoalFileError(f"goal file does not elaborate ({goal_path})")
                if p4.probe_ok() and not verified_application(p4):
                    raise ProbeError("P4 exited zero without its actual application/axiom witness")
                if verified_application(p4):
                    outcome = "VIA_BRIDGE"
                    decisive = p4
                    snippet = (f"by\n  exact {candidate_fqn} " + " ".join(bridge_fqns))
                else:
                    note_bits.append(f"P4 bridge failed: {block_summary(p4.probe_blocks(), 1)}")

            if outcome == "RELATED_ONLY":
                # P2 — apply diagnostic for missing premises.
                p2 = stage.run("p2-apply", apply_probe(candidate_fqn))
                if p2.goal_errors():
                    dump_scratch_log()
                    raise GoalFileError(f"goal file does not elaborate ({goal_path})")
                if p2.probe_ok():
                    closed = stage.run("p2-closed", "theorem QatlasProbe.closed : QatlasGoal.goal := by\n  apply " + candidate_fqn + "\n" + PROOF_AUDIT)
                    if not verified_application(closed):
                        raise ProbeError("closed apply diagnostic failed actual application/axiom replay")
                    outcome, decisive, snippet = "EXACT", closed, "by\n  apply " + candidate_fqn
                for block in p2.probe_blocks():
                    if block["severity"] != "error":
                        continue
                    if block["head"].startswith("unsolved goals"):
                        missing_premises.extend(parse_unsolved_goals(block))
                    synthesized = parse_synthesize_failure(block)
                    if synthesized:
                        missing_premises.append(synthesized)
                note_bits.append(f"P2 apply: {block_summary(p2.probe_blocks(), 1)}")

                # P3 — sorry-probe: conclusion unifiable once premises are supplied?
                binders = explicit_premise_binders(record)
                p3 = None
                if outcome == "RELATED_ONLY" and binders:
                    p3 = stage.run("p3-sorry",
                                   sorry_probe(candidate_fqn, [n for n, _t in binders]))
                    if p3.goal_errors():
                        dump_scratch_log()
                        raise GoalFileError(f"goal file does not elaborate ({goal_path})")
                    if p3.probe_ok():
                        outcome = "CONDITIONAL"
                        decisive = p3
                if outcome == "CONDITIONAL":
                    if not missing_premises:
                        missing_premises = [f"{name} : {btype}" for name, btype in binders]
                        limitations.append(
                            "missing_premises listed from the enriched index binders "
                            "(the apply diagnostic produced no leftover-goal list)")
                    limitations.append(
                        "CONDITIONAL classification rests on a sorry-based probe: the "
                        "conclusion unifies once the explicit premises are supplied, but "
                        "the premises themselves are UNVERIFIED and application_snippet "
                        "is unverified (and contains no sorry)")
                    snippet = (f"by\n  exact {candidate_fqn} "
                               + " ".join(f"MISSING({name} : {btype})"
                                          for name, btype in binders))
                elif outcome == "RELATED_ONLY":
                    if binders:
                        remaining_goals.append(
                            "candidate requires explicit premises not provided by the "
                            "goal: "
                            + "; ".join(f"{n} : {t}" for n, t in binders))
                    remaining_goals.append(
                        "conclusion shape mismatch — no instantiation of the candidate "
                        "closes the goal (grid/oracle/norm/ancilla/entanglement/output-"
                        "model mismatch class)")

        # ---- validation + scope bookkeeping
        if decisive is None:
            decisive = stage.results[-1]
        elaborated = not any(r.goal_errors() for r in stage.results)
        axiom_ok = (outcome in ("EXACT", "VIA_BRIDGE") and verified_application(decisive)
                    and set(record.get("axioms") or []) <= ALLOWED_AXIOMS
                    and all(set(b.get("axioms") or []) <= ALLOWED_AXIOMS for b in bridge_records))
        if outcome == "VIA_BRIDGE":
            bridges_out = bridge_entries(index, bridge_fqns, applied=True)
        elif bridge_fqns:
            bridges_out = bridge_entries(index, bridge_fqns, applied=False)
        application_closed = outcome in ("EXACT", "VIA_BRIDGE")
        note_bits.append(decisive.note())
        note_bits.append(f"scratch: {scratch_dir}")
        scope_note = "; ".join(note_bits)

        validation = {
            "elaborated": elaborated,
            "application_closed": application_closed,
            "exit_code": decisive.exit_code,
            "axiom_check_passed": axiom_ok,
            "log_path": None,
        }

        entry = check_result_entry(
            index, root, record, outcome,
            missing_premises=missing_premises, bridges=bridges_out,
            remaining_goals=remaining_goals, snippet=snippet,
            validation=validation, scope_note=scope_note)

        obligations = {
            "EXACT": ["record an independent semantic review (semantic_review is "
                      "not_reviewed): a closed application can still be the wrong "
                      "statement for the paper"],
            "VIA_BRIDGE": ["record an independent semantic review of the bridged "
                           "application, including each bridge's cost/error accounting"],
            "CONDITIONAL": [f"supply or prove the missing premises: {p}"
                            for p in missing_premises]
                           + ["re-run with --bridge once a premise has a proved theorem"],
            "RELATED_ONLY": remaining_goals or
                            ["the candidate does not close this goal under any "
                             "instantiation; search for a different declaration"],
        }[outcome]

        report = {
            "schema_version": "1.0",
            "outcome": outcome,
            "snapshot": snapshot_block_for(root, index),
            "query": {"kind": "lean_goal",
                      "text": f"{goal_path} :: {candidate_fqn}"
                              + (f" :: bridges {','.join(bridge_fqns)}" if bridge_fqns else ""),
                      "paper_id": record.get("paper"),
                      "goal_fingerprint": goal_fingerprint},
            "search_scope": {
                "searched_modules": sorted(set(m for m in imports if m)),
                "excluded_declarations": [],
                "upstream_snapshots": query_library.upstream_snapshots(index.metadata_dir),
                "limitations": limitations,
            },
            "results": [entry],
            "next_obligations": obligations,
        }

        # ---- scratch policy: collapse to one log on success, keep everything on failure
        if application_closed and snapshot_dirty(root):
            limitations.append(
                "snapshot is dirty: the retrieval-report schema requires a clean tree "
                "(dirty=false) for EXACT/VIA_BRIDGE claims — commit the tree or re-run "
                "from a clean checkout for a schema-valid report")
        log_path = root / ".qatlas" / "scratch" / f"check-goal-{run_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"goal: {goal_path}\ncandidate: {candidate_fqn}\nbridges: {bridge_fqns}\n\n"
            + "\n".join(stage.log_lines), encoding="utf-8")
        if not keep_scratch:
            shutil.rmtree(scratch_dir, ignore_errors=True)
        entry["validation"]["log_path"] = str(log_path)

        human.append(f"candidate {candidate_fqn} ({record.get('kind')}, "
                     f"{record.get('module')})")
        if missing_premises:
            human.append("missing premises: " + "; ".join(missing_premises))
        if bridges_out:
            human.append("bridges: " + ", ".join(
                f"{b['fqn']} (applied={b['application_checked']})" for b in bridges_out))
        if remaining_goals:
            human.append("remaining: " + remaining_goals[0])
        human.append(f"log: {log_path}")
        return report, human
    except Exception:
        # Any unexpected failure keeps the scratch directory for diagnosis.
        dump_scratch_log()
        raise


def human_summary(report: dict, extra: list[str]) -> str:
    lines = [f"outcome: {report['outcome']}"] + [f"  {line}" for line in extra]
    snapshot = report["snapshot"]
    lines.append(f"snapshot: {snapshot['repository']} @ {snapshot['commit'][:12]}"
                 f" (dirty={snapshot['dirty']}, {snapshot['lean_toolchain']})")
    for limitation in report["search_scope"]["limitations"]:
        lines.append(f"limitation: {limitation}")
    for obligation in report["next_obligations"]:
        lines.append(f"next: {obligation}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    child = sub.add_parser("check", help="classify one candidate against a goal file")
    child.add_argument("--goal-file", type=Path, required=True,
                       help="Lean file defining `QatlasGoal.goal : Prop` (see docstring)")
    child.add_argument("--candidate", required=True, help="candidate declaration FQN")
    child.add_argument("--bridge", action="append", default=[],
                       help="bridge theorem FQN (repeat the flag or comma-separate); "
                            "applied as the candidate's leading explicit arguments")
    child.add_argument("--json", action="store_true", help="emit the schema report JSON")
    child.add_argument("--keep-scratch", action="store_true",
                       help="keep the probe scratch directory even on success")
    child.add_argument("--index-dir", type=Path, default=DEFAULT_INDEX)
    child.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA)
    child.add_argument("--root", type=Path, default=ROOT,
                       help="repository root used for lake/git/scratch (tests, clean "
                            "checkouts)")
    args = parser.parse_args(argv)

    try:
        index = query_library.LibraryIndex(args.index_dir.resolve(),
                                           args.metadata_dir.resolve()
                                           if args.metadata_dir else None)
        index.validate_current(args.root.resolve())
        if snapshot_dirty(args.root.resolve()):
            raise ValueError("applicability checks require a clean committed source snapshot")
    except (FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    bridge_fqns = [fqn.strip() for chunk in args.bridge for fqn in chunk.split(",") if fqn.strip()]
    human: list[str] = []
    try:
        report, human = run_check(index, args.root.resolve(), args.goal_file.resolve(),
                                  args.candidate, bridge_fqns, args.keep_scratch, human)
        # A successful probe cannot authenticate a different checkout/lock that
        # appeared while the multi-stage check was running.
        index.validate_current(args.root.resolve())
        if snapshot_dirty(args.root.resolve()):
            raise ValueError("applicability inputs changed during probing; retry from a clean snapshot")
    except (GoalFileError, BridgeError, ProbeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(human_summary(report, human))
    return 0


if __name__ == "__main__":
    # GBK consoles crash printing Lean/UTF-8 output; never fail on encoding.
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
