"""Run the existing EnvLint checks over exactly selected compiled modules."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import uuid

from qatlib_env import workspace
from qatlib_env.project import libraries, primary_library
from qatlib_search.lean_declarations import name_parts


def scoped_source(source: str, modules: list[str], *, root_prefix: str) -> str:
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


def selected_modules(repo: Path, base: str, layouts) -> list[str]:
    changed = subprocess.check_output(["git", "diff", "--name-only", "-z", "--diff-filter=ACMR",
                                       base, "HEAD", "--"], cwd=repo)
    result = []
    for raw in changed.split(b"\0"):
        path = Path(os.fsdecode(raw))
        if not raw or path.suffix != ".lean" or not (repo / path).is_file():
            continue
        for directory, roots in layouts:
            if not path.is_relative_to(directory):
                continue
            module = ".".join(path.relative_to(directory).with_suffix("").parts)
            if any(module == root or module.startswith(root + ".") for root in roots):
                result.append(module)
                break
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", help="Audit changed Lean modules relative to this Git revision")
    parser.add_argument("--root-prefix", default=os.environ.get("QATLIB_ENV_ROOT"))
    parser.add_argument("--test-root-prefix", default=os.environ.get("QATLIB_ENV_TEST_ROOT"))
    parser.add_argument("--test-src-dir", type=Path, help="source directory for an explicit test root")
    parser.add_argument("modules", nargs="*")
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    libs = ()
    try:
        libs = libraries(repo)
        if args.root_prefix:
            root = args.root_prefix
        else:
            primary = primary_library(repo)
            if len(primary.roots) != 1:
                raise ValueError("library has several roots; select --root-prefix explicitly")
            root = primary.roots[0]
        layouts = [(lib.src_dir, lib.roots) for lib in libs]
    except ValueError as error:
        if not args.root_prefix or (repo / "lakefile.toml").exists():
            parser.error(str(error))
        root, layouts = args.root_prefix, []
    if args.root_prefix:
        layouts.append((Path("."), (root,)))
    if args.test_root_prefix:
        matching = next((lib.src_dir for lib in libs if args.test_root_prefix in lib.roots), None) if libs else None
        directory = args.test_src_dir or matching
        if directory is None:
            parser.error("--test-src-dir is required for a test root absent from lakefile.toml")
        if directory.is_absolute() or ".." in directory.parts:
            parser.error("test source directory must be inside the project")
        layouts.append((directory, (args.test_root_prefix,)))
    roots = tuple(name for _, names in layouts for name in names)
    if args.base:
        args.modules.extend(selected_modules(repo, args.base, layouts))
    args.modules = sorted(set(args.modules))
    if not args.modules:
        if args.base:
            print("SCOPED_ENV_LINT skipped=no_changed_modules")
            return 0
        parser.error("provide modules or --base")
    for module in args.modules:
        try:
            name_parts(module)
        except ValueError:
            parser.error("invalid Lean module name: " + module)
        if not any(module.startswith(prefix + ".") for prefix in roots):
            parser.error("expected a project leaf module: " + module)
    source = scoped_source((repo / "scripts/EnvLint.lean").read_text(), args.modules, root_prefix=root)
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

