"""System Trash dispatch and fail-closed behavior, without moving any files."""
from contextlib import nullcontext
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from qatlib_env import trash


PATH = Path('/example/derived file')
ENV = {'PATH': '/mock'}


@pytest.fixture(autouse=True)
def stable_identity(monkeypatch):
    monkeypatch.setattr(trash, '_identity', lambda path: (1, 2, 3, 4))


def result(stdout='', returncode=0, stderr=''):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def linux_which(name, path=None):
    return {'gio': '/mock/gio', 'trash-put': '/mock/trash-put'}.get(name)


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Linux')
@patch.object(trash.shutil, 'which', side_effect=linux_which)
def test_gio_probe_and_trash(_, __, ___):
    with patch.object(trash.subprocess, 'run', side_effect=[
            result('attributes:\n  access::can-trash: TRUE\n'), result()]) as run, \
         patch.object(trash, 'measure', return_value={'apparent_bytes': 42, 'allocated_bytes': 512}):
        receipt = trash.trash(PATH, ENV)
    assert receipt['backend'] == 'gio'
    assert receipt['apparent_bytes'] == 42
    assert receipt['allocated_bytes'] == 512
    assert run.call_args_list[0].args[0] == ['/mock/gio', 'info', '--nofollow-symlinks', '-a', 'access::can-trash', '--', str(PATH)]
    assert run.call_args_list[1].args[0] == ['/mock/gio', 'trash', '--', str(PATH)]
    assert run.call_args_list[0].kwargs['env']['GIO_USE_VFS'] == 'local'
    assert 'no physical space freed' in receipt['physical_space_note']


@pytest.mark.parametrize('output', ['access::can-trash: FALSE\n', 'attributes:\n',
                                    'access::can-trash: TRUE\naccess::can-trash: FALSE\n'])
@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Linux')
@patch.object(trash.shutil, 'which', side_effect=linux_which)
def test_gio_unsupported_never_falls_back(_, __, ___, output):
    with patch.object(trash.subprocess, 'run', return_value=result(output)) as run:
        with pytest.raises(trash.TrashError, match='ask the user'):
            trash.trash(PATH, ENV)
    assert run.call_count == 1


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Linux')
@patch.object(trash.shutil, 'which', side_effect=linux_which)
def test_gio_move_failure_does_not_retry_other_backend(_, __, ___):
    with patch.object(trash.subprocess, 'run', side_effect=[result('access::can-trash: TRUE\n'),
                                                           result(returncode=1, stderr='denied')]) as run, \
         patch.object(trash, 'measure', return_value={'apparent_bytes': 1, 'allocated_bytes': 512}):
        with pytest.raises(trash.TrashError, match='denied'):
            trash.trash(PATH, ENV)
    assert run.call_count == 2


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Linux')
def test_trash_put_only_when_gio_absent(_, __):
    with patch.object(trash.shutil, 'which', side_effect=lambda name, path=None: '/mock/trash-put' if name == 'trash-put' else None), \
         patch.object(trash.subprocess, 'run', return_value=result()) as run, \
         patch.object(trash, 'measure', return_value={'apparent_bytes': 1, 'allocated_bytes': 512}):
        receipt = trash.trash(PATH, ENV)
    assert receipt['backend'] == 'trash-put'
    assert run.call_args.args[0] == ['/mock/trash-put', '--', str(PATH)]


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Linux')
@patch.object(trash.shutil, 'which', return_value=None)
def test_missing_tools_refuse(_, __, ___):
    with pytest.raises(trash.TrashError, match='install gio or trash-put'):
        trash.probe(PATH, ENV)


