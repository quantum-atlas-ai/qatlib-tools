# Lean Proof Verification and Axiom Auditing

Lean's trustworthiness rests on a strict architectural separation between *proof search* and *kernel checking*: tactics, metaprograms, and AI search loops are free to explore and misfire, because the kernel ultimately checks only the submitted declaration and proof term against the typing judgment `Γ ⊢ p : P`. Yet a green build is only part of engineering acceptance—reviewers must also verify that the type `P` faithfully captures the intended mathematical statement, and account for every axiom, precompiled artifact, and checker binary in the trusted computing base.

## Five Common Skepticisms and Verification Boundaries

Debates around large formalization efforts and AI-generated proofs often blur what the Lean kernel enforces automatically with what specification review must establish separately. The table below maps five recurring concerns onto Lean's underlying mechanics, concrete engineering gates, and the exact scope of each guarantee:

| Skepticism or Risk Scenario | Underlying Lean Mechanism & Boundary | Engineering Controls & Gates | What Is Guaranteed—and What Is Not |
| --- | --- | --- | --- |
| **AI hallucinations & hidden placeholders**: AI models hallucinate constantly; if grep finds no `sorry`, could an unfinished proof still slip through? | Proof generation is decoupled from kernel checking, so intermediate hallucinations that fail to form a well-typed proof term are rejected. However, plain-text search misses `sorryAx`, custom `axiom`s, or compiler-trusting `native_decide` axioms pulled in transitively across modules. | Enumerate exported declarations in `Environment.constants` by owning module, collect transitive axioms across types and proof bodies against the three-axiom classical allowlist, and inspect `unsafe`, `@[implemented_by]`, and `@[extern]` separately. | Certifies the full transitive axiom footprint and audited runtime hooks; raw source-text grep only checks surface tokens and never substitutes for an environment-level audit. |
| **Specification drift & vacuous truth**: Every inference step typechecks, but could a definition be altered, hidden hypotheses smuggled in, or the domain collapse to the empty set? | The kernel verifies that the proof term inhabits the written type—not that the written type matches the intended problem. Redefined concepts, overly strong or contradictory `section` / `variable` assumptions, and empty domains all pass axiom checks cleanly. | Pin upstream commits and artifacts, audit full exported type signatures, lock target definitions via an external `module` consumer probe and `lake comparator`, flag dead hypotheses via environment lints, and supply a concrete satisfiability witness. | Establishes the theorem strictly under its stated definitions and interface; confirming that definitions, quantifiers, and premises faithfully model the mathematical question remains the job of specification review and witness checks. |
| **Kernel implementation bugs**: Dependent type theory is consistent on paper, but what if the C++ kernel binary itself has a software bug? | Logical soundness of the calculus does not make a single software implementation bug-free—for example, metaprogram-generated nested inductive declarations historically exposed missing free-variable and metavariable checks in the kernel. | Pin a patched toolchain, replay compiled environments from scratch with `leanchecker --fresh`, and cross-validate exported declarations across independent checkers implemented in distinct codebases (`lean4lean`, `nanoda`, `con-leche`). | Consensus across independent checker implementations drives the probability of a shared soundness bug down dramatically; the export pipeline, shared runtimes, and problem statement still belong to the broader review scope. |
| **Degenerate intermediate helpers or `rfl` steps**: If internal helper predicates collapse to `True` or intermediate equalities use `rfl`, does that invalidate the final theorem? | Corrupting the *endpoint* statement breaks a result, whereas proving a trivial helper `True` still leaves the full burden of proving `True → R` for a locked target `R`; uninstantiable premises cannot discharge hypotheses, and `rfl` succeeds only under true definitional equality. | For a single-theorem target, lock the endpoint statement and its transitive axioms. For a reusable library, where every public lemma is itself a downstream contract, audit exported types, non-degeneracy, and concrete call sites module by module. | Once the endpoint signature is locked and kernel replay passes, clumsy or degenerate internal scaffolding cannot falsify the final theorem—though reusable libraries still require per-lemma interface review. |
| **Verified code vs. accompanying paper prose**: Nobody has read hundreds of thousands of lines of tactic scripts, or the companion PDF has a flawed informal step—does the formal proof still stand? | Mathematical validity of the formal theorem is established by kernel verification of the proof term against its statement, without waiting for humans to digest every tactic line; conversely, proving theorem `T` in Lean never automatically certifies informal arguments or stronger constants in a companion paper. | Maintain an explicit claim-by-claim mapping between paper statements and Lean declarations, recording exact equivalence, narrowed hypotheses, constant or asymptotic differences, and unformalized corollaries. | Every mapped formal declaration can be verified independently; any prose claim in a manuscript that goes beyond or diverges from the formal statement requires its own theorem or separate mathematical review. |

