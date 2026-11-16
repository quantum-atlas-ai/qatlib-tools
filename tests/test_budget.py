"""Deterministic local policy tests: no Lean process or network is required."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import pytest

from qatlib_env import budget, ci, config, pool, r2_cache, resources, workspace
from qatlib_env.cloud import CloudLoop


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def commit(root):
    git(root, "add", ".")
    git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture")


def outputs(root):
    for path in (root / "Consumer").rglob("*.lean"):
        name = path.relative_to(root).with_suffix("")
        output = write(root, str(Path(".lake/build/lib/lean") / name) + ".olean", "fixture-output")
        trace = write(root, str(Path(".lake/build/lib/lean") / name) + ".trace", '{"depHash":"fixture"}')
        stamp = time.time_ns() + 1_000_000_000
        os.utime(output, ns=(stamp, stamp))
        os.utime(trace, ns=(stamp, stamp))


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    write(root, ".gitignore", ".lake/\n.qatlib-env.local.toml\n")
    write(root, "lakefile.toml", 'name="Consumer"\ndefaultTargets=["Consumer"]\n[[lean_lib]]\nname="Consumer"\n')
    write(root, "lake-manifest.json", '{"packages":[]}')
    write(root, "lean-toolchain", "leanprover/lean4:v4.35.0\n")
    write(root, "qatlib-env.toml", 'repository="sample/library"\ncache_bucket="cache"\nworkflow="build.yml"\nlocal_pool_mib=16384\nlocal_pool_cpu=4\n[resources]\njobs=2\nlean_threads=1\nmemory_budget="4GiB"\n')
    write(root, "Consumer/Leaf.lean", "def leaf := 1\n")
    write(root, "Consumer/Base.lean", "def base := 1\n")
    for index in range(25):
        write(root, f"Consumer/Group/M{index}.lean", "import Consumer.Base\ndef item := 1\n")
    commit(root)
    tc = tmp_path / "toolchain"
    write(tc, "src/lean/Init.lean", "prelude\n")
    write(tc, "lib/lean/Init.olean", "precompiled-toolchain")
    for variables in config.ENV_FIELDS.values():
        for variable in variables:
            monkeypatch.delenv(variable, raising=False)
    monkeypatch.delenv("QATLAS_RUNNER_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LAKE_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("QATLAS_REAL_SYSROOT", str(tc))
    outputs(root)
    return root


def test_four_real_graph_shapes_and_routes(project):
    write(project, "Consumer/Leaf.lean", "def leaf := 2\n")
    leaf = budget.estimate(project, ["Consumer.Leaf"])
    assert (leaf["target_modules"], leaf["closure_package_modules"], leaf["closure_toolchain_modules"]) == (1, 1, 1)
    assert (leaf["cold_or_rebuild_modules"], leaf["reverse_invalidated_modules"], leaf["mode"]) == (1, 0, "local")
    assert leaf["head"] == git(project, "rev-parse", "HEAD")
    targets = [f"Consumer.Group.M{i}" for i in range(10)]
    for index in range(10):
        write(project, f"Consumer/Group/M{index}.lean", "import Consumer.Base\ndef item := 2\n")
    middle = budget.estimate(project, targets)
    assert (middle["cold_or_rebuild_modules"], middle["warm_modules"], middle["mode"]) == (10, 1, "local")
    write(project, "Consumer/Base.lean", "def base := 2\n")
    foundation = budget.estimate(project, ["Consumer.Base"])
    assert foundation["reverse_invalidated_modules"] == 25
    assert foundation["cold_or_rebuild_modules"] == 26
    assert foundation["mode"] == "cloud"
    assert {r["code"] for r in foundation["reasons"]} >= {"cold_limit", "reverse_limit"}
    full = budget.estimate(project, cold=True)
    assert full["target_modules"] == 27
    assert full["cold_or_rebuild_modules"] == 27
    assert full["warm_modules"] == 0 and full["full_gate"] and full["mode"] == "cloud"
    assert full["estimated_seconds_range"] == [68, 1620]


def test_file_library_defaults_and_no_writes(project):
    before = {str(p.relative_to(project)): (p.stat().st_size, p.stat().st_mtime_ns)
              for p in project.rglob("*") if p.is_file()}
    assert budget.estimate(project, ["Consumer/Leaf.lean"])["mode"] == "local"
    assert budget.estimate(project, ["Consumer"])["mode"] == "cloud"
    after = {str(p.relative_to(project)): (p.stat().st_size, p.stat().st_mtime_ns)
             for p in project.rglob("*") if p.is_file()}
    assert before == after


def test_unresolved_and_bad_lock_are_never_certified_warm(project):
    write(project, "Consumer/Leaf.lean", "import Missing.Module\ndef leaf := 1\n")
    report = budget.estimate(project, ["Consumer.Leaf"])
    assert "Missing.Module" in report["unresolved_imports"] and report["mode"] == "cloud"
    write(project, "lake-manifest.json", json.dumps({"packages": [{"name": "Dependency", "rev": "a"*40, "type": "git"}]}))
    report = budget.estimate(project, ["Consumer.Leaf"])
    assert report["dependency_lock_mismatches"][0]["actual"] is None
    assert report["warm_modules"] == 0


def test_nested_comments_and_escaped_imports_use_the_shared_lexer(project):
    write(project, "Consumer/Space Name.lean", "def spaced := 1\n")
    write(project, "Consumer/Leaf.lean", '/- import Missing.Fake /- nested -/ -/\nimport Consumer.«Space Name»\ndef leaf := 1\n')
    report = budget.estimate(project, ["Consumer.Leaf"])
    assert not report["unresolved_imports"]
    assert report["closure_package_modules"] == 2


def test_unknown_dependency_invalidates_unchanged_cached_user(project):
    write(project, "Consumer/Leaf.lean", "import Missing.Module\ndef leaf := 1\n")
    commit(project)
    outputs(project)
    report = budget.estimate(project, ["Consumer.Leaf"])
    assert report["warm_modules"] == 0 and report["cold_or_rebuild_modules"] == 1


def test_missing_trace_stale_dependency_and_heavy_patterns(project):
    write(project, ".lake/build/lib/lean/Consumer/Base.trace", "{}")
    report = budget.estimate(project, ["Consumer.Group.M0"])
    assert report["cold_or_rebuild_modules"] >= 2
    write(project, ".qatlib-env.local.toml", '[resources]\nheavy_patterns=["Leaf"]\nmemory_budget="8GiB"\n')
    write(project, "Consumer/Leaf.lean", "def leaf := 3\n")
    report = budget.estimate(project, ["Consumer.Leaf"])
    assert report["heavy_modules"] == ["Consumer.Leaf"]
    assert report["estimated_peak_mib"] == 8192


@pytest.mark.parametrize("document", [
    'repository="mirror/library"\nlocal_pool_mib=8192\n[resources]\ndefault_jobs=1\ndefault_memory_mib=4096\nlean_threads=2\n[budget]\nmax_cold_modules=7\nmax_reverse_modules=8\nrequire_explicit_local_heavy=true\n',
    '[tool.qatlib-env]\nrepository="mirror/library"\nlocal_pool_mib=8192\ndefault_jobs=1\ndefault_memory_mib=4096\nlean_threads=2\n[tool.qatlib-env.budget]\nmax_cold_modules=7\nmax_reverse_modules=8\nrequire_explicit_local_heavy=true\n',
    '[tool.qatlib-env]\nrepository="mirror/library"\nlocal_pool_mib=8192\n[tool.qatlib-env.resources]\njobs=1\nmemory_budget="4GiB"\nlean_threads=2\n[tool.qatlib-env.budget]\nmax_cold_modules=7\nmax_reverse_modules=8\n',
])
def test_unified_local_resources_policy_profiles_and_precedence(project, document):
    write(project, ".qatlib-env.local.toml", document)
    cfg = config.load_config(project)
    runtime = resources.resolve_config(project)
    assert cfg.repository == "mirror/library" and cfg.local_pool_mib == 8192
    assert runtime.values["jobs"] == 1 and runtime.values["memory_budget"] == 4*resources.GIB
    assert cfg.budget.max_cold_modules == 7 and cfg.budget.max_reverse_modules == 8
    assert ci.profile("Consumer", project=project)[2] == "mirror/library"
    assert r2_cache.detect_repo_profile(project)["repo_slug"] == "mirror/library"
    assert runtime.sources["jobs"].startswith("local:")
    runtime = resources.resolve_config(project, {"jobs": 3}, {**os.environ, "QATLAS_JOBS": "2"})
    assert runtime.values["jobs"] == 3 and runtime.sources["jobs"] == "command-line"


def test_primary_worktree_fallback_and_local_override(project, tmp_path):
    local = write(project, ".qatlib-env.local.toml", '[resources]\njobs=1\n[budget]\nmax_cold_modules=6\n')
    child = tmp_path / "child"
    git(project, "worktree", "add", "-qb", "child", str(child))
    assert config.local_file(child, local.name) == local
    assert resources.resolve_config(child).values["jobs"] == 1
    write(child, local.name, '[resources]\njobs=3\n')
    assert resources.resolve_config(child).values["jobs"] == 3
    assert config.load_config(child).budget.max_cold_modules == 15


def test_nested_distribution_fallback(tmp_path):
    write(tmp_path, "python/pyproject.toml", '[tool.qatlib-env]\nrepository="sample/nested"\nworkflow="build.yml"\n[tool.qatlib-env.resources]\njobs=1\n')
    assert config.load_config(tmp_path).repository == "sample/nested"
    assert resources.resolve_config(tmp_path).values["jobs"] == 1
    write(tmp_path, "qatlib-env.toml", 'repository="sample/shared"\n')
    assert config.load_config(tmp_path).repository == "sample/shared"


def test_distribution_has_no_consumer_or_machine_configuration(tmp_path):
    root = Path(__file__).resolve().parents[1]
    settings = config.load_config(root, env={"HOME": str(tmp_path)})
    assert "search_source_roots" not in settings.metadata
    assert "search_module_roots" not in settings.metadata
    assert settings.sources["local_pool_mib"] == "automatic"


@pytest.mark.parametrize("text", ['[resources', '[resources]\njobs=true', '[budget]\nmax_cold_modules="15"',
                                        'source_roots="Consumer"', '[resources]\nheavy_patterns=8',
                                        '[tool.qatlib-env]\nresources=3', '[budget]\nrequire_explicit_local_heavy=false'])
def test_invalid_toml_and_types_fail_in_both_consumers(project, text):
    write(project, ".qatlib-env.local.toml", text)
    for load in (config.load_config, resources.resolve_config):
        with pytest.raises(ValueError): load(project)


def test_pool_request_cannot_enlarge_or_wait_for_impossible_capacity(tmp_path):
    instance = pool.Pool(budget_mib=4096, budget_cpu=2, state_dir=tmp_path / "ledger")
    with pytest.raises(pool.PoolCapacityError): instance.acquire(8192, 3)
    assert not instance.state_dir.exists()
    with patch.object(pool, "Pool") as constructor:
        with pytest.raises(ValueError, match="硬上限"):
            with workspace._pool_scope({"QATLAS_MEMORY_BUDGET": str(8*resources.GIB),
                                       "QATLAS_POOL_BUDGET_MIB": "4096", "QATLAS_JOBS": "3", "QATLAS_POOL_CPU": "2"}, building=True):
                pytest.fail("must not enter")
        constructor.assert_not_called()


def test_active_pool_cannot_be_raised_by_second_caller(tmp_path):
    first = pool.Pool(budget_mib=4096, budget_cpu=2, state_dir=tmp_path)
    lease = first.acquire(2048, 1)
    try:
        larger = pool.Pool(budget_mib=32768, budget_cpu=8, state_dir=tmp_path)
        with pytest.raises(pool.PoolCapacityError): larger.acquire(8192, 4)
    finally:
        first.release(lease)


@pytest.mark.parametrize("command", [["lake", "build"], ["lake", "lint"], ["lake", "test"],
    ["lake", "exe", "test"], ["lake", "lean", "Consumer/Leaf.lean"],
    ["lake", "--no-build", "lean", "Consumer/Leaf.lean"], ["python", "driver.py"],
    ["env", "lake", "build"], ["uv", "run", "python", "driver.py"], ["lean", "--stdin"],
    ["elan", "run", "leanprover/lean4:v4.35.0", "lake", "build"]])
def test_no_prepare_and_policy_local_do_not_bypass_admission(project, command):
    write(project, "Consumer/Leaf.lean", "import Missing.Module\ndef leaf := 1\n")
    with patch.object(workspace, "environment") as environment, patch.object(workspace, "run") as run:
        assert workspace.cli_main(["--project", str(project), "run", "--no-prepare", "--policy", "local", "--", *command]) == 1
        run.assert_not_called()
        environment.assert_not_called()


def test_check_is_a_full_gate_and_rejects_before_preparation(project):
    with patch("qatlib_env.prepare.prepare") as prepare, patch.object(workspace, "environment") as environment:
        assert workspace.cli_main(["--project", str(project), "check", "--no-prepare", "Consumer.Leaf"]) == 1
        prepare.assert_not_called()
        environment.assert_not_called()


def test_allow_flag_is_single_call_and_hard_pool_still_denies(project):
    with budget.permission(True):
        plan = budget.admit(project, full_gate=True)
        assert plan["allow_local_heavy"] is True
    with pytest.raises(budget.AdmissionError): budget.admit(project, full_gate=True)
    with pytest.raises(budget.AdmissionError):
        budget.admit(project, full_gate=True, allow_local_heavy=True, cli={"local_pool_mib": 1024})
    with pytest.raises(budget.AdmissionError):
        budget.admit(project, full_gate=True, policy="cloud", allow_local_heavy=True)


def test_environment_variables_cannot_grant_heavy_permission(project, monkeypatch):
    monkeypatch.setenv("QATLAS_POOL_BYPASS", "1")
    monkeypatch.setenv("QATLAS_ALLOW_LOCAL_HEAVY", "1")
    with pytest.raises(budget.AdmissionError): budget.admit(project, full_gate=True)


def test_full_gate_context_cannot_drop_controller_requirement(project):
    with budget.permission(True, full_gate=True):
        plan = budget.admit(project, ["Consumer.Leaf"])
        assert plan["full_gate"] and plan["allow_local_heavy"]
    assert not budget.estimate(project, ["Consumer.Leaf"])["full_gate"]


@pytest.mark.parametrize("options", [["+other-toolchain"], ["--toolchain", "other-toolchain"], ["-f", "alternate.toml"]])
def test_lake_global_overrides_cannot_change_planned_inputs(project, options):
    with pytest.raises(ValueError):
        budget.command_request(["lake", *options, "build", "Consumer.Leaf"], project)


def test_programmatic_check_retains_full_gate_scope_for_all_children(project):
    seen = []
    with budget.permission(True), patch.object(workspace, "run", side_effect=lambda *a, **kw: seen.append(budget._full_gate.get())):
        workspace.check(project, os.environ, ["Consumer.Leaf"])
    assert seen and all(seen)


def test_explicit_task_budget_cannot_be_exceeded_by_compiler_floor(project):
    with pytest.raises(budget.AdmissionError):
        budget.admit(project, ["Consumer.Leaf"], allow_local_heavy=True, cli={"memory_budget": "1GiB"})


def test_corrupt_ledger_is_not_treated_as_an_empty_pool(tmp_path):
    write(tmp_path, "ledger.json", "{broken")
    with pytest.raises(ValueError, match="corrupt"):
        pool.Pool(state_dir=tmp_path).acquire(2048)


def test_failed_process_callback_reaps_started_child():
    from qatlib_env import supervisor
    import sys
    children = []
    def fail(child):
        children.append(child)
        raise OSError("cannot attach resource controller")
    with pytest.raises(OSError):
        supervisor.run([sys.executable, "-c", "import time;time.sleep(60)"], process_callback=fail)
    assert children[0].poll() is not None


def test_explicit_full_gate_keeps_limits_for_a_small_target(project):
    with patch.object(workspace, "command_runtime_environment", side_effect=lambda p, e, a: e), \
         patch("qatlib_env.backends.linux.systemd_available", return_value=False), \
         patch("qatlib_env.backends.linux.enclosing_cgroup_limits", return_value=None), \
         patch.object(workspace.supervisor, "run") as child:
        assert workspace.cli_main(["--project", str(project), "run", "--no-prepare", "--full-gate",
                                   "--allow-local-heavy", "--", "lake", "build", "Consumer.Leaf"]) == 1
        child.assert_not_called()


def test_heavy_permission_cannot_disable_timeouts(project):
    with patch.object(workspace, "command_runtime_environment", side_effect=lambda p, e, a: e), \
         patch.object(workspace.supervisor, "run") as child:
        assert workspace.cli_main(["--project", str(project), "run", "--no-prepare", "--allow-local-heavy",
                                   "--stage-timeout", "0", "--", "lake", "lint"]) == 1
        child.assert_not_called()


def test_allowed_cli_preserves_pool_controller_and_timeouts(project):
    with patch.object(workspace, "command_runtime_environment", side_effect=lambda p, e, a: e), \
         patch("qatlib_env.backends.linux.systemd_available", return_value=True), \
         patch.object(workspace, "_pool_scope", return_value=nullcontext()) as scope, \
         patch.object(workspace.supervisor, "run", return_value=subprocess.CompletedProcess([], 0, stdout="")) as child, \
         patch("qatlib_env.checkpoint.capture", return_value={}):
        assert workspace.cli_main(["--project", str(project), "run", "--no-prepare", "--allow-local-heavy", "--", "lake", "lint"]) == 0
    assert scope.call_args.kwargs["building"] is True
    argv = child.call_args.args[0]
    assert argv[0] == "systemd-run" and "MemoryMax=4096M" in argv and "CPUQuota=200%" in argv
    assert child.call_args.kwargs["timeout"] > 0 and child.call_args.kwargs["module_timeout"] > 0
    assert "allow_local_heavy" not in child.call_args.kwargs["env"]


def test_missing_controller_or_disabled_scope_cannot_be_bypassed(project):
    with patch("qatlib_env.backends.linux.systemd_available", return_value=False), \
         patch("qatlib_env.backends.linux.enclosing_cgroup_limits", return_value=None):
        with pytest.raises(ValueError, match="systemd"):
            workspace._backend_command(["lake", "build"], {}, building=True, require_limits=True)
    with pytest.raises(ValueError, match="controller"):
        workspace._backend_command(["lake", "build"], {"QATLAS_USE_SYSTEMD_SCOPE": "0"}, building=True, require_limits=True)


def test_windows_job_configuration_sets_real_hard_limit_flags():
    import ctypes
    from types import SimpleNamespace
    from qatlib_env.backends import windows
    observations = []
    class Function:
        def __init__(self, result=1, callback=None): self.result, self.callback = result, callback
        def __call__(self, *args):
            if self.callback: self.callback(*args)
            return self.result
    def observe(handle, kind, pointer, size):
        value = pointer._obj
        if kind == 9:
            observations.append((kind, value.BasicLimitInformation.LimitFlags, value.JobMemoryLimit))
        if kind == 15:
            observations.append((kind, value.ControlFlags, value.CpuRate))
    kernel = SimpleNamespace(CreateJobObjectW=Function(), SetInformationJobObject=Function(callback=observe),
        OpenProcess=Function(), AssignProcessToJobObject=Function(), CloseHandle=Function())
    with patch.object(windows.sys, "platform", "win32"), patch.object(ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True):
        job = windows.JobObject()
        job.configure(memory_bytes=2*resources.GIB, cpu_percent=25)
        assert observations == [(9, 0x2300, 2*resources.GIB), (15, 0x5, 2500)]
        kernel.SetInformationJobObject.result = 0
        with pytest.raises(OSError): job.configure(memory_bytes=2*resources.GIB)
        job.close()


@pytest.mark.parametrize("status", ["queued", "unavailable"])
def test_remote_queue_and_failure_never_start_local(project, status):
    state = write(project, "state.json", '{"run_id":1}')
    class API:
        def run(self, run):
            if status == "unavailable": raise OSError("offline")
            return {"status": "queued"}
    with patch.object(workspace, "run") as local:
        with pytest.raises((TimeoutError, OSError)):
            CloudLoop(API(), state).wait(timeout=0)
        local.assert_not_called()


def test_remote_probe_is_opt_in_and_does_not_reduce_cold_count(project):
    with patch.object(budget, "remote_cache", return_value={"status": "reachable", "manifest_entries": 42, "module_hit_rate": None}) as probe:
        report = budget.estimate(project, ["Consumer.Leaf"])
        probe.assert_not_called()
        checked = budget.estimate(project, ["Consumer.Leaf"], check_remote_cache=True)
        assert checked["remote_cache"]["manifest_entries"] == 42
        assert checked["cold_or_rebuild_modules"] == report["cold_or_rebuild_modules"]


@pytest.mark.parametrize("http,expected", [("200", "reachable"), ("404", "missing"), ("403", "unknown")])
def test_remote_manifest_transport_uses_stdin_and_writes_nothing(project, http, expected):
    write(project, "lake-cache.toml", '[cache]\ndefaultService="fixture"\n[[cache.service]]\nname="fixture"\nrevisionEndpoint="https://cache.test/revisions"\ns3Auth="read:fixture-secret"\n')
    body = '"2026-03-17"\n["input", "0123456789abcdef.olean"]\n'
    before = {str(p) for p in project.rglob("*")}
    with patch.object(budget.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout=body + "\nHTTP=" + http)) as transfer:
        response = budget.remote_cache(project, "a"*40, config.EnvConfig(repository="sample/library"))
    assert response["status"] == expected and response["module_hit_rate"] is None
    assert "fixture-secret" not in " ".join(transfer.call_args.args[0])
    assert "fixture-secret" in transfer.call_args.kwargs["input"]
    assert "fixture-secret" not in json.dumps(response)
    assert {str(p) for p in project.rglob("*")} == before
