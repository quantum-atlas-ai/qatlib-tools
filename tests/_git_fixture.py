"""Build temporary Git fixtures through normal staging and commit hooks."""
import os
from pathlib import Path
import subprocess
from urllib.parse import unquote, urlsplit


def snapshot(root, env=None):
    root = Path(root)
    env = dict(os.environ if env is None else env)
    env = {k: v for k, v in env.items() if not k.startswith('GIT_')}
    # Callers may isolate clone configuration. Snapshot commits must still use
    # the real process's Git configuration, including its global hook policy.
    env.update({k: v for k, v in os.environ.items() if k.startswith('GIT_CONFIG')})
    # Preserve the existing synthetic test-data identity only in these
    # disposable fixtures; no repository or global identity configuration changes.
    env.update(GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@localhost',
               GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@localhost')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args], env=env, stderr=subprocess.PIPE).decode().strip()
    # Fixtures have independent temporary repositories. Run the same
    # global/repository hooks as normal work instead of creating Git objects.
    git('add', '--all')
    git('commit', '--quiet', '--allow-empty', '-m', 'test fixture')
    return git('rev-parse', 'HEAD')


def refresh_bare(url, source, env):
    bare = unquote(urlsplit(url).path)
    subprocess.run(['git', '--git-dir=' + bare, 'fetch', '-q', str(source), 'main:main'], env=env, check=True)
