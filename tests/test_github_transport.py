import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from qatlib_env import github_transport as transport, workspace


PUBLIC = "https://github.com/leanprover-community/mathlib4.git"
PREFIX = "https://mirror.example/"
REV = "12" * 20
CREDENTIALS = "user:" + "fake-secret@"


@pytest.mark.parametrize("env,expected", [
    ({}, ""),
    ({"QATLAS_GITHUB_PREFIX": PREFIX}, PREFIX),
    ({"QATLAS_GITHUB_PREFIX": PREFIX, "QATLIB_GITHUB_PREFIX": ""}, ""),
    ({"QATLAS_GITHUB_PREFIX": "invalid", "QATLIB_GITHUB_PREFIX": PREFIX}, PREFIX),
])
def test_prefix_precedence(env, expected):
    assert transport.prefix(env) == expected


@pytest.mark.parametrize("url", [
    "http://mirror.example/", "https://mirror.example", "https:///x/",
    "https://" + CREDENTIALS + "mirror.example/", "https://mirror.example/?a=/",
    "https://mirror.example/#x/", "https://mirror.example/ /", "https://mirror.example:bad/",
])
def test_invalid_prefix_is_redacted(url):
    with pytest.raises(ValueError, match="requires an HTTPS prefix") as error:
        transport.prefix({transport.KEY: url})
    assert url not in str(error.value)


