"""Promotion checks the receiving files, including fresh untracked outputs."""
import json
import subprocess

import pytest

from qatlib_env.workspace import main


def test_check_export_reports_marked_alias_and_shim(tmp_path, capsys):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    alias = tmp_path / "Renamed.lean"
    alias.write_bytes(
        b'theorem semanticName : True := trivial\r\n'
        b'-- promotion: dev-only\r\n'
        b'@[deprecated semanticName (since := "2026-11-17")]\r\n'
        b'alias original844 := semanticName\r\n'
    )
    shim = tmp_path / "OldTest.lean"
    shim.write_text("-- promotion: dev-only\nimport Tests.SemanticName\n", encoding="utf-8")
    before = [path.read_bytes() for path in (alias, shim)]

    assert main(["--project", str(tmp_path), "promotion", "check-export", alias.name, shim.name]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "action": "check-export", "files": 2,
        "errors": [
            "OldTest.lean:1: dev-only promotion marker in export",
            "Renamed.lean:2: dev-only promotion marker in export",
        ],
    }
    assert [path.read_bytes() for path in (alias, shim)] == before


def test_check_export_preserves_public_deprecation_and_ignores_comment_examples(tmp_path, capsys):
    path = tmp_path / "Export.lean"
    path.write_text(
        'theorem semanticName : True := trivial\n'
        '@[deprecated semanticName (since := "2026-11-17")]\n'
        'alias supportedOldName := semanticName\n'
        '/- Example marker:\n/- nested -/\n-- promotion: dev-only\n-/\n'
        'def exampleText := r#"\n-- promotion: dev-only\n"#\n', encoding="utf-8",
    )

    assert main(["--project", str(tmp_path), "promotion", "check-export", path.name]) == 0
    assert json.loads(capsys.readouterr().out) == {"action": "check-export", "files": 1, "errors": []}


@pytest.mark.parametrize("filename", ["Missing.lean", "../Outside.lean", "Note.md", "Redirected.lean"])
def test_check_export_rejects_invalid_paths(tmp_path, capsys, filename):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "Outside.lean"
    outside.touch()
    (project / "Note.md").touch()
    (project / "Redirected.lean").symlink_to(outside)

    assert main(["--project", str(project), "promotion", "check-export", filename]) == 1
    assert "missing or redirected Lean source" in json.loads(capsys.readouterr().out)["error"]
