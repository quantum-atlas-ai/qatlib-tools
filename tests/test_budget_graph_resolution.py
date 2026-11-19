"""Offline provider-resolution regressions; no Lean compiler or network runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from qatlib_env import budget, resources


def write(root: Path, name: str, contents: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    write(root, "lakefile.toml", 'name="Consumer"\ndefaultTargets=["Consumer"]\n'
          '[[lean_lib]]\nname="Consumer"\n'
          '[[lean_exe]]\nname="lint"\nroot="scripts.lint"\n')
    write(root, "Consumer/Leaf.lean", "def leaf := 1\n")
    write(root, "scripts/lint.lean", "import Consumer.Leaf\n")
    write(root, ".qatlib-env.local.toml", "")
    write(root, "lean-toolchain", "leanprover/lean4:v4.35.0\n")
    write(root, "qatlib-env.toml", 'local_pool_mib=16384\nlocal_pool_cpu=4\n'
          '[resources]\njobs=1\nlean_threads=1\nmemory_budget="4GiB"\n')
    write(root, "lake-manifest.json", '{"packages":[]}')
    toolchain = tmp_path / "toolchain"
    write(toolchain, "src/lean/Init.lean", "prelude\n")
    write(toolchain, "src/lean/lake/Lake.lean", "prelude\nimport Init\n")
    write(toolchain, "lib/lean/Init.olean", "precompiled")
    write(toolchain, "lib/lean/Lake.olean", "precompiled")
    output = write(root, ".lake/build/lib/lean/Consumer/Leaf.olean", "precompiled")
    trace = write(root, ".lake/build/lib/lean/Consumer/Leaf.trace", '{"depHash":"fixture"}')
    stamp = time.time_ns() + 1_000_000_000
    for path in (output, trace):
        os.utime(path, ns=(stamp, stamp))
    env = {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "xdg"),
           "QATLIB_REAL_SYSROOT": str(toolchain)}
    packages = []

    def fake_git(path, *args):
        if args == ("rev-parse", "HEAD"):
            return "a" * 40 if path == root else "b" * 40
        if args == ("rev-parse", "--show-toplevel"):
            return str(path.resolve())
        return ""

    monkeypatch.setattr(budget, "git", fake_git)
    monkeypatch.setattr(resources, "probe_resources", lambda: resources.HostResources(
        64 * resources.GIB, 64 * resources.GIB, 4, 4, 64 * resources.GIB))

    def dependency(name, *, library=None, dsl=False, expected="b" * 40):
        library = library or name
        base = root / ".lake/packages" / name
        if dsl:
            write(base, "lakefile.lean", "import Lake\nopen Lake DSL\n"
                  f"package {name}\nlean_lib {library} where\n  srcDir := \"src\"\n"
                  "lean_exe get_cache\nlean_exe lint where\n  root := `scripts.lint\n"
                  "lean_exe test\n")
            source = base / "src"
        else:
            write(base, "lakefile.toml", f'name="{name}"\n[[lean_lib]]\nname="{library}"\n'
                  '[[lean_exe]]\nname="get_cache"\n'
                  '[[lean_exe]]\nname="lint"\nroot="scripts.lint"\n'
                  '[[lean_exe]]\nname="test"\n')
            write(base, "lakefile.lean", "import Missing.PackageConfiguration\n")
            source = base
        write(source, f"{library}/Basic.lean", "def provider := 1\n")
        for driver in ("get_cache.lean", "scripts/lint.lean", "test.lean"):
            write(base, driver, "import Missing.PrivateDriver\n")
        packages.append({"name": name, "rev": expected, "type": "git"})
        write(root, "lake-manifest.json", json.dumps({"packages": packages}))
        return base, source

    return SimpleNamespace(root=root, env=env, dependency=dependency,
                           estimate=lambda targets: budget.estimate(root, targets, env=env))


@pytest.mark.parametrize("dsl", [False, True])
def test_dependency_drivers_do_not_become_global_providers(project, dsl):
    project.dependency("DependencyA", dsl=dsl)
    project.dependency("DependencyB", dsl=dsl)
    graph = budget.SourceGraph(project.root, project.env)
    assert "get_cache" not in graph.modules
    assert "test" not in graph.modules
    assert "lakefile" not in graph.modules
    assert graph.modules["scripts.lint"].package == "project"
    assert {"DependencyA.Basic", "DependencyB.Basic"} <= graph.modules.keys()
    assert not graph.module_errors
    helper = write(project.root, ".lake/artifact-audit/hash.lean", "import Lake\n")
    report = project.estimate([str(helper)])
    assert report["mode"] == "local"
    assert report["unresolved_imports"] == []
    assert report["closure_package_modules"] == 1
    assert report["closure_toolchain_modules"] == 2


def test_project_executable_remains_a_budgetable_target(project):
    project.dependency("DependencyA")
    project.dependency("DependencyB")
    report = project.estimate(["lint"])
    assert report["mode"] == "local"
    assert report["target_modules"] == 1
    assert report["closure_package_modules"] == 2
    assert report["unresolved_imports"] == []


def test_mathlib_dsl_roots_keep_library_modules_without_exporting_drivers(project):
    base, _ = project.dependency("mathlib", library="Mathlib", dsl=True)
    write(base, "lakefile.lean", "import Lake\nopen Lake DSL\n"
          'package mathlib where\n  lintDriver := "batteries/runLinter"\n'
          "@[default_target]\nlean_lib Mathlib where\n  leanOptions := #[]\n"
          "lean_lib Cache where\n  globs := #[`Cache.+]\n"
          "lean_exe cache where\n  root := `Cache.Main\n"
          'lean_exe autolabel where\n  srcDir := "scripts"\n')
    write(base, "Mathlib.lean", "import Mathlib.Algebra.Basic\n")
    write(base, "Mathlib/Algebra/Basic.lean", "def algebra := 1\n")
    write(base, "Cache/Main.lean", "import Lake\n")
    write(base, "scripts/autolabel.lean", "import Missing.PrivateDriver\n")
    graph = budget.SourceGraph(project.root, project.env)
    assert {"Mathlib", "Mathlib.Algebra.Basic", "Cache.Main"} <= graph.modules.keys()
    assert "scripts.autolabel" not in graph.modules
    assert "lakefile" not in graph.modules
    write(project.root, "Consumer/Leaf.lean", "import Mathlib.Algebra.Basic\n")
    report = project.estimate(["Consumer.Leaf"])
    assert report["mode"] == "local" and report["unresolved_imports"] == []


def test_unreachable_ambiguity_and_broken_sources_do_not_poison_small_target(project):
    project.dependency("DependencyA", library="Shared")
    project.dependency("DependencyB", library="Shared")
    write(project.root, "Consumer/Broken.lean", "/- unterminated comment\n")
    write(project.root, "Consumer/Unrelated.lean", "import Missing.Unrelated\n")
    report = project.estimate(["Consumer.Leaf"])
    assert report["mode"] == "local"
    assert report["unresolved_imports"] == []
    assert report["closure_package_modules"] == 1


def test_full_library_target_reports_source_errors_in_its_members(project):
    write(project.root, "Consumer/Broken.lean", "/- unterminated comment\n")
    report = project.estimate(["Consumer"])
    assert any("cannot parse Consumer.Broken" in error for error in report["unresolved_imports"])
    assert report["mode"] == "cloud"


def test_reachable_ambiguous_provider_fails_closed(project):
    project.dependency("DependencyA", library="Shared")
    project.dependency("DependencyB", library="Shared")
    write(project.root, "Consumer/Leaf.lean", "import Shared.Basic\n")
    report = project.estimate(["Consumer.Leaf"])
    assert "<ambiguous provider: Shared.Basic>" in report["unresolved_imports"]
    assert report["mode"] == "cloud"
    with pytest.raises(budget.AdmissionError):
        budget.admit(project.root, ["Consumer.Leaf"], env=project.env)


@pytest.mark.parametrize("imported", ["Missing.Provider", "get_cache", "test"])
def test_missing_or_private_driver_import_fails_closed(project, imported):
    project.dependency("DependencyA")
    project.dependency("DependencyB")
    write(project.root, "Consumer/Leaf.lean", f"import {imported}\n")
    report = project.estimate(["Consumer.Leaf"])
    assert imported in report["unresolved_imports"]
    assert report["mode"] == "cloud"


def test_reachable_source_parse_error_is_reported(project):
    _, source = project.dependency("DependencyA")
    write(source, "DependencyA/Basic.lean", "/- unterminated comment\n")
    write(project.root, "Consumer/Leaf.lean", "import DependencyA.Basic\n")
    report = project.estimate(["Consumer.Leaf"])
    assert any("cannot parse DependencyA.Basic" in error for error in report["unresolved_imports"])
    assert report["mode"] == "cloud"


def test_dependency_lock_checks_are_preserved_for_unrelated_target(project):
    project.dependency("DependencyA", expected="c" * 40)
    report = project.estimate(["Consumer.Leaf"])
    assert report["dependency_lock_mismatches"][0]["package"] == "DependencyA"
    assert report["warm_modules"] == 0
    assert "dependency_lock_mismatch" in {item["code"] for item in report["reasons"]}
    assert report["mode"] == "cloud"


def test_dependency_collision_with_toolchain_module_fails_closed(project):
    project.dependency("DependencyA", library="Init")
    report = project.estimate(["Consumer.Leaf"])
    # A descendant named Init.Basic does not collide with the implicit Init root.
    assert report["mode"] == "local"
    write(project.root / ".lake/packages/DependencyA", "Init.lean", "prelude\n")
    report = project.estimate(["Consumer.Leaf"])
    assert "<ambiguous provider: Init>" in report["unresolved_imports"]
    assert report["mode"] == "cloud"
