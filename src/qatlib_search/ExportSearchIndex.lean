import Lean
open Lean Meta

-- 完整模块列表可含数千项；这里只调整索引驱动的 elaboration 资源上限。
set_option maxRecDepth 100000

/-! qatlas search-index exporter — one NDJSON line (`.compress`) per declaration whose defining
module belongs to a tracked project library. `src/qatlib_search/build_search_index.py` strips this file's
header and generates a driver that imports the full tracked module set (not only the wide root).

Record fields: `fqn`, `module`, `kind` (theorem/def/structure/inductive/axiom/ctor/rec/opaque/
quot), `universes`, `binders` (name/type/binder_kind telescope, no reducing), single-line
`lean_type`, `expanded_prop` (one level only: a closed `def X : Prop` carries its own body; a
declaration whose type applies a project-internal def concluding in `Prop` carries
that def's value instantiated at the application's arguments;
never recursive), `deps_type` / `deps_proof` / `deps_instance` (direct referenced constants in types,
proofs and instance arguments; propext/Classical.choice/Quot.sound are not filtered out), and
`is_prop_definition` (closed def whose type is `Prop`, independent of its directory). Output sorted by
fqn, NDJSON to stdout (or to the file named by `QATLAS_INDEX_OUT` when that variable is set).
-/

namespace QatlasIndex

def projectModules : List Name := []

-- Module membership is queried for every declaration and dependency.
def projectModuleSet : NameSet :=
  projectModules.foldl (fun names moduleName => names.insert moduleName) {}

def definingModule (env : Environment) (n : Name) : Option Name :=
  (env.getModuleIdxFor? n).map (fun idx => env.header.modules[idx.toNat]!.module)

def inProject (env : Environment) (n : Name) : Bool :=
  match definingModule env n with
  | some m => projectModuleSet.contains m
  | none => false

def valueOf : ConstantInfo → Option Expr
  | .thmInfo v => some v.value
  | .defnInfo v => some v.value
  | .opaqueInfo v => some v.value
  | _ => none

def kindOf (env : Environment) (n : Name) : ConstantInfo → String
  | .thmInfo _ => "theorem" | .defnInfo _ => "def" | .axiomInfo _ => "axiom"
  | .opaqueInfo _ => "opaque"
  | .inductInfo _ => if isStructure env n then "structure" else "inductive"
  | .ctorInfo _ => "ctor" | .recInfo _ => "rec" | .quotInfo _ => "quot"

the `:=` line: with the full tracked import set, `QuantumInfo.ClassicalInfo.Prob`'s `[ _ ↔ _ ]`
notation hijacks a line-initial `[` following a `let :=` value. -/
def isNoise (n : Name) : Bool :=
  let suffixes : List String := [".casesOn", ".recOn", ".brecOn", ".rec", ".below", ".ibelow",
    ".noConfusion", ".noConfusionType", ".toCtorIdx", ".match_", ".eq_", ".sizeOf"]
  suffixes.any (fun suf => (n.toString.splitOn suf).length > 1)

def reportable (n : Name) : Bool := !n.isInternalDetail && !isNoise n

def binderKindOf : BinderInfo → String
  | .default => "explicit" | .implicit => "implicit"
  | .strictImplicit => "strict_implicit" | .instImplicit => "inst_implicit"

