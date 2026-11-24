"""Cache publication boundaries, including portable Win32 handle simulations."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest
import tests._bootstrap  # noqa: F401
from qatlib_env import prepare, workspace
from qatlib_env.backends import windows_paths

NAME = "0123456789abcdef.json"
RECORD = {"schemaVersion": "2026-02-25", "service": "fixture", "data": {"x": 1}}


@pytest.mark.skipif(sys.platform != "win32", reason="requires native Windows file locking")
def test_windows_shared_fetch_lock_serializes_two_processes(tmp_path):
    code = """
from pathlib import Path
import sys
import time
from qatlib_env.workspace import cache_lock
with cache_lock(Path(sys.argv[1])):
    print(time.monotonic(), flush=True)
    time.sleep(float(sys.argv[2]))
"""
    env = {**os.environ, "PYTHONPATH": str(Path(workspace.__file__).resolve().parents[1])}
    command = [sys.executable, "-B", "-c", code, str(tmp_path)]
    with subprocess.Popen([*command, "2"], stdout=subprocess.PIPE, text=True, env=env) as owner:
        acquired_at = float(owner.stdout.readline())
        waiter = subprocess.run([*command, "0"], capture_output=True, text=True,
                                env=env, check=True, timeout=15)
        assert float(waiter.stdout) - acquired_at >= 1.9
        assert owner.wait(timeout=15) == 0


@pytest.fixture
def mapping(tmp_path):
    cache = tmp_path / "缓存 pool"
    staged = cache / "staging"
    source = staged / "outputs/Example" / NAME
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(RECORD), encoding="utf-8")
    return cache, staged, source


class Handles:
    """Model the Win32 share contract; filesystem I/O still uses real files."""
    def __init__(self, *, attributes=None, fail=None):
        self.active, self.opened, self.closed = [], [], []
        self.attributes, self.fail = attributes or {}, fail

    def CreateFile(self, name, access, sharing, security, disposition, flags, template):
        path = Path(name)
        directory = access == 0x80
        assert access in (0x80, 0x80010000) and sharing == 1
        assert disposition == 3 and flags == (0x02200000 if directory else 0x00200000)
        if path == self.fail:
            raise PermissionError("incompatible directory handle")
        if not path.exists():
            raise FileNotFoundError(name)
        descriptor = os.open(path, os.O_RDONLY) if not directory or os.name == "posix" else None
        handle = SimpleNamespace(path=path, descriptor=descriptor)
        def close():
            self.active.remove(path)
            self.closed.append(path)
            if descriptor is not None:
                os.close(descriptor)
        handle.Close = close
        self.active.append(path)
        self.opened.append(path)
        return handle

    def GetFileInformationByHandle(self, handle):
        attributes = self.attributes.get(handle.path)
        if attributes is None:
            attributes = 0x400 if workspace.redirected_directory(handle.path) else 0
            if handle.path.is_dir():
                attributes |= 0x10
        return (attributes,)

    FileRenameInfo = 3

    def ReadFile(self, handle, size):
        return 0, os.read(handle.descriptor, size)

    def GetFinalPathNameByHandle(self, handle, flags):
        assert flags == 0
        return str(handle.path)

    def SetFileInformationByHandle(self, handle, kind, info):
        assert kind == self.FileRenameInfo and info["ReplaceIfExists"] is True
        assert info["RootDirectory"] is None
        target = Path(info["FileName"])
        assert target.parent in self.active and handle.path in self.active
        os.replace(handle.path, target)


def windows_publication(monkeypatch, handles):
    # Replace module bindings, never os.name globally (which breaks pathlib).
    monkeypatch.setitem(sys.modules, "win32file", handles)
    monkeypatch.setattr(prepare, "os", SimpleNamespace(name="nt", replace=os.replace))


def test_windows_mapping_is_complete_and_handles_outlive_replace(mapping, monkeypatch):
    cache, staged, source = mapping
    target = cache / "outputs/Example" / NAME
    handles = Handles()
    windows_publication(monkeypatch, handles)
    rename = handles.SetFileInformationByHandle
    def publish(handle, kind, info):
        for path in (cache, cache.parent, staged, source.parent, target.parent, cache / "outputs"):
            assert path in handles.active
        assert source in handles.active
        assert info["RootDirectory"] is None and Path(info["FileName"]) == target
        rename(handle, kind, info)
    monkeypatch.setattr(handles, "SetFileInformationByHandle", publish)
    assert prepare._publish_native_mappings(staged, cache) == 1
    assert json.loads(target.read_bytes()) == RECORD
    assert not source.exists() and not handles.active


@pytest.mark.parametrize("component", ["cache", "staging", "source_outputs", "source_namespace", "outputs", "namespace"])
def test_windows_reparse_attribute_rejected_at_every_boundary(mapping, monkeypatch, component):
    cache, staged, source = mapping
    target = cache / "outputs/Example" / NAME
    target.parent.mkdir(parents=True)
    paths = {"cache": cache, "staging": staged, "source_outputs": staged / "outputs",
             "source_namespace": source.parent, "outputs": target.parent.parent, "namespace": target.parent}
    handles = Handles(attributes={paths[component]: 0x410})
    windows_publication(monkeypatch, handles)
    with pytest.raises(prepare.CacheError, match="reparse-point"):
        prepare._publish_native_mappings(staged, cache)
    assert not target.exists() and source.is_file() and not handles.active


@pytest.mark.parametrize("component", ["cache", "staging", "source_outputs", "source_namespace", "outputs", "namespace"])
def test_real_directory_links_are_rejected_before_publication(mapping, monkeypatch, component):
    cache, staged, source = mapping
    target = cache / "outputs/Example" / NAME
    target.parent.mkdir(parents=True)
    paths = {"cache": cache, "staging": staged, "source_outputs": staged / "outputs",
             "source_namespace": source.parent, "outputs": target.parent.parent, "namespace": target.parent}
    path = paths[component]
    saved = path.with_name(path.name + "-original")
    path.rename(saved)
    try:
        path.symlink_to(saved, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    if os.name != "nt":
        windows_publication(monkeypatch, Handles())
    with pytest.raises(prepare.CacheError, match="reparse-point"):
        prepare._publish_native_mappings(staged, cache)


def test_replace_failure_closes_handles_without_unsafe_retry(mapping, monkeypatch):
    cache, staged, source = mapping
    target = cache / "outputs/Example" / NAME
    handles = Handles()
    windows_publication(monkeypatch, handles)
    attempts = []
    def publish(handle, kind, info):
        attempts.append((handle.path, Path(info["FileName"])))
        assert target.parent in handles.active and cache in handles.active
        raise PermissionError("incompatible target handle")
    monkeypatch.setattr(handles, "SetFileInformationByHandle", publish)
    with pytest.raises(PermissionError):
        prepare._publish_native_mappings(staged, cache)
    assert attempts == [(source, target)] and not handles.active
    assert source.is_file() and not target.exists()


@pytest.mark.parametrize("kind", ["regular", "directory", "symlink"])
def test_existing_target_is_replaced_only_when_regular(mapping, monkeypatch, kind):
    cache, staged, source = mapping
    target = cache / "outputs/Example" / NAME
    target.parent.mkdir(parents=True)
    outside = cache.parent / "outside.json"
    outside.write_text("untouched")
    if kind == "directory":
        target.mkdir()
    elif kind == "symlink":
        try:
            target.symlink_to(outside)
        except OSError as error:
            pytest.skip(f"file symlinks unavailable: {error}")
    else:
        target.write_text("old complete mapping")
    windows_publication(monkeypatch, Handles())
    if kind == "regular":
        assert prepare._publish_native_mappings(staged, cache) == 1
        assert json.loads(target.read_bytes()) == RECORD
    else:
        with pytest.raises(prepare.CacheError, match="not a regular file"):
            prepare._publish_native_mappings(staged, cache)
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("record", ["{broken", "[]", '{}', '{"schemaVersion":"other","data":null}',
                                     '{"schemaVersion":"2026-02-25","data":null,"repo":3}'])
def test_invalid_mapping_never_creates_destination(mapping, monkeypatch, record):
    cache, staged, source = mapping
    source.write_text(record)
    windows_publication(monkeypatch, Handles())
    with pytest.raises(prepare.CacheError, match="invalid output JSON|unsupported output schema"):
        prepare._publish_native_mappings(staged, cache)
    assert not (cache / "outputs").exists()


def test_windows_mapping_uses_the_pinned_namespace_path(mapping, monkeypatch):
    cache, staged, source = mapping
    target_dir = cache / "outputs/Example"
    handles = Handles()
    windows_publication(monkeypatch, handles)
    canonical_path = handles.GetFinalPathNameByHandle
    def resolve(handle, flags):
        assert handle.path == target_dir
        assert all(path in handles.active for path in (target_dir, target_dir.parent, cache, cache.parent))
        return canonical_path(handle, flags)
    monkeypatch.setattr(handles, "GetFinalPathNameByHandle", resolve)
    assert prepare._publish_native_mappings(staged, cache) == 1
    assert json.loads((target_dir / NAME).read_bytes()) == RECORD


def test_mapping_changed_after_json_validation_is_not_published(mapping, monkeypatch):
    cache, staged, source = mapping
    handles = Handles()
    windows_publication(monkeypatch, handles)
    create = handles.CreateFile
    def open_file(name, access, *args):
        if access == 0x80010000:
            source.write_bytes(b"changed after validation")
        return create(name, access, *args)
    monkeypatch.setattr(handles, "CreateFile", open_file)
    with pytest.raises(prepare.CacheError, match="changed after validation"):
        prepare._publish_native_mappings(staged, cache)
    assert not (cache / "outputs/Example" / NAME).exists() and not handles.active


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory fd identity")
def test_posix_namespace_replacement_keeps_original_directory_anchor(mapping, monkeypatch):
    cache, staged, source = mapping
    target_dir = cache / "outputs/Example"
    outside = cache.parent / "outside"
    outside.mkdir()
    saved = target_dir.with_name("Example-original")
    replace = os.replace
    def publish(src, name, **kwargs):
        target_dir.rename(saved)
        target_dir.symlink_to(outside, target_is_directory=True)
        replace(src, name, **kwargs)
    monkeypatch.setattr(prepare.os, "replace", publish)
    assert prepare._publish_native_mappings(staged, cache) == 1
    assert not list(outside.iterdir()) and json.loads((saved / NAME).read_bytes()) == RECORD


@pytest.mark.parametrize("attributes", [0, 0x400, 0x410])
def test_handle_attribute_validation_closes_on_failure(tmp_path, monkeypatch, attributes):
    handles = Handles(attributes={tmp_path: attributes})
    monkeypatch.setitem(sys.modules, "win32file", handles)
    with pytest.raises(ValueError, match="regular.*reparse"):
        with windows_paths.locked_directory(tmp_path):
            pytest.fail("unsafe directory yielded")
    assert not handles.active


def test_ancestor_open_failure_is_fail_closed(tmp_path, monkeypatch):
    handles = Handles(fail=tmp_path.parent)
    monkeypatch.setitem(sys.modules, "win32file", handles)
    with pytest.raises(PermissionError):
        with windows_paths.locked_directory(tmp_path):
            pytest.fail("unsafe directory yielded")
    assert tmp_path not in handles.opened and not handles.active


def test_parent_traversal_is_rejected_without_opening(tmp_path, monkeypatch):
    handles = Handles()
    monkeypatch.setitem(sys.modules, "win32file", handles)
    with pytest.raises(ValueError, match="parent traversal"):
        with windows_paths.locked_directory(tmp_path / ".." / "other"):
            pass
    assert not handles.opened


@pytest.mark.parametrize("component", ["cache", "artifacts", "outputs"])
def test_validate_cache_uses_311_reparse_attributes(tmp_path, monkeypatch, component):
    cache = tmp_path / "cache"
    cache.mkdir()
    path = cache if component == "cache" else cache / component
    original = Path.lstat
    def lstat(self, *args, **kwargs):
        if self == path:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(prepare.CacheError, match="reparse-point"):
        prepare.validate_cache(cache)


@pytest.mark.skipif(os.name != "nt", reason="native Windows directory sharing and junctions")
@pytest.mark.parametrize("component", ["cache", "outputs", "namespace", "staging", "source_namespace", "ancestor"])
def test_native_windows_rename_is_denied_during_replace(mapping, monkeypatch, component):
    cache, staged, source = mapping
    target = cache / "outputs/Example" / NAME
    paths = {"cache": cache, "outputs": target.parent.parent, "namespace": target.parent,
             "staging": staged, "source_namespace": source.parent, "ancestor": cache.parent}
    replace = windows_paths.replace_mapping
    def publish(src, directory, name, expected):
        path = paths[component]
        with pytest.raises(OSError):
            path.rename(path.with_name(path.name + "-saved"))
        replace(src, directory, name, expected)
    monkeypatch.setattr(windows_paths, "replace_mapping", publish)
    assert prepare._publish_native_mappings(staged, cache) == 1
    assert json.loads(target.read_bytes()) == RECORD


@pytest.mark.skipif(os.name != "nt", reason="native Windows junction")
def test_native_windows_junction_is_rejected(mapping):
    cache, staged, source = mapping
    outside = cache.parent / "outside"
    outside.mkdir()
    (cache / "outputs").mkdir()
    junction = cache / "outputs/Example"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    with pytest.raises(prepare.CacheError, match="reparse-point"):
        prepare._publish_native_mappings(staged, cache)
    assert not list(outside.iterdir())
