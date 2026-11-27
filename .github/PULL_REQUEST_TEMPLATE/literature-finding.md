## Candidate record

Add one JSON file per finding and record its path and stable ID here. Use the [candidate schema](../../src/qatlib_search/literature_finding.schema.json) and run `qatlib-retrieve validate-literature-finding RECORD.json`.

## Source and evidence

- Paper identifier, precise source version, public URL and SHA-256
- Equation, theorem or lemma label in that version
- Reproducible counterexample or repair, with public evidence and any relevant Lean declarations
- Affected versions, any published revision, and the scope of the finding

## Review gates

- **Logic kernel** — Give the Lean source commit, toolchain, build target and transitive axiom check. Identify any checks that are pending.
- **Source alignment** — Explain how the evidence traces to the named version and locator. Cite a separate source for each edition discussed.
- **Scientific semantics** — Describe the model, assumptions, coverage and any remaining review questions. State how the example violates the printed statement or how the repair changes it.

## Verification

Record the validation command and its result. Maintainers check the contribution under a trusted Lean build and review its source alignment and scientific meaning before admitting it to a public snapshot.
