"""Exercise unrelated Lake names, source layouts, exact pins and retirement."""
import argparse
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from qatlib_env import check_scoped_env_lint as scoped, promotion
from qatlib_env.config import load_config
from qatlib_env.project import libraries, primary_library, repository_slug
from qatlib_env.workspace import main
from tests._git_fixture import snapshot


def lake(root, name="Algebra", directory="src", tests="Checks"):
    (root / "lakefile.toml").write_text(f'''name = "Package"
defaultTargets = ["{name}"]
[[lean_lib]]
name = "{name}"
srcDir = "{directory}"
[[lean_lib]]
name = "{tests}"
srcDir = "test/strict"
''')


def test_lake_and_personal_config_are_independent(tmp_path):
    lake(tmp_path)
    home = tmp_path / "home"
    (home / ".qatlib").mkdir(parents=True)
    (home / ".qatlib/env.toml").write_text("local_pool_mib=8192\nlocal_pool_cpu=2\n")
    config = load_config(tmp_path, {"HOME": str(home)})
    assert config.metadata["library"] == "Algebra"
    assert config.metadata["tests"] == "Checks"
    assert config.source_roots == ("src/Algebra", "test/strict/Checks")
    assert config.local_pool_mib == 8192
    assert config.sources["local_pool_mib"].endswith(".qatlib/env.toml")
    assert libraries(tmp_path)[1].src_dir == Path("test/strict")


def test_explicit_config_overrides_discovery_and_invalid_personal_config_fails(tmp_path):
    lake(tmp_path)
    (tmp_path / "qatlib-env.toml").write_text('library="Other"\n')
    assert load_config(tmp_path, {"HOME": str(tmp_path)}).metadata["library"] == "Other"
    (tmp_path / ".qatlib").mkdir()
    (tmp_path / ".qatlib/env.toml").write_text("local_pool_mib=false\n")
    with pytest.raises(ValueError, match="local_pool_mib"):
        load_config(tmp_path, {"HOME": str(tmp_path)})


def test_repository_identity_uses_canonical_origin_before_transport_rewrites(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin",
                    "https://github.com/example/shared.git"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config",
                    "url.https://mirror.test/https://github.com/.insteadOf", "https://github.com/"], check=True)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "unrelated.git"))
    assert repository_slug(tmp_path) == "example/shared"


def test_ambiguous_default_and_escaping_srcdir_fail(tmp_path):
    lake(tmp_path)
    content = (tmp_path / "lakefile.toml").read_text()
    (tmp_path / "lakefile.toml").write_text(content.replace('["Algebra"]', '["Algebra", "Checks"]'))
    with pytest.raises(ValueError, match="ambiguous"):
        primary_library(tmp_path)
    (tmp_path / "lakefile.toml").write_text(content.replace('"src"', '"../outside"'))
    with pytest.raises(ValueError, match="layout"):
        libraries(tmp_path)


def test_scoped_changed_modules_follow_each_srcdir(tmp_path):
    lake(tmp_path)
    files = ["src/Algebra/Basic.lean", "test/strict/Checks/Safety.lean", "scratch/Other.lean"]
    for file in files:
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("-- fixture\n")
    layouts = [(lib.src_dir, lib.roots) for lib in libraries(tmp_path)]
    with patch.object(scoped.subprocess, "check_output", return_value="\0".join(files).encode() + b"\0"):
        assert scoped.selected_modules(tmp_path, "HEAD~1", layouts) == ["Algebra.Basic", "Checks.Safety"]
    from qatlib_search.build_search_index import modules_from_paths
    assert modules_from_paths(files, tmp_path) == ["Algebra.Basic", "Checks.Safety"]


