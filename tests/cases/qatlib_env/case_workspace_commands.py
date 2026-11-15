"""Lake command classification and safe portable workflow composition."""
import tests._bootstrap as _bootstrap  # noqa: F401
import base64
from contextlib import nullcontext
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from qatlib_env import workspace as ws


class WorkspaceCommandsTests(unittest.TestCase):
    def test_bootstrap_no_prepare_is_an_explicit_skip(self):
        with patch.object(ws, "environment", return_value={}), patch("qatlib_env.prepare.prepare") as prepare:
            self.assertEqual(ws.main(["bootstrap", "--no-prepare"]), 0)
        prepare.assert_not_called()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        (self.project / 'lakefile.toml').write_text('name="consumer"\n[[lean_lib]]\nname="Consumer"\n[[lean_lib]]\nname="ConsumerTests"\nsrcDir="verification"\n')
        (self.project / 'lake-manifest.json').write_text('{"packages": []}')
        self.env = {'LAKE_CACHE_DIR': str(self.project / 'cache')}

    def test_lake_update_global_options_and_executable_paths(self):
        commands = [
            ['lake', 'update', 'Qatlib'], ['lake', 'upgrade', 'Qatlib'],
            ['/opt/lean/bin/lake', '-d', 'somewhere', 'update', 'Pkg'],
            ['lake.exe', '--dir', 'somewhere', '-f', 'lakefile.toml', 'update', 'Pkg'],
            ['lake', '--dir=somewhere', '--file=config', '-Kfoo=bar', 'upgrade'],
            ['lake', '-K', 'foo=bar', '-dproject', '-fconfig', 'update'],
            ['lake', '--update', 'build', 'Consumer'],
            ['lake', '-d', 'somewhere', '--update', 'resolve-deps'],
            ['lake', '--log-level', 'info', 'update'], ['lake', '--', 'update'],
            ['lake', 'build', '--update'], ['lake', '+leanprover/lean4:v4.35.0', 'update'],
        ]
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(ws.lake_update_command(command))
                env = {**self.env, 'LAKE_PKG_URL_MAP': '{"Pkg":"local"}'}
                with patch.object(ws, 'dependency_url_map', side_effect=AssertionError('must not resolve mirrors')):
                    self.assertNotIn('LAKE_PKG_URL_MAP', ws.command_environment(self.project, env, command))
        for command in [[], ['python', 'update'], ['lake', '-d', 'update', 'build'],
                        ['lake', '-f', 'update', 'lint'], ['lake', 'exe', 'tool', 'update'],
                        ['lake', '--file=update', 'build'], ['lake', '-Kx=update', 'test']]:
            with self.subTest(command=command):
                self.assertFalse(ws.lake_update_command(command))

    def test_run_detects_absolute_lake_and_strips_update_map(self):
        with patch.object(ws.supervisor, 'run', return_value=subprocess.CompletedProcess([], 0, '', None)) as run:
            ws.run(['/opt/toolchain/bin/lake', '-d', 'path', 'update', 'Pkg'], cwd=self.project,
                   env={**self.env, 'LAKE_PKG_URL_MAP': '{}'})
        self.assertNotIn('LAKE_PKG_URL_MAP', run.call_args.kwargs['env'])

    def test_bootstrap_uses_child_credentials_without_mutating_environment(self):
        env = {**self.env, 'GITEA_TOKEN': 'secret', 'GITEA_HOST': 'mirror.test', 'GIT_CONFIG_COUNT': '1',
               'GIT_CONFIG_KEY_0': 'safe.setting', 'GIT_CONFIG_VALUE_0': 'preserved'}
        original = dict(env)
        with patch.object(ws, 'ensure_pkg_mirror') as mirror, patch.object(ws, 'run') as run, \
             patch.object(ws, 'cache_lock', return_value=nullcontext()):
            ws.bootstrap(self.project, env)
        self.assertEqual(env, original)
        self.assertEqual([call.args[0] for call in run.call_args_list], [['lake', 'resolve-deps']])
        for call in run.call_args_list:
            child = call.kwargs['env']
            self.assertEqual(child['GIT_CONFIG_COUNT'], '2')
            self.assertEqual(child['GIT_CONFIG_KEY_1'], 'http.https://mirror.test/.extraheader')
            self.assertEqual(child['GIT_CONFIG_VALUE_1'], 'Authorization: Basic ' + base64.b64encode(b'token:secret').decode())
            self.assertEqual(child['GIT_CONFIG_VALUE_0'], 'preserved')
        self.assertEqual(mirror.call_args.args[1]['GIT_CONFIG_COUNT'], '2')

    def test_check_module_and_full_library_targets(self):
        with patch.object(ws, 'run') as run:
            ws.check(self.project, self.env, ['Consumer.Topic'])
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ['lake', 'lint'], ['lake', 'build', 'Consumer.Topic'], ['lake', 'exe', 'test', 'Consumer.Topic']])
        with patch.object(ws, 'run') as run:
            ws.check(self.project, self.env)
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ['lake', 'lint'], ['lake', 'build', 'Consumer', 'ConsumerTests'], ['lake', 'test']])

    def test_check_source_gate_is_optional_and_precedes_lint(self):
        gate = self.project / 'python/dev_metadata/check_source_imports.py'
        gate.parent.mkdir(parents=True); gate.write_text('# test')
        (self.project/'lake-manifest.json').write_text(json.dumps({'packages': [{'name': 'Qatlib'}]}))
        with patch.object(ws, 'run') as run:
            ws.check(self.project, self.env, ['Consumer'])
        command = run.call_args_list[0].args[0]
        self.assertIn("dev_metadata.check_source_imports", command)
        self.assertIn('--dependency-repo', command)
        self.assertEqual(run.call_args_list[1].args[0], ['lake', 'lint'])

    def test_check_stops_when_lint_fails(self):
        with patch.object(ws, 'run', side_effect=subprocess.CalledProcessError(1, ['lake', 'lint'])) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                ws.check(self.project, self.env)
        self.assertEqual(run.call_count, 1)

    def test_cleanup_refuses_visible_active_compilers_and_preserves_setup(self):
        path = self.project / '.lake/build/ir/Consumer.setup.json'
        path.parent.mkdir(parents=True); path.write_text('{}')
        with patch.object(ws, 'active_lean_processes', return_value=[123]):
            with self.assertRaisesRegex(ValueError, 'idle workspace'):
                ws.clean_setup(self.project, apply=True)
        self.assertTrue(path.is_file())
        self.assertEqual(ws.clean_setup(self.project)['files'], 1)
        with patch.object(ws, 'active_lean_processes', return_value=[]), patch.object(ws, 'remove_derived') as trash:
            self.assertEqual(ws.clean_setup(self.project, apply=True)['files'], 1)
        trash.assert_called_once_with(path)

    def test_successful_run_never_sweeps_setup(self):
        with patch.object(ws, 'environment', return_value=self.env), patch.object(ws, 'run'), \
             patch.object(ws, 'clean_setup', side_effect=AssertionError('unsafe cleanup')):
            self.assertEqual(ws.main(['--project', str(self.project), 'run', '--', 'lake', 'build']), 0)

    def test_worktree_can_skip_bootstrap_and_reports_preferences(self):
        child = self.project / 'child'
        preference = self.project / 'AGENTS.local.md'
        for skip in (False, True):
            with self.subTest(skip=skip), patch.object(ws, 'environment', return_value=self.env), \
                 patch.object(ws, 'run') as run, patch('qatlib_env.prepare.prepare') as bootstrap, \
                 patch.object(ws, 'agents_local_path', return_value=preference), \
                 patch('sys.stderr') as stderr:
                argv = ['--project', str(self.project), 'worktree', str(child), 'topic']
                self.assertEqual(ws.main(argv + (['--no-bootstrap'] if skip else [])), 0)
                run.assert_called_once_with(['git', 'worktree', 'add', '-b', 'topic', str(child)], cwd=self.project)
                self.assertIn(str(preference), ''.join(call.args[0] for call in stderr.write.call_args_list))
                if skip:
                    bootstrap.assert_not_called()
                else:
                    bootstrap.assert_called_once_with(child, self.env, source=self.project, offline=False)

    def test_failed_run_preserves_setup_for_diagnostics(self):
        path = self.project / '.lake/build/ir/Consumer.setup.json'
        path.parent.mkdir(parents=True)
        path.write_text('{}')
        with patch.object(ws, 'environment', return_value=self.env), \
             patch.object(ws, 'run', side_effect=subprocess.CalledProcessError(1, ['lake', 'build'])):
            with self.assertRaises(subprocess.CalledProcessError):
                ws.main(['--project', str(self.project), 'run', '--', 'lake', 'build'])
        self.assertTrue(path.is_file())
