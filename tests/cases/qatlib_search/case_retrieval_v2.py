"""Exercise v2 source discovery and Claim joins through the retrieval APIs."""
from __future__ import annotations

import tests._bootstrap as _bootstrap  # noqa: F401
from tests._git_fixture import snapshot

import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from qatlib_search.source_roots import SourceCatalog, lean_path
from qatlib_search import build_search_index, check_search_index, cross_proof_index, enrich_search_index, index_identity, proof_index
from qatlib_search.lean_doc_source import parse_module_text
from qatlib_search.lean_declarations import declaration_kinds
from qatlib_search.lean_source import mask_static_tactic_macros
from qatlib_search.query_library import LibraryIndex, search_theorem, snapshot_block


class ModuleSyntaxTests(unittest.TestCase):
    def parse(self, source):
        return parse_module_text(source, Path('Qatlib/Concepts/Flat.lean'), Path('.'))

    def assert_public_names(self, source, expected):
        self.assertEqual({'.'.join(name) for name in declaration_kinds(source)}, expected)
        self.assertEqual({decl.fqn for decl in self.parse(source).decls}, expected)

    def test_module_visibility_follows_nested_public_section_scope(self):
        source = '''module
public import Mathlib.Data.Nat.Basic
namespace Example
/-- A module-local helper. -/
def hidden : Nat := 0
/-- An explicitly exported value. -/
public def explicit : Nat := 1
public section Exports
variable (n : Nat)
noncomputable section Internal
/-- A section-exported identity. -/
theorem inside : n = n := rfl
/-- A private helper inside a public section. -/
private theorem hiddenInside : n = n := rfl
end Internal
namespace Nested
/-- A nested exported identity. -/
theorem nested : n = n := rfl
end Nested
end Exports
/-- A helper after the public section closes. -/
def hiddenAfter : Nat := 2
/-- An explicitly exported theorem after the section. -/
public theorem outside : True := True.intro
end Example
'''
        self.assert_public_names(source, {
            'Example.explicit', 'Example.inside', 'Example.Nested.nested', 'Example.outside'})
        decls = {decl.fqn: decl for decl in self.parse(source).decls}
        self.assertEqual(decls['Example.inside'].variable_context, ('variable (n : Nat)',))
        self.assertEqual(decls['Example.outside'].variable_context, ())
        self.assertEqual(decls['Example.inside'].doc, 'A section-exported identity.')

    def test_staging_header_preserves_attributed_public_section_and_docstrings(self):
        source = '''\ufeff/- Copyright and license. -/
module
public import Qatlib.Concepts.Shared
public meta import Lean

@[expose] public section
/-!
# An exported module

Exported results from the staging syntax.
-/
noncomputable section
namespace Qatlib.Concepts.Flat
/-- The exported bound holds. -/
theorem bound : 0 ≤ (1 : Nat) := Nat.zero_le 1
end Qatlib.Concepts.Flat
'''
        self.assert_public_names(source, {'Qatlib.Concepts.Flat.bound'})
        module = self.parse(source)
        self.assertEqual(module.header, 'Exported results from the staging syntax.')
        self.assertEqual(module.decls[0].doc, 'The exported bound holds.')
        self.assertEqual(module.decls[0].signature, ': 0 ≤ (1 : Nat)')

    def test_comments_and_strings_do_not_change_module_visibility(self):
        source = '''module
-- public section
/- public section
theorem fake : True := True.intro
-/
public def text : String := "public section
theorem fakeString : True := True.intro"
def hidden : Nat := 0
public theorem exported : True := True.intro
'''
        self.assert_public_names(source, {'text', 'exported'})
        legacy = '''/- module -/
def module' : Nat := 0
public theorem exported : True := True.intro
'''
        self.assert_public_names(legacy, {"module'", 'exported'})

    def test_module_header_does_not_enable_generated_declaration_scanning(self):
        source = 'module\npublic section\nrun_cmd pure ()\n'
        for parser in (declaration_kinds, self.parse):
            with self.subTest(parser=parser.__name__):
                with self.assertRaisesRegex(ValueError, 'generated declarations'):
                    parser(source)

    def test_parameterized_term_application_macros_keep_following_declarations(self):
        source = '''module
public section
namespace Example
/-- Internal matrix notation. -/
macro "matmul " a:term:max b:term:max : term =>
  `(@HMul.hMul _ _ _ Matrix.instHMulOfFintypeOfMulOfAddCommMonoid $a $b)
/-- The exported multiplication identity. -/
theorem exported : True := True.intro
end Example
'''
        masked = mask_static_tactic_macros(source)
        self.assertEqual(len(masked), len(source))
        self.assertEqual([i for i, char in enumerate(masked) if char == '\n'],
                         [i for i, char in enumerate(source) if char == '\n'])
        self.assert_public_names(source, {'Example.exported'})
        declaration, = self.parse(source).decls
        self.assertEqual(declaration.doc, 'The exported multiplication identity.')
        self.assertEqual(declaration.line, source[:source.index('theorem exported')].count('\n') + 1)

    def test_term_macro_argument_splices_and_parentheses_are_structural(self):
        for prefix in ('', 'local ', 'scoped '):
            with self.subTest(prefix=prefix):
                source = prefix + 'macro "pair " a:term b:term:42 : term => `(term| Prod.mk ($a) ($b))\n'
                source += 'theorem result : True := True.intro\n'
                self.assert_public_names(source, {'result'})

    def test_term_macro_metaprograms_and_undeclared_splices_fail_closed(self):
        unsupported = [
            'macro "bad " a:term : command => `(command| theorem fake : True := by trivial)',
            'macro "bad " a:term : term => `(by run_tac addDecl)',
            'macro "bad " a:term : term => do pure a',
            'macro "bad " a:term : term => `($missing)',
            'macro "bad " a:term : term => `($(a))',
            'macro "bad " a:term : term => `(`($a))',
            'macro "bad " a:term a:term : term => `($a)',
            'macro "bad " a:term : term => `(Prod.mk ($a])',
            'macro "bad " a:term : term => `(Prod.mk $a) >>= pure',
            'macro "bad " a:term : term => `(Prod.mk $a)\n  >>= pure',
            'macro "bad " a:term : term => `($a)\nrun_cmd pure ()',
        ]
        for body in unsupported:
            for parser in (declaration_kinds, self.parse):
                with self.subTest(body=body, parser=parser.__name__):
                    with self.assertRaises(ValueError):
                        parser(body + '\ntheorem real : True := True.intro\n')

    def test_comments_and_strings_do_not_impersonate_term_macros(self):
        source = '''/- macro "bad " a:term : term => `($missing) -/
def message := "macro not executable"
theorem result : True := True.intro
'''
        self.assert_public_names(source, {'message', 'result'})