> Historical context: Lean's official kernel previously merged a dedicated soundness fix for free-variable and metavariable checks in nested inductive declarations constructed via metaprogramming. High-assurance reviews should align source commits, dependency manifests, and precompiled artifacts, and record the exact versions of both the toolchain and all external checkers. [Lean kernel fix #14607](https://github.com/leanprover/lean4/pull/14607) · [Lean 4.35 release notes](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/)

## Environment-Level Transitive Axiom and Zero-`sorry` Auditing

Before running an audit, pin `lean-toolchain`, `lake-manifest.json`, and the exact source commit under review, and record the provenance of both target modules and dependency artifacts. During a controlled build, `lake build --wfail` promotes compiler warnings in freshly built modules to hard errors; however, incremental builds do not re-emit historical source warnings inside cached upstream artifacts, so acceptance gates must inspect the compiled `Environment` directly. For artifact layouts and single-file invocations, see [Build and cache behavior](build.md).

While `#print axioms Some.theoremName` is handy for interactive spot checks, an automated CI gate should iterate over `Environment.constants`, filter declarations by their owning module index, and run `Lean.collectAxioms` from every root declaration. Axiom collection traverses the full transitive closure of constant types and proof terms—including private helper lemmas. Under Lean 4.35's `module` system, cross-module references carry precomputed transitive axiom summaries in the imported `.olean` artifacts, allowing `collectAxioms` to surface axioms used inside an imported theorem's hidden proof body even across an ordinary `import`. Because summaries rely on trustworthy upstream artifacts, high-stakes gates pair axiom auditing with fresh kernel replay below. [Lean 4.35 axiom collection implementation](https://github.com/leanprover/lean4/blob/v4.35.0/src/Lean/Util/CollectAxioms.lean)

For standard classical mathematics, the allowlist consists solely of `propext`, `Classical.choice`, and `Quot.sound` (individual declarations may depend on any subset). Every explicit `sorry` or compiler-synthesized placeholder resolves to `sorryAx`; any axiom outside the three-element allowlist fails the gate immediately. In Lean 4.35, `native_decide` and `decide +native` emit a fresh per-invocation axiom, making explicit that the compiler and native runtime have been added to the trusted computing base (the legacy fixed names `Lean.ofReduceBool` and `Lean.trustCompiler` were removed in 4.35; keeping them in mind helps diagnose older artifacts, while strict allowlisting rejects both old and new native-evaluation axioms automatically). [Lean 4.35 release notes](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/) · [Native evaluation implementation](https://github.com/leanprover/lean4/blob/v4.35.0/src/Lean/Elab/Tactic/Decide.lean)

`unsafe` declarations and the `@[implemented_by]` / `@[extern]` runtime replacement attributes require a dedicated check alongside axiom collection. Safe kernel proofs cannot reference `unsafe` definitions directly, and runtime overrides do not alter logical bodies seen by the kernel—yet whenever exported definitions also serve as executable interfaces, gates must inspect safety flags and foreign/runtime overrides explicitly. If upstream overrides must also be constrained, traverse type, value, and compiler-replacement edges across all loaded artifacts and maintain an explicit allowlist of audited declaration names. The minimal gate below enforces the three-axiom allowlist transitively across all dependencies while rejecting any `unsafe` or runtime-overridden declaration inside the listed project modules.

### Minimal Runnable Audit Example

In an empty directory with `lean-toolchain` pinned to `leanprover/lean4:v4.35.0`, save the following `lakefile.toml`:

```toml
name = "verification-example"
defaultTargets = ["ExampleLib"]

[[lean_lib]]
name = "ExampleLib"
```

Save the target library module as `ExampleLib.lean`:

```lean
module

@[expose] public def step (n : Nat) : Nat := n + 1

public theorem step_pos (n : Nat) : 0 < step n := by
  exact Nat.zero_lt_succ n
```

Save the environment audit driver as `Audit.lean`. Omitting `module` at the top of this driver allows it to inspect private declarations owned by the audited project modules; in production repositories, also import any standalone files omitted from the umbrella module and distinguish pure re-exporting umbrella files from modules expected to own declarations:

```lean
import Lean
import ExampleLib

open Lean Elab Command

run_cmd liftCoreM do
  let env ← getEnv
  let allowed := #[`propext, `Classical.choice, `Quot.sound]
  let modules := #[`ExampleLib]  -- List all audited modules and import them above.
  let mut covered : Std.HashSet Name := {}
  let mut checked := 0
  for (name, info) in env.constants.toList do
    let some idx := env.getModuleIdxFor? name | continue
    let owner := env.header.modules[idx.toNat]!.module
    unless modules.contains owner do continue
    covered := covered.insert owner
    checked := checked + 1
    for ax in ← collectAxioms name do
      if ax == `sorryAx then
        throwError "{name}: depends on sorryAx"
      unless allowed.contains ax do
        throwError "{name}: unexpected axiom {ax}"
    if info.isUnsafe || (Compiler.getImplementedBy? env name).isSome ||
        (getExternAttrData? env name).isSome then
      throwError "{name}: unsafe declaration or runtime override"
  for owner in modules do
    unless covered.contains owner do
      throwError "no declarations audited from {owner}"
  IO.println s!"AXIOM_AUDIT_OK declarations={checked}"
```

Build with warnings-as-errors and run the audit without rebuilding:

```sh
lake build --wfail
lake --no-build lean Audit.lean
```

CI pipelines should gate strictly on the process exit code while logging the audited module list, total declaration count, and the first offending constant name; if `SORRY_FREE_OK` and `AXIOM_AUDIT_OK` markers are emitted separately, print each only after its respective pass completes. Validate the gate itself against negative fixtures—including a private cross-file `sorry`, a custom `axiom`, a `native_decide` proof, and an `@[implemented_by]` override—to confirm every violation exits nonzero.

## Declaration Interface Lints and External Consumer Probes

A clean axiom list proves no extra axioms were assumed, not that exported interfaces are usable or non-vacuous. Pair axiom checks with declaration-level lints and external consumer probes in `lake lint` and `lake test`:

- **Import, style, and environment hypothesis lints**: Wire import-hierarchy, style, and duplicate-declaration checks into your `lake lint` driver (Lean 4.35 also supports `lake lint --builtin-only ExampleLib`, and Batteries-based projects can enable `unusedArguments`, `simpNF`, and `dupNamespace`). Post-build environment lints scoped by owning module catch exported conclusions literally equal to `True`, explicit `False` premises, and dead parameters or typeclass binders dragged in unintentionally via `section`, `variable`, or `include`. Because syntactic checks catch only overt degeneracies, core theorems with rich hypotheses should also carry concrete satisfiability witnesses demonstrating that all premises can hold simultaneously.
- **Umbrella collision checks & ordinary `module` consumer probes**: Configure `lake test` to import all top-level umbrella modules into a single environment—surfacing public name collisions across sibling modules—and compile a standalone consumer outside the library using an ordinary `module` header and `import`. In Lean 4.35, `public` exports the declaration name while `@[expose]` is required before downstream modules can unfold a definition body; checking `#check` alone misses missing-`@[expose]` regressions. Save the probe below as `Consumer.lean` and run `lake --no-build lean Consumer.lean`:

```lean
module
import ExampleLib

#check @step_pos
example : ∀ n : Nat, 0 < n + 1 := @step_pos
example : step 0 = 1 := rfl
#print axioms step_pos
```

When verifying a benchmark problem or conjecture, keep the reference definitions and target signature in an independently pinned challenge package outside the candidate branch's write scope, and run `lake comparator` so a submission cannot pass by subtly altering definitions or aliases. [Lean 4.35 module system](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/) · [Lean 4.35 release notes](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/)

## Independent Kernel Replay and Multi-Checker Cross-Validation

Incremental builds decide *what needs recompilation*; kernel replay bypasses tactic execution, macro expansion, and incremental build caches altogether, loading compiled declarations from `.olean` files into a clean environment to re-verify every constant from scratch. Build from pinned inputs first and ensure all target and dependency `.olean` files reside on the checker's search path (restoring cached artifacts into the search path first when Lake's lazy artifact cache left them materialized only in the shared cache):

