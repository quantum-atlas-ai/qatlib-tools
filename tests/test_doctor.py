"""Doctor diagnostics through the public CLI, with local Git and Lake fixtures."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from qatlib_env import doctor
from qatlib_env.__main__ import main
from tests._git_fixture import snapshot

TOOLCHAIN = "leanprover/lean4:v4.35.0"
ELAN_NAME = "leanprover--lean4---v4.35.0"
BASE = "0000000000000001.olean"
SERVER = "0000000000000002.olean.server"
ARCHIVE = "0000000000000003.ltar"


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def mapping(cache, key, data):
    return write(cache / f"outputs/Example/{key:016x}.json", json.dumps({"data": data}))


def manifest(project, entries):
    write(project / "lake-manifest.json", json.dumps({"packagesDir": ".lake/packages", "packages": entries}))


def report(project, capsys, *, source=None, exit_code=0):
    options = ["--source-root", str(source)] if source else []
    assert main([*options, "doctor", "--project", str(project), "--json"]) == exit_code
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out)


def findings(report):
    return {entry["code"]: entry for entry in report["findings"]}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for name in ("ELAN_TOOLCHAIN", "QATLIB_REAL_SYSROOT", "QATLAS_REAL_SYSROOT", "LEAN_SYSROOT",
                 "QATLIB_REAL_LEAN", "QATLAS_REAL_LEAN", "QATLIB_SOURCE_ROOT", "QATLAS_SOURCE_ROOT",
                 "QATLIB_PKG_MIRROR", "QATLAS_PKG_MIRROR", "ELAN"):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "project"
    elan = tmp_path / "elan"
    cache = tmp_path / "cache"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ELAN_HOME", str(elan))
    monkeypatch.setenv("LAKE_CACHE_DIR", str(cache))
    write(project / "lean-toolchain", TOOLCHAIN)
    manifest(project, [])
    prefix = elan / "toolchains" / ELAN_NAME
    suffix = ".exe" if os.name == "nt" else ""
    for file in (f"bin/lean{suffix}", f"bin/lake{suffix}", "lib/lean/Init.olean"):
        write(prefix / file, "fixture")
    return project, prefix, cache


def test_doctor_cold_project_uses_elan_cache_without_creating_it(setup, monkeypatch, capsys):
    project, prefix, _ = setup
    monkeypatch.delenv("LAKE_CACHE_DIR")
    result = report(project, capsys)
    assert result["schema_version"] == 1
    assert result["toolchain"]["locked"] == TOOLCHAIN
    assert result["toolchain"]["resolved_prefix"] == str(prefix.resolve())
    assert result["toolchain"]["missing"] == []
    assert result["cache"]["path"] == str(prefix / "lake/cache")
    assert result["builds"][0]["restore"]["mode"] == "hardlink_eligible"
    assert set(findings(result)) == {"cache_artifacts_missing", "cache_output_mappings_missing"}
    assert result["status"] == "warning"
    assert not (prefix / "lake").exists()
    assert not (project / ".lake").exists()


def test_doctor_reports_bad_inputs_and_still_inspects_explicit_cache(setup, capsys):
    project, _, cache = setup
    write(project / "lean-toolchain", "stable")
    write(project / "lake-manifest.json", '{"packages": [null]}')
    result = report(project, capsys, exit_code=1)
    assert result["status"] == "error"
    assert result["cache"]["path"] == str(cache)
    assert {"toolchain_lock_unreadable", "manifest_unreadable"} <= findings(result).keys()


def test_doctor_reports_missing_toolchain_files_and_override(setup, monkeypatch, capsys):
    project, _, _ = setup
    monkeypatch.setenv("QATLIB_REAL_SYSROOT", str(project / "missing"))
    monkeypatch.setenv("ELAN_TOOLCHAIN", "other")
    result = report(project, capsys, exit_code=1)
    assert "lib/lean/Init.olean" in result["toolchain"]["missing"]
    assert {"toolchain_incomplete", "toolchain_override_mismatch"} <= findings(result).keys()


def test_doctor_distinguishes_pins_and_mirrors(setup, tmp_path, capsys):
    project, _, _ = setup
    mirror = tmp_path / "mirror"
    origin = mirror / ".lake/packages/FromMirror"
    origin.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "init", "-q", str(origin)], check=True)
    revision = snapshot(origin)
    write(origin / "readme", "new state")
    other = snapshot(origin)
    packages = project / ".lake/packages"
    packages.mkdir(parents=True)
    for name in ("Aligned", "FromMirror"):
        subprocess.run(["git", "clone", "-q", str(origin), str(packages / name)], check=True)
    subprocess.run(["git", "-C", str(packages / "Aligned"), "checkout", "-q", revision], check=True)
    unpinned = "a" * 40
    manifest(project, [
        {"name": "«Aligned»", "type": "git", "rev": revision},
        {"name": "FromMirror", "type": "git", "rev": revision},
        {"name": "Missing", "type": "git", "rev": unpinned},
        {"name": "Local", "type": "path", "dir": "local"},
        {"name": "Invalid", "type": "git", "rev": "main"},
    ])
    (project / "local").mkdir()
    result = report(project, capsys, source=mirror, exit_code=1)
    aligned, stale, missing, local, invalid = result["packages"]
    assert (aligned["status"], aligned["head"]) == ("aligned", revision)
    assert (stale["status"], stale["head"], stale["revision"]) == ("mismatch", other, revision)
    assert stale["mirror"] == str(origin)
    assert (missing["status"], missing["mirror"]) == ("missing", None)
    assert local["status"] == "present"
    assert invalid["status"] == "invalid_pin"
    assert {"package_mismatch", "package_missing", "local_mirror_pin_unavailable",
            "invalid_package_pin"} <= findings(result).keys()


def test_doctor_ignores_parent_git_head_for_empty_dependency_directory(setup, capsys):
    project, _, _ = setup
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    revision = snapshot(project)
    manifest(project, [{"name": "Empty", "rev": revision}])
    (project / ".lake/packages/Empty").mkdir(parents=True)
    package, = report(project, capsys)["packages"]
    assert package["head"] is None and package["status"] == "missing"


def test_doctor_detects_downloaded_artifacts_without_published_mappings(setup, capsys):
    project, _, cache = setup
    write(cache / "artifacts" / ARCHIVE, "archive")
    result = report(project, capsys)
    assert result["cache"]["artifact_files"] == 1
    assert result["cache"]["output_mappings"] == 0
    assert set(findings(result)) == {"unmapped_cache_artifacts"}


def test_doctor_reports_unregistered_revision_even_with_other_package_outputs(setup, capsys):
    project, _, cache = setup
    revision = "a" * 40
    manifest(project, [{"name": "Dependency", "rev": revision}])
    revision_map = write(cache / f"revisions/Dependency/{revision}.jsonl", '"2026-03-17"\n')
    write(cache / "artifacts" / ARCHIVE, "archive")
    mapping(cache, 1, ARCHIVE)
    result = report(project, capsys)
    package, = result["packages"]
    assert result["cache"]["output_scopes"] == {"Example": 1}
    assert package["cache"] == {"output_mappings": 0, "locked_revision_manifest": str(revision_map)}
    assert findings(result)["unregistered_package_revision"]["examples"] == [str(revision_map)]


def test_doctor_cache_distinguishes_missing_server_descriptor_from_missing_blob(setup, capsys):
    project, _, cache = setup
    write(cache / "artifacts" / BASE, "olean")
    write(cache / "artifacts" / ARCHIVE, "archive")
    partial = mapping(cache, 1, {"m": True, "o": [BASE]})
    absent_blob = mapping(cache, 2, {"m": True, "o": [BASE, SERVER]})
    mapping(cache, 3, {"m": False, "o": [BASE]})
    mapping(cache, 4, ARCHIVE)
    mapping(cache, 5, {"m": True, "o": []})
    mapping(cache, 6, {"m": False, "o": ["0000000000000004.olean"]})
    mapping(cache, 7, {"m": False, "o": ["../../escape"]})
    write(cache / "outputs/Example/0000000000000008.json", "{")
    result = report(project, capsys)
    issues = findings(result)
    assert issues["missing_server_olean_descriptor"]["message"].startswith(doctor.NO_SERVER_OLEAN)
    assert issues["missing_server_olean_descriptor"]["examples"][0] == str(partial)
    assert issues["missing_server_olean_descriptor"]["count"] == 2
    assert issues["missing_cache_server_olean"]["examples"] == [f"{absent_blob} -> {SERVER}"]
    for code in ("missing_olean_descriptor", "missing_cache_olean", "invalid_artifact_descriptor",
                 "invalid_output_mapping"):
        assert issues[code]["count"] == 1
    assert main(["--project", str(project), "doctor"]) == 0
    human = capsys.readouterr().out
    assert doctor.NO_SERVER_OLEAN in human
    assert "hardlink eligible with copy fallback" in human


def test_doctor_clean_cache_with_legacy_module_flag_inference(setup, capsys):
    project, _, cache = setup
    write(cache / "artifacts" / BASE, "olean")
    write(cache / "artifacts" / SERVER, "server")
    write(cache / "artifacts" / ARCHIVE, "archive")
    mapping(cache, 1, {"o": [BASE, SERVER]})
    mapping(cache, 2, {"ltar": ARCHIVE})
    write(cache / "outputs/Example/0000000000000003.json", json.dumps(ARCHIVE))
    result = report(project, capsys)
    assert result["status"] == "ok"
    assert result["findings"] == []


def test_doctor_detects_partial_local_modules_without_flagging_legacy_or_unbuilt_source(setup, capsys):
    project, _, cache = setup
    write(project / "lakefile.toml", 'name = "Example"\nsrcDir = "src"\n')
    build = project / ".lake/build/lib/lean"
    write(project / "src/Partial.lean", "/- nested /- comment -/ -/\nmodule\n")
    write(build / "Partial.olean", "olean")
    write(project / "src/Legacy.lean", "-- module\nimport Lean\n")
    write(build / "Legacy.olean", "olean")
    write(project / "src/Cold.lean", "module\n")
    write(build / "ServerOnly.olean.server", "server")
    write(build / "ServerOnly.olean.private", "private")
    write(build / "Empty.olean", "")
    write(cache / "artifacts" / BASE, "olean")
    mapping(cache, 1, {"o": [BASE]})
    result = report(project, capsys)
    issues = findings(result)
    assert issues["missing_local_server_olean"]["examples"] == [str(build / "Partial.olean.server")]
    assert issues["missing_local_server_olean"]["message"].startswith(doctor.NO_SERVER_OLEAN)
    assert issues["missing_local_olean"]["examples"] == [str(build / "Empty.olean"),
                                                       str(build / "ServerOnly.olean")]
    assert result["builds"][0]["olean_files"] == 2


def test_doctor_copy_fallback_uses_filesystems_not_path_prefixes(setup, monkeypatch, capsys):
    project, _, cache = setup
    build = project / ".lake/build/lib/lean"

    def volume(path):
        return (cache, 13) if path == cache / "artifacts" else (project, 27)

    monkeypatch.setattr(doctor, "_volume", volume)
    result = report(project, capsys)
    assert result["builds"][0]["restore"] == {
        "mode": "copy_fallback", "cache_volume_at": str(cache), "build_volume_at": str(project),
        "cache_device": 13, "build_device": 27,
    }
    assert not build.exists()
    assert main(["--project", str(project), "doctor"]) == 0
    assert "different volumes, Lake copies restored artifacts" in capsys.readouterr().out


def test_doctor_resolves_cache_override_before_volume_and_checks(setup, tmp_path, monkeypatch, capsys):
    project, _, cache = setup
    real_cache = tmp_path / "real-cache"
    real_cache.mkdir()
    try:
        cache.symlink_to(real_cache, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")
    monkeypatch.setenv("LAKE_CACHE_DIR", str(cache))
    result = report(project, capsys)
    assert result["cache"]["resolved_path"] == str(real_cache)
    assert result["builds"][0]["restore"]["cache_volume_at"] == str(real_cache)