class RetrievalV2Tests(unittest.TestCase):
    def test_git_environment_cannot_replace_validated_source_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            actual, other = (Path(directory) / name for name in ('actual', 'other'))
            relative = 'Project/Foundations/Topic/Core/Main.lean'
            heads, blobs = [], []
            environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            for root, value in ((actual, 1), (other, 2)):
                root.mkdir()
                def git(*arguments):
                    return subprocess.check_output(['git', '-C', str(root), *arguments], env=environment)
                git('init', '-q')
                source = root / relative
                source.parent.mkdir(parents=True)
                source.write_text(f'def value := {value}\n')
                if root == other:
                    (root / 'Project/Foundations/Topic/Core/Foreign.lean').write_text('def foreign := 3\n')
                snapshot(root, environment)
                heads.append(git('rev-parse', 'HEAD').decode().strip())
                blobs.append(git('rev-parse', 'HEAD:' + relative).decode().strip())
            self.assertNotEqual(blobs[0], blobs[1])
            with patch.dict(os.environ, {'GIT_DIR': str(other / '.git'), 'GIT_WORK_TREE': str(actual)}):
                self.assertEqual(index_identity.snapshot(actual, [relative]), {relative: blobs[0]})
                self.assertEqual(build_search_index.git_head(actual), heads[0])
                self.assertEqual(build_search_index.tracked_modules(actual), [relative[:-5].replace('/', '.')])
                self.assertEqual(proof_index.git_head(actual), heads[0])
                self.assertEqual(proof_index.fingerprint(actual), {relative: blobs[0]})

    def test_export_provenance_uses_bundled_asset_without_dev_tool_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            subprocess.run(['git', 'init', '-q'], cwd=root, env=environment, check=True)
            source = root / 'Qatlib/Foundations/Topic/Core/Main.lean'
            source.parent.mkdir(parents=True)
            source.write_text('def value := 1\n')
            (root / 'lean-toolchain').write_text('leanprover/lean4:v4.35.0')
            (root / 'lake-manifest.json').write_text('{"packages": []}')
            (root / 'lakefile.toml').write_text('name="consumer"')
            snapshot(root, environment)
            provenance = build_search_index.input_provenance(root, ['Qatlib.Foundations.Topic.Core.Main'])
            self.assertIsNone(provenance['exporter_git_blob'])
            self.assertEqual(len(provenance['exporter_sha256']), 64)
            index = root / 'index'; index.mkdir()
            (index / 'declarations.jsonl').write_text('{}\n')
            self.assertEqual(len(enrich_search_index.review_snapshot_sha256(
                root, index, ['Qatlib.Foundations.Topic.Core.Main'])), 64)

    def test_frozen_index_binds_metadata_from_its_separate_dev_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            frozen, dev = (Path(directory) / name for name in ('frozen', 'dev'))
            frozen.mkdir()
            metadata = dev / 'metadata'
            papers = metadata / 'papers'
            papers.mkdir(parents=True)
            paper = papers / 'Example25.json'
            paper.write_text('{"id":"Example25","claims":[]}')
            environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
            def git(*arguments):
                return subprocess.check_output(['git', '-C', str(dev), *arguments], env=environment)
            git('init', '-q')
            snapshot(dev, environment)
            expected = git('rev-parse', 'HEAD:metadata/papers/Example25.json').decode().strip()
            index_dir = frozen / 'index'
            index_dir.mkdir()
            for name in ('declarations.jsonl', 'summary.json', 'enriched.jsonl'):
                (index_dir / name).write_text('{}\n')
            provenance = enrich_search_index.enrichment_provenance(frozen, index_dir, metadata)
            self.assertEqual(provenance['metadata_git_blob'], {'papers/Example25.json': expected})
            (index_dir / 'enriched-summary.json').write_text(json.dumps(provenance))
            self.assertEqual(enrich_search_index.enrichment_errors(frozen, index_dir, metadata), [])
            original = paper.read_bytes()
            fqn = 'QuantumAlgorithm.Foundations.Topic.Core.bound'
            paper.write_text(json.dumps({'id': 'Example25', 'claims': [{
                'id': 'c001', 'label': 'Transient source label', 'status': 'active',
                'covered_by': [{'fqn': fqn, 'coverage': 'partial_support'}]}]}))
            consumed = {}
            with patch.object(build_search_index, 'source_bindings', return_value={}):
                joined, _ = enrich_search_index.enrich_records([{
                    'fqn': fqn, 'module': 'QuantumAlgorithm.Foundations.Topic.Core.Main'}],
                    frozen, papers, None, 'fixture', consumed_metadata=consumed)
            self.assertEqual(joined[0]['result_labels'], ['Transient source label'])
            paper.write_bytes(original)
            # Even though current files match Git again, the enriched rows
            # must not be certified against metadata that was not consumed.
            with self.assertRaisesRegex(ValueError, 'consumed claim metadata differs'):
                enrich_search_index.enrichment_provenance(
                    frozen, index_dir, metadata, consumed_metadata=consumed)
            stable = {}
            enrich_search_index.load_papers_metadata(papers, stable)
            self.assertEqual(enrich_search_index.metadata_bindings(metadata, stable),
                             {'papers/Example25.json': expected})
            paper.write_text('{"id":"Example25","claims":[],"changed":true}')
            with self.assertRaisesRegex(ValueError, 'modified tracked sources'):
                enrich_search_index.enrichment_provenance(frozen, index_dir, metadata)
            self.assertTrue(enrich_search_index.enrichment_errors(frozen, index_dir, metadata))

    def test_validation_binds_the_claim_metadata_actually_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / 'metadata/papers'
            metadata.mkdir(parents=True)
            path = metadata / 'Example25.json'
            fqn = 'QuantumAlgorithm.Foundations.Topic.Core.bound'
            original = {'id': 'Example25', 'identity': {}, 'claims': [
                {'id': 'c001', 'label': 'Original', 'status': 'active', 'covered_by': [
                    {'fqn': fqn, 'coverage': 'partial_support', 'note': 'Original restriction'}]}]}
            original_bytes = json.dumps(original).encode()
            transient = json.loads(original_bytes)
            transient['claims'][0]['covered_by'][0]['note'] = 'Transient source restriction'
            index_dir = root / 'index'
            index_dir.mkdir()
            (index_dir / 'summary.json').write_text('{}')
            (index_dir / 'enriched.jsonl').write_text(json.dumps({
                'fqn': fqn, 'module': 'QuantumAlgorithm.Foundations.Topic.Core.Main',
                'kind': 'theorem', 'lean_type': 'True', 'result_labels': ['Original']}) + '\n')
            path.write_bytes(json.dumps(transient).encode())
            index = LibraryIndex(index_dir, root / 'metadata')
            self.assertIn('Transient source restriction', next(iter(index.source_coverage[fqn])))
            # Current source/index checks can pass after the file is restored;
            # they cannot authenticate different metadata already held in memory.
            path.write_bytes(original_bytes)
            with patch.object(build_search_index, 'provenance_errors', return_value=[]), \
                    patch.object(enrich_search_index, 'enrichment_errors', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'loaded claim metadata changed'):
                    index.validate_current(root)
                current = LibraryIndex(index_dir, root / 'metadata')
                current.validate_current(root)
                added = metadata / 'Added.json'
                added.write_text('{"id":"Added","claims":[]}')
                with self.assertRaisesRegex(ValueError, 'metadata file set changed'):
                    current.validate_current(root)

    def test_sources_include_all_four_levels_and_keep_exact_owner(self):
        self.assertEqual(index_identity.MODULE_ROOTS, ())
        self.assertFalse(lean_path('UnconfiguredRoot/Research/Topic/Core/A.lean'))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dev = root / 'Project/Concepts/Topic/Core/A.lean'
            dev.parent.mkdir(parents=True)
            dev.write_text('def x := 1\n')
            catalog = SourceCatalog(root)
            self.assertEqual(index_identity.module_roots(root), ('Project',))
            self.assertEqual(catalog.resolve(dev.relative_to(root).as_posix()).path, dev)
            # The same suffix in another module root must never act as an alias.
            with self.assertRaisesRegex(ValueError, 'invalid Lean source path'):
                catalog.resolve('ExternalProject/Concepts/Topic/Core/A.lean')

    def test_paper_namespace_is_papers_in_both_repositories(self):
        derive = enrich_search_index.derive_paper
        self.assertEqual(derive('QuantumAlgorithm.Papers.Example.Core.Main'), 'Example')
        self.assertEqual(derive('Project.Papers.Example.Core.Main'), 'Example')
        self.assertIsNone(derive('QuantumAlgorithm.Concepts.Example.Core.Main'))
        self.assertIsNone(derive('QuantumAlgorithm.Papers.Example'))

    def test_active_claim_evidence_and_open_targets_reach_query_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            metadata = root / 'metadata/papers'
            metadata.mkdir(parents=True)
            fqn = 'QuantumAlgorithm.Foundations.Example.Core.bound'
            paper = {'id': 'Example25', 'identity': {'title': 'Example theorem paper',
                     'authors': ['A Researcher'], 'arxiv_id': '2501.12345'}, 'claims': [
                {'id': 'c001', 'label': 'Theorem 1', 'status': 'active',
                 'content': 'Corrected source coefficient', 'claim-rank': 'B',
                 'binds': ['Example25.Core', 'Example.Core'], 'binding_note': 'Printed formula omits a factor', 'covered_by': [
                    {'fqn': fqn, 'coverage': 'partial_support', 'note': 'Finite input only'}]},
                {'id': 'c002', 'label': 'Theorem 2', 'status': 'active',
                 'content': 'Uniform budget remains open', 'claim-rank': 'C',
                 'binds': ['Example25.Budget', 'Example.Core'], 'binding_note': 'No exact sign without gap', 'covered_by': []},
                {'id': 'c003', 'label': 'Retired result', 'status': 'retired', 'covered_by': [
                    {'fqn': fqn, 'coverage': 'equivalent'}]},
            ]}
            (metadata / 'Example25.json').write_text(json.dumps(paper))
            joined = enrich_search_index.theorem_link_index(
                enrich_search_index.load_papers_metadata(metadata))
            self.assertEqual(joined[fqn]['pairs'], [('Theorem 1', 'partial_support')])
            index_dir = root / 'index'
            index_dir.mkdir()
            (index_dir / 'summary.json').write_text('{}')
            record = {'fqn': fqn, 'kind': 'theorem', 'lean_type': 'True',
                      'module': 'QuantumAlgorithm.Foundations.Example.Core.Main',
                      'result_labels': ['Theorem 1']}
            (index_dir / 'enriched.jsonl').write_text(json.dumps(record) + '\n')
            index = LibraryIndex(index_dir, root / 'metadata')
            self.assertEqual(index.resolve_paper('2501.12345')[0], 'Example25')
            self.assertEqual(index.paper_domain('Example25'), [record])
            self.assertEqual(index.resolve_theorem('Theorem 1')[0], [record])
            self.assertEqual(index.resolve_theorem('Theorem 2')[1][0]['paper'], 'Example25')
            self.assertEqual(index.resolve_theorem('Retired result'), ([], []))
            notes = [json.loads(value) for value in index.source_coverage[fqn]]
            self.assertEqual(notes[0]['note'], 'Finite input only')
            self.assertEqual(notes[0]['claim_id'], 'c001')
            self.assertEqual(notes[0]['claim_rank'], 'B')
            report, _ = search_theorem(index, root, 'Theorem 1', 5)
            scope_note = report['results'][0]['scope']['scope_note']
            context = json.loads(scope_note.split('\nmetadata coverage: ', 1)[1])[0]
            self.assertEqual(context['binding_note'], 'Printed formula omits a factor')
            self.assertEqual(context['content'], 'Corrected source coefficient')
            self.assertEqual(context['binds'], ['Example25.Core', 'Example.Core'])
            self.assertEqual(report['outcome'], 'RELATED_ONLY')
            self.assertFalse(report['results'][0]['validation']['application_closed'])
            open_report, _ = search_theorem(index, root, 'Theorem 2', 5)
            self.assertEqual(open_report['outcome'], 'NO_MATCH')
            open_context = next(value for value in open_report['search_scope']['limitations']
                                if value.startswith('open-target metadata notes: '))
            self.assertIn('No exact sign without gap', open_context)
            self.assertIn('Example25.Budget', open_context)

    def test_existing_statement_named_directory_has_no_special_obligation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = 'QuantumAlgorithm.Papers.Example.Core.Statements.Helper'
            path = root / (module.replace('.', '/') + '.lean')
            path.parent.mkdir(parents=True)
            path.write_text('theorem helper : True := True.intro\n')
            records = [{'module': module, 'kind': 'theorem'}]
            self.assertEqual(check_search_index.check_modules(records, [module], root), [])

    def test_shared_reader_retains_inline_doc_and_canonical_namespace(self):
        source = ('namespace QuantumAlgorithm.Research.Example.Core\n'
                  '/-- The bound holds for every input. -/\n'
                  'theorem bound : True := True.intro\n'
                  'end QuantumAlgorithm.Research.Example.Core\n')
        module = parse_module_text(source, Path('QuantumAlgorithm/Research/Example/Core/Main.lean'), Path('.'))
        self.assertEqual(module.decls[0].fqn, 'QuantumAlgorithm.Research.Example.Core.bound')
        self.assertEqual(module.decls[0].doc, 'The bound holds for every input.')


