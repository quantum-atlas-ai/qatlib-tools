"""Send2Trash contracts; no real user data is moved."""
import os
from pathlib import Path
import stat
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from qatlib_env import trash


@pytest.mark.parametrize("system", ["Linux", "Darwin", "Windows"])
@pytest.mark.parametrize("name", ["derived file", "引号'与\" $HOME; $(touch sentinel)\nnext"])
def test_paths_are_data_for_all_platforms(tmp_path, monkeypatch, system, name):
    path = tmp_path / name
    path.write_text("payload")
    move = Mock()
    move.__module__ = "send2trash.native"
    monkeypatch.setattr(trash.platform, "system", lambda: system)
    monkeypatch.setattr(trash, "_backend", lambda: move)
    receipt = trash.trash(path)
    move.assert_called_once_with(str(path))
    assert receipt["backend"] == "send2trash.native"
    assert receipt["apparent_bytes"] == len(b"payload")
    assert "no physical space freed" in receipt["physical_space_note"]


def test_probe_is_read_only_and_does_not_promise_mount_capability(tmp_path, monkeypatch):
    move = Mock()
    move.__module__ = "send2trash.native"
    monkeypatch.setattr(trash, "_backend", lambda: move)
    before = list(tmp_path.iterdir())
    result = trash.probe(tmp_path)
    assert result["supported"] and result["can_trash"] is None
    assert list(tmp_path.iterdir()) == before
    move.assert_not_called()


@pytest.mark.parametrize("error", [PermissionError("no trash permission"), OSError("unsupported mount")])
def test_backend_failure_never_falls_back(tmp_path, monkeypatch, error):
    path = tmp_path / "derived"
    path.write_text("keep")
    move = Mock(side_effect=error)
    move.__module__ = "send2trash.native"
    monkeypatch.setattr(trash, "_backend", lambda: move)
    with pytest.raises(trash.TrashError, match="Send2Trash failed"):
        trash.trash(path)
    assert path.read_text() == "keep"
    assert move.call_count == 1


def test_missing_backend_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(trash, "_backend", Mock(side_effect=ImportError("missing pywin32")))
    with pytest.raises(trash.TrashError, match="backend unavailable"):
        trash.probe(tmp_path)


def test_changed_target_during_size_inspection_refuses(tmp_path, monkeypatch):
    move = Mock()
    move.__module__ = "send2trash.native"
    monkeypatch.setattr(trash, "_backend", lambda: move)
    monkeypatch.setattr(trash, "_identity", Mock(side_effect=[(1, 2, 3, 4), (1, 3, 3, 4)]))
    with pytest.raises(trash.TrashError, match="changed during"):
        trash.trash(tmp_path)
    move.assert_not_called()


def test_read_only_mount_is_refused(tmp_path, monkeypatch):
    move = Mock()
    move.__module__ = "send2trash.native"
    monkeypatch.setattr(trash, "_backend", lambda: move)
    monkeypatch.setattr(trash.os, "statvfs", lambda p: SimpleNamespace(f_flag=1), raising=False)
    monkeypatch.setattr(trash.os, "ST_RDONLY", 1, raising=False)
    with pytest.raises(trash.TrashError, match="read-only filesystem"):
        trash.probe(tmp_path)
    move.assert_not_called()


