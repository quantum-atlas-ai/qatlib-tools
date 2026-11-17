"""Cross-library ownership and syntax checks without loading Lean libraries."""
from pathlib import Path
import json
import subprocess
import tempfile
import unittest

from qatlib_env import conflicts as guard
from tests._git_fixture import snapshot


class LibraryConflictsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / 'source'
        self.public = Path(self.temporary.name) / 'public'
        for repo, library in ((self.source, 'Work'), (self.public, 'Published')):
            (repo / library).mkdir(parents=True)
            (repo / 'lakefile.toml').write_text(
                f'defaultTargets = ["{library}"]\n[[lean_lib]]\nname = "{library}"\n')

    def write(self, repo, module, body):
        file = repo / (module.replace('.', '/') + '.lean')
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body)

    def check(self):
        return guard.check(self.source, self.public)[0]

    def test_exact_fqns_detect_collisions_across_different_module_paths(self):
        self.write(self.source, 'Work.Left', 'namespace Shared\ndef value := 1\nend Shared\n')
        self.write(self.public, 'Published.Right', 'namespace Shared\ndef value := true\nend Shared\n')
        self.assertIn('export Shared.value', self.check()[0])

    def test_library_roots_are_not_normalized_for_import_conflicts(self):
        self.write(self.source, 'Work.Left', 'namespace Work\ndef value := 1\nend Work\n')
        self.write(self.public, 'Published.Right', 'namespace Published\ndef value := 1\nend Published\n')
        self.assertEqual(self.check(), [])

    def test_module_mode_and_public_sections_restore_visibility(self):
        self.write(self.source, 'Work.Left', '''module
public section
namespace Shared
def visible := 1
private def hidden := 1
end Shared
end
namespace Shared
def internal := 1
end Shared
''')
        self.write(self.public, 'Published.Right', '''namespace Shared
def visible := true
def hidden := true
def internal := true
end Shared
''')
        self.assertEqual(len(self.check()), 1)
        self.assertIn('Shared.visible', self.check()[0])

    def test_global_operator_overlap_is_reported_but_local_and_scoped_are_isolated(self):
        self.write(self.source, 'Work.Left', 'infixl:70 " ⋆⋆ " => Nat.add\n')
        for scope in ('local ', 'scoped ', ''):
            with self.subTest(scope=scope):
                self.write(self.public, 'Published.Right', scope + 'infixl:70 " ⋆⋆ " => Nat.mul\n')
                errors = self.check()
                self.assertEqual(len(errors), int(not scope))
                if errors:
                    self.assertIn('global syntax', errors[0])

    def test_comments_and_literals_cannot_impersonate_exports_or_syntax(self):
        self.write(self.source, 'Work.Left', 'namespace Shared\ndef value := 1\nend Shared\n')
        self.write(self.public, 'Published.Right', '''/- namespace Shared
/- nested comment -/
def value := true
notation "fake" => True
end Shared -/
def text := "namespace Shared\\ndef value := true"
''')
        self.assertEqual(self.check(), [])

    def test_multiline_macro_header_and_nested_comment_terminals(self):
        rules = guard.global_syntax('''macro "action "
  x:term : term => `(Nat.succ $x)
local notation "ignored" => True
''')
        self.assertEqual(rules, {('term', ('action',))})
        self.assertEqual(guard.terminals('"real" /- "fake" /- "nested" -/ -/ "tail"'),
                         ('real', 'tail'))
        self.assertEqual(guard.global_syntax('@[some_attribute] syntax "action " : term\n'),
                         {('term', ('action',))})

    def test_literal_syntax_and_macro_conflicts_are_checked_without_importing_them(self):
        for header in ('syntax "action " term : term',
                       'macro "action " x:term : term => `(Nat.succ $x)'):
            with self.subTest(header=header):
                self.write(self.source, 'Work.Left', header + '\n')
                self.write(self.public, 'Published.Right', header + '\n')
                self.assertIn('global syntax', self.check()[0])

    def test_same_module_path_is_checked_even_with_disjoint_exports(self):
        (self.public / 'lakefile.toml').write_text(
            'defaultTargets = ["Work"]\n[[lean_lib]]\nname = "Work"\n')
        self.write(self.source, 'Work.Same', 'def first := 1\n')
        self.write(self.public, 'Work.Same', 'def second := 1\n')
        self.assertIn('module Work.Same', self.check()[0])

    def test_srcdir_is_read_from_lake(self):
        (self.source / 'lakefile.toml').write_text(
            'defaultTargets = ["Work"]\n[[lean_lib]]\nname = "Work"\nsrcDir = "src"\n')
        self.write(self.source, 'src.Work.Left', 'def value := 1\n')
        self.assertEqual(list(guard.library_sources(self.source))[0][0], 'Work.Left')

    def test_unsupported_generated_exports_fail_instead_of_passing(self):
        self.write(self.source, 'Work.Left', 'run_cmd IO.println "generate declarations"\n')
        self.write(self.public, 'Published.Right', 'def value := 1\n')
        with self.assertRaisesRegex(ValueError, 'Lean declaration index'):
            self.check()

    def test_root_override_selects_one_ambiguous_library_and_honors_explicit_roots(self):
        (self.source / 'lakefile.toml').write_text('''defaultTargets = ["First", "Second"]
[[lean_lib]]
name = "First"
roots = ["Work"]
srcDir = "src"
[[lean_lib]]
name = "Second"
roots = ["Other"]
''')
        self.write(self.source, 'src.Work.A', 'def value := 1\n')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            list(guard.library_sources(self.source))
        self.assertEqual(list(guard.library_sources(self.source, 'Work'))[0][0], 'Work.A')

    def test_cli_verifies_exact_pin_and_reports_dirty_layout_and_aggregate(self):
        self.write(self.source, 'Work.A', 'namespace Work\ndef value := 1\nend Work\n')
        self.write(self.public, 'Published.B', 'namespace Published\ndef value := 1\nend Published\n')
        self.write(self.public, 'Published', 'import Published.B\n')
        subprocess.run(['git', 'init', '-q', str(self.public)], check=True)
        revision = snapshot(self.public)
        (self.source / 'lake-manifest.json').write_text(json.dumps({'packages': [
            {'name': 'ProviderPackage', 'type': 'git', 'rev': revision}]}))
        args = ['--package', 'ProviderPackage', '--source', str(self.public)]
        self.assertEqual(guard.main(args, project=self.source), 0)
        # A stale untracked source never becomes part of the pinned provider.
        self.write(self.public, 'Published.Stale', 'namespace Work\ndef value := 1\nend Work\n')
        self.assertEqual(guard.main(args, project=self.source), 0)
        self.write(self.public, 'Published', 'def altered := 1\n')
        self.assertEqual(guard.main(args, project=self.source), 2)
        self.write(self.public, 'Published', 'import Published.B\n')
        config = self.public / 'lakefile.toml'
        config.write_text(config.read_text() + '\n-- drift\n')
        self.assertEqual(guard.main(args, project=self.source), 2)

    def test_cli_defaults_to_manifest_packages_directory(self):
        self.write(self.source, 'Work.A', 'def value := 1\n')
        upstream = self.source / '.lake/packages/provider'
        (upstream / 'Published').mkdir(parents=True)
        (upstream / 'lakefile.toml').write_text('defaultTargets=["Published"]\n[[lean_lib]]\nname="Published"\n')
        (upstream / 'Published/B.lean').write_text('def different := 1\n')
        subprocess.run(['git', 'init', '-q', str(upstream)], check=True)
        revision = snapshot(upstream)
        (self.source / 'lake-manifest.json').write_text(json.dumps({'packagesDir': '.lake/packages', 'packages': [
            {'name': 'provider', 'type': 'git', 'rev': revision, 'inherited': True}]}))
        self.assertEqual(guard.main(['--package', 'provider'], project=self.source), 0)