/-- Pretty one line. Coercion elaboration is disabled: delaborating coerced applications that
mix telescope fvars trips `whnfEasyCases`'s loose-bvar panic inside `getParamKinds` (printed
to stdout, which would pollute the NDJSON stream even though the exception is caught). -/
def oneLine (e : Expr) : MetaM String :=
  return (← withOptions (fun o => o.set `pp.coercions false) (ppExpr e)).pretty.replace "\n" " "

def nameLt (a b : Name) : Bool := a.toString < b.toString

def sortedNames (set : NameSet) : Array Name :=
  set.toList.toArray.qsort nameLt

def bindersOf (ty : Expr) : MetaM (Array Json) :=
  forallTelescope ty fun xs _ => xs.mapM fun x => do
    let decl ← getFVarLocalDecl x
    return Json.mkObj [("name", .str decl.userName.toString),
      ("type", .str (← oneLine decl.type)),
      ("binder_kind", .str (binderKindOf decl.binderInfo))]

/-- A stored definition value is lambda-headed up to its type telescope; peel one lambda per
argument (an argument the value never mentions may have no lambda and is dropped). -/
def betaApply : Expr → List Expr → Expr
  | val, [] => val
  | .lam _ _ body _, arg :: rest => betaApply (body.instantiate1 arg) rest
  | val, args => mkAppN val args.toArray

/-- Does the telescope `ty` conclude in the sort `Prop`? -/
partial def endsInProp : Expr → Bool
  | .forallE _ _ body _ => endsInProp body
  | e => e.isProp

/-- One-level claim expansion: `some` pretty of the project def's value instantiated at the
arguments of the application heading `ci.type`'s telescope. -/
def expandedProp? (env : Environment) (ci : ConstantInfo) : MetaM (Option String) :=
  forallTelescope ci.type fun _ body => do
    match body.getAppFn with
    | .const name us =>
      if let some (.defnInfo claim) := env.find? name then
        if inProject env name && endsInProp claim.type then
          let val := claim.value.instantiateLevelParams claim.levelParams us
          return some (← oneLine (betaApply val body.getAppArgs.toList))
      return none
    | _ => return none

def depsJson (env : Environment) (names : Array Name) : MetaM Json := do
  let set := names.foldl (fun s n => s.insert n) ({} : NameSet)
  let mut arr : Array Json := #[]
  for d in sortedNames set do
    arr := arr.push (Json.mkObj [("name", .str d.toString), ("in_project", .bool (inProject env d))])
  return .arr arr

/-- A closed proposition-valued definition, independent of physical file organization. -/
def propDefinitionOf (ci : ConstantInfo) : MetaM Bool := do
  match ci with
  | .defnInfo _ => return (← whnfD ci.type).isProp
  | _ => return false

/-- `expanded_prop` of one record: the body of a closed claim def, else the one-level expansion
of the def heading the declaration's type. -/
def expandedPropJson (env : Environment) (ci : ConstantInfo) (isClaim : Bool) : MetaM Json := do
  if isClaim then
    match ci with
    | .defnInfo d => return .str (← oneLine d.value)
    | _ => return .null
  else if let some s ← expandedProp? env ci then
    return .str s
  else
    return .null

end QatlasIndex

open QatlasIndex in
set_option maxHeartbeats 0 in
run_cmd Lean.Elab.Command.liftTermElabM do
  let env ← getEnv
  let mut records : Array (String × Json) := #[]
  for (n, ci) in env.constants.toList do
    if n.isInternalDetail then continue
    let some m := definingModule env n | continue
    unless projectModuleSet.contains m do continue
    let kind := kindOf env n ci
    let isClaim ← propDefinitionOf ci
    let expanded ← expandedPropJson env ci isClaim
    let tyDeps := ci.type.getUsedConstants
    let prDeps := match valueOf ci with
      | some v => v.getUsedConstants
      | none => #[]
    let tyJ ← depsJson env tyDeps
    let prJ ← depsJson env prDeps
    let union := (tyDeps ++ prDeps).foldl (fun s n => s.insert n) ({} : NameSet)
    let insts ← (sortedNames union).filterM (fun d => liftM (isInstance d))
    let inJ := Json.arr (insts.map (fun d =>
      Json.mkObj [("name", .str d.toString), ("in_project", .bool (inProject env d))]))
    let axs ← collectAxioms n
    let axs := axs.qsort nameLt
    let us : Array Json := ci.levelParams.toArray.map (fun u => Json.str u.toString)
    let binders ← bindersOf ci.type
    let tyPretty ← oneLine ci.type
    let record := Json.mkObj [
      ("fqn", .str n.toString), ("module", .str m.toString), ("kind", .str kind),
      ("universes", .arr us), ("binders", .arr binders),
      ("lean_type", .str tyPretty), ("expanded_prop", expanded),
      ("deps_type", tyJ), ("deps_proof", prJ), ("deps_instance", inJ),
      ("axioms", .arr (axs.map (fun a => Json.str a.toString))),
      ("is_prop_definition", .bool isClaim)]
    records := records.push (n.toString, record)
  let sorted := records.qsort (fun a b => a.1 < b.1)
  -- NDJSON goes to stdout by default. When QATLAS_INDEX_OUT is set, it goes to that file
  -- instead: in run_cmd context even IO.eprintln and runtime panic spew share fd 1, and the
  -- delaborator prints caught "loose bvar" panics while prettying a handful of matrix-heavy
  -- theorem types, so a caller that needs a clean stream must use a separate channel.
  if let some out := (← IO.getEnv "QATLAS_INDEX_OUT") then
    unless out == "" do
      IO.FS.withFile out .write fun handle =>
        for (_, j) in sorted do
          handle.putStrLn j.compress
  else
    for (_, j) in sorted do
      IO.println j.compress