class CrossProofIndexTests(unittest.TestCase):
    def git(self, root, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        return subprocess.check_output(['git', '-C', str(root), *arguments], env=environment).decode().strip()

    def commit(self, root):
        return snapshot(root)

    def fixture(self, directory):
        dev = Path(directory) / 'dev'
        public = dev / '.lake/packages/Qatlib'
        public.mkdir(parents=True)
        self.git(public, 'init', '-q')
        self.git(public, 'remote', 'add', 'origin', 'https://github.com/quantum-atlas-ai/qatlib-staging.git')
        source = public / 'Qatlib/Concepts/CliffordTheory/Algebra/Main.lean'
        source.parent.mkdir(parents=True)
        source.write_text('namespace Qatlib.Concepts.CliffordTheory.Algebra\n'
                          '/-- Common Clifford lemma. -/\n'
                          'theorem clifford_lemma : True := True.intro\n'
                          'end Qatlib.Concepts.CliffordTheory.Algebra\n')
        (public / 'lean-toolchain').write_text('leanprover/lean4:v4.35.0\n')
        revision = self.commit(public)
        (dev / 'lean-toolchain').write_bytes((public / 'lean-toolchain').read_bytes())
        (dev / 'lake-manifest.json').write_text(json.dumps({
            'version': '1.1.0', 'packagesDir': '.lake/packages', 'packages': [{
                'name': 'Qatlib', 'type': 'git', 'rev': revision,
                'url': 'https://github.com/quantum-atlas-ai/qatlib-staging.git'}]}))
        self.git(dev, 'init', '-q')
        self.git(dev, 'remote', 'add', 'origin', 'git@github.com:quantum-atlas-ai/qatlib-tools.git')
        (dev / '.gitignore').write_text('.lake/\n.qatlas/\n')
        local = dev / 'Project/Research/Example/Core/Main.lean'
        local.parent.mkdir(parents=True)
        local.write_text('/-- Local Clifford consumer. -/\n'
                         'theorem clifford_consumer : True := True.intro\n')
        self.commit(dev)
        return dev, public, source, revision

    def test_qatlib_local_catalog_needs_no_release_or_build_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            dev, public, source, revision = self.fixture(directory)
            answer = cross_proof_index.search(dev, 'clifford', scope='all')
            self.assertFalse(answer['incomplete'], answer['corpora'])
            self.assertEqual([c['corpus'] for c in answer['corpora']], ['qatlib', 'dev'])
            evidence = answer['corpora'][0]
            self.assertEqual(evidence['origin'], 'pinned-local-source')
            self.assertTrue(evidence['matches_dependency_pin'])
            self.assertFalse(evidence['publication_verified'])
            public_hits = [hit for hit in answer['results'] if hit['corpus'] == 'qatlib']
            self.assertEqual(len(public_hits), 1)
            self.assertEqual(public_hits[0]['source_path'], str(source))
            self.assertIn('/blob/' + revision + '/Qatlib/', public_hits[0]['source_url'])
            self.assertTrue((dev / '.qatlas/qatlib-proof-index' / (revision + '.json')).is_file())
            self.assertFalse((dev / '.qatlas/frozen-proof-index').exists())
            self.assertFalse((public / '.lake/build').exists())
            with patch.object(proof_index, 'build', side_effect=AssertionError('unexpected rebuild')):
                reused = cross_proof_index.search(dev, 'clifford', scope='frozen', offline=True)
            self.assertFalse(reused['incomplete'])
            self.assertEqual(reused['scope'], 'qatlib')

    def test_modified_public_source_is_an_error_not_an_empty_corpus(self):
        with tempfile.TemporaryDirectory() as directory:
            dev, public, source, _ = self.fixture(directory)
            cross_proof_index.qatlib_index(dev)
            source.write_text(source.read_text() + '\n-- modified source\n')
            answer = cross_proof_index.search(dev, 'clifford', scope='qatlib')
            self.assertTrue(answer['incomplete'])
            self.assertEqual(answer['results'], [])
            self.assertIn('modified tracked sources', answer['corpora'][0]['error'])

    def test_qatlib_lean_scope_ignores_owned_document_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            dev, public, source, _ = self.fixture(directory)
            docs = public / 'docs/guide.md'
            docs.parent.mkdir()
            docs.write_text('Published guide.\n')
            (public / 'README.md').write_text('Published readme.\n')
            self.commit(public)
            docs.write_text('Uncommitted guide.\n')
            (public / 'README.md').write_text('Uncommitted readme.\n')
            with self.assertRaisesRegex(ValueError, 'modified tracked sources'):
                proof_index.build(public)
            answer = cross_proof_index.search(dev, 'clifford', scope='qatlib', public_repo=public)
            self.assertFalse(answer['incomplete'], answer['corpora'])
            self.assertEqual(answer['corpora'][0]['source_scope'], 'qatlib-lean')
            value, _, _ = cross_proof_index.qatlib_index(dev, public_repo=public)
            self.assertEqual(set(value['source_git_blob']), {source.relative_to(public).as_posix()})
            source.write_text(source.read_text() + '\n-- uncommitted Lean change\n')
            rejected = cross_proof_index.search(dev, 'clifford', scope='qatlib', public_repo=public)
            self.assertTrue(rejected['incomplete'])
            self.assertEqual(rejected['results'], [])

    def test_checkout_must_match_lock_unless_explicitly_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            dev, public, source, locked = self.fixture(directory)
            source.write_text(source.read_text() + '\n-- a later committed source\n')
            current = self.commit(public)
            self.assertNotEqual(current, locked)
            with self.assertRaisesRegex(ValueError, 'differs from the dependency lock'):
                cross_proof_index.qatlib_pin(dev)
            answer = cross_proof_index.search(dev, 'clifford', scope='qatlib', public_repo=public)
            self.assertFalse(answer['incomplete'], answer['corpora'])
            evidence = answer['corpora'][0]
            self.assertEqual(evidence['origin'], 'explicit-local-source')
            self.assertEqual(evidence['commit'], current)
            self.assertEqual(evidence['dependency_revision'], locked)
            self.assertFalse(evidence['matches_dependency_pin'])

    def test_public_toolchain_and_repository_identity_are_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            dev, public, _, _ = self.fixture(directory)
            (public / 'lean-toolchain').write_text('leanprover/lean4:v4.36.0\n')
            with self.assertRaisesRegex(ValueError, 'toolchains differ'):
                cross_proof_index.qatlib_pin(dev)
            manifest = json.loads((dev / 'lake-manifest.json').read_text())
            manifest['packages'][0]['name'] = 'qatlas_frozen'
            (dev / 'lake-manifest.json').write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'source checkout is unavailable'):
                cross_proof_index.qatlib_pin(dev)
            self.assertEqual(cross_proof_index.current_repository(dev), 'quantum-atlas-ai/qatlib-tools')

    def test_query_snapshot_skips_non_git_fixture_but_rejects_dirty_real_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'exported/index'
            outside.mkdir(parents=True)
            (root / 'lake-manifest.json').write_text('{}')
            exported = SimpleNamespace(index_dir=outside, summary={}, records=[])
            self.assertIsNone(snapshot_block(exported)['lockfile_git_blob'])
            dev, _, _, _ = self.fixture(root / 'fixture')
            index_dir = dev / '.qatlas/index'
            index_dir.mkdir(parents=True)
            indexed = SimpleNamespace(index_dir=index_dir, summary={}, records=[])
            self.assertIsNotNone(snapshot_block(indexed)['lockfile_git_blob'])
            (dev / 'lake-manifest.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'modified tracked sources'):
                snapshot_block(indexed)

    def test_cached_records_cannot_omit_or_duplicate_committed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            _, public, _, _ = self.fixture(directory)
            admitted = proof_index.build(public, source_scope='qatlib-lean')
            proof_index.admit(admitted, public)
            primary = next(record for record in admitted['records'] if record['kind'] == 'source')
            for label, records in (
                    ('missing', [r for r in admitted['records'] if r is not primary]),
                    ('duplicate', [*admitted['records'], dict(primary)])):
                with self.subTest(label=label):
                    qatlib_forged = {**admitted, 'records': records,
                              'records_sha256': proof_index.digest(proof_index.json_bytes(records))}
                    with self.assertRaisesRegex(ValueError, 'full source record'):
                        proof_index.admit(qatlib_forged, public)

    def test_qatlib_forged_cache_text_is_rejected_even_with_recomputed_catalog_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            _, public, _, _ = self.fixture(directory)
            admitted = proof_index.build(public, source_scope='qatlib-lean')
            qatlib_forged = copy.deepcopy(admitted)
            qatlib_forged['records'][0]['text'] += '\n-- text never present in the committed source\n'
            with self.assertRaisesRegex(ValueError, 'recorded hash'):
                proof_index.admit(qatlib_forged, public)
            qatlib_forged['records_sha256'] = proof_index.digest(proof_index.json_bytes(qatlib_forged['records']))
            with self.assertRaisesRegex(ValueError, 'differs from committed Git blob'):
                proof_index.admit(qatlib_forged, public)
            qatlib_forged_doc = copy.deepcopy(admitted)
            next(record for record in qatlib_forged_doc['records'] if record['kind'] == 'doc')['text'] = 'qatlib_forged doc snippet'
            qatlib_forged_doc['records_sha256'] = proof_index.digest(proof_index.json_bytes(qatlib_forged_doc['records']))
            with self.assertRaisesRegex(ValueError, 'verified source derivation'):
                proof_index.admit(qatlib_forged_doc, public)

    def test_markdown_records_are_complete_and_bound_to_committed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            _, public, _, _ = self.fixture(directory)
            (public / 'README.md').write_text('Committed λ documentation.\n')
            self.commit(public)
            admitted = proof_index.build(public)
            proof_index.admit(admitted, public)
            for label in ('missing', 'qatlib_forged'):
                with self.subTest(label=label):
                    qatlib_forged = copy.deepcopy(admitted)
                    if label == 'missing':
                        qatlib_forged['records'] = [r for r in qatlib_forged['records'] if r['path'] != 'README.md']
                    else:
                        next(r for r in qatlib_forged['records'] if r['path'] == 'README.md')['text'] = 'Uncommitted prose.'
                    qatlib_forged['records_sha256'] = proof_index.digest(proof_index.json_bytes(qatlib_forged['records']))
                    with self.assertRaises(ValueError):
                        proof_index.admit(qatlib_forged, public)

    def test_build_rejects_transient_read_bytes_that_do_not_match_its_git_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            _, public, source, _ = self.fixture(directory)
            original_read = Path.read_bytes
            original_bytes = original_read(source)
            def transient_read(path):
                data = original_read(path)
                return data + b'\n-- transient dirty Lean bytes\n' if path == source else data
            with patch.object(Path, 'read_bytes', transient_read):
                with self.assertRaisesRegex(ValueError, 'consumed source bytes differ'):
                    proof_index.build(public, source_scope='qatlib-lean')
            self.assertEqual(source.read_bytes(), original_bytes)


if __name__ == '__main__':
    unittest.main()