@pytest.mark.parametrize('native', [True, False])
@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Darwin')
@pytest.mark.parametrize('name', ['derived file', "引号'与\" $HOME; $(touch sentinel)\nnext"])
def test_macos_native_or_finder(_, __, native, name):
    path = Path('/example') / name
    with patch.object(trash.os.path, 'isfile', return_value=native), \
         patch.object(trash.os, 'access', return_value=native), \
         patch.object(trash.shutil, 'which', return_value='/mock/osascript'), \
         patch.object(trash.subprocess, 'run', return_value=result()) as run, \
         patch.object(trash, 'measure', return_value={'apparent_bytes': 1, 'allocated_bytes': 512}):
        receipt = trash.trash(path, ENV)
    args = run.call_args.args[0]
    assert receipt['backend'] == ('macos-trash' if native else 'macos-finder')
    assert args[-1] == str(path)
    if not native:
        assert 'POSIX file (item 1 of argv)' in args[2]
        assert str(path) not in args[2]


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Windows')
def test_windows_refuses_unsafe_dotnet_backend_even_with_powershell(_, __):
    path = Path("/example/it's $important; file")
    with patch.object(trash.shutil, 'which', return_value='powershell.exe'), \
         patch.object(trash.subprocess, 'run') as run:
        with pytest.raises(trash.TrashError, match='can permanently delete'):
            trash.trash(path, ENV)
    run.assert_not_called()


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Darwin')
def test_macos_missing_backend_stops(_, __):
    with patch.object(trash.os.path, 'isfile', return_value=False), \
         patch.object(trash.shutil, 'which', return_value=None):
        with pytest.raises(trash.TrashError, match='ask the user'):
            trash.probe(PATH, ENV)


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Linux')
@patch.object(trash.shutil, 'which', side_effect=linux_which)
def test_probe_backend_failure_stops(_, __, ___):
    with patch.object(trash.subprocess, 'run', return_value=result(returncode=1, stderr='unsupported')) as run:
        with pytest.raises(trash.TrashError, match='unsupported'):
            trash.probe(PATH, ENV)
        assert run.call_count == 1


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Windows')
def test_windows_refuses_reparse_points(_, __):
    with patch.object(trash.stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400, create=True), \
         patch.object(Path, 'lstat', return_value=SimpleNamespace(st_file_attributes=0x400)), \
         patch.object(Path, 'is_symlink', return_value=False), \
         patch.object(trash.subprocess, 'run') as run:
        with pytest.raises(trash.TrashError, match='native Recycle Bin manually'):
            trash.trash(PATH, ENV)
        run.assert_not_called()


def test_measure_deduplicates_hardlinks_and_does_not_follow_symlinks(monkeypatch):
    def item(mode, inode, links, size, blocks):
        return SimpleNamespace(st_mode=mode, st_dev=1, st_ino=inode, st_nlink=links,
                               st_size=size, st_blocks=blocks)
    data = {
        '/root': item(stat.S_IFDIR, 1, 2, 10, 1),
        '/root/first': item(stat.S_IFREG, 2, 2, 100, 4),
        '/root/second': item(stat.S_IFREG, 2, 2, 100, 4),
        '/root/link': item(stat.S_IFLNK, 3, 1, 5, 0),
    }
    monkeypatch.setattr(Path, 'lstat', lambda self: data[str(self)])
    entries = [SimpleNamespace(path=p) for p in ('/root/first', '/root/second', '/root/link')]
    scandir = Mock(return_value=nullcontext(entries))
    monkeypatch.setattr(trash.os, 'scandir', scandir)
    assert trash.measure(Path('/root')) == {'apparent_bytes': 115, 'allocated_bytes': 2560}
    scandir.assert_called_once_with(Path('/root'))


def test_measure_refuses_nested_filesystem(monkeypatch):
    monkeypatch.setattr(Path, 'lstat', lambda self: SimpleNamespace(
        st_mode=stat.S_IFDIR, st_dev=1 if str(self) == '/root' else 2,
        st_ino=1, st_nlink=1, st_size=10, st_blocks=1))
    monkeypatch.setattr(trash.os, 'scandir', lambda path: nullcontext([SimpleNamespace(path='/root/mount')]))
    with pytest.raises(trash.TrashError, match='nested filesystem'):
        trash.measure(Path('/root'))


@patch.object(trash.os.path, 'lexists', return_value=True)
@patch.object(trash.platform, 'system', return_value='Linux')
@patch.object(trash.shutil, 'which', side_effect=linux_which)
def test_changed_target_during_probe_does_not_move(_, __, ___, monkeypatch):
    monkeypatch.setattr(trash, '_identity', Mock(side_effect=[(1, 2, 3, 4), (1, 3, 3, 4)]))
    with patch.object(trash.subprocess, 'run', return_value=result('access::can-trash: TRUE\n')) as run, \
         patch.object(trash, 'measure', return_value={'apparent_bytes': 1, 'allocated_bytes': 512}):
        with pytest.raises(trash.TrashError, match='changed during'):
            trash.trash(PATH, ENV)
    assert run.call_count == 1
