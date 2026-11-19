"""Promotion gates bind scanned source bytes to the consumer lock without writes."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from qatlib_search import promotion
from tests._git_fixture import snapshot


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


class ExactPromotionLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.consumer = self.root / 'consumer'
        (self.consumer / 'ConsumerLib').mkdir(parents=True)
        self.public = self.root / 'public'
        self.module = self.public / 'Qatlib/Concepts/Alpha/Main.lean'
        write(self.module, 'namespace Qatlib.Concepts.Alpha\ndef result := 1\nend Qatlib.Concepts.Alpha\n')
        subprocess.run(['git', 'init', '-q', str(self.public)], check=True)
        self.rev = snapshot(self.public)
        self.lock(self.rev)

    def lock(self, rev):
        write(self.consumer / 'lake-manifest.json', json.dumps({'packages': [
            {'name': 'Qatlib', 'type': 'git', 'rev': rev}]}))

    def test_exact_lock_checks_actual_source_and_only_runs_read_commands(self):
        before = self.module.read_bytes()
        with mock.patch.object(promotion, '_git', wraps=promotion._git) as git:
            self.assertEqual(promotion.verify_locked_source(self.consumer, self.public), self.rev)
        self.assertEqual(before, self.module.read_bytes())
        self.assertTrue(all(call.args[1] in {'rev-parse', 'ls-tree', 'ls-files'}
                            for call in git.call_args_list))

    def test_wrong_head_is_not_fetched_checked_out_or_accepted(self):
        self.lock('0' * 40)
        with self.assertRaisesRegex(ValueError, 'expected .*actual HEAD'):
            promotion.verify_locked_source(self.consumer, self.public, root='Qatlib')
        self.assertEqual(subprocess.check_output(
            ['git', '-C', str(self.public), 'rev-parse', 'HEAD'], text=True).strip(), self.rev)

    def test_manifest_missing_malformed_ambiguous_or_unpinned_fails(self):
        manifest = self.consumer / 'lake-manifest.json'
        for content in ('{', '{}', '{"packages": []}',
                        '{"packages": [{"name":"Qatlib","type":"path","rev":"' + self.rev + '"}]}',
                        '{"packages": [{"name":"Qatlib","rev":"main"}]}',
                        json.dumps({'packages': [{'name':'Qatlib', 'rev':self.rev}] * 2})):
            with self.subTest(content=content):
                write(manifest, content)
                with self.assertRaisesRegex(ValueError, 'invalid dependency lock'):
                    promotion.verify_locked_source(self.consumer, self.public)
        manifest.rename(self.root / 'saved-manifest.json')
        with self.assertRaisesRegex(ValueError, 'invalid dependency lock'):
            promotion.verify_locked_source(self.consumer, self.public)

    def test_source_repository_manifest_does_not_replace_the_consumer_lock(self):
        write(self.public / 'lake-manifest.json', json.dumps({'packages': [
            {'name':'Qatlib', 'rev':'0' * 40}]}))
        self.assertEqual(promotion.verify_locked_source(self.consumer, self.public), self.rev)

    def test_dirty_bytes_are_rejected_even_if_head_matches(self):
        write(self.module, self.module.read_text() + 'def altered := 2\n')
        with self.assertRaisesRegex(ValueError, 'source bytes differ'):
            promotion.verify_locked_source(self.consumer, self.public)

    def test_staged_extra_sources_are_rejected(self):
        extra = self.public / 'Qatlib/Concepts/Alpha/Extra.lean'
        write(extra, 'def extra := 2\n')
        subprocess.run(['git', '-C', str(self.public), 'add', str(extra)], check=True)
        with self.assertRaisesRegex(ValueError, 'inventory differs'):
            promotion.verify_locked_source(self.consumer, self.public)

    def test_untracked_stale_files_are_never_source_providers(self):
        extra = self.public / 'Qatlib/Concepts/Alpha/Stale.lean'
        write(extra, 'def stale := 2\n')
        self.assertEqual(promotion.source_files(self.public, 'Qatlib'), [self.module])
        self.assertEqual(promotion.verify_locked_source(self.consumer, self.public), self.rev)

    def test_missing_tracked_source_and_git_errors_do_not_fall_back(self):
        with mock.patch.object(promotion, '_git', side_effect=ValueError('git unavailable')):
            with self.assertRaisesRegex(ValueError, 'git unavailable'):
                promotion.source_files(self.public, 'Qatlib')
        self.module.rename(self.root / 'saved-module.lean')
        with self.assertRaisesRegex(ValueError, 'missing or redirected'):
            promotion.verify_locked_source(self.consumer, self.public)

    def test_parent_repository_or_object_only_directory_is_not_a_checkout(self):
        nested = self.public / 'nested'
        write(nested / 'Qatlib/Concepts/Alpha/Main.lean', self.module.read_text())
        with self.assertRaisesRegex(ValueError, 'independent Git checkout'):
            promotion.verify_source_revision(nested, self.rev)
        bare = self.root / 'objects.git'
        subprocess.run(['git', 'clone', '--bare', '-q', str(self.public), str(bare)], check=True)
        with self.assertRaisesRegex(ValueError, 'missing source tree'):
            promotion.verify_source_revision(bare, self.rev, root='Qatlib')

    def test_redirected_source_is_rejected(self):
        other = self.root / 'other.lean'
        write(other, self.module.read_text())
        self.module.rename(self.root / 'saved-module.lean')
        self.module.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'redirected Lean source'):
            promotion.verify_locked_source(self.consumer, self.public)

class ParameterizedPromotionTests(unittest.TestCase):
    def test_reviewed_third_party_roots_are_explicit_and_cannot_admit_another_project(self):
        from qatlib_search.promotion_closure import candidate_closure
        with tempfile.TemporaryDirectory() as temp:
            repo, public = Path(temp) / 'dev', Path(temp) / 'public'
            write(repo / 'AltConsumer/Concepts/Example/Main.lean', 'import QuantumInfo.States.Mixed.MState\n')
            write(public / 'Qatlib/Foundations/Helper/Main.lean', 'def helper := 1\n')
            args = (repo, public, ['AltConsumer.Concepts.Example.Main'])
            self.assertTrue(candidate_closure(*args, source_root='AltConsumer').errors)
            self.assertEqual(candidate_closure(*args, source_root='AltConsumer',
                                               external_roots=('QuantumInfo',)).errors, ())
            bad = candidate_closure(*args, source_root='AltConsumer',
                                    external_roots=('QuantumInfo', 'AltConsumer'))
            self.assertEqual(bad.modules, ())
            self.assertIn('project root cannot be declared', bad.errors[0])

    def test_candidate_selection_keeps_private_theorems_but_public_inventory_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            write(repo / 'ConsumerLib/Foundations/Helper/Defs.lean', 'def helper := 1\n')
            write(repo / 'ConsumerLib/Concepts/Alpha/Private.lean',
                  'module\nnamespace ConsumerLib.Concepts.Alpha\n'
                  'theorem hidden : True := True.intro\nend ConsumerLib.Concepts.Alpha\n')
            write(repo / 'ConsumerLib/Research/Research/Main.lean', 'run_cmd pure ()\n')
            self.assertEqual(promotion.theorem_candidates(repo, source_root='ConsumerLib'), ('ConsumerLib.Concepts.Alpha.Private',))
            self.assertEqual(promotion.inventory(repo, 'ConsumerLib',
                allowed_layers=promotion.PUBLIC_LAYERS).declarations, {})

    def test_generic_root_filters_research_layer_and_checks_named_and_private_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            source, public = Path(temp) / 'consumer', Path(temp) / 'public'
            for root in ('AltConsumer', 'Qatlib'):
                repo = source if root == 'AltConsumer' else public
                write(repo / root / 'Concepts/Foo/Private.lean',
                      f'namespace {root}.Concepts.Foo\nprivate def helper := 1\nend {root}.Concepts.Foo\n')
                write(repo / root / 'Concepts/Foo/Named.lean',
                      f'namespace {root}.Concepts.Foo\ndef fact := 1\nend {root}.Concepts.Foo\n')
                write(repo / root / 'Research/Example/Body.lean', 'run_cmd pure ()\n')
            errors, stats = promotion.check_duplicates(source, public,
                source_root='AltConsumer', allowed_layers=promotion.PUBLIC_LAYERS)
            self.assertEqual(stats['source_files'], 2)
            self.assertEqual(stats['duplicate_declarations'], 1)
            self.assertEqual(stats['identical_modules'], 2)
            self.assertFalse(any('cannot index' in error for error in errors))

    def test_scope_names_cannot_escape_the_selected_source_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            for root in ('../outside', '/absolute', 'Foo/Bar'):
                with self.subTest(root=root), self.assertRaisesRegex(ValueError, 'invalid source root'):
                    promotion.source_files(Path(temp), root)
