import json

import pytest

from qatlib_search.lean_tools import main


@pytest.fixture
def source_project(tmp_path):
    (tmp_path / "lakefile.toml").write_text(
        'name="fixture"\ndefaultTargets=["Library"]\n'
        '[[lean_lib]]\nname="Library"\nsrcDir="src"\n'
        '[[lean_lib]]\nname="Extra"\n', encoding="utf-8")
    library = tmp_path / "src/Library"
    library.mkdir(parents=True)
    (library / "Basic.lean").write_text('''namespace Outer.Inner
variable (n : Nat)

/-- Identity theorem with Unicode α.
The second line is preserved. -/
@[simp] theorem sameValue
    : n = n := by rfl

/- def decoy : Nat := 10 -/
private theorem hidden : True := trivial
def someValue : Nat := 42
end Outer.Inner
''', encoding="utf-8")
    (tmp_path / "Extra.lean").write_text("def Extra.someValue : Nat := 19\n", encoding="utf-8")
    (library / "Generated.lean").write_text("run_cmd pure ()\n", encoding="utf-8")
    # An independently maintained bibliography cannot affect direct name lookup.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/references.bib").write_text("broken bibliography", encoding="utf-8")
    return tmp_path


def test_prefix_and_kind_return_written_signature_and_docstring(source_project, capsys):
    assert main(["retrieve", "--project", str(source_project), "--prefix", "Outer.Inner.",
                 "--kind", "theorem", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["total"] == 1
    row, = report["results"]
    assert row["fqn"] == "Outer.Inner.sameValue"
    assert row["module"] == "Library.Basic"
    assert row["path"] == str(source_project / "src/Library/Basic.lean")
    assert row["line"] == 6
    assert row["signature"] == ": n = n"
    assert row["doc"] == "Identity theorem with Unicode α.\nThe second line is preserved."
    assert row["variable_context"] == ["variable (n : Nat)"]
    assert "Generated.lean" in report["warnings"][0]


@pytest.mark.parametrize("query", [["someValue"], ["--substring", "someValue"]])
def test_literal_substring_includes_nondefault_library_and_is_limited(source_project, capsys, query):
    assert main(["retrieve", "--project", str(source_project), *query, "--limit", "1", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["total"] == 2
    assert [row["fqn"] for row in result["results"]] == ["Extra.someValue"]


def test_module_lookup_and_text_output(source_project, capsys, monkeypatch):
    monkeypatch.chdir(source_project / "src")
    assert main(["retrieve", "--module", "Library.Basic"]) == 0
    output = capsys.readouterr()
    assert "Outer.Inner.sameValue" in output.out and "Identity theorem" in output.out
    assert "Outer.Inner.someValue" in output.out
    assert not output.err
    assert main(["retrieve", "--module", "Library", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["results"] == []


@pytest.mark.parametrize("query", [[], ["--substring", ""], ["x", "--limit", "0"], ["x", "--substring", "y"]])
def test_invalid_queries_fail_without_results(source_project, capsys, query):
    with pytest.raises(SystemExit) as exc:
        main(["retrieve", "--project", str(source_project), *query, "--json"])
    assert exc.value.code == 2
    assert not capsys.readouterr().out
