"""Run real processes through the non-Linux psutil observation/cleanup path."""
from contextlib import nullcontext
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest
import tests._bootstrap  # noqa: F401
from qatlib_env import supervisor


@pytest.fixture
def portable(monkeypatch):
    class Platform:
        platform = "darwin"

        def __getattr__(self, name):
            return getattr(sys, name)

    previous = supervisor._subreaper(True)
    with monkeypatch.context() as platform:
        platform.setattr(supervisor, "sys", Platform())
        yield
    if previous is not None:
        supervisor._subreaper(bool(previous))


def test_real_non_linux_module_timeout(portable, tmp_path, capsys):
    if os.name == "nt":
        pytest.skip("Python executable alias fixture requires POSIX; native observer covered separately")
    lean = tmp_path / "lean"
    lean.symlink_to(sys.executable)
    source = tmp_path / "模块 Slow.lean"
    source.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        supervisor.run([str(lean), str(source)], module_timeout=0.12, timeout=5, grace=0.05)
    assert raised.value.timeout == 0.12 and time.monotonic() - started < 3
    assert "module exceeded" in capsys.readouterr().err


def test_stage_timeout_is_independent_of_module_detection(portable):
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        supervisor.run([sys.executable, "-c", "import time;time.sleep(30)"],
                       module_timeout=0.05, timeout=0.15, grace=0.05)
    assert raised.value.timeout == 0.15


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGTERM disposition")
def test_portable_cleanup_escalates_for_term_ignoring_process(portable):
    children = []
    with pytest.raises(subprocess.TimeoutExpired):
        supervisor.run([sys.executable, "-c", "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"],
                       timeout=0.3, grace=0.05, process_callback=children.append)
    assert children[0].poll() is not None


def test_observation_failure_still_stops_direct_process(portable, monkeypatch):
    class Unobservable:
        def __init__(self, *args): pass
        def update(self): raise RuntimeError("cannot observe owned process")
        def cleanup(self, grace): raise RuntimeError("cannot observe owned process")
    children = []
    popen = subprocess.Popen
    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(supervisor, "_Tree", Unobservable)
    monkeypatch.setattr(supervisor.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError, match="cannot observe"):
        supervisor.run([sys.executable, "-c", "import time;time.sleep(30)"], grace=0.05)
    assert children[0].poll() is not None


