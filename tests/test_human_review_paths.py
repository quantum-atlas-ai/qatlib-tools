"""Check the protected block boundary and approval lifecycle."""
import hashlib
from pathlib import Path

import pytest

from scripts import check_human_contracts as contracts


def test_repo_has_valid_contract_boundaries():
    assert contracts.pending(Path(__file__).resolve().parents[1]).keys() <= set(contracts.PATHS)


def test_only_edits_inside_block_need_a_new_digest(tmp_path, monkeypatch, capsys):
    sample = f"{contracts.START}\nHuman-readable text.\n{contracts.END}\n"
    for name in contracts.PATHS:
        (tmp_path / name).write_text(sample, encoding="utf-8")
    (tmp_path / "README.md").write_text("Project overview.\n", encoding="utf-8")
    manifest = tmp_path / contracts.MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.touch()
    monkeypatch.setattr(contracts, "__file__", str(tmp_path / "scripts/check_human_contracts.py"))
    assert contracts.main(["--draft"]) == 0
    assert contracts.main([]) == 1
    assert "Human review pending" in capsys.readouterr().out
    digest = hashlib.sha256(sample.encode()).hexdigest()
    manifest.write_text("".join(f"{digest}  {name}\n" for name in contracts.PATHS), encoding="utf-8")
    assert contracts.main([]) == 0
    (tmp_path / "README.md").write_text("Technical details.\n", encoding="utf-8")
    assert contracts.main([]) == 0
    (tmp_path / "AGENTS.md").write_text(sample.replace("text", "changes"), encoding="utf-8")
    assert contracts.main([]) == 1
    (tmp_path / "README.md").write_text(sample, encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        contracts.main(["--draft"])
    assert error.value.code == 1
    assert "markers belong only in AGENTS.md" in capsys.readouterr().err


@pytest.mark.parametrize("sample", [
    "", contracts.START, contracts.END + "\n" + contracts.START,
    f"{contracts.START}\n{contracts.START}\n{contracts.END}\n",
    f"{contracts.START}\n{contracts.END}\nin text {contracts.START}\n",
])
def test_malformed_contract_markers_are_rejected(sample):
    with pytest.raises(ValueError):
        contracts.block(sample, "AGENTS.md")