def test_cli_overrides_and_mutex(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(transport.KEY, "invalid")
    prepared = Mock(return_value={})
    monkeypatch.setattr("qatlib_env.prepare.prepare", prepared)
    (tmp_path / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n", encoding="utf-8")
    (tmp_path / "lake-manifest.json").write_text('{"packages":[]}', encoding="utf-8")
    monkeypatch.setenv("LAKE_CACHE_DIR", str(tmp_path / "cache"))
    for options, expected in [(["--no-gh-proxy"], ""), (["--gh-proxy", PREFIX], PREFIX)]:
        assert workspace.main(["--project", str(tmp_path), "prepare", *options]) == 0
        assert prepared.call_args.args[1][transport.KEY] == expected
    with pytest.raises(SystemExit):
        workspace.main(["--gh-proxy", PREFIX, "prepare", "--no-gh-proxy"])
    with pytest.raises(SystemExit):
        workspace.main(["prepare", "--gh-proxy", "https://" + CREDENTIALS + "mirror.example/"])
    assert "fake-secret" not in capsys.readouterr().err


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_exact_public_remapping_and_existing_config(tmp_path, monkeypatch, platform):
    candidates = {
        "mathlib": PUBLIC,
        "prefixCollision": PUBLIC + "/different",
        "credentialed": "https://" + CREDENTIALS + "github.com/leanprover-community/mathlib4.git",
        "unknown": "https://github.com/example/private.git",
        "gitea": "https://gitea.example/owner/project.git",
    }
    lock = tmp_path / "lake-manifest.json"
    lock.write_text(json.dumps({"packages": [
        {"type": "git", "name": name, "url": url, "rev": REV}
        for name, url in candidates.items()]}), encoding="utf-8")
    before = lock.read_bytes()
    monkeypatch.setattr(workspace.sys, "platform", platform)
    monkeypatch.setattr(workspace, "ensure_pkg_mirror", Mock())
    monkeypatch.setattr(workspace, "git_head", Mock(return_value=REV))
    mock_run = Mock()
    monkeypatch.setattr(workspace, "run", mock_run)
    cached = Mock(return_value=tmp_path / "local-mirror")
    monkeypatch.setattr(transport, "cached_repo", cached)
    env = {
        "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": "Authorization: fake-secret",
        "GITEA_HOST": "gitea.example", "GITEA_TOKEN": "fixture",
    }
    assert "LAKE_PKG_URL_MAP" not in workspace.command_environment(tmp_path, env, ["lake", "build"])
    workspace.resolve_dependencies(tmp_path, env)
    child = mock_run.call_args.kwargs["env"]
    assert child["GIT_CONFIG_VALUE_0"] == "Authorization: fake-secret"
    assert child["GIT_CONFIG_KEY_1"] == "http.https://gitea.example/.extraheader"
    assert child["GIT_CONFIG_COUNT"] == ("3" if platform == "win32" else "2")
    if platform == "win32":
        assert child["GIT_CONFIG_KEY_2"] == "core.autocrlf"
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert not cached.called

    workspace.git_head.return_value = None
    mirrored = {**env, transport.KEY: PREFIX}
    for argv in (["lake", "build"], ["lake", "lint"], ["lake", "env", "lean", "--server"]):
        child = workspace.command_environment(tmp_path, mirrored, argv)
        assert json.loads(child["LAKE_PKG_URL_MAP"]) == {"mathlib": str(tmp_path / "local-mirror")}
        assert child["GIT_CONFIG_COUNT"] == "1"
        assert cached.call_args.args[1:4] == (PUBLIC, REV, PREFIX)
    count = cached.call_count
    assert "LAKE_PKG_URL_MAP" not in workspace.command_environment(tmp_path, mirrored, ["lake", "update"])
    assert cached.call_count == count
    assert lock.read_bytes() == before


def test_anonymous_environment_drops_authentication_and_routing(tmp_path):
    inputs = dict.fromkeys(("GH_TOKEN", "GITHUB_TOKEN", "GITEA_TOKEN", "QATLIB_R2_KEY",
                            "GIT_CONFIG_PARAMETERS", "GIT_DIR", "GIT_CONFIG_VALUE_31", "GIT_TRACE_CURL",
                            "HTTPS_PROXY", "http_proxy", "SSH_AUTH_SOCK", "GIT_ASKPASS"), "fake-secret")
    inputs.update(HOME="fake-secret", USERPROFILE="fake-secret", GIT_CONFIG_COUNT="32", PATH=os.defpath)
    child = transport.anonymous_environment(inputs, tmp_path)
    assert "fake-secret" not in child.values()
    assert child["PATH"] == os.defpath
    assert child["GIT_CONFIG_COUNT"] == "2"
    assert child["GIT_CONFIG_NOSYSTEM"] == "1"
    assert Path(child["GIT_CONFIG_GLOBAL"]).parent == tmp_path
    assert child["GIT_ALLOW_PROTOCOL"] == "https"
    assert child["GIT_CONFIG_VALUE_0"] == "false"


@pytest.mark.parametrize("status", ["403", "404"])
def test_http_errors_include_stage_and_safe_host(tmp_path, monkeypatch, status):
    monkeypatch.setattr(workspace, "remove_derived", Mock())
    def git(home, env, *args):
        if "fetch" in args:
            assert PREFIX + PUBLIC in args
            return subprocess.CompletedProcess(args, 1, "", f"returned error: {status} fake-secret")
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(transport, "anonymous_git", git)
    with pytest.raises(ValueError, match=f"Lean Git mirror fetch from mirror.example failed \\(HTTP {status}\\)") as error:
        transport.cached_repo(tmp_path, PUBLIC, REV, PREFIX, {})
    assert "fake-secret" not in str(error.value)


def test_verified_repo_reuse_and_sha_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "remove_derived", Mock())
    git = Mock()
    monkeypatch.setattr(transport, "anonymous_git", git)
    def stub(home, env, *args):
        if args[0] == "init":
            Path(args[-1]).mkdir()
        return subprocess.CompletedProcess(args, 0, REV if "rev-parse" in args else "", "")
    git.side_effect = stub
    repo = transport.cached_repo(tmp_path, PUBLIC, REV, PREFIX, {})
    assert repo.is_dir()
    git.reset_mock()
    assert transport.cached_repo(tmp_path, PUBLIC, REV, PREFIX, {}) == repo
    assert git.call_count == 1
    git.side_effect = None
    git.return_value = subprocess.CompletedProcess([], 0, "ab" * 20, "")
    with pytest.raises(ValueError, match="conflicts with the locked SHA"):
        transport.cached_repo(tmp_path, PUBLIC, REV, PREFIX, {})
    with pytest.raises(ValueError, match="did not return the locked SHA"):
        transport.cached_repo(tmp_path, PUBLIC, "ab" * 19 + "cc", PREFIX, {})
