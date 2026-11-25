"""Read-only inspection of the inputs and outputs of Lake cache reuse."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from qatlib_search.lean_source import LEAN_ID_REST, mask_comments

from . import diagnostics, toolchain, workspace
from .envvars import env_value

NO_SERVER_OLEAN = "No server olean generated. Ensure the module system is enabled."
ARTIFACT = re.compile(r"([0-9a-fA-F]{16})(?:\.[A-Za-z0-9._-]+)?\Z")
PIN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
MODULE_HEADER = re.compile(r"^\s*module(?!" + LEAN_ID_REST + ")")
EXAMPLE_LIMIT = 8


def _artifact_present(path: Path) -> bool:
    try:
        return (not workspace.redirected_directory(path) and path.is_file()
                and path.stat().st_size > 0 and os.access(path, os.R_OK))
    except OSError:
        return False


def _volume(path: Path) -> tuple[Path, int]:
    # Stat the resolved path (including Windows junctions) or its closest
    # existing ancestor if Lake hasn't created the destination yet.
    resolved = path.resolve()
    for candidate in (resolved, *resolved.parents):
        try:
            return candidate, candidate.stat().st_dev
        except FileNotFoundError:
            continue
    raise FileNotFoundError(path)


def _restore_mode(artifacts: Path, output: Path) -> dict:
    try:
        source, source_device = _volume(artifacts)
        target, target_device = _volume(output)
    except (OSError, RuntimeError):
        return {"mode": "unknown"}
    if os.name == "nt" and (not source_device or not target_device):
        mode = "unknown"
    elif source_device == target_device:
        mode = "hardlink_eligible"
    else:
        mode = "copy_fallback"
    return {"mode": mode, "cache_volume_at": str(source), "build_volume_at": str(target),
            "cache_device": source_device, "build_device": target_device}


def _git_head(checkout: Path, env: dict) -> str | None:
    if not (checkout / ".git").exists():
        return None
    result = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--verify", "HEAD"],
                            env=env, capture_output=True, text=True, timeout=10, check=False)
    return result.stdout.strip().lower() if result.returncode == 0 else None


def _packages(project, entries, source, env, finding):
    git_env = {**env, "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0"}
    roots = None
    packages = []
    for entry in entries:
        name = entry["name"].strip("«»")
        kind = entry.get("type", "git")
        checkout = project / ".lake/packages" / name
        package = {"name": name, "type": kind, "checkout": str(checkout)}
        packages.append(package)
        if kind == "path" and isinstance(entry.get("dir"), str):
            checkout = project / entry["dir"]
            package.update(checkout=str(checkout), status="present" if checkout.is_dir() else "missing")
            if not checkout.is_dir():
                finding("path_dependency_missing", checkout)
            continue
        revision = entry.get("rev")
        if kind != "git" or not isinstance(revision, str) or not PIN.fullmatch(revision):
            package["status"] = "invalid_pin"
            finding("invalid_package_pin", name, "error")
            continue
        revision = revision.lower()
        package.update(revision=revision, head=None, mirror=None)
        try:
            if any(workspace.redirected_directory(path) for path in
                   (project / ".lake", checkout.parent, checkout)):
                package["status"] = "redirected"
                finding("redirected_package_checkout", checkout, "error")
                continue
            head = _git_head(checkout, git_env)
            package.update(head=head, status="aligned" if head == revision else "mismatch" if head else "missing")
            if head == revision and not env.get("QATLIB_PKG_MIRROR"):
                continue
            if head == revision:
                candidates = [Path(env["QATLIB_PKG_MIRROR"]).expanduser()]
            else:
                finding("package_" + package["status"], checkout)
                if roots is None:
                    roots = workspace.candidate_sources(project, source, git_env)
                candidates = roots
            for root in candidates:
                try:
                    for candidate in workspace.source_packages(root, name):
                        if workspace.usable_git_mirror(candidate, revision, project, git_env):
                            package["mirror"] = str(candidate)
                            break
                except (OSError, ValueError, KeyError, AttributeError, subprocess.SubprocessError):
                    continue
                if package["mirror"]:
                    break
            if package["mirror"] is None:
                finding("local_mirror_pin_unavailable", name)
        except (OSError, subprocess.SubprocessError):
            package["status"] = "unreadable"
            finding("package_inspection_failed", checkout, "error")
    return packages


def _scan_files(root: Path, finding):
    if not root.is_dir() or workspace.redirected_directory(root):
        return

    def onerror(error):
        finding("directory_unreadable", error.filename or root, "error")

    for parent, dirs, files in os.walk(root, onerror=onerror):
        dirs[:] = sorted(name for name in dirs if not workspace.redirected_directory(Path(parent) / name))
        for name in sorted(files):
            yield Path(parent) / name


def _check_outputs(path: Path, artifacts: Path, data, finding):
    # Lake's ModuleOutputDescrs uses "m" to mark the module system and
    # positional "o" entries for the ordinary, server and private oleans.
    descriptors = []
    if isinstance(data, dict):
        parts = data.get("o", [])
        if not isinstance(parts, list):
            finding("invalid_output_mapping", path)
            return
        is_module = data.get("m", len(parts) > 1) is True
        if ("o" in data or is_module) and not parts:
            finding("missing_olean_descriptor", path)
        if is_module and len(parts) < 2:
            finding("missing_server_olean_descriptor", path)
        kinds = ["missing_cache_olean", "missing_cache_server_olean"]
        descriptors.extend(zip(parts[:2], kinds))
        descriptors.extend((part, "missing_cache_artifact") for part in parts[2:])
        for key in ("l", "ltar"):
            if key in data:
                descriptors.append((data[key], "missing_cache_artifact"))
    elif isinstance(data, str):
        descriptors.append((data, "missing_cache_artifact"))
    for descriptor, missing_code in descriptors:
        match = ARTIFACT.fullmatch(descriptor) if isinstance(descriptor, str) else None
        if not match:
            finding("invalid_artifact_descriptor", path)
            continue
        blob = artifacts / (match[1].lower() + descriptor[16:])
        if not _artifact_present(blob):
            finding(missing_code, f"{path} -> {blob.name}")


def _inspect_cache(cache: Path, finding) -> dict:
    artifacts, outputs = cache / "artifacts", cache / "outputs"
    report = {"path": str(cache), "resolved_path": str(cache.resolve()),
              "artifact_files": 0, "output_mappings": 0, "output_scopes": {}, "scanned": False}
    ancestors = cache.parents if os.name == "nt" else ()
    for directory in (*ancestors, cache, artifacts, outputs):
        if workspace.redirected_directory(directory):
            finding("redirected_cache_directory", directory, "error")
            return report
        if directory.exists() and not directory.is_dir():
            finding("invalid_cache_directory", directory, "error")
            return report
    if artifacts.is_dir():
        with os.scandir(artifacts) as entries:
            report["artifact_files"] = sum(
                bool(ARTIFACT.fullmatch(entry.name)) and entry.is_file(follow_symlinks=False) for entry in entries
            )
    for path in _scan_files(outputs, finding):
        if path.suffix != ".json" or workspace.redirected_directory(path):
            continue
        scope = path.parent.relative_to(outputs).as_posix()
        report["output_scopes"][scope] = report["output_scopes"].get(scope, 0) + 1
        report["output_mappings"] += 1
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            # Older Lake versions stored the output value directly.
            data = record["data"] if isinstance(record, dict) and (
                "schemaVersion" in record or "data" in record) else record
            _check_outputs(path, artifacts, data, finding)
        except (OSError, ValueError, KeyError):
            finding("invalid_output_mapping", path)
    if not report["artifact_files"]:
        finding("cache_artifacts_missing", artifacts)
    if not report["output_mappings"]:
        code = "unmapped_cache_artifacts" if report["artifact_files"] else "cache_output_mappings_missing"
        finding(code, outputs)
    report["scanned"] = True
    return report


def _inspect_build(root: Path, cache: Path | None, finding) -> dict:
    output = root / ".lake/build/lib/lean"
    source_dirs = [root]
    config = root / "lakefile.toml"
    if config.is_file():
        try:
            data = tomllib.loads(config.read_text(encoding="utf-8"))
            output = root / data.get("buildDir", ".lake/build") / data.get("leanLibDir", "lib/lean")
            source_dirs = list(dict.fromkeys(root / data.get("srcDir", ".") / target.get("srcDir", ".")
                                            for target in [{}, *data.get("lean_lib", []), *data.get("lean_exe", [])]))
        except (OSError, ValueError, TypeError, AttributeError):
            finding("lake_config_unreadable", config)
    report = {"path": str(output), "olean_files": 0}
    if cache is not None:
        report["restore"] = _restore_mode(cache / "artifacts", output)
    if any(workspace.redirected_directory(path) for path in (root / ".lake", root / ".lake/build")):
        finding("redirected_build_directory", output, "error")
        return report
    oleans = dict.fromkeys(path if path.name.endswith(".olean") else path.with_suffix("")
                           for path in _scan_files(output, finding)
                           if path.name.endswith((".olean", ".olean.server", ".olean.private")))
    for olean in oleans:
        if _artifact_present(olean):
            report["olean_files"] += 1
        else:
            finding("missing_local_olean", olean)
        server = olean.with_suffix(".olean.server")
        if _artifact_present(server):
            continue
        rel = olean.relative_to(output).with_suffix(".lean")
        source = next((base / rel for base in source_dirs if (base / rel).is_file()), None)
        if source is not None:
            try:
                header = mask_comments(source.read_text(encoding="utf-8-sig"))
                if MODULE_HEADER.match(header):
                    finding("missing_local_server_olean", server)
            except (OSError, ValueError):
                finding("lean_source_unreadable", source)
    return report


MESSAGES = {
    "toolchain_lock_unreadable": "Select an explicit Lean release in lean-toolchain.",
    "toolchain_override_mismatch": "ELAN_TOOLCHAIN differs from the locked Lean release.",
    "toolchain_incomplete": "Install the locked toolchain or check the real toolchain path.",
    "manifest_unreadable": "Check lake-manifest.json and its private .lake/packages layout.",
    "invalid_package_pin": "The manifest entry has an invalid or unsupported pin.",
    "path_dependency_missing": "The manifest's local path dependency is missing.",
    "redirected_package_checkout": "The package checkout uses redirected working state.",
    "package_missing": "The pinned package checkout has no readable Git HEAD.",
    "package_mismatch": "The worktree package HEAD differs from the manifest pin.",
    "local_mirror_pin_unavailable": "No usable local mirror contains the pinned commit.",
    "package_inspection_failed": "The local Git package inspection failed.",
    "redirected_cache_directory": "Cache directory is a symlink or reparse point. Set LAKE_CACHE_DIR to the real directory.",
    "invalid_cache_directory": "Expected a cache directory.",
    "directory_unreadable": "A directory could not be scanned.",
    "cache_artifacts_missing": "No content-addressed artifact files found.",
    "cache_output_mappings_missing": "No native output mappings found.",
    "unmapped_cache_artifacts": "Artifacts exist but output mappings are absent. Check cache registration or publication.",
    "unregistered_package_revision": "The locked revision manifest exists but its package has no registered outputs.",
    "invalid_output_mapping": "A native Lake output mapping could not be read.",
    "invalid_artifact_descriptor": "The mapped artifact descriptor is invalid.",
    "missing_cache_artifact": "A mapped artifact file is absent, empty or unreadable.",
    "missing_cache_olean": "The mapped base .olean file is absent, empty or unreadable.",
    "missing_cache_server_olean": "The mapped .olean.server file is absent, empty or unreadable.",
    "missing_olean_descriptor": "The native output mapping has no base .olean descriptor.",
    "missing_server_olean_descriptor": NO_SERVER_OLEAN + " The module mapping lacks a .olean.server descriptor.",
    "missing_local_server_olean": NO_SERVER_OLEAN + " A local module-system output is partial.",
    "missing_local_olean": "A local .olean is absent, empty or unreadable.",
    "lake_config_unreadable": "The Lake TOML configuration could not be read.",
    "redirected_build_directory": "The build directory uses redirected working state.",
    "lean_source_unreadable": "Could not check whether the Lean source uses the module system.",
}


def run(project: Path, env: dict, *, source: Path | None, as_json: bool) -> int:
    findings = {}

    def finding(code, example, severity="warning"):
        entry = findings.setdefault(code, {"code": code, "severity": severity, "message": MESSAGES[code],
                                           "count": 0, "examples": []})
        entry["count"] += 1
        if len(entry["examples"]) < EXAMPLE_LIMIT:
            entry["examples"].append(str(example))

    report = {"schema_version": 1, "project": str(project), "platform": sys.platform, "toolchain": {}, "packages": [],
              "cache": None, "builds": []}
    locked = None
    try:
        locked = toolchain.locked_toolchain(project)
        prefix = diagnostics.toolchain_prefix(locked, env)
        suffix = ".exe" if os.name == "nt" else ""
        missing = [name for name in (f"bin/lean{suffix}", f"bin/lake{suffix}", "lib/lean/Init.olean")
                   if not (prefix / name).is_file()]
        report["toolchain"] = {"locked": locked, "prefix": str(prefix), "resolved_prefix": str(prefix.resolve()),
                               "elan": shutil.which("elan", path=env.get("PATH", "")), "missing": missing}
        if missing:
            finding("toolchain_incomplete", prefix, "error")
        if env.get("ELAN_TOOLCHAIN", locked) != locked:
            finding("toolchain_override_mismatch", "ELAN_TOOLCHAIN", "error")
    except (OSError, ValueError, RuntimeError):
        finding("toolchain_lock_unreadable", project / "lean-toolchain", "error")
    try:
        entries = workspace.manifest(project)["packages"]
        if not isinstance(entries, list):
            raise TypeError("expected a package list")
        mirror_env = dict(env)
        mirror = env_value(env, "QATLIB_PKG_MIRROR", None)
        if mirror is None:
            default = Path(env.get("HOME", str(Path.home()))) / "pkg-mirror"
            mirror = str(default) if default.is_dir() else None
        if mirror:
            mirror_env["QATLIB_PKG_MIRROR"] = mirror
        report["mirror_root"] = mirror
        report["packages"] = _packages(project, entries, source, mirror_env, finding)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        finding("manifest_unreadable", project / "lake-manifest.json", "error")
    cache = None
    if locked or env.get("LAKE_CACHE_DIR"):
        try:
            cache = workspace.cache_directory(project, env)
            report["cache"] = _inspect_cache(cache, finding)
        except (OSError, ValueError, RuntimeError):
            finding("directory_unreadable", cache or "LAKE_CACHE_DIR", "error")
    if cache is not None and report["cache"] and report["cache"]["scanned"]:
        for package in report["packages"]:
            if revision := package.get("revision"):
                name = package["name"]
                namespace = cache / "revisions" / name
                revision_map = namespace / f"{revision}.jsonl"
                output_count = report["cache"]["output_scopes"].get(name, 0)
                try:
                    has_revision_map = (not any(workspace.redirected_directory(path) for path in
                                                (namespace.parent, namespace))
                                        and _artifact_present(revision_map))
                except OSError:
                    has_revision_map = False
                    finding("directory_unreadable", namespace, "error")
                package["cache"] = {"output_mappings": output_count,
                                    "locked_revision_manifest": str(revision_map) if has_revision_map else None}
                if has_revision_map and not output_count:
                    finding("unregistered_package_revision", revision_map)
    roots = [project, *(Path(pkg["checkout"]) for pkg in report["packages"]
                        if pkg["status"] in {"aligned", "mismatch", "present"})]
    for root in dict.fromkeys(roots):
        try:
            report["builds"].append(_inspect_build(root, cache, finding))
        except (OSError, RuntimeError):
            finding("directory_unreadable", root, "error")
    report["findings"] = list(findings.values())
    errors = any(item["severity"] == "error" for item in findings.values())
    report["status"] = "error" if errors else "warning" if findings else "ok"
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        _print_report(report)
    return int(errors)


def _print_report(report: dict) -> None:
    print(f"Lake environment for {report['project']} [{report['status']}]")
    if toolchain_report := report["toolchain"]:
        print(f"Toolchain {toolchain_report['locked']} at {toolchain_report['resolved_prefix']}")
        if toolchain_report["missing"]:
            print("  Missing " + ", ".join(toolchain_report["missing"]))
    for package in report["packages"]:
        print(f"Package {package['name']} [{package['status']}]")
        if package.get("revision"):
            print(f"  Pin {package['revision']}, HEAD {package['head'] or 'unavailable'}")
        if package.get("mirror"):
            print(f"  Usable local mirror {package['mirror']}")
        if package_cache := package.get("cache"):
            print(f"  {package_cache['output_mappings']} package output mappings")
            if revision_map := package_cache["locked_revision_manifest"]:
                print(f"  Locked revision manifest {revision_map}")
    if cache := report["cache"]:
        print(f"Lake cache {cache['resolved_path']} ({cache['artifact_files']} artifacts, "
              f"{cache['output_mappings']} output mappings)")
    restore_messages = {
        "hardlink_eligible": "same volume, hardlink eligible with copy fallback",
        "copy_fallback": "different volumes, Lake copies restored artifacts",
        "unknown": "volume relationship unknown",
    }
    for build in report["builds"]:
        restore = restore_messages[build["restore"]["mode"]] if "restore" in build else "cache path unavailable"
        print(f"Build output {build['path']} ({build['olean_files']} .olean files, {restore})")
    for finding in report["findings"]:
        print(f"{finding['severity'].upper()} ({finding['count']}) {finding['message']}")
        for example in finding["examples"]:
            print(f"  {example}")
