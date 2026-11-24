"""A small persistent Lean LSP server for fast single-file diagnostics.

The command line client owns no compiler state.  A per-worktree daemon owns one
``lean --server`` process and speaks a deliberately small JSON protocol over a
private Unix socket.  stdout from the public command is one JSON document and
diagnostic telemetry is written to stderr.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import psutil

from . import resources, workspace
from .envvars import env_value
from .locations import pool_paths, project_key, qatlib_home

DEFAULT_IDLE_TTL = 300.0
DEFAULT_TIMEOUT = 30.0
MAX_REQUEST_BYTES = 16 * 1024 * 1024
DAEMON_PROTOCOL = 3
POOL_OPTIONS = ("QATLIB_POOL_BUDGET_MIB", "QATLIB_POOL_CPU", "QATLIB_MEMORY_BUDGET",
                "QATLIB_JOBS", "QATLIB_LEAN_THREADS", "QATLIB_POOL_WAIT_TIMEOUT",
                "QATLIB_USE_SYSTEMD_SCOPE")


class ServerError(RuntimeError):
    """A server could not be started or did not complete a request."""


LEAN_SESSION_ERRORS = (ServerError, OSError, subprocess.SubprocessError, ValueError)


@dataclass(frozen=True)
class ServerKey:
    lean_toolchain: str
    lock_digest: str
    lake_cache_dir: str
    lake_pkg_url_map: str

    @property
    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()[:32]


def _phase(event: str, **fields: Any) -> None:
    record = {"event": event, **fields}
    print("QATLIB_PHASE " + json.dumps(record, sort_keys=True), file=sys.stderr, flush=True)


def _file_digest(project: Path) -> str:
    digest = hashlib.sha256()
    names = ["lean-toolchain", "lake-manifest.json", "lakefile.toml", "lakefile.lean"]
    for name in names:
        path = project / name
        digest.update(name.encode())
        try:
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        except FileNotFoundError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def server_key(project: Path, env: dict[str, str] | None = None) -> ServerKey:
    env = dict(os.environ if env is None else env)
    toolchain_path = project / "lean-toolchain"
    try:
        toolchain = toolchain_path.read_text(encoding="utf-8").strip()
    except OSError:
        toolchain = env.get("LEAN_TOOLCHAIN", "unknown")
    return ServerKey(
        lean_toolchain=env.get("ELAN_TOOLCHAIN") or toolchain,
        lock_digest=_file_digest(project),
        lake_cache_dir=env.get("LAKE_CACHE_DIR", ""),
        lake_pkg_url_map=env.get("LAKE_PKG_URL_MAP", ""),
    )


def _state_dir(env: dict[str, str]) -> Path:
    path = qatlib_home(env) / "state" / "server"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record_path(project: Path, env: dict[str, str]) -> Path:
    return _state_dir(env) / f"worktree-{project_key(project)}.json"


@contextmanager
def _locked_record(path: Path):
    if os.name != "posix":
        raise ServerError("Lean server requires Unix sockets and POSIX process supervision")
    import fcntl

    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def _server_environment(project: Path, env: dict[str, str]) -> tuple[dict[str, str], bool]:
    threads = env.get("QATLIB_SERVER_THREADS")
    config = resources.resolve_config(project, cli={"lean_threads": threads}, env=env)
    effective = config.environment(env)
    if config.sources["jobs"] == "automatic":
        effective["QATLIB_JOBS"] = "1"
    if config.sources["lean_threads"] == "automatic":
        effective.update(QATLIB_LEAN_THREADS="1", LEAN_NUM_THREADS="1")
    if config.sources["memory_budget"] == "automatic":
        # Reserve enough for a single file without holding the whole build
        # budget while the worker is idle.
        memory_gib = 4 if config.values["memory_budget"] >= 4 * resources.GIB else 2
        effective["QATLIB_MEMORY_BUDGET"] = str(memory_gib * resources.GIB)
    return effective, config.configuration.sources["local_pool_mib"] == "automatic"


def _policy_digest(env: dict[str, str], timeout: float, idle_ttl: float, *, automatic_pool: bool) -> str:
    # Automatic headroom fluctuates. The active lease already pins the pool's
    # capacity; an explicit change still requires readmission.
    settings = [env_value(env, name, None) for name in POOL_OPTIONS
                if not (automatic_pool and name == "QATLIB_POOL_BUDGET_MIB")]
    settings.extend((str(pool_paths(env)[2].resolve()), timeout, idle_ttl,
                     env.get("QATLIB_LEAN_SERVER_COMMAND"), env.get("PATH"),
                     workspace.github_transport.prefix(env)))
    return hashlib.sha256(json.dumps(settings).encode()).hexdigest()


def _pool_wait(env: dict[str, str]) -> float:
    wait = float(env_value(env, "QATLIB_POOL_WAIT_TIMEOUT", "300"))
    if not math.isfinite(wait) or wait < 0:
        raise ServerError("pool wait timeout must be finite and nonnegative")
    return wait


def _uri(path: Path) -> str:
    return "file://" + quote(str(path.resolve()).replace(os.sep, "/"), safe="/:@")


def _json_message(stream, value: dict[str, Any], deadline: float) -> None:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    frame = memoryview(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    while frame:
        _, ready, _ = select.select([], [stream], [], _remaining(deadline))
        if not ready:
            raise TimeoutError("timed out writing to Lean LSP")
        frame = frame[os.write(stream.fileno(), frame):]


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("timed out communicating with Lean LSP")
    return remaining


def _lean_command(project: Path, env: dict[str, str]) -> list[str]:
    override = env.get("QATLIB_LEAN_SERVER_COMMAND", "").strip()
    if override:
        return shlex.split(override)
    path = env.get("PATH", os.defpath)
    lake = shutil.which("lake", path=path)
    if lake and ((project / "lakefile.toml").is_file() or (project / "lakefile.lean").is_file()):
        command = [lake, "env", "lean", "--server"]
    else:
        lean = shutil.which("lean", path=path)
        if not lean:
            elan = shutil.which("elan", path=path)
            if elan:
                result = subprocess.run([elan, "which", "lean"], cwd=project, env=env,
                                        capture_output=True, text=True, check=False, timeout=DEFAULT_TIMEOUT)
                lean = result.stdout.strip() if result.returncode == 0 else None
        if not lean:
            raise ServerError("could not locate Lean or Lake")
        command = [lean, "--server"]
    threads = env_value(env, "QATLIB_LEAN_THREADS", env.get("LEAN_NUM_THREADS", "1"))
    return [*command, f"--threads={threads}"]


def _diagnostic(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        "message": str(value.get("message", "")),
        "severity": value.get("severity"),
        "source": value.get("source"),
        "code": value.get("code"),
        "range": value.get("range"),
    }
    return {key: val for key, val in result.items() if val is not None}


class LeanSession:
    """One supervised Lean LSP process and its open-document versions."""

    def __init__(self, project: Path, env: dict[str, str], timeout: float):
        self.project = project.resolve()
        self.env = env
        self.timeout = timeout
        self.process: subprocess.Popen[bytes] | None = None
        self.documents: dict[str, int] = {}
        self.request_id = 0
        self.buffer = bytearray()

    def _start(self, deadline: float) -> None:
        lean_command = _lean_command(self.project, self.env)
        command = workspace._backend_command(lean_command, self.env, building=True)
        child_env = dict(self.env)
        if workspace._is_lake(lean_command):
            child_env = workspace.command_environment(self.project, child_env, lean_command)
        child_env.setdefault("LEAN_NUM_THREADS", "1")
        self.process = subprocess.Popen(command, cwd=self.project, env=child_env,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        os.set_blocking(self.process.stdin.fileno(), False)
        self.documents.clear()
        self.buffer.clear()
        self.request_id = 0
        self._request("initialize", {"processId": os.getpid(), "rootUri": _uri(self.project),
                                      "capabilities": {}, "workspaceFolders": None}, deadline)
        self._notify("initialized", {}, deadline)

    def _send(self, value: dict[str, Any], deadline: float) -> None:
        if not self.process or not self.process.stdin:
            raise ServerError("Lean LSP stdin is unavailable")
        _json_message(self.process.stdin, value, deadline)

    def _notify(self, method: str, params: dict[str, Any] | None, deadline: float) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message, deadline)

    def _read_message(self, deadline: float) -> dict[str, Any]:
        stream = self.process.stdout

        def read_more() -> None:
            ready, _, _ = select.select([stream], [], [], _remaining(deadline))
            if not ready:
                raise TimeoutError("timed out waiting for Lean LSP response")
            data = os.read(stream.fileno(), 65536)
            if not data:
                raise ServerError("Lean LSP server closed stdout")
            self.buffer.extend(data)

        _remaining(deadline)
        while (header_end := self.buffer.find(b"\r\n\r\n")) == -1:
            read_more()
        headers = dict(line.split(b":", 1) for line in bytes(self.buffer[:header_end]).lower().split(b"\r\n")
                       if b":" in line)
        try:
            length = int(headers[b"content-length"])
            if length < 0:
                raise ValueError("negative length")
        except (KeyError, ValueError) as error:
            raise ServerError("invalid Lean LSP Content-Length") from error
        body_start = header_end + 4
        frame_end = body_start + length
        while len(self.buffer) < frame_end:
            read_more()
        try:
            message = json.loads(self.buffer[body_start:frame_end])
        except (ValueError, UnicodeError) as error:
            raise ServerError("invalid Lean LSP JSON") from error
        del self.buffer[:frame_end]
        if not isinstance(message, dict):
            raise ServerError("expected a Lean LSP message object")
        return message

    def _request(self, method: str, params: dict[str, Any], deadline: float, on_notification=None) -> None:
        self.request_id += 1
        identifier = self.request_id
        self._send({"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}, deadline)
        while True:
            message = self._read_message(deadline)
            if "method" in message:
                if "id" in message:
                    response = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "workspace/configuration":
                        response["result"] = [None for _ in message["params"]["items"]]
                    elif message["method"] == "workspace/workspaceFolders":
                        response["result"] = None
                    else:
                        response["error"] = {"code": -32601, "message": "Method not found"}
                    self._send(response, deadline)
                elif on_notification is not None:
                    on_notification(message)
            elif message.get("id") == identifier:
                if "error" in message:
                    raise ServerError(str(message["error"]))
                return

    def check(self, path: Path, text: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout
        if self.process is None or self.process.poll() is not None:
            self.close()
            self._start(deadline)
        uri = _uri(path)
        version = self.documents.get(uri, 0) + 1
        diagnostics = []

        def collect(message):
            if message["method"] != "textDocument/publishDiagnostics":
                return
            params = message["params"]
            if params.get("uri") != uri or params.get("version") not in (None, version):
                return
            if not params.get("isIncremental", False):
                diagnostics.clear()
            diagnostics.extend(_diagnostic(item) for item in params["diagnostics"])

        if uri not in self.documents:
            self._notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "lean",
                                                                    "version": version, "text": text}}, deadline)
        else:
            self._notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version},
                                                      "contentChanges": [{"text": text}]}, deadline)
        self._request("textDocument/waitForDiagnostics", {"uri": uri, "version": version}, deadline, collect)
        self.documents[uri] = version
        errors = [item for item in diagnostics if item.get("severity") == 1]
        return {"ok": not errors, "file": str(path.resolve()), "diagnostics": diagnostics,
                "version": version}

    def close(self) -> None:
        process = self.process
        self.documents.clear()
        if not process:
            return
        try:
            deadline = time.monotonic() + 1.0
            self._request("shutdown", {}, deadline)
            self._notify("exit", None, deadline)
            process.stdin.close()
            process.wait(timeout=_remaining(deadline))
        except (OSError, ServerError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1.0)
        finally:
            self.process = None
            process.stdin.close()
            process.stdout.close()


class ServerDaemon:
    def __init__(self, socket_path: Path, project: Path, env: dict[str, str], idle_ttl: float, timeout: float):
        self.socket_path = socket_path
        self.project = project
        self.env = env
        self.idle_ttl = idle_ttl
        self.timeout = timeout
        self.session: LeanSession | None = None
        self.lease = ExitStack()
        self.started = time.time()
        self.last_used = time.monotonic()

    def _close_session(self) -> None:
        session, self.session = self.session, None
        try:
            if session:
                session.close()
        finally:
            self.lease.close()

    def _handle(self, request: dict[str, Any]) -> dict[str, Any]:
        action = request.get("action")
        if action in ("ping", "status"):
            response = {"ok": True, "pid": os.getpid(), "started": self.started,
                        "documents": len(self.session.documents) if self.session else 0}
            if action == "ping":
                response["idle_ttl"] = self.idle_ttl
            return response
        if action != "check":
            return {"ok": False, "error": "unknown server action"}
        file = Path(request["file"])
        content = request["text"]
        if not isinstance(content, str):
            raise TypeError("check text must be a string")
        if self.session is None:
            try:
                self.lease.enter_context(workspace._pool_scope(
                    self.env, building=True, on_lease_lost=lambda: os.kill(os.getpid(), signal.SIGTERM)))
            except (OSError, RuntimeError, ValueError) as error:
                return {"ok": False, "error": str(error)}
        for attempt in range(2):
            if self.session is None:
                self.session = LeanSession(self.project, self.env, self.timeout)
            try:
                response = self.session.check(file, content)
            except LEAN_SESSION_ERRORS as error:
                if attempt:
                    self._close_session()
                    return {"ok": False, "error": str(error), "restarted": True}
                self.session.close()
                self.session = None
            else:
                if attempt:
                    response["restarted"] = True
                return response

    def run(self) -> int:
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        bound = False
        try:
            umask = os.umask(0o177)
            try:
                listener.bind(str(self.socket_path))
                bound = True
            finally:
                os.umask(umask)
            listener.listen(8)
            listener.settimeout(0.25)
            while True:
                if time.monotonic() - self.last_used > max(1.0, self.idle_ttl):
                    return 0
                try:
                    connection, _ = listener.accept()
                except TimeoutError:
                    continue
                with connection:
                    connection.settimeout(self.timeout + 1)
                    action = None
                    try:
                        with connection.makefile("rb") as stream:
                            data = stream.read(MAX_REQUEST_BYTES + 1)
                        if not data:
                            continue
                        if len(data) > MAX_REQUEST_BYTES:
                            raise ValueError("server request exceeds 16 MiB")
                        request = json.loads(data)
                        if not isinstance(request, dict):
                            raise TypeError("expected a server request object")
                        action = request.get("action")
                        response = {"ok": True, "stopping": True} if action == "stop" else self._handle(request)
                    except OSError:
                        continue
                    except (KeyError, TypeError, ValueError) as error:
                        response = {"ok": False, "error": f"invalid server request: {error}"}
                    retire = action == "stop" or (action == "check" and (self.session is None or self.idle_ttl == 0))
                    if retire:
                        self._close_session()
                        listener.close()
                        _unlink_socket(self.socket_path)
                        bound = False
                    elif action == "check":
                        self.last_used = time.monotonic()
                    try:
                        connection.sendall(json.dumps(response, ensure_ascii=False).encode())
                    except OSError:
                        pass
                    if retire:
                        return 0
        finally:
            try:
                self._close_session()
            finally:
                listener.close()
                if bound:
                    _unlink_socket(self.socket_path)


def _request(socket_path: Path, value: dict[str, Any], timeout: float) -> dict[str, Any]:
    payload = json.dumps(value, ensure_ascii=False).encode()
    if len(payload) > MAX_REQUEST_BYTES:
        raise ServerError("server request exceeds 16 MiB")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        data = bytearray()
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
    response = json.loads(data)
    if not isinstance(response, dict):
        raise ServerError("expected a daemon response object")
    return response


@dataclass(frozen=True)
class ServerRecord:
    socket: Path
    protocol: int
    fingerprint: str
    policy: str


def _read_record(path: Path) -> ServerRecord | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        socket_path = Path(record["socket"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
    fingerprint = record.get("fingerprint", path.stem)
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{32}", fingerprint):
        return None
    if not socket_path.is_absolute() or not socket_path.match(f"qatlib-server-{fingerprint[:12]}-*.sock"):
        return None
    return ServerRecord(socket_path, record.get("protocol", 1), fingerprint, record.get("policy", ""))


def _unlink_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
        path.unlink(missing_ok=True)


def _forget(record_path: Path, socket_path: Path) -> None:
    _unlink_socket(socket_path)
    record_path.unlink(missing_ok=True)


def _stop_record(path: Path, record: ServerRecord) -> dict[str, Any]:
    try:
        response = _request(record.socket, {"action": "stop"}, 3.0)
    except (FileNotFoundError, ConnectionRefusedError):
        response = {"ok": False, "error": "server was not reachable"}
    else:
        if not response.get("ok"):
            raise ServerError("previous Lean daemon could not stop")
    _forget(path, record.socket)
    return response


def _stop_legacy_daemons(directory: Path, project: Path, key: ServerKey) -> bool:
    # Older versions used a fingerprint-only filename and did not record the
    # worktree. The live daemon's working directory identifies the checkout
    # even if Lake generated a manifest since the record was written.
    stopped = False
    for path in directory.glob("*.json"):
        if not re.fullmatch(r"[0-9a-f]{32}", path.stem):
            continue
        with _locked_record(path):
            record = _read_record(path)
            if not record:
                continue
            try:
                state = _request(record.socket, {"action": "status"}, 1.0)
                process = psutil.Process(state["pid"])
                daemon_project = Path(process.cwd()).resolve()
            except (ConnectionRefusedError, FileNotFoundError, psutil.NoSuchProcess):
                if path.stem == key.digest:
                    _forget(path, record.socket)
                continue
            except (OSError, ValueError, KeyError, psutil.AccessDenied):
                continue
            if daemon_project == project:
                stopped = bool(_stop_record(path, record).get("ok")) or stopped
    return stopped


def _daemon_for(record_path: Path, key: ServerKey, project: Path, env: dict[str, str],
                timeout: float, idle_ttl: float, policy: str) -> Path:
    record = _read_record(record_path)
    if record:
        if (record.protocol, record.fingerprint, record.policy) == (DAEMON_PROTOCOL, key.digest, policy):
            return record.socket
        _stop_record(record_path, record)
    else:
        _stop_legacy_daemons(record_path.parent, project, key)
    # AF_UNIX paths are limited to roughly 108 bytes on Linux.  The stable
    # record carries the identity, so the actual socket only needs a short
    # random name.
    socket_name = f"qatlib-server-{key.digest[:12]}-{uuid.uuid4().hex[:8]}.sock"
    socket_path = Path(tempfile.gettempdir()) / socket_name
    if len(os.fsencode(socket_path)) >= 104:
        socket_path = Path("/tmp") / socket_name
    command = [sys.executable, "-m", "qatlib_env.server", "--daemon", "--socket", str(socket_path),
               "--project", str(project), "--idle-ttl", str(idle_ttl), "--timeout", str(timeout)]
    child_env = dict(env)
    package_root = str(Path(__file__).resolve().parents[1])
    child_env["PYTHONPATH"] = os.pathsep.join(filter(None, [package_root, child_env.get("PYTHONPATH", "")]))
    record_path.write_text(json.dumps({"fingerprint": key.digest, "policy": policy, "socket": str(socket_path),
                                       "protocol": DAEMON_PROTOCOL}), encoding="utf-8")
    process = None
    try:
        process = subprocess.Popen(command, cwd=project, env=child_env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process.poll() is None:
            try:
                if _request(socket_path, {"action": "ping"}, min(0.2, _remaining(deadline))).get("ok"):
                    return socket_path
            except (OSError, ValueError):
                time.sleep(0.02)
        raise ServerError("Lean server daemon did not become ready")
    except Exception:
        if process and process.poll() is None:
            process.kill()
            process.wait()
        _forget(record_path, socket_path)
        raise


def check_file(file: Path, *, project: Path | None = None, env: dict[str, str] | None = None,
               timeout: float | None = None, idle_ttl: float | None = None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    file = file.resolve()
    project = (project or Path.cwd()).resolve()
    timeout = float(env.get("QATLIB_SERVER_TIMEOUT", DEFAULT_TIMEOUT) if timeout is None else timeout)
    idle_ttl = float(env.get("QATLIB_SERVER_IDLE_TTL", DEFAULT_IDLE_TTL) if idle_ttl is None else idle_ttl)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ServerError("server timeout must be finite and positive")
    if not math.isfinite(idle_ttl) or idle_ttl < 0:
        raise ServerError("server idle TTL must be finite and nonnegative")
    if not file.is_file():
        raise ServerError(f"Lean file does not exist: {file}")
    started = time.monotonic()
    request = {"action": "check", "file": str(file), "text": file.read_text(encoding="utf-8")}
    with _locked_record(_record_path(project, env)) as record_path:
        env, automatic_pool = _server_environment(project, env)
        key = server_key(project, env)
        policy = _policy_digest(env, timeout, idle_ttl, automatic_pool=automatic_pool)
        _phase("server.start", key=key.digest, file=str(file))
        request_timeout = _pool_wait(env) + 2 * timeout + 5
        socket_path = _daemon_for(record_path, key, project, env, timeout, idle_ttl, policy)
        try:
            response = _request(socket_path, request, request_timeout)
        except (FileNotFoundError, ConnectionRefusedError):
            _forget(record_path, socket_path)
            socket_path = _daemon_for(record_path, key, project, env, timeout, idle_ttl, policy)
            response = _request(socket_path, request, request_timeout)
    response["server_key"] = key.digest
    response["elapsed_s"] = round(time.monotonic() - started, 6)
    _phase("server.done", key=key.digest, ok=bool(response.get("ok")), elapsed_s=response["elapsed_s"])
    return response


def status(*, project: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    project = (project or Path.cwd()).resolve()
    key = server_key(project, env)
    servers = []
    with _locked_record(_record_path(project, env)) as record_path:
        record = _read_record(record_path)
        if record:
            try:
                current = _request(record.socket, {"action": "status"}, 1.0)
                current.update({"server_key": record.fingerprint, "socket": str(record.socket)})
                servers.append(current)
            except (FileNotFoundError, ConnectionRefusedError):
                _forget(record_path, record.socket)
    return {"servers": servers, "server_key": key.digest}


def stop(*, project: Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ if env is None else env)
    project = (project or Path.cwd()).resolve()
    key = server_key(project, env)
    with _locked_record(_record_path(project, env)) as record_path:
        record = _read_record(record_path)
        if not record:
            stopped = _stop_legacy_daemons(record_path.parent, project, key)
            return {"stopped": stopped, "server_key": key.digest}
        response = _stop_record(record_path, record)
    response.update({"stopped": bool(response.get("ok")), "server_key": record.fingerprint})
    return response


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="qatlib-env server")
    workspace.github_transport.add_options(parser)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--socket")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--idle-ttl", type=float)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("action", nargs="?", choices=("status", "stop", "check"))
    parser.add_argument("file", nargs="?")
    args = parser.parse_intermixed_args(argv)
    env = dict(os.environ)
    if args.gh_proxy is not None:
        env[workspace.github_transport.KEY] = args.gh_proxy
    if args.daemon:
        if not args.socket or not args.project or args.idle_ttl is None or args.timeout is None:
            parser.error("daemon requires --socket, --project, --idle-ttl and --timeout")
        def terminate(signum, _frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, terminate)
        return ServerDaemon(Path(args.socket), args.project, env, args.idle_ttl, args.timeout).run()
    project = args.project or Path.cwd()
    try:
        if args.action == "check":
            if not args.file:
                parser.error("server check requires a Lean file")
            result = check_file(Path(args.file), project=project, env=env, timeout=args.timeout, idle_ttl=args.idle_ttl)
        elif args.action == "status":
            result = status(project=project, env=env)
        elif args.action == "stop":
            result = stop(project=project, env=env)
        else:
            parser.error("choose check, status, or stop")
    except LEAN_SESSION_ERRORS as error:
        result = {"ok": False, "error": str(error)}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
