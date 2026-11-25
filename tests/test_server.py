from __future__ import annotations

import json
import shlex
import socket
import sys
import time
from pathlib import Path

import pytest

from qatlib_env import server, workspace
from qatlib_env.locations import pool_paths
from qatlib_env.pool import Pool

FAKE_SERVER = r'''
import json, pathlib, sys, time
def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode().partition(":")
        headers[name.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
def frame(value):
    body = json.dumps(value).encode()
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body
def send(*messages):
    sys.stdout.buffer.write(b"".join(map(frame, messages)))
    sys.stdout.buffer.flush()
shutdown = False
while True:
    message = read()
    if message is None:
        break
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc":"2.0", "id":message["id"], "result":{}})
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        params = message["params"]
        document = params["textDocument"]
        text = document.get("text") or params.get("contentChanges", [{}])[0].get("text", "")
    elif method == "textDocument/waitForDiagnostics":
        uri, version = document["uri"], document["version"]
        assert message["params"] == {"uri": uri, "version": version}
        def diagnostics(uri, version, items, incremental=False):
            return {"jsonrpc":"2.0", "method":"textDocument/publishDiagnostics",
                    "params":{"uri":uri, "version":version, "diagnostics":items,
                              "isIncremental":incremental}}
        warning = {"message":"warning", "severity":2}
        error = {"message":"synthetic error", "severity":1}
        send({"jsonrpc":"2.0", "id":message["id"], "method":"workspace/configuration",
              "params":{"items":[{}]}})
        assert read() == {"jsonrpc":"2.0", "id":message["id"], "result":[None]}
        messages = [
            {"jsonrpc":"2.0", "method":"$/lean/fileProgress", "params":{}},
            diagnostics(uri, version - 1, [error]),
            diagnostics(uri + ".other", version, [error]),
            diagnostics(uri, version, []),
        ]
        if "ERROR" in text:
            messages.extend([diagnostics(uri, version, [warning]),
                             diagnostics(uri, version, [error], incremental=True)])
        messages.append({"jsonrpc":"2.0", "id":message["id"], "result":{}})
        if "STALL" in text:
            sys.stdout.buffer.write(frame(messages[-1])[:-1])
            sys.stdout.buffer.flush()
            time.sleep(60)
        if "MANIFEST" in text:
            pathlib.Path("lake-manifest.json").write_text("{}")
        send(*messages)
    elif method == "shutdown":
        shutdown = True
        send({"jsonrpc":"2.0", "id":message["id"], "result":None})
    elif method == "exit":
        assert shutdown and "params" not in message
        pathlib.Path(__file__).with_suffix(".stopped").write_text("shutdown then exit")
        break
'''


def fake_command(tmp_path: Path) -> str:
    script = tmp_path / "fake_lean.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    return shlex.join([sys.executable, str(script)])


