"""Cache credentials and source gates work without repository-name conventions."""
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest

from qatlib_env import ci, r2_cache


def test_arbitrary_packages_require_their_own_key_and_explicit_aliases(tmp_path):
    key = "fixture_access_12345:fixture_secret_6789012345"
    incoming = {"HOME": str(tmp_path), "LAKE_CACHE_KEY": key, "LAKE_OTHER_CACHE_KEY": key}
    assert r2_cache.credential_variables("OrionSignals", {}) == ("LAKE_ORION_SIGNALS_CACHE_KEY",)
    assert r2_cache.resolve_write_key("OrionSignals", env=incoming) == ""
    selected = {**incoming, "QATLIB_CACHE_KEY_VARIABLES": json.dumps({"OrionSignals": "LAKE_TEAM_CACHE_KEY"}),
                "LAKE_TEAM_CACHE_KEY": key}
    assert r2_cache.resolve_write_key("OrionSignals", env=selected) == key
    assert r2_cache.resolve_write_key("OtherSignals", env=selected) == ""
    assert "LAKE_CACHE_KEY" not in r2_cache.package_environment("OrionSignals", selected)
    assert incoming["LAKE_CACHE_KEY"] == key


def test_generic_native_credential_is_only_preserved_for_an_explicitly_selected_package():
    incoming = {"LAKE_CACHE_KEY": "selected-key",
                "QATLIB_CACHE_KEY_VARIABLES": '{"OrionSignals":"LAKE_CACHE_KEY"}'}
    selected = r2_cache.package_environment("OrionSignals", incoming)
    assert "LAKE_CACHE_KEY" not in selected
    assert ci.write_key("OrionSignals", selected) == "selected-key"
    assert ci.write_key("OtherSignals", selected) == ""


@pytest.mark.parametrize("aliases", ['[]', '{"Orion":"not a variable"}', '{"Orion":[]}'])
def test_invalid_credential_configuration_fails_closed(aliases):
    with pytest.raises(ValueError):
        r2_cache.credential_variables("Orion", {"QATLIB_CACHE_KEY_VARIABLES": aliases})


def test_materialized_sources_and_inputs_follow_custom_lake_source_directories(tmp_path):
    (tmp_path / "lakefile.toml").write_text('''name="package_name"
defaultTargets=["LibraryTarget"]
[[lean_lib]]
name="LibraryTarget"
roots=["Orion"]
srcDir="source"
[[lean_lib]]
name="Checks"
roots=["Spec"]
srcDir="checks"
''')
    (tmp_path / "source/Orion").mkdir(parents=True)
    (tmp_path / "source/Orion/Basic.lean").write_text("def answer := 42\n")
    (tmp_path / "checks/Spec").mkdir(parents=True)
    (tmp_path / "checks/Spec/Basic.lean").write_text("example : True := by trivial\n")
    expected = ["source/Orion", "checks/Spec", "lakefile.toml", "lean-toolchain", "lake-manifest.json"]
    assert ci.source_inputs(tmp_path, "LibraryTarget") == expected
    locked = "f" * 40
    result = subprocess.CompletedProcess([], 0, stdout=str(tmp_path) + "\n" + locked + "\n")
    with patch.object(ci.supervisor, "run", return_value=result):
        assert ci.source_checkout(tmp_path, locked, {})
        assert not ci.source_checkout(tmp_path, "e" * 40, {})


def test_source_dependency_selection_never_guesses_between_multiple_locks(tmp_path):
    (tmp_path / "lake-manifest.json").write_text(json.dumps({"packages": [
        {"name": "FirstLibrary", "rev": "a" * 40}, {"name": "SecondLibrary", "rev": "b" * 40}]}))
    assert ci.public_repo(tmp_path, {}) is None
    with pytest.raises(ValueError, match="one locked dependency"):
        ci.public_repo(tmp_path, {"QATLIB_SOURCE_PACKAGE": "MissingLibrary"})
