"""Bound subprocess lifetimes and reap their descendants on timeout/cancellation."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
import math
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


class ProcessCancelled(subprocess.SubprocessError):
    def __init__(self, signum: int, command):
        self.signum = signum
        self.returncode = 128 + signum
        self.cmd = command
        super().__init__(f"command cancelled by {signal.Signals(signum).name}: {command}")


@dataclass(frozen=True)
class _Process:
    pid: int
    parent: int
    group: int
    session: int
    started: int
    state: str
    command: tuple[str, ...]


def _children(pid: int) -> set[int]:
    result = set()
    try:
        for task in Path(f"/proc/{pid}/task").iterdir():
            try:
                result.update(map(int, (task / "children").read_text().split()))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                pass
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        pass
    return result


def _processes(roots: set[int]) -> dict[int, _Process]:
    result = {}
    pending = set(roots)
    seen = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pending.update(_children(pid) - seen)
        item = Path(f"/proc/{pid}")
        try:
            # comm can contain spaces and closing parentheses.
            fields = (item / "stat").read_text().rsplit(")", 1)[1].split()
            command = tuple(part.decode(errors="replace") for part in (item / "cmdline").read_bytes().split(b"\0") if part)
            result[pid] = _Process(pid, int(fields[1]), int(fields[2]), int(fields[3]),
                                   int(fields[19]), fields[0], command)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    return result


def _subreaper(enable: bool | None = None) -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0):  # PR_GET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot inspect child subreaper")
    if enable is not None and libc.prctl(36, int(enable), 0, 0, 0):  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot configure child subreaper")
    return previous.value


def _send(pid: int, signum: int, started: int | None = None) -> None:
    try:
        if started is not None:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[19]) != started:
                return
        os.kill(pid, signum)
    except (ProcessLookupError, FileNotFoundError):
        pass


def _is_lean(process: _Process) -> bool:
    # Python admission wrappers are not Lean processes. Their queueing time is
    # bounded by the stage timeout; module timeout begins after exec(real lean).
    if not process.command or Path(process.command[0]).name not in ("lean", "lean.exe"):
        return False
    return any(part.endswith(".lean") for part in process.command[1:])


class _Tree:
    def __init__(self, process: subprocess.Popen, baseline: set[int]):
        self.process = process
        self.baseline = baseline
        self.owned: dict[int, _Process] = {}
        self.module_started: dict[tuple[int, int], float] = {}

    def update(self) -> dict[int, _Process]:
        if not sys.platform.startswith("linux"):
            return {}
        roots = {self.process.pid, *self.owned, *(_children(os.getpid()) - self.baseline)}
        all_processes = _processes(roots)
        changed = True
        while changed:
            changed = False
            for pid, item in all_processes.items():
                if pid in self.owned and self.owned[pid].started == item.started:
                    continue
                if (pid == self.process.pid or item.parent in self.owned or item.group == self.process.pid
                        or item.session == self.process.pid
                        or (item.parent == os.getpid() and pid not in self.baseline)):
                    self.owned[pid] = item
                    changed = True
        live = {pid: item for pid, item in all_processes.items()
                if pid in self.owned and self.owned[pid].started == item.started}
        for pid, item in live.items():
            if _is_lean(item):
                self.module_started.setdefault((pid, item.started), time.monotonic())
        return live

    def cleanup(self, grace: float) -> None:
        if os.name == "nt":
            # taskkill is the native process-tree primitive; the supervisor owns
            # exactly this new process group, never a globally matched process.
            killer = subprocess.Popen(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            killer.wait()
            self.process.wait()
            return
        live = self.update()
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for pid, item in live.items():
            _send(pid, signal.SIGTERM, item.started)
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            self.process.poll()
            live = self.update()
            self._reap(live)
            if not any(item.state != "Z" for item in live.values()):
                break
            time.sleep(0.02)
        # Re-scan after TERM to include descendants spawned by signal handlers.
        live = self.update()
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for pid, item in live.items():
            _send(pid, signal.SIGKILL, item.started)
        self.process.wait()
        deadline = time.monotonic() + max(2.0, grace)
        while True:
            live = self.update()
            self._reap(live)
            if not live or time.monotonic() >= deadline:
                break
            for pid, item in live.items():
                _send(pid, signal.SIGKILL, item.started)
            time.sleep(0.01)
        remaining = self.update()
        if remaining:
            raise RuntimeError("process cleanup failed; remaining owned PIDs: " + ", ".join(map(str, remaining)))

    def _reap(self, live: dict[int, _Process]) -> None:
        for pid in live:
            if pid == self.process.pid:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass


def run(command, *, cwd=None, env=None, timeout=None, module_timeout=None,
        grace=2.0, capture_output=False, text=False, check=False, input=None,
        stdout=None, stderr=None, process_callback=None, **kwargs) -> subprocess.CompletedProcess:
    """A sequential, main-thread ``subprocess.run`` with process-tree ownership.

    On Linux, a subreaper also collects double-forked and setsid descendants.
    SIGTERM/SIGINT handlers are scoped to this call. No PID-name matching or
    global killing occurs. Timeout diagnostics include the offending module.
    """
    budget_env = os.environ if env is None else env
    if budget_env.get("QATLAS_STAGE_DEADLINE"):
        remaining = float(budget_env["QATLAS_STAGE_DEADLINE"]) - time.monotonic()
        if not math.isfinite(remaining):
            raise ValueError("invalid stage deadline")
        if remaining <= 0:
            print("TIMEOUT: stage deadline exhausted before command", file=sys.stderr, flush=True)
            raise subprocess.TimeoutExpired(command, 0)
        timeout = min(timeout, remaining) if timeout else remaining
    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError("stdout/stderr cannot be used with capture_output")
        stdout = stderr = subprocess.PIPE
    if input is not None:
        if "stdin" in kwargs:
            raise ValueError("stdin and input may not both be supplied")
        kwargs["stdin"] = subprocess.PIPE
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    baseline = _children(os.getpid()) if sys.platform.startswith("linux") else set()
    previous_subreaper = _subreaper(True)
    handlers = {}
    process = None
    tree = None
    cancellation = None

    def cancelled(signum, _frame):
        # Raising inside Popen can lose the newly forked child before Python has
        # returned its handle. Record cancellation and raise after ownership is
        # established; polling bounds response latency to 50 milliseconds.
        nonlocal cancellation
        cancellation = signum

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGINT):
            handlers[signum] = signal.signal(signum, cancelled)
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=stdout, stderr=stderr,
                                   text=text, **kwargs)
        if process_callback is not None:
            process_callback(process)
        tree = _Tree(process, baseline)
        started = time.monotonic()
        pending_input = input
        while True:
            if cancellation is not None:
                raise ProcessCancelled(cancellation, command)
            live = tree.update()
            # A failed Lake can leave a compiler holding a captured stdout pipe.
            # communicate() alone then waits for EOF until the *stage* timeout.
            # Reap descendants as soon as the direct parent has exited instead.
            if process.poll() is not None:
                tree.cleanup(grace)
                output, errors = process.communicate()
                break
            elapsed = time.monotonic() - started
            reason = None
            if timeout and elapsed >= timeout:
                reason = f"stage exceeded {timeout}s"
            if module_timeout:
                for pid, item in live.items():
                    key = (pid, item.started)
                    if _is_lean(item) and time.monotonic() - tree.module_started[key] >= module_timeout:
                        reason = f"module exceeded {module_timeout}s: {' '.join(item.command)} (pid={pid})"
                        break
            if reason:
                print("TIMEOUT: " + reason, file=sys.stderr, flush=True)
                raise subprocess.TimeoutExpired(command, timeout if reason.startswith("stage") else module_timeout)
            try:
                output, errors = process.communicate(input=pending_input, timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        if cancellation is not None:
            raise ProcessCancelled(cancellation, command)
        result = subprocess.CompletedProcess(command, process.returncode, output, errors)
        if check:
            result.check_returncode()
        return result
    finally:
        # Ignore repeated cancellation while reaping. Restore the caller's
        # handlers and subreaper status only after every owned child is waited.
        for signum in handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if tree is not None:
                tree.cleanup(grace)
        finally:
            for signum, handler in handlers.items():
                signal.signal(signum, handler)
            if previous_subreaper is not None:
                _subreaper(bool(previous_subreaper))