@pytest.fixture
def daemon_env(tmp_path, monkeypatch):
    env = {"QATLIB_HOME": str(tmp_path / "home"), "XDG_STATE_HOME": str(tmp_path / "state"),
           "QATLIB_LEAN_SERVER_COMMAND": fake_command(tmp_path), "QATLIB_POOL_BUDGET_MIB": "4096",
           "QATLIB_MEMORY_BUDGET": "2GiB", "QATLIB_POOL_CPU": "1", "QATLIB_JOBS": "1",
           "QATLIB_LEAN_THREADS": "1", "QATLIB_USE_SYSTEMD_SCOPE": "0", "QATLIB_POOL_WAIT_TIMEOUT": "0"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def test_server_key_includes_toolchain_lock_and_cache_identity(tmp_path):
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n", encoding="utf-8")
    first = server.server_key(tmp_path, {"LAKE_CACHE_DIR": "a", "LAKE_PKG_URL_MAP": "b"})
    second = server.server_key(tmp_path, {"LAKE_CACHE_DIR": "c", "LAKE_PKG_URL_MAP": "b"})
    assert first.digest != second.digest
    assert first.lean_toolchain == "leanprover/lean4:v4.35.0"


def test_daemon_reuses_session_and_preserves_stdout_contract(tmp_path, daemon_env, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n", encoding="utf-8")
    (project / "lakefile.toml").write_text("name = 'Mock'\n", encoding="utf-8")
    target = project / "Main.lean"
    target.write_text("def good := 1\n", encoding="utf-8")
    env = daemon_env
    first = server.check_file(target, project=project, env=env)
    assert first["ok"] is True and first["diagnostics"] == []
    target.write_text("ERROR\n" + "-- " + "x" * 300_000, encoding="utf-8")
    second = server.check_file(target, project=project, env=env)
    assert second["ok"] is False and [item["severity"] for item in second["diagnostics"]] == [2, 1]
    assert second["version"] == 2 and "restarted" not in second
    target.write_text("def good := 2\n", encoding="utf-8")
    third = server.check_file(target, project=project, env=env)
    assert third["ok"] is True and third["diagnostics"] == []
    state = server.status(project=project, env=env)
    assert len(state["servers"]) == 1
    socket_path = Path(state["servers"][0]["socket"])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(socket_path))
        connection.sendall(b'{"action":')
        connection.shutdown(socket.SHUT_WR)
        assert json.loads(connection.recv(4096))["ok"] is False
    assert server.stop(project=project, env=env)["stopped"] is True
    assert (tmp_path / "fake_lean.stopped").read_text() == "shutdown then exit"
    assert not socket_path.exists()
    assert not server._record_path(project, env).exists()
    assert capsys.readouterr().out == ""


def test_replaces_stale_socket_and_recovers_after_partial_lsp_reply(tmp_path, daemon_env):
    env = daemon_env
    target = tmp_path / "Main.lean"
    target.write_text("STALL\n", encoding="utf-8")
    key = server.server_key(tmp_path, env)
    record = server._state_dir(env) / f"{key.digest}.json"
    socket_path = Path("/tmp") / f"qatlib-server-{key.digest[:12]}-{tmp_path.parent.name[-12:]}.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(socket_path))
    record.write_text(json.dumps({"socket": str(socket_path)}))
    try:
        response = server.check_file(target, project=tmp_path, env=env, timeout=1)
        assert response["restarted"] is True and "timed out" in response["error"]
        assert not socket_path.exists()
        target.write_text("def good := 1\n", encoding="utf-8")
        assert server.check_file(target, project=tmp_path, env=env)["ok"] is True
    finally:
        server.stop(project=tmp_path, env=env)


def test_cli_emits_result_on_stdout_and_phase_events_on_stderr(tmp_path, daemon_env, capsys):
    from qatlib_env.__main__ import main

    project = tmp_path / "project"
    project.mkdir()
    (project / "lean-toolchain").write_text("toolchain\n", encoding="utf-8")
    target = project / "Main.lean"
    target.write_text("def good := 1\n", encoding="utf-8")
    assert main(["--project", str(project), "server-check", "--timeout", "3", str(target)]) == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["ok"] is True
    assert "QATLIB_PHASE" in captured.err
    server.stop(project=project, env=daemon_env)


def test_check_server_reuses_session_and_reports_failed_diagnostics(tmp_path, daemon_env, capsys):
    from qatlib_env.__main__ import main

    (tmp_path / "lean-toolchain").write_text("toolchain\n", encoding="utf-8")
    target = tmp_path / "Main.lean"
    target.write_text("def good := 1\n", encoding="utf-8")
    args = ["check", "--project", str(tmp_path), "--server", str(target), "--no-gh-proxy"]
    try:
        assert main(args) == 0
        first = json.loads(capsys.readouterr().out)
        assert first["ok"] and first["version"] == 1
        target.write_text("ERROR\n", encoding="utf-8")
        assert main(args) == 1
        second = json.loads(capsys.readouterr().out)
        assert not second["ok"] and second["version"] == 2
        assert second["server_key"] == first["server_key"]
    finally:
        server.stop(project=tmp_path, env=daemon_env)


@pytest.mark.parametrize("extra", [["Mock.Module"], ["--full-gate"], ["--jobs", "2"], ["--offline"]])
def test_check_server_rejects_build_arguments(extra, capsys):
    with pytest.raises(SystemExit) as exc:
        workspace.main(["check", "--server", "Main.lean", *extra])
    assert exc.value.code == 2
    assert "check --server accepts one file" in capsys.readouterr().err


def test_daemon_owns_shared_pool_lease_and_rechecks_changed_budget(tmp_path, daemon_env):
    target = tmp_path / "Main.lean"
    target.write_text("def good := 1\n", encoding="utf-8")
    env = daemon_env
    pool = Pool(state_dir=pool_paths(env)[2], budget_mib=4096, budget_cpu=1)
    try:
        effective, _ = server._server_environment(tmp_path, env)
        with workspace._pool_scope(effective, building=True):
            denied = server.check_file(target, project=tmp_path, env=env)
            assert not denied["ok"] and "pool admission timed out" in denied["error"]
        assert server.check_file(target, project=tmp_path, env=env)["ok"]
        running = server.status(project=tmp_path, env=env)["servers"][0]
        lease, = pool.status()["leases"]
        assert (lease["pid"], lease["memory_mib"], lease["cpu"]) == (running["pid"], 2048, 1)
        with pytest.raises(TimeoutError, match="pool admission timed out"), workspace._pool_scope(effective, building=True):
            pytest.fail("managed run must account for the idle server")
        assert server.check_file(target, project=tmp_path, env=env)["version"] == 2
        smaller = {**env, "QATLIB_POOL_BUDGET_MIB": "1"}
        for _ in range(2):
            denied = server.check_file(target, project=tmp_path, env=smaller)
            assert not denied["ok"] and "pool=1MiB" in denied["error"]
        assert not Path(running["socket"]).exists()
        assert not pool.status()["leases"]
    finally:
        server.stop(project=tmp_path, env=env)


def test_manifest_generation_replaces_daemon_and_idle_timeout_releases_lease(tmp_path, daemon_env):
    target = tmp_path / "Main.lean"
    target.write_text("-- MANIFEST\n", encoding="utf-8")
    env = daemon_env
    pool = Pool(state_dir=pool_paths(env)[2], budget_mib=4096, budget_cpu=1)
    try:
        first = server.check_file(target, project=tmp_path, env=env, idle_ttl=1)
        assert first["ok"] and (tmp_path / "lake-manifest.json").exists()
        old = server.status(project=tmp_path, env=env)["servers"][0]
        second = server.check_file(target, project=tmp_path, env=env, idle_ttl=1)
        new = server.status(project=tmp_path, env=env)["servers"][0]
        assert second["ok"] and second["server_key"] != first["server_key"]
        assert new["pid"] != old["pid"] and not Path(old["socket"]).exists()
        assert (tmp_path / "fake_lean.stopped").read_text() == "shutdown then exit"
        lease, = pool.status()["leases"]
        assert lease["pid"] == new["pid"]
        deadline = time.monotonic() + 5
        while Path(new["socket"]).exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not Path(new["socket"]).exists()
        assert not server.status(project=tmp_path, env=env)["servers"]
        assert not pool.status()["leases"]
    finally:
        server.stop(project=tmp_path, env=env)