def test_scoped_main_uses_lake_template_root(tmp_path):
    lake(tmp_path)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "EnvLint.lean").write_text("import Algebra\n  let env ← getEnv\n"
        "    unless modName.getRoot == `Algebra do continue\n"
        "  let decls ← liftCoreM <| getDeclsInPackage `Algebra\n")
    with patch.object(scoped.workspace, "run") as run, patch.object(scoped.workspace, "environment", return_value={}):
        assert scoped.main(["--repo", str(tmp_path), "Checks.Safety"]) == 0
        generated = Path(run.call_args.args[0][-1]).read_text()
    assert "import Checks.Safety" in generated
    assert "targetMods.contains" in generated
    assert "no declarations audited" in generated


def test_scoped_base_skips_import_only_library_aggregates(tmp_path, capsys):
    lake(tmp_path)
    for file in ("src/Algebra.lean", "test/strict/Checks.lean"):
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("-- aggregate imports\n")
    changed = b"src/Algebra.lean\0test/strict/Checks.lean\0"
    with patch.object(scoped.subprocess, "check_output", return_value=changed):
        assert scoped.main(["--repo", str(tmp_path), "--base", "HEAD~1"]) == 0
    assert "skipped=no_changed_modules" in capsys.readouterr().out


def test_generic_retirement_checks_exact_git_pin_and_nested_source_directory(tmp_path, capsys):
    upstream, consumer = tmp_path / "upstream", tmp_path / "consumer"
    for root, name in ((upstream, "Provider"), (consumer, "Consumer")):
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        lake(root, name)
        directory = root / "src" / name
        directory.mkdir(parents=True)
        (directory / "Basic.lean").write_text(f"namespace {name}\ndef value := 7\nend {name}\n")
    revision = snapshot(upstream)
    (consumer / "lake-manifest.json").write_text(json.dumps({"packages": [
        {"name": "ArbitraryDependency", "type": "git", "rev": revision, "inherited": False}]}))
    snapshot(consumer)
    args = argparse.Namespace(action="check-retired", package="ArbitraryDependency", source=upstream,
                              root=None, source_root=None)
    result = promotion.execute(consumer, args)
    assert result["stats"]["duplicate_declarations"] == 1
    assert result["stats"]["identical_modules"] == 1
    assert main(["--project", str(consumer), "promotion", "verify-lock", "--package",
                 "ArbitraryDependency", "--source", str(upstream)]) == 0
    assert revision in capsys.readouterr().out
    (upstream / "src/Provider/Basic.lean").write_text("def drift := 9\n")
    with pytest.raises(ValueError, match="source bytes differ"):
        promotion.execute(consumer, args)


def test_bump_is_targeted_and_inherited_lock_cannot_mutate(tmp_path):
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [
        {"name": "Dependency", "rev": "a" * 40, "inherited": True}]}))
    args = argparse.Namespace(action="bump-lock", package="Dependency", source=None,
                              root=None, source_root=None)
    with patch.object(promotion.workspace, "run") as run:
        with pytest.raises(ValueError, match="inherited"):
            promotion.execute(tmp_path, args)
        run.assert_not_called()
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [
        {"name": "Dependency", "rev": "a" * 40}]}))
    with patch.object(promotion.workspace, "run") as run, \
            patch.object(promotion.workspace, "environment", return_value={}), \
            patch.object(promotion, "dependency", side_effect=ValueError("fixture stop")):
        with pytest.raises(ValueError, match="fixture stop"):
            promotion.execute(tmp_path, args)
        assert run.call_args.args[0] == ["lake", "update", "Dependency"]


def test_docs_reads_the_unique_root_readme(capsys):
    root = Path(__file__).resolve().parents[1]
    assert main(["docs"]) == 0
    assert capsys.readouterr().out == (root / "README.md").read_text() + "\n"
    assert main(["docs", "--path"]) == 0
    assert Path(capsys.readouterr().out.strip()) == root / "README.md"
    readmes = [path for path in root.rglob("README.md") if not any(
        part in {".git", ".venv", "dist", "build", ".pytest_cache"}
        or part.startswith(".wheel-venv") for part in path.relative_to(root).parts)]
    assert readmes == [root / "README.md"]
