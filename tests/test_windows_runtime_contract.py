"""Windows overlay selection and build/bootstrap hard-control contracts."""
from contextlib import nullcontext
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import tests._bootstrap  # noqa: F401
from qatlib_env import compiler, prepare, resources, workspace
from qatlib_env.backends import windows


@pytest.fixture
def win32(monkeypatch):
    class Platform:
        platform = "win32"

        def __getattr__(self, name):
            return getattr(sys, name)

    platform = Platform()
    monkeypatch.setattr(workspace, "sys", platform)
    monkeypatch.setattr(prepare, "sys", platform)
    monkeypatch.setattr(workspace, "command_environment", lambda p, e, a: e)


@pytest.fixture
def environment():
    return {"QATLIB_JOBS": "2", "QATLIB_LEAN_THREADS": "3", "QATLIB_MEMORY_BUDGET": str(4 * resources.GIB),
            "QATLIB_HEAVY_PATTERNS": "", "QATLIB_HEAVY_JOBS": "1",
            "QATLIB_STAGE_TIMEOUT": "5", "QATLIB_MODULE_TIMEOUT": "0.12"}


@pytest.mark.parametrize("command", [["lake", "build"], ["lake.exe", "exe", "cache", "get"],
                                    ["python", "driver.py"], ["bash", "driver.sh"], ["elan", "run"]])
def test_windows_skips_posix_overlay_and_preserves_module_timeout(win32, environment, tmp_path, monkeypatch, command):
    overlay = Mock(side_effect=AssertionError("POSIX overlay used"))
    monkeypatch.setattr(compiler, "environment", overlay)
    original = dict(environment)
    effective = workspace.command_runtime_environment(tmp_path, environment, command)
    assert effective["LEAN_NUM_THREADS"] == "3" and effective["QATLIB_MODULE_TIMEOUT"] == "0.12"
    assert "LEAN_SYSROOT" not in effective and environment == original
    overlay.assert_not_called()


@pytest.mark.parametrize("patterns", [None, "BalancedGram", "*.Heavy.*,Expensive"])
def test_windows_slot_contract_is_rejected_before_launch(win32, environment, tmp_path, patterns):
    if patterns is None:
        environment.pop("QATLIB_HEAVY_PATTERNS")
    else:
        environment["QATLIB_HEAVY_PATTERNS"] = patterns
    with pytest.raises(ValueError, match=r"heavy_patterns/heavy_jobs.*heavy_patterns=\[\]"):
        workspace.command_runtime_environment(tmp_path, environment, ["lake", "build"])


def test_posix_overlay_is_retained(tmp_path, environment, monkeypatch):
    monkeypatch.setattr(workspace, "sys", SimpleNamespace(platform="linux", stderr=sys.stderr))
    monkeypatch.setattr(workspace, "command_environment", lambda p, e, a: e)
    overlay = Mock(return_value={"overlay": "used"})
    monkeypatch.setattr(compiler, "environment", overlay)
    assert workspace.command_runtime_environment(tmp_path, environment, ["lake", "build"]) == {"overlay": "used"}
    overlay.assert_called_once()


def test_windows_non_compiler_command_has_no_overlay_contract(win32, environment, tmp_path):
    environment["QATLIB_HEAVY_PATTERNS"] = "Heavy"
    assert workspace.command_runtime_environment(tmp_path, environment, ["git", "status"]) == environment


@pytest.mark.parametrize("bootstrap", [False, True])
def test_build_and_bootstrap_share_job_limits_and_timeouts(win32, environment, tmp_path, monkeypatch, capsys, bootstrap):
    job = Mock()
    job.attach_pid.return_value = True
    monkeypatch.setattr(windows, "JobObject", Mock(return_value=job))
    monkeypatch.setattr(workspace.os, "cpu_count", lambda: 12)
    pool = Mock(return_value=nullcontext())
    monkeypatch.setattr(workspace, "_pool_scope", pool)
    resolved = SimpleNamespace(environment=lambda base: {**base, **environment})
    monkeypatch.setattr(workspace.resources, "resolve_config", Mock(return_value=resolved))
    def child(command, **kwargs):
        assert kwargs["timeout"] == 5 and kwargs["module_timeout"] == 0.12
        assert kwargs["env"]["LEAN_NUM_THREADS"] == "3"
        kwargs["process_callback"](SimpleNamespace(pid=123))
        kwargs["cleanup_callback"]()
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")
    monkeypatch.setattr(workspace.supervisor, "run", child)
    if bootstrap:
        assert prepare._cache_run(["lake", "exe", "cache", "get"], tmp_path, environment).stdout == "ok"
    else:
        from qatlib_env import budget
        monkeypatch.setattr(budget, "command_request", lambda *a: (True, [], False))
        monkeypatch.setattr(budget, "admit", lambda *a, **kw: {"reasons": []})
        assert workspace.run(["lake", "build"], cwd=tmp_path, env=environment, capture=True) == "ok"
    job.configure.assert_called_once_with(memory_bytes=4 * resources.GIB, cpu_percent=50)
    job.attach_pid.assert_called_once_with(123)
    assert job.close.called and pool.call_args.kwargs["building"] is True
    assert "per-compiler -M and heavy-module slots are unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("bootstrap", [False, True])
def test_disabled_job_is_refused_even_for_light_builds(win32, environment, tmp_path, monkeypatch, bootstrap):
    environment["QATLIB_USE_JOB_OBJECT"] = "0"
    monkeypatch.setattr(workspace, "_pool_scope", lambda *a, **kw: nullcontext())
    resolved = SimpleNamespace(environment=lambda base: {**base, **environment})
    monkeypatch.setattr(resources, "resolve_config", Mock(return_value=resolved))
    runner = Mock()
    monkeypatch.setattr(workspace.supervisor, "run", runner)
    if bootstrap:
        call = lambda: prepare._cache_run(["lake", "exe", "cache", "get"], tmp_path, environment)
    else:
        from qatlib_env import budget
        monkeypatch.setattr(budget, "command_request", lambda *a: (True, [], False))
        monkeypatch.setattr(budget, "admit", lambda *a, **kw: {"reasons": []})
        call = lambda: workspace.run(["lake", "build"], cwd=tmp_path, env=environment)
    with pytest.raises(ValueError, match="requires a Windows Job Object"):
        call()
    runner.assert_not_called()


@pytest.mark.parametrize("failure", ["configure", "attach"])
def test_job_failures_close_handle_and_propagate(win32, environment, monkeypatch, failure):
    job = Mock()
    monkeypatch.setattr(windows, "JobObject", Mock(return_value=job))
    if failure == "configure":
        job.configure.side_effect = OSError("configuration failed")
    else:
        job.attach_pid.return_value = False
    with pytest.raises(OSError):
        with workspace._windows_job_scope(environment, building=True) as (attach, cleanup):
            attach(SimpleNamespace(pid=123))
    job.close.assert_called_once()


def test_cache_bootstrap_rejects_slots_before_pool_or_child(win32, environment, tmp_path, monkeypatch):
    environment["QATLIB_HEAVY_PATTERNS"] = "BalancedGram"
    monkeypatch.setattr(resources, "resolve_config", Mock(return_value=SimpleNamespace(environment=lambda base: base)))
    runner = Mock()
    monkeypatch.setattr(workspace.supervisor, "run", runner)
    with pytest.raises(ValueError, match="cannot enforce heavy_patterns/heavy_jobs"):
        prepare._cache_run(["lake", "exe", "cache", "get"], tmp_path, environment)
    runner.assert_not_called()
