"""Documentation evidence and incremental reverse-reference regression tests."""
import json
from pathlib import Path
import tempfile
import unittest

from qatlib_env.trash import trash
from qatlib_search import doc_references as docs
from qatlib_search.lean_source import comments, mask_comments


POLICY = (r"(?<![\w])(?:[A-Za-z][A-Za-z0-9]*\.)?c\d{3,}(?!\d)",
          r"(?i)\b(?:PR|Issue|Ticket)[ -]*#?\d+\b")


class DocReferenceTests(unittest.TestCase):
    def layer(self, files):
        identity = docs.input_identity(files, toolchain="lean4:test", manifest="{}", tree="fixture",
                                       internal_patterns=POLICY)
        return docs.build_layer(files, identity, internal_patterns=POLICY)

    def check(self, before, after, old_keys=None, new_keys=None):
        return docs.check_changes(self.layer(before), self.layer(after), bib_keys=new_keys or {"Known2020"},
                                  before_bib_keys=old_keys, retired_roots=("Retired",))

    def codes(self, diagnostics, severity="error"):
        return [d.code for d in diagnostics if d.severity == severity]

    def test_nested_comments_literals_raw_strings_and_escaped_names_share_the_lexer(self):
        source = ('def example := "/-- [Fake2020] -/"\n'
                  'def raw := r##"/-! [Fake2020] -/"##\n'
                  'def «/-! [Fake2020] -/» := 1\n'
                  '/-! Outer /- inner -/ [Known2020] -/\n-- real\n')
        spans = comments(source)
        self.assertEqual([source[s.start:s.end] for s in spans],
                         ['/-! Outer /- inner -/ [Known2020] -/', '-- real'])
        self.assertEqual(len(mask_comments(source)), len(source))
        refs = docs.analyze_source(source, "Lib/Example.lean").references
        self.assertEqual([(r.kind, r.target) for r in refs], [("citation", "Known2020")])

    def test_long_identifiers_without_binder_colons_do_not_backtrack(self):
        result = docs.analyze_source("def longName := (" + "longIdentifier" * 80 + ")\n", "Lib/Main.lean")
        self.assertEqual(result.problems, [])

    def test_diagnostic_comments_and_fenced_examples_are_not_documentation(self):
        source = '''/-- error: `[MissingKey]` Retired.foo c003 -/
#guard_msgs in
#check unknown
/-! A code example:
```lean
[MissingKey] `Retired.foo` c003
```
~~~lean
`Lib/Missing.lean`
~~~
Normal text [Known2020].
-/
'''
        self.assertEqual(self.codes(self.check({}, {"Lib/Example.lean": source})), [])

    def test_citation_unknown_duplicate_missing_entries_and_math_brackets(self):
        keys, diagnostics = docs.parse_bibliography("@article{Known2020, title={One}}\n@book{Known2020, title={Two}}")
        self.assertEqual(keys, {"Known2020"})
        self.assertEqual(self.codes(diagnostics), ["duplicate-bib-key"])
        self.assertEqual(self.codes(docs.parse_bibliography("% @article{Fake2020,}\n")[1]), ["empty-bibliography"])
        result = self.check({}, {"Lib/Main.lean": "/-! [MissingKey] [Known2020] [Nat] [ABC] [Paper](https://example.org) -/"})
        self.assertEqual(self.codes(result), ["undefined-citation"])

    def test_reference_style_and_canonical_locators(self):
        refs = docs.citation_references("[Author, Year][MissingKey], Theorem 2.1; [Known2020], Lemma 3.")
        self.assertEqual([r.target for r in refs], ["MissingKey", "Known2020"])
        result = self.check({}, {"Lib/Main.lean": "/-! [Known2020, Theorem 2.1] -/"})
        self.assertEqual(self.codes(result), ["citation-locator"])

    def test_math_brackets_are_not_citations_and_diagnostic_lines_are_preserved(self):
        source = "First\n$[U_jk, U_kk]$ and \\([MissingKey]\\)\n$$[E_n]$$\n\\[[H_0,H_1]\\]\n[MissingKey]\n"
        refs = docs.citation_references(source)
        self.assertEqual([(r.target, r.line) for r in refs], [("MissingKey", 5)])
        self.assertEqual(docs.citation_references("Costs $5; see [MissingKey].")[0].target, "MissingKey")
        self.assertEqual(docs.citation_references("[U_jk, U_kk], [E_n, E_m], [H_0,H_1], [K_u]"), [])

    def test_typed_source_excerpt_has_source_citation_scope_only(self):
        source = ('<span data-source-excerpt="true">[HK73], [NS02, Theorem 1], '
                  '`Retired.foo` at `Lib/Missing.lean`; c003</span>\n'
                  '[MissingKey], Theorem 1.\n')
        result = self.check({}, {"docs/reader.md": source})
        self.assertCountEqual(self.codes(result), ["retired-root", "missing-lean-path", "internal-identifier", "undefined-citation"])
        citation = [r for r in result if r.code == "undefined-citation"][0]
        self.assertEqual(citation.line, 2)
        self.assertIn("MissingKey", citation.message)

    def test_unbalanced_typed_excerpt_is_not_a_blanket_suppression(self):
        with self.assertRaisesRegex(ValueError, "unterminated"):
            docs.analyze_document('<span data-source-excerpt="true">[MissingKey]', "docs/reader.md")

    def test_unrelated_markdown_inequalities_cannot_swallow_a_typed_excerpt(self):
        text = ('A note with x<epsilon and a raw <unfinished inequality.\n'
                '<span data-source-excerpt="true">[HK73]</span>\n'
                '[MissingKey]\n')
        result = self.check({}, {"docs/reader.md": text})
        self.assertEqual(self.codes(result), ["undefined-citation"])
        self.assertIn("MissingKey", result[0].message)

    def test_unbalanced_source_backticks_cannot_hide_the_excerpt_closing_tag(self):
        text = '<span data-source-excerpt="true">A literal ` and [HK73]</span> | `Lib.value` | [MissingKey]'
        refs = docs.analyze_document(text, "docs/reader.md").references
        self.assertEqual([r.target for r in refs if r.kind == "citation"], ["MissingKey"])
        self.assertEqual([r.target for r in refs if r.kind == "name"], ["Lib.value"])
        self.assertEqual(docs.strip_source_excerpts('Use `<span data-source-excerpt="true">` literally.'),
                         'Use `<span data-source-excerpt="true">` literally.')

    def test_line_map_handles_empty_final_lines_and_unicode_offsets(self):
        text = "αβ\nline\n"
        locations = docs.SourceLines(text)
        self.assertEqual([locations.line(n) for n in (0, 2, 3, 7, 8)], [1, 1, 2, 2, 3])
        self.assertEqual(locations.context(1), "αβ")

    def test_masked_example_padding_is_removed_without_changing_active_escaped_names(self):
        text = "```lean\n" + "x" * 10000 + "\n```\nUses `Lib.«two  spaces»`.\n"
        result = docs.analyze_document(text, "docs/reader.md")
        self.assertEqual([(r.target, r.line) for r in result.references], [("Lib.«two  spaces»", 4)])

    def test_unchanged_consumer_is_checked_after_provider_deletion(self):
        before = {"Lib/Provider.lean": "namespace Lib\ndef original := 1\nend Lib\n",
                  "Lib/Consumer.lean": "/-! Uses `Lib.original`. -/"}
        after = {**before, "Lib/Provider.lean": "namespace Lib\ndef renamed := 1\nend Lib\n"}
        result = self.check(before, after)
        self.assertEqual(self.codes(result), ["deleted-declaration"])
        self.assertEqual(result[0].path, "Lib/Consumer.lean")

    def test_unchanged_markdown_consumer_is_also_reverse_checked(self):
        before = {"Lib/Provider.lean": "namespace Lib\ndef original := 1\nend Lib\n",
                  "docs/reader.md": "Use `Lib.original`; [Known2020], Theorem 1.\n"}
        after = {**before, "Lib/Provider.lean": ""}
        result = self.check(before, after)
        self.assertEqual(self.codes(result), ["deleted-declaration"])
        self.assertEqual(result[0].path, "docs/reader.md")

    def test_rename_and_consumer_migration_is_atomic(self):
        before = {"Lib/Provider.lean": "namespace Lib\ndef original := 1\nend Lib\n",
                  "Lib/Consumer.lean": "/-! Uses `Lib.original`. -/"}
        after = {path: source.replace("original", "renamed") for path, source in before.items()}
        self.assertEqual(self.check(before, after), [])

    def test_unchanged_citation_consumer_checked_when_bib_key_removed(self):
        sources = {"Lib/Consumer.lean": "/-! [Removed2020], Theorem 1. -/"}
        self.assertEqual(self.codes(self.check(sources, sources, {"Known2020", "Removed2020"})),
                         ["undefined-citation"])

    def test_unknown_names_are_warnings_but_namespaces_and_local_variables_are_not(self):
        source = '''namespace Lib.Example
variable (c003 : Nat)
/-! Namespace `Lib.Example`, local `c003`, unknown `Lib.Example.generated`. -/
def value := c003
end Lib.Example
'''
        result = self.check({}, {"Lib/Example.lean": source})
        self.assertEqual(self.codes(result), [])
        self.assertEqual(self.codes(result, "warning"), ["unknown-name"])

    def test_new_internal_comments_and_declarations_fail_but_existing_debt_does_not(self):
        old = {"Lib/Main.lean": "-- Claim c003 from PR #123\nnamespace Lib\ndef c004 := 1\nend Lib\n"}
        self.assertEqual(self.check(old, old), [])
        added = {"Lib/Main.lean": old["Lib/Main.lean"] + "-- Another Claim c005 from Ticket123\n"}
        self.assertEqual(self.codes(self.check(old, added)), ["internal-identifier", "internal-identifier"])
        renamed = {"Lib/Main.lean": old["Lib/Main.lean"].replace("c004", "c006")}
        self.assertEqual(self.codes(self.check(old, renamed)), ["internal-identifier"])

    def test_comment_edit_reusing_internal_key_is_still_new_prose(self):
        result = self.check({"Lib/Main.lean": "-- Claim c003 is first\n"},
                            {"Lib/Main.lean": "-- Claim c003 is second\n"})
        self.assertEqual(self.codes(result), ["internal-identifier"])

    def test_private_declaration_names_are_also_source_identifiers(self):
        result = self.check({}, {"Lib/Main.lean": "private def c003 := 1\n"})
        self.assertEqual(self.codes(result), ["internal-identifier"])

    def test_new_retired_root_and_lean_paths_fail(self):
        result = self.check({}, {"Lib/Main.lean": "/-! `Retired.value`; `Lib/Missing.lean`; [source](Lib/Other.lean). -/"})
        self.assertCountEqual(self.codes(result), ["retired-root", "missing-lean-path", "missing-lean-path"])

    def test_external_lean_source_urls_are_not_local_paths(self):
        link = "[Source](https://example.org/Lib/Provider.lean#L42); `https://example.org/Lib/Provider.lean`"
        result = self.check({}, {"Lib/Main.lean": "/-! " + link + " -/", "docs/reader.md": link})
        self.assertEqual(self.codes(result), [])
        self.assertEqual(self.codes(self.check({}, {"docs/reader.md": "A `.lean` file extension."})), [])

    def test_removed_module_path_checks_unchanged_consumer(self):
        old = {"Lib/Provider.lean": "def provider := 1\n",
               "Lib/Consumer.lean": "/-! Module `Lib.Provider` at `Lib/Provider.lean`. -/"}
        new = {"Lib/Consumer.lean": old["Lib/Consumer.lean"]}
        self.assertCountEqual(self.codes(self.check(old, new)), ["deleted-module", "missing-lean-path"])

    def test_explicit_name_becoming_generated_is_not_asserted_deleted(self):
        old = {"Lib/Main.lean": "namespace Lib\ndef Object.field := 1\nend Lib\n",
               "Lib/Consumer.lean": "/-! `Lib.Object.field`. -/"}
        new = {**old, "Lib/Main.lean": "namespace Lib\nstructure Object where\n  field : Nat\nend Lib\n"}
        result = self.check(old, new)
        self.assertEqual(self.codes(result), [])
        self.assertEqual(self.codes(result, "warning"), ["generated-name-review"])

    def test_escaped_fqn_roundtrips_without_treating_dot_inside_component_as_namespace(self):
        old = {"Lib/Main.lean": 'namespace Lib\ndef «some.name» := 1\nend Lib\n',
               "Lib/Consumer.lean": '/-! `Lib.«some.name»`. -/'}
        self.assertEqual(self.check({}, old), [])
        new = {**old, "Lib/Main.lean": ""}
        self.assertEqual(self.codes(self.check(old, new)), ["deleted-declaration"])

    def test_dependency_upgrade_rechecks_unchanged_consumer(self):
        consumer = self.layer({"Lib/Main.lean": "/-! `Public.oldName`. -/"})
        result = docs.check_changes(consumer, consumer, bib_keys={"Known2020"},
                                    dependencies_before=[self.layer({"Public/Main.lean": "namespace Public\ndef oldName := 1\nend Public\n"})],
                                    dependencies_after=[self.layer({"Public/Main.lean": "namespace Public\ndef newName := 1\nend Public\n"})])
        self.assertEqual(self.codes(result), ["deleted-declaration"])

    def test_incomplete_dependency_upgrade_is_not_misreported_as_declaration_deletion(self):
        consumer = self.layer({"Lib/Main.lean": "/-! `Public.oldName`. -/"})
        old = self.layer({"Public/Main.lean": "namespace Public\ndef oldName := 1\nend Public\n"})
        unsupported = self.layer({"Public/Main.lean": "run_cmd pure ()\n"})
        for before, after in ((old, unsupported), (unsupported, old)):
            result = docs.check_changes(consumer, consumer, bib_keys={"Known2020"},
                                       dependencies_before=[before], dependencies_after=[after])
            self.assertEqual(self.codes(result), ["required-index-incomplete"])

    def test_moving_provider_preserves_declaration_identity(self):
        old = {"Lib/A.lean": "namespace Lib\ndef value := 1\nend Lib\n",
               "Lib/Consumer.lean": "/-! `Lib.value`. -/"}
        new = {"Lib/B.lean": old["Lib/A.lean"], "Lib/Consumer.lean": old["Lib/Consumer.lean"]}
        self.assertEqual(self.check(old, new), [])

    def test_unsupported_generated_commands_do_not_silently_pass(self):
        result = self.check({}, {"Lib/Generated.lean": "run_cmd pure ()\n"})
        self.assertEqual(self.codes(result), ["source-index-incomplete"])

    def test_cache_rejects_all_stale_input_dimensions_and_payload_corruption(self):
        root = Path(tempfile.mkdtemp(prefix="doc-reference-cache-"))
        try:
            path = root / "index.json"
            layer = self.layer({"Lib/Main.lean": "/-! [Known2020] -/\ndef value := 1\n"})
            docs.write_cache(path, layer)
            self.assertEqual(docs.read_cache(path, layer.identity), layer)
            for key in ("schema", "parser", "toolchain", "manifest", "tree", "sources", "policy"):
                with self.subTest(key=key):
                    self.assertIsNone(docs.read_cache(path, {**layer.identity, key: "changed"}))
            raw = json.loads(path.read_text())
            raw["payload"]["files"]["Lib/Main.lean"]["declarations"] = {"invented": "def"}
            path.write_text(json.dumps(raw))
            self.assertIsNone(docs.read_cache(path, layer.identity))
        finally:
            trash(root)

    def test_per_file_cache_reuses_unchanged_content_across_trees_and_reparses_one_edit(self):
        root = Path(tempfile.mkdtemp(prefix="doc-reference-files-"))
        try:
            sources = {"Lib/Main.lean": "namespace Lib\ndef value := 1\nend Lib\n",
                       "docs/reader.md": "Uses `Lib.value`.\n"}
            def cached(inputs, tree, **kwargs):
                identity = docs.input_identity(inputs, toolchain="lean4:test", manifest="{}", tree=tree,
                                               internal_patterns=POLICY)
                stats = {}
                layer = docs.cached_layer(inputs, identity, root, internal_patterns=POLICY,
                                          statistics=stats, **kwargs)
                return layer, stats
            first, stats = cached(sources, "first")
            self.assertEqual(stats["files_parsed"], 2)
            _, stats = cached(sources, "first")
            self.assertEqual(stats, {"layer_hits": 1, "file_hits": 0, "files_parsed": 0})
            second, stats = cached(sources, "second")
            self.assertNotEqual(first.identity, second.identity)
            self.assertEqual(stats, {"layer_hits": 0, "file_hits": 2, "files_parsed": 0})
            modified = {**sources, "Lib/Main.lean": sources["Lib/Main.lean"].replace("value", "renamed")}
            changed, stats = cached(modified, "edit")
            self.assertEqual(stats, {"layer_hits": 0, "file_hits": 1, "files_parsed": 1})
            self.assertEqual(self.codes(docs.check_changes(first, changed, bib_keys={"Known2020"})), ["deleted-declaration"])
            _, stats = cached(sources, "declaration-mode", prefix="dependency", documentation=False)
            self.assertEqual(stats["files_parsed"], 1)  # Markdown mode is shared; Lean mode differs.
            for path in (root / "files").glob("*.json"):
                envelope = json.loads(path.read_text())
                if envelope["payload"]["identity"]["path"] == "docs/reader.md":
                    envelope["checksum"] = "corrupt"
                    path.write_text(json.dumps(envelope))
            _, stats = cached(sources, "fresh-tree")
            self.assertEqual(stats["files_parsed"], 1)
        finally:
            trash(root)


if __name__ == "__main__":
    unittest.main()
