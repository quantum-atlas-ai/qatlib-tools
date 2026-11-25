import hashlib
import json
import subprocess

import pytest

from qatlib_env import r2_cache
from qatlib_env.__main__ import main
from tests._git_fixture import snapshot


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("QATLIB_HOME", str(tmp_path / "user-config"))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, content in {
        "lakefile.toml": 'name = "Example"\n',
        "lean-toolchain": "leanprover/lean4:v4.35.0\n",
        "lake-manifest.json": '{"packages": []}\n',
        "Example.lean": "import Example.Basic\n",
        "Example/Basic.lean": "def answer := 42\n",
        "qatlib-env.toml": 'cache_repository = "owner/example"\n',
        "lake-cache.toml": (
            'cache.defaultService = "read"\n'
            '[[cache.service]]\nname = "read"\n'
            'revisionEndpoint = "https://cache.example.com/revisions"\n'
        ),
    }.items():
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8")
    snapshot(tmp_path)
    return tmp_path


def test_ordered_committed_inputs_survive_commit_change(project, capsys):
    paths = ["Example", "Example.lean"]
    argv = ["--project", str(project), "cache", "inspect", "--json",
            "--path", paths[0], "--path", paths[1]]
    assert main(argv) == 0
    before = json.loads(capsys.readouterr().out)
    raw = subprocess.check_output(["git", "rev-parse", "HEAD:Example", "HEAD:Example.lean"], cwd=project)
    assert before["paths"] == paths
    assert before["input_tree"] == hashlib.sha256(raw).hexdigest()
    (project / "README.md").write_text("Different revision, same cache inputs.\n")
    snapshot(project)
    (project / "Example.lean").write_text("Uncommitted source edit.\n")
    assert main(argv) == 0
    after = json.loads(capsys.readouterr().out)
    assert after["head_sha"] != before["head_sha"]
    assert after["input_tree"] == before["input_tree"]
    assert not (project / ".lake").exists()


@pytest.mark.parametrize(("tree_status", "curl_code", "body", "state", "count", "exit_code"), [
    (200, 0, b'"lean4:v1"\n["0000000000000001","0000000000000002.ltar"]\n', "valid", 1, 0),
    (200, 0, b'"lean4:v1"\n', "invalid_manifest", None, 1),
    (403, 0, b"", "http_error", None, 1),
    (200, 92, b"", "transport_error", None, 1),
    (None, 28, b"", "transport_error", None, 1),
])
def test_probes_both_aliases(project, monkeypatch, capsys, tree_status, curl_code, body, state, count, exit_code):
    requested = []
    run = subprocess.run

    def curl(command, **kwargs):
        if command[0] != "curl":
            return run(command, **kwargs)
        assert command[:2] == ["curl", "-q"]
        requested.append(command[-1])
        if "/inputs/" in command[-1]:
            stdout = body + f"\n{tree_status or 0:03d}".encode()
            return subprocess.CompletedProcess(command, curl_code, stdout)
        return subprocess.CompletedProcess(command, 0, b"\n404")

    monkeypatch.setattr(r2_cache.subprocess, "run", curl)
    assert main(["--project", str(project), "cache", "inspect", "--remote", "--json"]) == exit_code
    result = json.loads(capsys.readouterr().out)
    assert result["paths"] == ["Example", "Example.lean", "lakefile.toml", "lean-toolchain", "lake-manifest.json"]
    prefix = "https://cache.example.com/revisions/owner/example/tc/leanprover--lean4---v4.35.0/"
    assert requested == [prefix + result["head_sha"] + ".jsonl",
                         prefix + "inputs/" + result["input_tree"] + ".jsonl"]
    assert result["manifests"]["revision"]["state"] == "miss"
    tree = result["manifests"]["input_tree"]
    assert (tree["http_status"], tree["state"], tree["entry_count"]) == (tree_status, state, count)
    assert not (project / ".lake").exists()