def test_windows_uses_modern_backend_and_raises_to_cancel(monkeypatch):
    class Sink:
        pass

    class Cancelled(Exception):
        def __init__(self, **details):
            self.details = details

    modern = ModuleType("send2trash.win.modern")
    modern.send2trash = Mock()
    win = ModuleType("send2trash.win")
    win.modern = modern
    sink_module = ModuleType("send2trash.win.IFileOperationProgressSink")
    sink_module.FileOperationProgressSink = Sink
    exception = ModuleType("win32com.server.exception")
    exception.COMException = Cancelled
    shell = ModuleType("win32com.shell")
    shell.shellcon = SimpleNamespace(TSF_DELETE_RECYCLE_IF_POSSIBLE=0x80)
    winerror = ModuleType("winerror")
    winerror.E_ABORT = -2147467260
    for name, module in {win.__name__: win, modern.__name__: modern,
                         sink_module.__name__: sink_module,
                         exception.__name__: exception, shell.__name__: shell,
                         "winerror": winerror}.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(trash.platform, "system", lambda: "Windows")
    assert trash._backend() is modern.send2trash
    assert Sink().PreDeleteItem(0x80, None) == 0
    with pytest.raises(Cancelled) as error:
        Sink().PreDeleteItem(0, None)
    assert error.value.details["hresult"] == winerror.E_ABORT


def test_windows_long_descendant_is_refused_before_native_call(tmp_path):
    directory = tmp_path / ("a" * 130)
    directory.mkdir()
    (directory / ("b" * 130)).write_text("keep")
    with pytest.raises(trash.TrashError, match="long path"):
        trash._windows_path_check(tmp_path)


def test_measure_hardlinks_and_symlinks(tmp_path):
    first = tmp_path / "first"
    first.write_bytes(b"payload")
    try:
        os.link(first, tmp_path / "second")
        (tmp_path / "link").symlink_to(first)
    except OSError:
        pytest.skip("hard/symbolic links unavailable")
    expected = tmp_path.lstat().st_size + first.lstat().st_size + (tmp_path / "link").lstat().st_size
    assert trash.measure(tmp_path)["apparent_bytes"] == expected


def test_measure_does_not_recurse_windows_junction(monkeypatch):
    from contextlib import nullcontext
    def info(path):
        return SimpleNamespace(st_dev=1, st_ino=1 if str(path) == "/root" else 2,
                               st_nlink=1, st_size=10, st_blocks=1, st_mode=stat.S_IFDIR,
                               st_file_attributes=0 if str(path) == "/root" else 0x400)
    monkeypatch.setattr(Path, "lstat", info)
    scandir = Mock(return_value=nullcontext([SimpleNamespace(path="/root/junction")]))
    monkeypatch.setattr(trash.os, "scandir", scandir)
    assert trash.measure(Path("/root"))["apparent_bytes"] == 20
    scandir.assert_called_once_with(Path("/root"))


def test_nested_mount_is_measured_and_reported_not_refused(monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(
        st_dev=1 if str(path) == "/root" else 2, st_ino=1, st_nlink=1,
        st_size=10, st_blocks=1, st_mode=stat.S_IFDIR if str(path) == "/root" else stat.S_IFREG))
    monkeypatch.setattr(trash.os, "scandir", lambda path: nullcontext([SimpleNamespace(path="/root/mounted")]))
    row = trash.measure(Path("/root"))
    assert row["apparent_bytes"] == 20
    assert row["nested_mounts"] == ["/root/mounted"]


def test_windows_path_walk_prunes_junctions(monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda p: True)
    monkeypatch.setattr(trash, "is_link", lambda p: p.name == "junction")
    def walk(path, **kwargs):
        dirs = ["junction"]
        yield "/root", dirs, []
        assert "junction" not in dirs
    monkeypatch.setattr(trash.os, "walk", walk)
    trash._windows_path_check(Path("/root"))


def test_caller_environment_uses_isolated_process(tmp_path, monkeypatch):
    path = tmp_path / "derived"
    path.write_text("keep")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"backend":"send2trash.native"}', stderr=""))
    monkeypatch.setattr(trash.subprocess, "run", run)
    result = trash.trash(path, {"XDG_DATA_HOME": str(tmp_path / "xdg")})
    assert result["backend"] == "send2trash.native"
    assert run.call_args.args[0][-1] == str(path)
    assert run.call_args.kwargs["env"]["XDG_DATA_HOME"] == str(tmp_path / "xdg")
    assert path.exists()
