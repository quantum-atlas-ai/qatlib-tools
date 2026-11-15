"""Temporary Git snapshots must honor repository and global hook execution."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._git_fixture import snapshot


class GitFixtureHookTests(unittest.TestCase):
    def test_caller_configuration_cannot_replace_the_ambient_hook_policy(self):
        policy = {
            'GIT_CONFIG_GLOBAL': '/runtime/gitconfig',
            'GIT_CONFIG_SYSTEM': '/runtime/system-gitconfig',
            'GIT_CONFIG_COUNT': '1',
            'GIT_CONFIG_KEY_0': 'core.hooksPath',
            'GIT_CONFIG_VALUE_0': '/runtime/hooks',
        }
        # No Git command runs under this simulated configuration. Inspect the
        # subprocess contract so the test cannot disable the actual host hooks.
        with mock.patch.dict(os.environ, policy), \
                mock.patch('subprocess.check_output', return_value=b'fixture-head\n') as git:
            isolated = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null',
                        'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_CONFIG_VALUE_0': '/dev/null'}
            snapshot(Path('/fixture'), isolated)
            for call in git.call_args_list:
                for key, expected in policy.items():
                    self.assertEqual(call.kwargs['env'][key], expected)

    def test_rejecting_pre_commit_hook_prevents_a_fixture_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'source.txt').write_text('fixture source\n')
            hook = root / '.git/hooks/pre-commit'
            hook.write_text('#!/bin/sh\nexit 47\n')
            hook.chmod(0o755)
            with self.assertRaises(subprocess.CalledProcessError):
                snapshot(root)
            head = subprocess.run(['git', '-C', str(root), 'rev-parse', '--verify', 'HEAD'],
                                  capture_output=True)
            self.assertNotEqual(head.returncode, 0)


if __name__ == '__main__':
    unittest.main()
