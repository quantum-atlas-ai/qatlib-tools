"""Offline promotion closure contracts for complete files and import boundaries."""
import tests._bootstrap as _bootstrap  # noqa: F401

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qatlib_search.promotion_closure import candidate_closure


class PromotionClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / 'source'
        self.public = Path(self.temp.name) / 'public'
        (self.source / 'ExampleLibrary').mkdir(parents=True)
        (self.public / 'Qatlib').mkdir(parents=True)

    def write(self, module, text='', *, public=False):
        path = (self.public if public else self.source) / (module.replace('.', '/') + '.lean')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def closure(self, modules, **kwargs):
        kwargs.setdefault('source_root', 'ExampleLibrary')
        kwargs.setdefault('public_root', 'Qatlib')
        kwargs.setdefault('non_promotable_roots', ('Research',))
        kwargs.setdefault('test_roots', ('Tests',))
        return candidate_closure(self.source, self.public, modules, **kwargs)

    def assert_failed(self, result, message):
        self.assertEqual(result.modules, ())
        self.assertTrue(any(message in error for error in result.errors), result.errors)

    def test_whole_file_closure_keeps_definition_only_helpers_and_real_header_imports(self):
        definitions = 'ExampleLibrary.Foundations.Shared.Defs'
        lemma = 'ExampleLibrary.Concepts.Topic.Lemma'
        first = 'ExampleLibrary.Papers.Paper.Result'
        second = 'ExampleLibrary.Papers.Other.Result'
        public = 'Qatlib.Foundations.Published.Defs'
        self.write(definitions, 'import Init\ndef helper : Nat := 1\n')
        self.write(lemma, f'module\npublic meta import {definitions}\ndef value := helper\n')
        self.write(public, 'import Mathlib.Data.Nat.Basic\ndef published : Nat := 1\n', public=True)
        self.write(first, f'''/- import ExampleLibrary.Research.Fake.Result -/
module
public import {lemma}
meta import all {public}
import Lean
import Std
def prose := "import ExampleLibrary.Tests.Fake"
''')
        self.write(second, f'import {definitions}\ndef other := helper\n')
        self.write('ExampleLibrary.Concepts.Unused.Bad', 'import Research.Private\n')
        result = self.closure([first, second, first])
        self.assertEqual(result.errors, ())
        self.assertEqual(set(result.modules), {definitions, lemma, first, second})
        self.assertLess(result.modules.index(definitions), result.modules.index(lemma))
        self.assertLess(result.modules.index(lemma), result.modules.index(first))
        self.assertLess(result.modules.index(definitions), result.modules.index(second))
        self.assertEqual(result.imports[first], (lemma, public, 'Lean', 'Std'))
        self.assertNotIn(public, result.imports)
        self.assertEqual(result, self.closure([second, first]))

    def test_generic_root_and_reviewed_extra_layer_are_explicit_configuration(self):
        helper = 'AltConsumer.Concepts.Shared.Defs'
        question = 'AltConsumer.Experimental.Task.Result'
        self.write(helper, 'def model := 1\n')
        self.write(question, f'import {helper}\ndef claim := model\n')
        self.assert_failed(self.closure([question], source_root='AltConsumer'), 'forbidden layer')
        result = self.closure([question], source_root='AltConsumer',
                              allowed_layers=('Foundations', 'Concepts', 'Papers', 'Experimental'))
        self.assertEqual(result.errors, ())
        self.assertEqual(result.modules, (helper, question))

    def test_invalid_or_missing_explicit_candidates_cannot_produce_partial_plans(self):
        valid = 'ExampleLibrary.Concepts.Topic.Valid'
        self.write(valid)
        for invalid in ('../escape', 'ExampleLibrary/Concepts/Topic', 'ExampleLibrary..Topic',
                        'ExampleLibrary', 'ExampleLibrary.Concepts', 'ExampleLibrary.Tests.Topic',
                        'Qatlib.Concepts.Other', 'ExampleLibrary.Concepts.Missing', None):
            with self.subTest(candidate=invalid):
                result = self.closure([valid, invalid])
                self.assertTrue(result.errors)
                self.assertEqual(result.modules, ())
        self.assert_failed(self.closure([]), 'at least one explicit')
        self.assert_failed(self.closure(valid), 'not a single string')

    def test_missing_local_and_public_providers_report_the_import_source_line(self):
        module = 'ExampleLibrary.Concepts.Topic.Result'
        for missing in ('ExampleLibrary.Foundations.Missing.Defs', 'Qatlib.Foundations.Missing.Defs'):
            with self.subTest(missing=missing):
                path = self.write(module, f'/- License. -/\nmodule\npublic meta import\n  {missing}\n')
                result = self.closure([module])
                self.assert_failed(result, 'missing ' + ('public' if missing.startswith('Qatlib.') else 'local'))
                self.assertTrue(any(f'{path}:4:' in error for error in result.errors), result.errors)

    def test_inventory_exclusions_cannot_be_used_as_import_providers(self):
        module = 'ExampleLibrary.Concepts.Topic.Result'
        missing = 'ExampleLibrary.Concepts.Topic.Excluded'
        self.write(module, f'import {missing}\n')
        excluded = self.write(missing, 'def excluded := 1\n')
        from qatlib_search.promotion import source_files
        def selected(repo, root):
            return [path for path in source_files(repo, root) if path != excluded]
        with patch('qatlib_search.promotion.source_files', side_effect=selected):
            self.assert_failed(self.closure([module]), 'missing local source provider')

    def test_reverse_local_and_public_layer_dependencies_are_rejected(self):
        for first, dependency, is_public in (
                ('ExampleLibrary.Foundations.Topic.Result', 'ExampleLibrary.Concepts.Other.Defs', False),
                ('ExampleLibrary.Concepts.Topic.Result', 'Qatlib.Papers.Paper.Defs', True)):
            with self.subTest(dependency=dependency):
                self.write(first, f'import {dependency}\n')
                self.write(dependency, public=is_public)
                self.assert_failed(self.closure([first]), 'reverse layer dependency')

    def test_unknown_projects_disallowed_layers_and_aggregate_imports_fail_closed(self):
        module = 'ExampleLibrary.Papers.Topic.Result'
        for dependency in ('ExampleLibrary', 'Qatlib', 'ExampleLibrary.Concepts', 'Mathlib',
                           'ExampleLibrary.Research.Task.Result', 'ExampleLibrary.Research.Private',
                           'ExampleLibrary.Tests.Suite', 'AltConsumerTests.Suite', 'Research.Private'):
            with self.subTest(dependency=dependency):
                self.write(module, f'import {dependency}\n')
                self.assert_failed(self.closure([module]), 'forbidden')
        self.write(module, 'import UserLibrary.Topic\n')
        self.assert_failed(self.closure([module]), 'foreign project import')
        self.assertEqual(self.closure([module], external_roots=('UserLibrary',)).errors, ())

    def test_all_reachable_public_providers_are_checked(self):
        candidate = 'ExampleLibrary.Concepts.Topic.Result'
        provider = 'Qatlib.Foundations.Shared.Defs'
        path = self.write(provider, 'import Qatlib.Papers.Missing.Defs\n', public=True)
        self.write(candidate, f'import {provider}\n')
        result = self.closure([candidate])
        self.assert_failed(result, 'missing public source provider')
        self.assertTrue(any(f'{path}:1:' in error for error in result.errors))
        self.assertTrue(any('reverse layer dependency' in error for error in result.errors))

    def test_source_public_and_cross_project_cycles_have_actionable_errors(self):
        candidate = 'ExampleLibrary.Concepts.Topic.Result'
        provider = 'Qatlib.Concepts.Shared.Defs'
        helper = 'ExampleLibrary.Concepts.Topic.Helper'
        self.write(candidate, f'import {helper}\n')
        self.write(helper, f'import {candidate}\n')
        self.assert_failed(self.closure([candidate]), 'import cycle:')
        self.write(candidate, f'import {provider}\n')
        other = 'Qatlib.Concepts.Shared.Other'
        self.write(provider, f'import {other}\n', public=True)
        self.write(other, f'import {provider}\n', public=True)
        result = self.closure([candidate])
        self.assert_failed(result, 'import cycle:')
        self.assertTrue(any('Shared/Defs.lean:1' in error for error in result.errors))
        self.write(provider, f'import {candidate}\n', public=True)
        result = self.closure([candidate])
        self.assert_failed(result, 'public provider')
        self.assertTrue(any('import cycle:' in error for error in result.errors))

    def test_malformed_headers_and_inventory_errors_are_clean_failures(self):
        candidate = 'ExampleLibrary.Concepts.Topic.Result'
        for text in ('import ../escape\n', 'import\n', '/- unterminated'):
            with self.subTest(text=text):
                path = self.write(candidate, text)
                result = self.closure([candidate])
                self.assert_failed(result, 'cannot read import header')
                self.assertTrue(any(str(path) in error for error in result.errors))
        with patch('qatlib_search.promotion.source_files', side_effect=ValueError('Git inventory unavailable')):
            self.assert_failed(self.closure([candidate]), 'Git inventory unavailable')

    def test_read_only_analysis_preserves_source_and_public_bytes(self):
        candidate = 'ExampleLibrary.Concepts.Topic.Result'
        self.write(candidate, 'import Init\nimport Lean\nimport Std\ndef value := 1\n')
        before = {path: path.read_bytes() for path in Path(self.temp.name).rglob('*') if path.is_file()}
        result = self.closure([candidate])
        self.assertEqual(result.errors, ())
        self.assertEqual(result.modules, (candidate,))
        self.assertEqual(before, {path: path.read_bytes() for path in Path(self.temp.name).rglob('*')
                                 if path.is_file()})

    def test_invalid_configuration_is_rejected_before_source_resolution(self):
        candidate = 'ExampleLibrary.Concepts.Topic.Result'
        self.write(candidate)
        for options in ({'source_root': '../outside'}, {'public_root': 'ExampleLibrary'},
                        {'allowed_layers': ('Concepts', 'Concepts')},
                        {'allowed_layers': 'Concepts'}, {'external_roots': 'Lean'}):
            with self.subTest(options=options):
                result = self.closure([candidate], **options)
                self.assertTrue(result.errors)
                self.assertEqual(result.modules, ())

    def test_reviewed_source_layers_do_not_open_research_layers_in_public(self):
        candidate = 'ExampleLibrary.Research.Task.Result'
        provider = 'Qatlib.Research.Task.Defs'
        self.write(candidate, f'import {provider}\n')
        self.write(provider, public=True)
        result = self.closure([candidate],
                              allowed_layers=('Foundations', 'Concepts', 'Papers', 'Research'))
        self.assert_failed(result, 'forbidden layer')
        paper = 'ExampleLibrary.Papers.Paper.Result'
        foundation = 'Qatlib.Foundations.Shared.Defs'
        self.write(paper, f'import {foundation}\n')
        self.write(foundation, public=True)
        self.assertEqual(self.closure([paper], allowed_layers=('Papers',)).errors, ())

    def test_unsafe_inventory_module_names_do_not_become_providers(self):
        candidate = 'ExampleLibrary.Concepts.Topic.Result'
        self.write(candidate)
        self.write('ExampleLibrary.Concepts.bad-name')
        self.assert_failed(self.closure([candidate]), 'unsupported source module name')


if __name__ == '__main__':
    unittest.main()
