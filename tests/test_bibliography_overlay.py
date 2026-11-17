"""Entry contents and keys must survive Dev → Public bibliography promotion."""
import unittest
import json

from qatlib_search.doc_references import merge_bibliographies


def merge(baseline, increment, **options):
    return merge_bibliographies(baseline, increment, baseline_path="Public", increment_path="Dev", **options)


class BibliographyOverlayTests(unittest.TestCase):
    def test_nested_and_quoted_values_do_not_include_neighbouring_comments(self):
        known = '@article(Known2020, title="{A} (B)", note={Inside ({braces})})'
        new = "@article{New2026, title={{New}}}"
        entries, errors = merge(known + "\n% Upstream heading\n", "% Dev introduction\n" + known + "\n" + new)
        self.assertEqual(errors, [])
        self.assertEqual(list(entries.items()), [("Known2020", known), ("New2026", new)])

    def test_conflict_is_reported_even_when_already_published_keys_are_allowed(self):
        _, errors = merge("@book{Known2020, title={Original}}", "@book{Known2020, title={Changed}}")
        self.assertEqual([error.code for error in errors], ["conflicting-bib-key"])

    def test_local_duplicates_and_redundant_upstream_entries_are_distinct_errors(self):
        known = "@article{Known2020, title={Known}}"
        _, errors = merge(known, f"{known}\n{known}", require_thin=True)
        self.assertEqual([error.code for error in errors], ["duplicate-bib-key", "published-bib-key"])

    def test_empty_delta_is_valid_after_cutover(self):
        entries, errors = merge("@book{Known2020, title={Known}}", "% No pending references.\n", require_thin=True)
        self.assertEqual(errors, [])
        self.assertEqual(set(entries), {"Known2020"})

    def test_directives_and_embedded_entry_text_do_not_create_keys(self):
        text = ('@comment{\n@book{Fake2026, title={Ignored}}\n}\n'
                '@string{heading = "A title"}\n'
                '@article{Known2020, title={\n@book{Embedded2026, title={Text}}\n}}\n')
        entries, errors = merge(text, "")
        self.assertEqual(errors, [])
        self.assertEqual(set(entries), {"Known2020"})


def test_merge_cli_preview_write_idempotence_and_conflict(tmp_path, capsys):
    from qatlib_env.workspace import main
    target = tmp_path / "upstream"
    (target / "docs").mkdir(parents=True)
    baseline = b"% Preserve upstream comments\r\n@book{Known2020, title={Known}}\r\n"
    bibliography = target / "docs/references.bib"
    bibliography.write_bytes(baseline)
    increment = tmp_path / "increment.bib"
    addition = '@article(New2026, title="{New} (Result)", note={Nested {value}})'
    increment.write_text(addition + "\n")
    args = ["--project", str(target), "promotion", "merge-bibliography", "--increment", str(increment)]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["written"] is False
    assert bibliography.read_bytes() == baseline
    assert main([*args, "--write"]) == 0
    assert json.loads(capsys.readouterr().out)["added"] == ["New2026"]
    merged = bibliography.read_bytes()
    assert merged.startswith(baseline) and addition.encode() in merged
    assert main([*args, "--write"]) == 0
    assert json.loads(capsys.readouterr().out)["written"] is False
    assert bibliography.read_bytes() == merged
    increment.write_text("@book{Known2020, title={Changed}}\n")
    assert main([*args, "--write"]) == 1
    assert "conflicting-bib-key" in capsys.readouterr().out
    assert bibliography.read_bytes() == merged