def test_failed_parent_cleans_observed_child_holding_stdout(portable, tmp_path):
    pid_file = tmp_path / "pid"
    script = ("import subprocess,sys,time\n"
              "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
              "open(sys.argv[1],'w').write(str(p.pid))\n"
              "print('before failure',flush=True)\n"
              "time.sleep(.3)\nraise SystemExit(7)\n")
    started = time.monotonic()
    with pytest.raises(subprocess.CalledProcessError) as raised:
        supervisor.run([sys.executable, "-c", script, str(pid_file)], capture_output=True,
                       text=True, check=True, timeout=5, grace=0.05)
    assert raised.value.returncode == 7 and raised.value.stdout == "before failure\n"
    assert time.monotonic() - started < 3
    pid = int(pid_file.read_text())
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_cancellation_cleans_portable_tree(portable, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX signal delivery fixture")
    import threading
    children = []
    def signal_after_launch(child):
        children.append(child)
        timer = threading.Timer(0.15, lambda: os.kill(os.getpid(), signal.SIGTERM))
        timer.start()
    with pytest.raises(supervisor.ProcessCancelled) as raised:
        supervisor.run([sys.executable, "-c", "import time;time.sleep(30)"],
                       process_callback=signal_after_launch, grace=0.05)
    assert raised.value.signum == signal.SIGTERM and children[0].poll() is not None


def fake_process(pid, started, *, children=(), command=(), parent=1):
    return SimpleNamespace(pid=pid, create_time=lambda: started, oneshot=nullcontext,
                           status=lambda: psutil.STATUS_RUNNING, ppid=lambda: parent,
                           cmdline=lambda: command, children=lambda: list(children))


def test_observer_retains_orphan_identity_and_module_start(portable, monkeypatch):
    child = fake_process(20, 2.5, command=("lean.exe", "Slow.lean"), parent=10)
    root = fake_process(10, 1.5, children=[child])
    processes = {10: root, 20: child}
    def lookup(pid):
        if pid not in processes:
            raise psutil.NoSuchProcess(pid)
        return processes[pid]
    monkeypatch.setattr(supervisor.psutil, "Process", lookup)
    tree = supervisor._Tree(SimpleNamespace(pid=10), set())
    assert set(tree.update()) == {10, 20}
    started = tree.module_started[(20, 2.5)]
    processes.pop(10)
    assert set(tree.update()) == {20}
    assert tree.module_started[(20, 2.5)] == started
    processes[20] = fake_process(20, 99.0, command=("lean.exe", "Other.lean"))
    assert tree.update() == {} and (20, 99.0) not in tree.module_started


def test_cannot_observe_owned_process_is_not_silent(portable, monkeypatch):
    monkeypatch.setattr(supervisor.psutil, "Process", Mock(side_effect=psutil.AccessDenied(10)))
    with pytest.raises(RuntimeError, match="module timeout/cleanup cannot be enforced"):
        supervisor._Tree(SimpleNamespace(pid=10), set()).update()


def test_cleanup_never_signals_reused_pid(portable, monkeypatch):
    changed = Mock()
    changed.create_time.return_value = 3.0
    monkeypatch.setattr(supervisor.psutil, "Process", Mock(return_value=changed))
    parent = SimpleNamespace(pid=10, poll=lambda: 0, wait=lambda **kw: 0)
    tree = supervisor._Tree(parent, set())
    old = supervisor._Process(20, 10, 0, 0, 2.0, "R", ())
    monkeypatch.setattr(tree, "update", Mock(side_effect=[{20: old}, {}]))
    tree.cleanup(0)
    changed.terminate.assert_not_called()
    changed.kill.assert_not_called()


@pytest.mark.parametrize("fail_attach", [False, True])
def test_windows_gate_releases_only_after_callback_and_closes_before_cleanup(monkeypatch, fail_attach):
    from qatlib_env.backends import windows
    events = []
    class Gate:
        def __init__(self, command): self.command = ["gated", *command]
        def release(self): events.append("release")
        def close(self): events.append("gate-close")
    class Tree:
        def __init__(self, *args): pass
        def update(self): return {}
        def cleanup(self, grace): events.append("tree-cleanup")
    process = SimpleNamespace(pid=10, returncode=0, poll=lambda: 0,
                              communicate=lambda **kw: ("output", ""))
    def popen(command, **kwargs):
        assert command == ["gated", "lake", "build"]
        events.append("popen")
        return process
    def attach(child):
        events.append("attach")
        if fail_attach:
            raise OSError("attachment failed")
    monkeypatch.setattr(supervisor, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    monkeypatch.setattr(supervisor, "os", SimpleNamespace(name="nt", environ={}, getpid=os.getpid))
    monkeypatch.setattr(supervisor.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(supervisor.subprocess, "Popen", popen)
    monkeypatch.setattr(supervisor, "_Tree", Tree)
    monkeypatch.setattr(windows, "LaunchGate", Gate)
    if fail_attach:
        with pytest.raises(OSError, match="attachment failed"):
            supervisor.run(["lake", "build"], process_callback=attach,
                           cleanup_callback=lambda: events.append("job-close"))
        assert "release" not in events
    else:
        result = supervisor.run(["lake", "build"], process_callback=attach,
                                cleanup_callback=lambda: events.append("job-close"))
        assert result.args == ["lake", "build"] and result.stdout == "output"
        assert events.index("attach") < events.index("release")
    assert events.index("job-close") < events.index("tree-cleanup") < events.index("gate-close")


@pytest.mark.skipif(os.name != "nt", reason="native Windows Job inheritance and launch gate")
def test_native_job_gate_cleans_unobserved_orphan_and_captured_pipe(tmp_path):
    from qatlib_env.backends.windows import JobObject
    marker = tmp_path / "child-pid"
    job = JobObject()
    job.configure(memory_bytes=2 * 1024**3, cpu_percent=25)
    script = ("import subprocess,sys\n"
              "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
              "open(sys.argv[1],'w').write(str(p.pid))\n"
              "print('failed child',flush=True)\nraise SystemExit(7)\n")
    def attach(child):
        time.sleep(0.1)
        assert not marker.exists(), "command ran before Job attachment"
        assert job.attach_pid(child.pid)
    try:
        with pytest.raises(subprocess.CalledProcessError) as raised:
            supervisor.run([sys.executable, "-c", script, str(marker)], process_callback=attach,
                           cleanup_callback=job.close, capture_output=True, text=True,
                           check=True, timeout=5, grace=0.05)
        assert raised.value.returncode == 7 and raised.value.stdout == "failed child\n"
        assert not psutil.pid_exists(int(marker.read_text()))
    finally:
        job.close()


@pytest.mark.parametrize("wait_result", [0, 0xFFFFFFFF])
def test_launcher_never_executes_when_event_wait_fails(monkeypatch, wait_result):
    from qatlib_env.backends import windows_launcher
    event = Mock()
    events = SimpleNamespace(OpenEvent=Mock(return_value=event), INFINITE=0xFFFFFFFF,
                             WAIT_OBJECT_0=0, WaitForSingleObject=Mock(return_value=wait_result))
    child = Mock(return_value=7)
    monkeypatch.setitem(sys.modules, "win32event", events)
    monkeypatch.setattr(windows_launcher, "sys", SimpleNamespace(argv=["launcher", "event", "lake", "build"]))
    monkeypatch.setattr(windows_launcher.subprocess, "call", child)
    if wait_result:
        with pytest.raises(OSError, match="gate wait failed"):
            windows_launcher.main()
        child.assert_not_called()
    else:
        assert windows_launcher.main() == 7
        child.assert_called_once_with(["lake", "build"])
    event.Close.assert_called_once()