```sh
lake env leanchecker --fresh ExampleLib
lake check --paranoid
```

`lake env leanchecker --fresh ExampleLib` invokes the official checker binary bundled with Lean 4.35 to replay the module and its full import graph from an empty environment (`lake exe leanchecker` applies only when a workspace explicitly defines a Lake executable target by that name). Because kernel replay treats declared `axiom`s as valid environment assumptions, always run it alongside—never in place of—the allowlist axiom audit above. [Lean 4.35 checker source](https://github.com/leanprover/lean4/blob/v4.35.0/src/LeanChecker.lean)

On Linux hosts with Bubblewrap support, `lake check --paranoid` builds the default targets, exports kernel-level declarations, and replays them through both the official kernel and bundled checkers. For high-assurance releases, feed the same NDJSON export into [`lean4lean`](https://arxiv.org/abs/2403.14064) (a Lean reimplementation of the kernel) as well as independent external checkers such as [`nanoda`](https://github.com/ammkrn/nanoda_lib) and [`con-leche`](https://github.com/leanprover/con-leche), archiving the export digest, checker versions, and individual verdicts together. When invoking `lake check --from-export FILE --paranoid` on a standalone export file, remember that the verdict applies strictly to that export; linking it back to a source commit requires preserving provenance for the build and export step as well. [Lean 4.35 release notes](https://lean-lang.org/doc/reference/4.35.0/releases/v4.35.0/)
