"""Run the existing EnvLint checks over exactly selected compiled modules."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import uuid

from qatlib_env import workspace


def scoped_source(source: str, modules: list[str], *, root_prefix: str = "Project") -> str:
    """Filter both vacuity checks and Batteries checks; fail on template drift."""
    def replace_once(old: str, new: str) -> None:
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("EnvLint template changed: " + old)
        source = source.replace(old, new)

    replace_once(f"import {root_prefix}\n", "".join("import " + m + "\n" for m in modules))
    names = "[" + ", ".join("`" + m for m in modules) + "]"
    replace_once("  let env ← getEnv\n",
                 "  let env ← getEnv\n  let targetMods : List Name := " + names + "\n")
    replace_once(f"    unless modName.getRoot == `{root_prefix} do continue",
                 "    unless targetMods.contains modName do continue")
    replace_once(f"  let decls ← liftCoreM <| getDeclsInPackage `{root_prefix}", """  if auditedDecls == 0 then
    throwError "SCOPED_ENV_LINT_ERROR: no declarations audited"
  let decls : Array Name := env.constants.toList.foldl (fun acc (name, _) =>
    match env.getModuleIdxFor? name with
    | some idx => if targetMods.contains env.header.modules[idx.toNat]!.module
      then acc.push name else acc
    | none => acc) #[]""")
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="Audit changed Lean modules relative to this Git revision")
    parser.add_argument("--root-prefix", default=os.environ.get("QATLIB_ENV_ROOT", "Project"))
    parser.add_argument("--test-root-prefix", default=os.environ.get("QATLIB_ENV_TEST_ROOT", "Verification"))
    parser.add_argument("modules", nargs="*")
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.base:
        changed = subprocess.check_output(["git", "diff", "--name-only", "--diff-filter=ACMR",
                                           args.base, "HEAD", "--", args.root_prefix, "verification"],
                                          cwd=repo, text=True)
        args.modules.extend(path.removeprefix("verification/").removesuffix(".lean").replace("/", ".")
                            for path in changed.splitlines() if path.endswith(".lean") and (repo / path).is_file()
                            and path.startswith((args.root_prefix + "/", "verification/" + args.test_root_prefix + "/")))
    args.modules = sorted(set(args.modules))
    if not args.modules:
        if args.base:
            print("SCOPED_ENV_LINT skipped=no_changed_modules")
            return 0
        parser.error("provide modules or --base")
    for module in args.modules:
        if not re.fullmatch(r"(?:" + re.escape(args.root_prefix) + r"|" + re.escape(args.test_root_prefix) + r")(?:\.[A-Za-z_][A-Za-z0-9_]*)+", module):
            parser.error("expected a project leaf module: " + module)
    source = scoped_source((repo / "scripts/EnvLint.lean").read_text(), args.modules, root_prefix=args.root_prefix)
    directory = repo / ".lake/qatlas-tmp"
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / ("scoped_env_lint_" + uuid.uuid4().hex + ".lean")
    probe.write_text(source)
    print("SCOPED_ENV_LINT modules=" + ",".join(args.modules), flush=True)
    try:
        workspace.run(["lake", "--no-build", "lean", str(probe)], cwd=repo,
                      env=workspace.environment(repo))
        return 0
    except subprocess.CalledProcessError as error:
        return error.returncode


if __name__ == "__main__":
    raise SystemExit(main())

