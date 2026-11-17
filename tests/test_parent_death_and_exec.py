"""Regression coverage for detached workloads and bounded generic commands."""
import tests._bootstrap as _bootstrap  # noqa: F401
from contextlib import nullcontext
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import patch

import pytest

from qatlib_env import execute, supervisor
from qatlib_env.backends import linux
from qatlib_search.index import SearchIndex


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux guardian")
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_guardian_preserves_workload_signal_exit(signum):
    command = [sys.executable, "-c", f"import os; os.kill(os.getpid(), {int(signum)})"]
    assert supervisor.run(command).returncode == -signum


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux PDEATHSIG")
def test_hard_killed_supervisor_still_reaps_detached_workload(tmp_path):
    pids = tmp_path / "pids"
    leaf = "import os,signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    workload = ("import os,subprocess,sys,time; "
                f"p=subprocess.Popen([sys.executable,'-c',{leaf!r}],start_new_session=True); "
                "open(sys.argv[1],'w').write(str(os.getpid())+' '+str(p.pid)); time.sleep(60)")
    driver = "from qatlib_env import supervisor; import sys; supervisor.run(sys.argv[1:],grace=.1)"
    env = {**os.environ, "PYTHONPATH": str(Path(supervisor.__file__).parents[1])}
    previous = supervisor._subreaper(True)
    process = subprocess.Popen([sys.executable, "-c", driver, sys.executable, "-c", workload, str(pids)], env=env)
    owned = []
    guardians = set()
    try:
        deadline = time.monotonic() + 5
        while not pids.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert pids.exists(), "workload did not start"
        owned = list(map(int, pids.read_text().split()))
        guardians = supervisor._children(process.pid)
        # Kill only the test supervisor, leaving the guardian to reap a setsid
        # child which ignores TERM. No process-name or global kill is involved.
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while any(Path(f"/proc/{pid}").exists() for pid in owned) and time.monotonic() < deadline:
            time.sleep(.02)
        assert not [pid for pid in owned if Path(f"/proc/{pid}").exists()]
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        # The guardian itself is adopted by this test subreaper after exit.
        for pid in guardians:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        supervisor._subreaper(bool(previous))


def test_exec_refuses_missing_controller():
    with patch.object(execute, "systemd_available", return_value=False), \
         patch.object(execute.supervisor, "run") as run, pytest.raises(SystemExit):
        execute.main(["--", "python", "experiment.py"])
    run.assert_not_called()


@pytest.mark.parametrize("option,value", [("--timeout", "0"), ("--timeout", "nan"), ("--timeout", "inf"),
                                         ("--cpu", "0"), ("--memory-mib", "1")])
def test_exec_rejects_unbounded_values(option, value):
    with pytest.raises(SystemExit):
        execute.main([option, value, "--", "true"])


def test_exec_has_kernel_and_supervisor_deadlines():
    with patch.object(execute.sys, "platform", "linux"), \
         patch.object(execute, "_pool_scope", return_value=nullcontext()) as pool, \
         patch.object(execute, "systemd_available", return_value=True), \
         patch.object(linux, "systemd_available", return_value=True), \
         patch.object(execute.supervisor, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
        assert execute.main(["--timeout", "12", "--", "python", "experiment.py"]) == 0
    command = run.call_args.args[0]
    assert "RuntimeMaxSec=12.0" in command
    assert "MemoryMax=4096M" in command and "MemorySwapMax=0" in command
    assert "CPUQuota=100%" in command and "KillMode=control-group" in command
    assert run.call_args.kwargs["timeout"] == 12
    assert pool.call_args.args[0]["QATLAS_MEMORY_BUDGET"] == str(4096 * 1024**2)
    assert pool.call_args.args[0]["QATLAS_JOBS"] == "1"


def test_search_prunes_build_trees_but_accepts_explicit_dependency_root(tmp_path):
    (tmp_path / "Source.lean").write_text("def visible := 1\n")
    cache = tmp_path / ".lake" / "packages"
    cache.mkdir(parents=True)
    (cache / "Dependency.lean").write_text("def hidden := 2\n")
    assert [item.name for item in SearchIndex([tmp_path]).build()] == ["visible"]
    assert [item.name for item in SearchIndex([cache]).build()] == ["hidden"]
