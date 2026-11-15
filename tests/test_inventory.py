"""Read-only inventory and fail-closed cleanup tests: no real Trash calls."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qatlib_env import inventory


def fact(path, **changes):
    result = {'path': str(path), 'errors': [], 'identity': [1, 2, 3, 4],
              'head': 'a' * 40, 'branch': 'main', 'common_dir': '/common',
              'git_dir': '/common', 'linked_worktree': False,
              'last_activity': 1, 'dirty_count': 0, 'untracked_count': 0,
              'ignored_count': 0}
    result.update(changes)
    return result


def group(**changes):
    result = {'errors': [], 'stash_count': 0, 'worktrees': [],
              'remote_verified': True, 'unpushed_branches': [], 'unknown_branches': []}
    result.update(changes)
    return result


def test_discover_multiple_roots_nested_markers_and_symlinks(monkeypatch):
    trees = {
        '/root': [('/root', ['one', 'link', '.git', '.lake'], ['lakefile.toml']),
                  ('/root/one', ['nested'], ['.git']),
                  ('/root/one/nested', ['questions'], ['.git'])],
        '/elsewhere': [('/elsewhere', [], ['theorem.lean'])],
    }
    def walk(root, **kwargs):
        for path, dirs, files in trees[str(root)]:
            yield path, list(dirs), files
    monkeypatch.setattr(inventory.os, 'walk', walk)
    monkeypatch.setattr(Path, 'stat', lambda self: SimpleNamespace(st_dev=1))
    monkeypatch.setattr(Path, 'is_symlink', lambda self: str(self) == '/root/link')
    repos, markers, errors, skipped = inventory.discover(['/root', '/elsewhere'])
    assert repos == ['/root', '/root/one', '/root/one/nested']
    assert {'path': '/root', 'markers': ['.lake', 'lakefile.toml']} in markers
    assert {'path': '/elsewhere', 'markers': ['lean-source']} in markers
    assert skipped == [{'path': '/root/link', 'reason': 'symlink'}]
    assert not errors


def test_discover_uses_requested_generic_marker(monkeypatch):
    monkeypatch.setattr(inventory.os, 'walk', lambda root, **kw: iter([('/root', [], ['studies'])]))
    monkeypatch.setattr(Path, 'stat', lambda self: SimpleNamespace(st_dev=1))
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    repos, markers, errors, _ = inventory.discover(['/root'], semantic={'studies'})
    assert not repos and not errors
    assert markers == [{'path': '/root', 'markers': ['studies']}]


def test_exact_scan_skips_discovery_and_deduplicates(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('exact scan must not recurse')
    monkeypatch.setattr(inventory, 'discover', forbidden)
    observed = Mock(side_effect=lambda path, **kw: fact(path))
    monkeypatch.setattr(inventory, 'checkout_facts', observed)
    monkeypatch.setattr(inventory, 'common_facts', lambda *a, **k: group())
    monkeypatch.setattr(inventory, 'processes', lambda: ([], []))
    monkeypatch.setattr(Path, 'exists', lambda self: False)
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    result = inventory.scan(['/r/a', '/r/a', '/r/b'], now=100000,
                            exact=True, semantic={'studies'})
    assert result['exact'] is True
    assert [r['path'] for r in result['checkouts']] == ['/r/a', '/r/b']
    assert observed.call_count == 2
    assert all(call.kwargs['semantic'] == {'studies'} for call in observed.call_args_list)


def test_scan_groups_shared_common_dir_and_never_writes(monkeypatch):
    monkeypatch.setattr(inventory, 'discover', lambda roots: (['/r/a', '/r/b'], [], [], []))
    monkeypatch.setattr(inventory, 'checkout_facts', lambda path: fact(path))
    common = Mock(return_value=group())
    monkeypatch.setattr(inventory, 'common_facts', common)
    monkeypatch.setattr(inventory, 'processes', lambda: ([], []))
    monkeypatch.setattr(Path, 'exists', lambda self: False)
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    def forbidden(*args, **kwargs):
        raise AssertionError('read-only scan tried to write or trash')
    for name in ('write_text', 'write_bytes', 'mkdir', 'rename', 'replace', 'unlink', 'rmdir'):
        monkeypatch.setattr(Path, name, forbidden)
    monkeypatch.setattr(inventory.system_trash, 'trash', forbidden)
    result = inventory.scan(['/r'], now=100000)
    assert len(result['checkouts']) == 2
    assert list(result['common_dirs']) == ['/common']
    assert common.call_count == 1
    assert not result['errors']


def test_git_observation_disables_locks_fetch_and_prompts(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr(inventory.subprocess, 'run', run)
    inventory.git('/r/a', 'status')
    env = run.call_args.kwargs['env']
    assert env['GIT_OPTIONAL_LOCKS'] == '0'
    assert env['GIT_NO_LAZY_FETCH'] == '1'
    assert env['GIT_TERMINAL_PROMPT'] == '0'
    assert run.call_args.args[0][-1] == 'status'


def test_checkout_status_counts_rename_once_and_untracked(monkeypatch):
    sha = 'a' * 40
    def read(path, *args):
        values = {
            ('rev-parse', 'HEAD'): sha,
            ('rev-parse', '--show-toplevel'): '/r/a',
            ('branch', '--show-current'): 'topic',
            ('rev-parse', '--absolute-git-dir'): '/common/worktrees/a',
            ('rev-parse', '--path-format=absolute', '--git-common-dir'): '/common',
            ('status', '--porcelain=v1', '-z', '--untracked-files=all'): 'R  new\0old\0?? untracked\0',
            ('ls-files', '--others', '--ignored', '--exclude-standard', '-z'): 'ignored\0',
            ('log', '-1', '--format=%ct'): '1',
        }
        return values[args]
    monkeypatch.setattr(inventory, '_read_git', read)
    monkeypatch.setattr(inventory, 'git', lambda *a, **k: SimpleNamespace(returncode=0, stdout='origin', stderr=''))
    monkeypatch.setattr(Path, 'exists', lambda self: True)
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    monkeypatch.setattr(Path, 'lstat', lambda self: SimpleNamespace(st_dev=1, st_ino=2,
                                                                  st_mtime_ns=3, st_ctime_ns=4,
                                                                  st_mtime=1))
    result = inventory.checkout_facts('/r/a', inspect_ignored=True)
    assert not result['errors']
    assert result['dirty_count'] == 2
    assert result['untracked_count'] == 1
    assert result['ignored_count'] == 1
    assert result['linked_worktree']


@pytest.mark.parametrize(('activity', 'active', 'protected'), [
    (99999, [], True), (1, [{'pid': 7, 'cwd': '/r/a/src', 'command': ['lean']}], True),
    (1, [], False),
])
def test_scan_hot_and_active_protection(monkeypatch, activity, active, protected):
    monkeypatch.setattr(inventory, 'discover', lambda roots: (['/r/a'], [], [], []))
    monkeypatch.setattr(inventory, 'checkout_facts', lambda path: fact(path, last_activity=activity))
    monkeypatch.setattr(inventory, 'common_facts', lambda *a, **k: group())
    monkeypatch.setattr(inventory, 'processes', lambda: (active, []))
    monkeypatch.setattr(Path, 'exists', lambda self: False)
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    row = inventory.scan(['/r'], now=100000)['checkouts'][0]
    assert bool(row['protection']) is protected


def mock_cleanup(monkeypatch, *, facts=None, groups=None, active=None):
    monkeypatch.setattr(Path, 'is_symlink', lambda self: False)
    monkeypatch.setattr(Path, 'exists', lambda self: True)
    monkeypatch.setattr(Path, 'is_dir', lambda self: True)
    monkeypatch.setattr(inventory, 'checkout_facts', facts or (lambda path, **kwargs: fact(path)))
    monkeypatch.setattr(inventory, 'common_facts', groups or (lambda *a, **k: group()))
    monkeypatch.setattr(inventory, 'processes', lambda: (active or [], []))
    monkeypatch.setattr(inventory, '_lake_guard', lambda path, **kwargs: [])
    monkeypatch.setattr(inventory, '_identity', lambda path: [1, 2, 3, 4])
    monkeypatch.setattr(inventory, 'discover', lambda roots: ([str(roots[0])], [], [], []))
    monkeypatch.setattr(inventory.system_trash, 'measure', lambda path: {'apparent_bytes': 5, 'allocated_bytes': 8})
    monkeypatch.setattr(inventory.system_trash, 'probe', lambda path: {'supported': True})
    moved = Mock(side_effect=lambda path: {'original_path': str(path), 'backend': 'mock'})
    monkeypatch.setattr(inventory.system_trash, 'trash', moved)
    return moved


@pytest.mark.parametrize(('case', 'expected'), [
    ('hot', 'hot checkout'), ('active', 'active Lean/Lake'), ('dirty', 'dirty/untracked'),
    ('stash', 'stash or incomplete'), ('ignored', 'ignored files'),
    ('unknown', 'unpushed/unknown'), ('unpushed', 'unpushed/unknown'),
    ('linked_main', 'still serves linked worktrees'),
])
def test_clean_refuses_protected_targets(monkeypatch, case, expected):
    changes = {'last_activity': inventory.time.time()} if case == 'hot' else {}
    if case == 'dirty': changes['dirty_count'] = 1
    if case == 'ignored': changes['ignored_count'] = 1
    g = group(stash_count=int(case == 'stash'),
              unknown_branches=['unknown'] if case == 'unknown' else [],
              unpushed_branches=['branch'] if case == 'unpushed' else [],
              worktrees=['/r/other'] if case == 'linked_main' else [])
    active = [{'pid': 5, 'cwd': '/r/a', 'command': ['lake']}] if case == 'active' else []
    moved = mock_cleanup(monkeypatch, facts=lambda p, **k: fact(p, **changes),
                         groups=lambda *a, **k: g, active=active)
    mode = 'checkouts' if case in {'ignored', 'unknown', 'unpushed', 'linked_main'} else 'caches'
    result = inventory.clean(['/r/a'], mode=mode, apply=True)
    assert expected in ' '.join(result['targets'][0]['reasons'])
    assert not result['applied']
    moved.assert_not_called()


def test_cleanup_preview_and_batch_refusal_never_move_anything(monkeypatch):
    moved = mock_cleanup(monkeypatch, facts=lambda p, **k: fact(p, dirty_count=int(str(p).endswith('/b'))))
    preview = inventory.clean(['/r/a'], apply=False)
    assert preview['targets'][0]['eligible']
    result = inventory.clean(['/r/a', '/r/b'], apply=True)
    assert 'batch refused' in result['error']
    assert not result['receipts']
    moved.assert_not_called()


def test_overlapping_targets_are_refused_before_observation(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    with pytest.raises(ValueError, match='overlapping'):
        inventory.clean(['/r/a', '/r/a/sub'], mode='checkouts', apply=True)
    moved.assert_not_called()


def test_changed_target_identity_stops_before_move(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory, '_identity', Mock(side_effect=[[1, 2, 3, 4], [1, 2, 3, 5]]))
    result = inventory.clean(['/r/a'], apply=True)
    assert 'target changed' in result['error']
    assert result['receipts'] == []
    moved.assert_not_called()


@pytest.mark.parametrize('hours', [-1, 0, 23.99, float('nan'), float('inf')])
def test_scan_refuses_invalid_hot_window(hours):
    with pytest.raises(ValueError, match='at least 24'):
        inventory.scan(['/r'], hot_hours=hours)


def test_cli_negative_hot_window_returns_error(capsys):
    assert inventory.cli_main(['scan', '--exact', '--hot-hours', '-1', '/r/a']) == 1
    assert 'at least 24' in capsys.readouterr().err


def test_cleanup_short_hot_window_refuses_apply(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    result = inventory.clean(['/r/a'], hot_hours=1, apply=True)
    assert not result['applied']
    assert 'at least 24' in ' '.join(result['targets'][0]['reasons'])
    moved.assert_not_called()


def test_cli_defaults_to_preview_and_passes_exact_and_markers(monkeypatch, capsys):
    clean = Mock(return_value={'applied': False, 'targets': []})
    scan = Mock(return_value={'checkouts': []})
    monkeypatch.setattr(inventory, 'clean', clean)
    monkeypatch.setattr(inventory, 'scan', scan)
    assert inventory.cli_main(['clean-caches', '--checkout', '/r/a']) == 0
    assert clean.call_args.kwargs['apply'] is False
    assert inventory.cli_main(['trash-checkouts', '--checkout', '/r/a', '--dry-run']) == 0
    assert clean.call_args.kwargs['apply'] is False
    assert inventory.cli_main(['scan', '--exact', '--marker', 'studies', '/r/a']) == 0
    assert scan.call_args.kwargs['exact'] is True
    assert 'studies' in scan.call_args.kwargs['semantic']
    assert inventory.SEMANTIC <= scan.call_args.kwargs['semantic']
    capsys.readouterr()


def test_nested_checkout_prevents_trashing_parent(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory, 'discover', lambda roots: ([str(roots[0]), str(roots[0] / 'nested')], [], [], []))
    result = inventory.clean(['/r/a'], mode='checkouts', apply=True)
    assert 'nested checkout' in ' '.join(result['targets'][0]['reasons'])
    moved.assert_not_called()


def test_unknown_process_observation_refuses_all(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    monkeypatch.setattr(inventory, 'processes', lambda: ([], ['process inspection unavailable']))
    result = inventory.clean(['/r/a'], apply=True)
    assert 'process inspection unavailable' in result['targets'][0]['reasons']
    moved.assert_not_called()


def test_partial_receipts_preserved_on_backend_failure(monkeypatch):
    moved = mock_cleanup(monkeypatch)
    moved.side_effect = [{'original_path': '/r/a/.lake', 'backend': 'mock'}, RuntimeError('failed')]
    result = inventory.clean(['/r/a', '/r/b'], apply=True)
    assert not result['applied']
    assert result['receipts'] == [{'original_path': '/r/a/.lake', 'backend': 'mock'}]
    assert result['error'] == 'failed'


@pytest.mark.parametrize(('missing', 'expected'), [(False, 'unpushed_branches'), (True, 'unknown_branches')])
def test_remote_ancestry_and_missing_objects_are_distinguished(monkeypatch, missing, expected):
    local, remote = 'a' * 40, 'b' * 40
    def read(path, *args):
        if args[:2] == ('stash', 'list'): return ''
        if args[:2] == ('worktree', 'list'): return 'worktree /r/a\0HEAD ' + local + '\0'
        if args[0] == 'for-each-ref': return local + ' refs/heads/main\n'
        if args[:2] == ('remote', 'get-url'): return 'https://example.com/org/repo.git\n'
        if args[:2] == ('rev-parse', '--is-shallow-repository'): return 'false\n'
        raise AssertionError(args)
    def git(path, *args, **kwargs):
        if args[0] == 'ls-remote': return SimpleNamespace(returncode=0, stdout=remote + '\trefs/heads/main\n', stderr='')
        if args[0] == 'cat-file':
            return SimpleNamespace(returncode=0, stdout=(remote + ' missing\n' if missing else remote + ' commit\n'), stderr='')
        if args[0] == 'rev-list': return SimpleNamespace(returncode=0, stdout=remote + '\n', stderr='')
        raise AssertionError(args)
    monkeypatch.setattr(inventory, '_read_git', read)
    monkeypatch.setattr(inventory, 'git', git)
    result = inventory.common_facts('/r/a', check_remote=True)
    assert not result['errors']
    assert result[expected]
    assert result['remote_verified'] is (not missing)


@pytest.mark.parametrize('origin', ['https://example.com/org/repo.git', '/local/source'])
def test_remote_ancestor_makes_local_branch_reachable(monkeypatch, origin):
    local, remote = 'a' * 40, 'b' * 40
    def read(path, *args):
        if args[:2] == ('stash', 'list'): return ''
        if args[:2] == ('worktree', 'list'): return 'worktree /r/a\0HEAD ' + local + '\0'
        if args[0] == 'for-each-ref': return local + ' refs/heads/main\n'
        if args[:2] == ('remote', 'get-url'): return origin + '\n'
        if args[:2] == ('rev-parse', '--is-shallow-repository'): return 'false\n'
        raise AssertionError(args)
    def git(path, *args, **kwargs):
        if args[0] == 'ls-remote': output = remote + '\trefs/heads/main\n'
        elif args[0] == 'cat-file': output = remote + ' commit\n'
        elif args[0] == 'rev-list': output = remote + '\n' + local + '\n'
        else: raise AssertionError(args)
        return SimpleNamespace(returncode=0, stdout=output, stderr='')
    monkeypatch.setattr(inventory, '_read_git', read)
    monkeypatch.setattr(inventory, 'git', git)
    result = inventory.common_facts('/r/a', check_remote=True)
    assert result['remote_verified'] is (origin.startswith('https:'))
    assert not result['unknown_branches'] and not result['unpushed_branches']
