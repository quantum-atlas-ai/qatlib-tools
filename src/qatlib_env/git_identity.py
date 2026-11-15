"""Bounded Git source identity checks using a private index, never build files."""
from pathlib import Path
import os
import subprocess
import tempfile


def assert_clean_git_sources(root: Path, paths=(), *, expected_head=None,
                             reject_untracked=False) -> str:
    root = Path(root).resolve()
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    environment.update(GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0')
    git = ['git', '-c', f'core.hooksPath={os.devnull}', '-c', 'core.fsmonitor=false',
           '-c', 'core.ignoreStat=false', '-c', 'core.sparseCheckout=false',
           '-c', 'core.splitIndex=false', '-c', 'index.sparse=false', '-C', str(root),
           '--work-tree=' + str(root)]
    def run(*args, env=environment):
        return subprocess.check_output([*git, *args], env=env, stderr=subprocess.PIPE)
    head = run('rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    if expected_head is not None and head != expected_head:
        raise ValueError('dependency HEAD differs from the lock')
    arguments = ['--', *paths]
    staged = subprocess.run([*git, 'diff', '--cached', '--quiet', '--no-ext-diff',
                             '--no-textconv', head, *arguments], env=environment, capture_output=True)
    if staged.returncode == 1:
        raise ValueError('modified tracked sources or staged changes')
    staged.check_returncode()
    with tempfile.TemporaryDirectory(prefix='qatlas-source-index-') as temporary:
        private = {**environment, 'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        run('read-tree', head, env=private)
        changed = subprocess.run([*git, 'diff', '--quiet', '--no-ext-diff', '--no-textconv',
                                  head, *arguments], env=private, capture_output=True)
    if changed.returncode == 1:
        raise ValueError('modified tracked sources')
    changed.check_returncode()
    if reject_untracked and run('ls-files', '--others', '--exclude-standard', '-z', *arguments):
        raise ValueError('untracked source inputs')
    if run('rev-parse', '--verify', 'HEAD^{commit}').decode().strip() != head:
        raise ValueError('source HEAD changed during identity check')
    return head

