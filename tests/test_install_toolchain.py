import hashlib
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock
import zipfile

import pytest

from qatlib_env import toolchain


LOCK = "leanprover/lean4:v4.35.0"


@pytest.fixture
def installation(tmp_path, monkeypatch):
    (tmp_path / "lean-toolchain").write_text(LOCK + "\r\n", encoding="utf-8")
    env = {"ELAN_HOME": str(tmp_path / "elan"), "ELAN_DIST_SERVER": "https://example.invalid/"}
    slot = tmp_path / "elan/toolchains/leanprover--lean4---v4.35.0"
    suffix = ".exe" if os.name == "nt" else ""
    monkeypatch.setattr(toolchain.shutil, "which", lambda *_a, **_kw: "elan")
    monkeypatch.setattr("qatlib_env.workspace.remove_derived", Mock())

    def runner(args, **kwargs):
        assert "ELAN_DIST_SERVER" not in kwargs["env"]
        assert kwargs["env"]["ELAN_TOOLCHAIN"] == LOCK
        if args[1:3] == ["which", "lean"]:
            out = str(slot / "bin" / ("lean" + suffix))
        elif args[1] == "--print-prefix":
            out = str(Path(args[0]).parent.parent)
        elif args[1] == "--version":
            out = "Lean (version 4.35.0, release)" if Path(args[0]).stem == "lean" else "Lake (Lean version 4.35.0)"
        else:
            out = ""
        return subprocess.CompletedProcess(args, 0, out, "")

    mock = Mock(side_effect=runner)
    monkeypatch.setattr(toolchain.supervisor, "run", mock)
    return tmp_path, env, slot, suffix, mock


def test_default_uses_official_elan_and_checks_versions(installation):
    root, env, slot, suffix, run = installation
    (slot / "bin").mkdir(parents=True)
    for name in ("lean", "lake"):
        (slot / "bin" / (name + suffix)).touch()
    result = toolchain.install(root, env, archive=None, digest=None)
    assert run.call_args_list[0].args[0] == ["elan", "toolchain", "install", LOCK]
    assert result["source"] == "elan"
    assert result["sysroot"] == str(slot)


def test_verified_archive_is_checked_before_installation(installation):
    root, env, slot, suffix, run = installation
    archive = root / "lean.zip"
    with zipfile.ZipFile(archive, "w") as file:
        for name in ("lean", "lake"):
            file.writestr("lean-4.35.0/bin/" + name + suffix, "binary fixture")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        toolchain.install(root, env, archive=archive, digest="0" * 64)
    assert not slot.exists() and not run.called
    result = toolchain.install(root, env, archive=archive, digest=digest)
    assert result["source"] == "verified-archive"
    assert result["sysroot"] == str(slot)
    assert result["archive_sha256"] == digest
    assert toolchain.install(root, env, archive=archive, digest=digest)["source"] == "existing"
    assert not any("install" in call.args[0] for call in run.call_args_list)


def test_truncated_zip_and_invalid_release(installation):
    root, env, slot, _, run = installation
    archive = root / "lean.zip"
    archive.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValueError, match="invalid or truncated"):
        toolchain.install(root, env, archive=archive, digest=hashlib.sha256(archive.read_bytes()).hexdigest())
    assert not slot.exists() and not run.called
    (root / "lean-toolchain").write_text("leanprover/lean4:nightly", encoding="utf-8")
    with pytest.raises(ValueError, match="explicit Lean 4 release"):
        toolchain.install(root, env, archive=None, digest=None)


@pytest.mark.parametrize("status", [403, 404])
def test_official_error_reports_phase_and_status(installation, status):
    root, env, _, _, run = installation
    run.side_effect = None
    run.return_value = subprocess.CompletedProcess([], 1, "", f"HTTP {status} secret-url")
    with pytest.raises(ValueError, match=f"official Elan source failed \\(HTTP {status}\\)") as error:
        toolchain.install(root, env, archive=None, digest=None)
    assert "secret-url" not in str(error.value)
