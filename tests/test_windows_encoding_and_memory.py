"""UTF-8/LF protocol files and host memory fallback, independent of locale."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
import tests._bootstrap  # noqa: F401
from qatlib_env import budget, ci, prepare, r2_cache, resources, server


def test_prepare_and_server_keys_ignore_crlf_but_detect_config_edits(tmp_path):
    config = {
        "lean-toolchain": "leanprover/lean4:v4.35.0\n",
        "lake-manifest.json": '{"packages": []}\n',
        "lakefile.toml": 'name = "Example"\n# 中文配置\n',
        "lakefile.lean": "-- 中文配置\n",
        "lake-cache.toml": "[cache]\n# 中文配置\n",
    }
    env = {"LAKE_CACHE_DIR": str(tmp_path / "cache")}
    for eol in ("\n", "\r\n"):
        for filename, value in config.items():
            (tmp_path / filename).write_bytes(value.replace("\n", eol).encode("utf-8"))
        keys = prepare.identity(tmp_path, env), server.server_key(tmp_path, env).digest
        if eol == "\n":
            expected = keys
        else:
            assert keys == expected
    (tmp_path / "lakefile.toml").write_bytes(b'name = "Changed"\r\n')
    assert prepare.identity(tmp_path, env) != expected[0]
    assert server.server_key(tmp_path, env).digest != expected[1]


def test_ci_and_budget_read_toml_as_utf8(tmp_path, monkeypatch):
    config = tmp_path / "lakefile.toml"
    config.write_bytes('name = "Example"\r\n# 中文配置\r\n'.encode("utf-8"))
    read_text = Path.read_text

    def legacy_locale(path, encoding=None, **kwargs):
        return read_text(path, encoding=encoding or "ascii", **kwargs)

    monkeypatch.setattr(Path, "read_text", legacy_locale)
    assert budget.SourceGraph._toml(config)["name"] == "Example"
    assert ci.source_inputs(tmp_path, "Example") == [
        "Example", "Example.lean", "lakefile.toml", "lean-toolchain", "lake-manifest.json"]


def test_curlrc_uses_explicit_utf8_and_lf(tmp_path, monkeypatch):
    observed = []
    write = Path.write_text
    def record(path, data, **kwargs):
        if path.name == ".curlrc":
            observed.append(kwargs)
        return write(path, data, **kwargs)
    monkeypatch.setattr(Path, "write_text", record)
    key = "fixture_access:fixture_secret"
    with ci.authenticated({"TMPDIR": str(tmp_path)}, key) as env:
        raw = (Path(env["CURL_HOME"]) / ".curlrc").read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n")
        assert 'user = "fixture_access:fixture_secret"\n' in raw.decode("utf-8")
    assert observed == [{"encoding": "utf-8", "newline": "\n"}]


def test_artifact_listing_is_utf8_lf_and_preserves_unicode_paths(tmp_path, monkeypatch):
    (tmp_path / ".lake").mkdir()
    paths = {}
    for index, name in enumerate(("缓存 甲.art", "café β.art")):
        path = tmp_path / name
        path.write_bytes(f"artifact-{index}".encode())
        paths[f"{index:016x}.art"] = path
    observed = []
    write = Path.write_text
    def record(path, data, **kwargs):
        if path.name == "artifacts.txt":
            observed.append(kwargs)
        return write(path, data, **kwargs)
    monkeypatch.setattr(Path, "write_text", record)
    def native_hash(repo, env, command, **kwargs):
        raw = Path(env["QATLIB_ARTIFACT_AUDIT_LIST"]).read_bytes()
        assert b"\r" not in raw and raw.decode("utf-8").splitlines() == [str(p) for p in paths.values()]
        return "0000000000000000\n0000000000000001\n"
    monkeypatch.setattr(r2_cache, "_run", native_hash)
    assert r2_cache._verify_artifact_hashes(tmp_path, paths, {}) == {
        name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    assert observed == [{"encoding": "utf-8", "newline": "\n"}]


def test_psutil_reads_real_host_totals_without_proc(tmp_path, monkeypatch):
    probe = Mock(return_value=SimpleNamespace(total=64 * resources.GIB, available=48 * resources.GIB))
    monkeypatch.setattr(psutil, "virtual_memory", probe)
    host = resources.probe_resources(proc_root=tmp_path / "proc", cgroup_root=tmp_path / "cgroup",
                                     cpu_count=16, affinity=set(range(16)))
    assert (host.physical_memory, host.available_memory) == (64 * resources.GIB, 48 * resources.GIB)
    assert host.memory_budget == 48 * resources.GIB - host.reserve
    probe.assert_called_once()


@pytest.mark.parametrize("error", [OSError("unavailable"), PermissionError("denied"), RuntimeError("backend failure")])
def test_psutil_failure_keeps_conservative_fallback(tmp_path, monkeypatch, error):
    monkeypatch.setattr(psutil, "virtual_memory", Mock(side_effect=error))
    host = resources.probe_resources(proc_root=tmp_path, cgroup_root=tmp_path / "cgroup", cpu_count=2, affinity={0, 1})
    assert host.physical_memory == host.available_memory == 4 * resources.GIB
    assert 0 < host.memory_budget < 4 * resources.GIB


def test_linux_meminfo_remains_authoritative(tmp_path, monkeypatch):
    (tmp_path / "meminfo").write_text("MemTotal: 16777216 kB\nMemAvailable: 8388608 kB\n")
    probe = Mock(side_effect=AssertionError("psutil should not replace /proc values"))
    monkeypatch.setattr(psutil, "virtual_memory", probe)
    host = resources.probe_resources(proc_root=tmp_path, cgroup_root=tmp_path / "cgroup", cpu_count=2, affinity={0, 1})
    assert host.physical_memory == 16 * resources.GIB and host.available_memory == 8 * resources.GIB
    probe.assert_not_called()
