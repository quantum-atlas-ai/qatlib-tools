from importlib.metadata import version
from pathlib import Path

import pytest

from qatlib_env import __version__
from qatlib_env.__main__ import main as env_main
from qatlib_forge.cli import main as forge_main
from qatlib_search.retrieve import main as retrieve_main
from scripts.release_metadata import metadata


def test_release_version_matches_metadata_lock_changelog_and_clis(capsys):
    root = Path(__file__).resolve().parents[1]
    installed = version("qatlib-tools")
    declared, notes = metadata(root, f"v{installed}")
    assert str(declared) == __version__ == installed
    assert notes
    for name, main in (("qatlib-env", env_main), ("qatlib-forge", forge_main), ("qatlib-retrieve", retrieve_main)):
        assert main(["--version"]) == 0
        assert capsys.readouterr().out == f"{name} {installed}\n"
    with pytest.raises(ValueError, match="does not match"):
        metadata(root, "v-mismatched")
