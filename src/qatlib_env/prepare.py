"""Idempotent, lock-bound workspace preparation and explicit cache diagnostics."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import quote

from qatlib_env import workspace, supervisor

PREPARE_VERSION = 3


class CacheError(ValueError):
    """A cache is inaccessible or invalid, rather than absent remotely."""


def validate_cache(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    for directory in (cache, cache / "artifacts", cache / "outputs"):
        if directory.is_symlink():
            raise CacheError(f"cache storage must not be symlinked: {directory}")
        if directory.exists():
            mode = stat.S_IMODE(directory.stat().st_mode)
            if not mode & 0o444 or not mode & 0o222 or not mode & 0o111:
                raise PermissionError(f"cache directory needs read/write/search permission: {directory}")
            if not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
                raise PermissionError(f"cache directory is inaccessible: {directory}")
    # Opening a probe catches ACL/read-only mounts that mode checks cannot see.
    with tempfile.TemporaryFile(dir=cache) as probe:
        probe.write(b"cache permission probe")
        probe.flush()


def identity(project: Path, env: dict) -> str:
    digest = hashlib.sha256(b"qatlas-prepare-v2\0")
    for name in ("lean-toolchain", "lake-manifest.json", "lakefile.toml", "lakefile.lean", "lake-cache.toml"):
        path = project / name
        digest.update(name.encode() + b"\0" + (path.read_bytes() if path.is_file() else b""))
    digest.update(str(Path(env["LAKE_CACHE_DIR"]).resolve()).encode())
    digest.update((workspace.git_head(project) or "uncommitted").encode())
    for package in workspace.manifest(project)["packages"]:
        path = project / ".lake/packages" / package["name"].strip("«»")
        digest.update((workspace.git_head(path) or "missing").encode())
    return digest.hexdigest()


def _cache_run(command, project, env):
    effective = workspace.command_runtime_environment(project, env, command)
    result = supervisor.run(command, cwd=project, env=effective, capture_output=True, text=True,
                            timeout=float(effective.get("QATLAS_STAGE_TIMEOUT", "10800")) or None,
                            module_timeout=float(effective.get("QATLAS_MODULE_TIMEOUT", "1800")) or None)
    if result.returncode:
        # Preserve useful diagnostics without echoing authentication/config text.
        details = (result.stderr or "") + (result.stdout or "")
        secrets = [value for name, value in effective.items()
                   if value and any(word in name.upper() for word in ("KEY", "TOKEN", "PASSWORD", "SECRET"))]
        config_path = effective.get("LAKE_CONFIG")
        if config_path and Path(config_path).is_file():
            config = tomllib.loads(Path(config_path).read_text())
            secrets.extend(service.get("s3Auth", "") for service in config.get("cache", {}).get("service", []))
        for secret in sorted(set(secrets), key=len, reverse=True):
            if secret:
                details = details.replace(secret, "[redacted]")
        diagnostic = "\n".join(details.strip().splitlines()[-12:])
        kind = "permission" if any(term in details.lower() for term in ("permission denied", "access denied", "403", "401")) else "invalid or unavailable"
        raise CacheError(f"{kind} cache: {' '.join(command[:3])} exited {result.returncode}; run cache explain\n{diagnostic}")
    return result


def _artifact_descriptors(path: Path) -> set[str]:
    """Flatten complete Lake output data with the native collectOutputDescrs rules."""
    try:
        lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if not lines or not isinstance(lines[0], str) or len(lines) == 1:
            raise ValueError("missing mapping rows")
        descriptors = set()
        def collect(value):
            if value is None or isinstance(value, bool):
                return
            if isinstance(value, int):
                if value < 0 or value >= 2 ** 64:
                    raise ValueError("artifact hash must fit UInt64")
                descriptors.add(f"{value:016x}.art")
            elif isinstance(value, str):
                if not re.fullmatch(r"[0-9a-fA-F]{16}(?:\.[A-Za-z0-9._-]*)?", value):
                    raise ValueError("unsafe artifact descriptor")
                content_hash, dot, extension = value.partition(".")
                descriptors.add(content_hash.lower() + (dot + extension if extension else ""))
            elif isinstance(value, list):
                for item in value:
                    collect(item)
            elif isinstance(value, dict):
                for item in value.values():
                    collect(item)
            else:
                raise ValueError("unsupported artifact data")
        mappings = {}
        for row in lines[1:]:
            if not isinstance(row, list) or len(row) < 2:
                raise ValueError("invalid mapping row")
            key = row[0]
            valid_key = (isinstance(key, int) and not isinstance(key, bool) and 0 <= key < 2 ** 64) \
                or (isinstance(key, str) and re.fullmatch(r"[0-9a-fA-F]{16}", key))
            if not valid_key:
                raise ValueError("invalid input hash")
            canonical = f"{key:016x}" if isinstance(key, int) else key.lower()
            if canonical in mappings and mappings[canonical] != row[1]:
                raise ValueError("conflicting mapping rows")
            mappings[canonical] = row[1]
            collect(row[1])
        return descriptors
    except (OSError, ValueError, TypeError) as error:
        raise CacheError("corrupt cache manifest: invalid Lake mapping or artifact descriptor") from error


def _fetch(url: str, destination: Path, project: Path, env: dict, package: str) -> str:
    result = supervisor.run(["curl", "-sS", "-L", "-w", "%{http_code}", "-o", str(destination), url],
                            cwd=project, env=env, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise CacheError(f"cache transport failed (curl {result.returncode}) for {package}; remote absence is unconfirmed")
    return result.stdout.strip()


def _readable_artifact(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise CacheError(f"cache artifact must be a regular file: {path}")
    if not stat.S_IMODE(path.stat().st_mode) & 0o444 or not os.access(path, os.R_OK):
        raise PermissionError(f"cache artifact is not readable: {path}")
    with path.open("rb") as stream:
        stream.read(1)


def _fetch_artifacts(downloads: dict[str, Path], endpoint: str, scope: str,
                     project: Path, env: dict, package: str, staging: Path) -> None:
    """One supervised curl owns bounded concurrent transfers and their statuses."""
    if not downloads:
        return
    configuration = []
    for content_hash, target in downloads.items():
        if configuration:
            configuration.append("next")
        # --next resets per-transfer auth options, so reload the same selected,
        # private curl configuration for every transfer, without copying keys.
        if env.get("CURL_HOME"):
            configuration.append("config = " + json.dumps(str(Path(env["CURL_HOME"]) / ".curlrc")))
        configuration += ["silent", "show-error", "location",
                          "url = " + json.dumps(endpoint + "/" + quote(scope, safe="/") + "/" + content_hash + ".art"),
                          "output = " + json.dumps(str(target)),
                          "write-out = " + json.dumps(content_hash + " %{http_code}\n")]
    config = staging / "curl-transfers.conf"
    config.write_text("\n".join(configuration) + "\n")
    config.chmod(0o600)
    result = supervisor.run(["curl", "--parallel", "--parallel-max", "8", "--config", str(config)],
                            cwd=project, env=env, capture_output=True, text=True,
                            timeout=float(env.get("QATLAS_STAGE_TIMEOUT", "10800")) or None)
    statuses = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in downloads:
            statuses[parts[0]] = parts[1]
    if result.returncode or set(statuses) != set(downloads):
        raise CacheError(f"cache artifact transport failed for {package} (curl {result.returncode}); manifest mappings were not registered")
    for content_hash, status in statuses.items():
        if status in {"401", "403"}:
            raise CacheError(f"cache artifact access denied with HTTP {status} for {package} ({content_hash})")
        if status != "200":
            raise CacheError(f"corrupt cache manifest for {package}: artifact returned HTTP {status} ({content_hash})")


def _restore_artifacts(project: Path, manifest: Path, service: dict, scope: str, env: dict, package: str) -> None:
    """Verify unique native artifacts before registering the unchanged full map.

    Lake 4.35's bulk downloader rewrites duplicate descriptors, including a
    read-only artifact onto itself.  Install only missing paths here, without
    weakening permissions or exposing temporary partial output mappings.
    """
    from qatlib_env.r2_cache import _verify_artifact_hashes
    descriptors = _artifact_descriptors(manifest)
    cache = Path(env["LAKE_CACHE_DIR"])
    validate_cache(cache)
    artifacts = cache / "artifacts"
    artifacts.mkdir(exist_ok=True)
    paths, by_hash = {}, {}
    missing = []
    for name in sorted(descriptors):
        target = artifacts / name
        if target.exists() or target.is_symlink():
            _readable_artifact(target)
            paths[name] = target
            by_hash.setdefault(name.split(".", 1)[0], target)
        else:
            missing.append(name)
    with tempfile.TemporaryDirectory(prefix="qatlas-artifacts-", dir=cache) as temporary:
        staging = Path(temporary)
        endpoint = service.get("artifactEndpoint", "").rstrip("/")
        if missing and not endpoint:
            raise CacheError(f"cache artifact endpoint is missing for {package}")
        downloads = {}
        for name in missing:
            content_hash = name.split(".", 1)[0]
            if content_hash not in by_hash:
                downloaded = staging / (content_hash + ".download")
                downloads[content_hash] = downloaded
                by_hash[content_hash] = downloaded
            paths[name] = by_hash[content_hash]
        _fetch_artifacts(downloads, endpoint, scope, project, env, package, staging)
        # Includes existing 0444 files, checks every extension and does not trust
        # sidecars. A real permission/hash error is never treated as a miss.
        try:
            checksums = _verify_artifact_hashes(project, paths, env)
        except (ValueError, subprocess.SubprocessError) as error:
            raise CacheError(f"corrupt cache artifact content for {package}; native hash validation failed") from error
        for index, name in enumerate(missing):
            target = artifacts / name
            source = staging / f"install-{index}"
            shutil.copyfile(paths[name], source)
            if hashlib.sha256(source.read_bytes()).hexdigest() != checksums[name]:
                raise CacheError(f"cache artifact changed during verification: {name}")
            source.chmod(0o444)
            try:
                # A hard link from a unique temporary inode gives an atomic,
                # no-replace installation; extensions have independent inodes.
                os.link(source, target)
            except FileExistsError:
                _readable_artifact(target)
                try:
                    _verify_artifact_hashes(project, {name: target}, env)
                except (ValueError, subprocess.SubprocessError) as error:
                    raise CacheError(f"concurrently installed corrupt cache artifact: {name}") from error


def _publish_native_mappings(staged: Path, cache: Path) -> int:
    """Promote validated native per-module JSON atomically, without scope links."""
    outputs = staged / "outputs"
    if outputs.is_symlink() or not outputs.is_dir():
        raise CacheError("native cache add produced no regular outputs directory")
    mappings = []
    for path in sorted(outputs.rglob("*")):
        if path.is_symlink():
            raise CacheError("native cache add produced symlinked output mappings")
        if path.is_dir():
            continue
        relative = path.relative_to(outputs)
        if len(relative.parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", relative.parts[0]) \
                or not re.fullmatch(r"[0-9a-f]{16}\.json", relative.parts[1]) or not path.is_file():
            raise CacheError("native cache add produced an unsafe output mapping path")
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            raise CacheError("native cache add produced invalid output JSON") from error
        if not isinstance(record, dict) or record.get("schemaVersion") != "2026-02-25" \
                or "data" not in record or not isinstance(record.get("service"), (str, type(None))) \
                or any(key in record and not isinstance(record[key], str) for key in ("repo", "scope")):
            raise CacheError("native cache add produced an unsupported output schema")
        mappings.append((path, relative.parts[0], relative.parts[1]))
    if not mappings:
        raise CacheError("native cache add produced no module output mappings")
    shared = cache / "outputs"
    if shared.is_symlink():
        raise CacheError("cache outputs directory must not be symlinked")
    shared.mkdir(exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    # Directory descriptors keep the destination anchored even if a path is
    # replaced after inspection. Never follow an existing namespace symlink.
    with _mapping_directory(shared, flags) as outputs_fd:
        for source, namespace, name in mappings:
            try:
                os.mkdir(namespace, dir_fd=outputs_fd)
            except FileExistsError:
                pass
            try:
                namespace_fd = os.open(namespace, flags, dir_fd=outputs_fd)
            except OSError as error:
                raise CacheError(f"cache output namespace is inaccessible or symlinked: {namespace}") from error
            try:
                try:
                    info = os.stat(name, dir_fd=namespace_fd, follow_symlinks=False)
                except FileNotFoundError:
                    info = None
                if info is not None and not stat.S_ISREG(info.st_mode):
                    raise CacheError(f"cache output mapping is not a regular file: {namespace}/{name}")
                # Native writes only private staging JSON. The shared reader
                # sees either complete version; there is no truncate window.
                os.replace(source, name, dst_dir_fd=namespace_fd)
            finally:
                os.close(namespace_fd)
    return len(mappings)


@contextmanager
def _mapping_directory(path: Path, flags: int):
    descriptor = os.open(path, flags)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _register_mappings(command: list[str], project: Path, env: dict) -> None:
    cache = Path(env["LAKE_CACHE_DIR"])
    # Same-filesystem staging is outside outputs/, so running Lake builds can
    # never discover temporary registrations while the native writer truncates.
    with tempfile.TemporaryDirectory(prefix="qatlas-mappings-", dir=cache) as temporary:
        staged = Path(temporary)
        _cache_run(command, project, {**env, "LAKE_CACHE_DIR": str(staged)})
        _publish_native_mappings(staged, cache)


def restore_repository(project: Path, package_root: Path, env: dict) -> str:
    """Fetch exact/input/ancestor candidates; only HTTP 404 means a remote miss."""
    from qatlib_env import ci
    config_file = package_root / "lake-cache.toml"
    if not config_file.is_file():
        return "unconfigured"
    config = tomllib.loads(config_file.read_text())["cache"]
    service = next(s for s in config["service"] if s["name"] == config["defaultService"])
    package = workspace.package_name(package_root)
    from qatlib_env.r2_cache import package_environment
    env = package_environment(package if package_root == project else "", env)
    scope_option = "--repo="
    if package == "Physlib":
        legacy = env["ELAN_TOOLCHAIN"].replace("/", "-").replace(":", "-")
        slug = "physlib-master/" + legacy + "/physlib"
        library = "Physlib"
        scope_option = "--scope="
    elif package == "Qatlib":
        library, slug = "Qatlib", "quantum-atlas-ai/qatlib"
    elif package in ci.DEV_PACKAGES | ci.SECONDARY_PACKAGES:
        library, _targets, slug = ci.profile(package, env, package_root)
    else:
        origin = supervisor.run(["git", "remote", "get-url", "origin"], cwd=package_root,
                                env=env, capture_output=True, text=True, check=True).stdout.strip()
        from urllib.parse import urlsplit
        slug = urlsplit(origin).path.strip("/").removesuffix(".git") if "://" in origin else origin.split(":", 1)[-1].strip("/").removesuffix(".git")
        library = package
    sha = workspace.git_head(package_root)
    if not sha:
        raise CacheError(f"cache source identity unavailable: {package_root}")
    paths = [library, library + ".lean", "lakefile.toml", "lean-toolchain", "lake-manifest.json"]
    if package in ci.DEV_PACKAGES | ci.SECONDARY_PACKAGES:
        paths.insert(2, "verification")
    tree_result = supervisor.run(["git", "rev-parse", *("HEAD:" + p for p in paths)],
                                 cwd=package_root, env=env, capture_output=True, text=True)
    tree = hashlib.sha256(tree_result.stdout.encode()).hexdigest() if tree_result.returncode == 0 else ""
    ancestors = supervisor.run(["git", "rev-list", "--max-count=25", "HEAD~1"],
                               cwd=package_root, env=env, capture_output=True, text=True)
    exact_keys = [sha + ".jsonl", *(["inputs/" + tree + ".jsonl"] if tree and package != "Physlib" else [])]
    keys = [*exact_keys,
            *(s + ".jsonl" for s in ancestors.stdout.splitlines() if ancestors.returncode == 0)]
    endpoint = service["revisionEndpoint"].rstrip("/")
    base = endpoint + "/" + quote(slug, safe="/")
    if scope_option == "--repo=":
        base += "/tc/" + ci.toolchain_dir(env["ELAN_TOOLCHAIN"])
    # A root job's generic/private write key must never authenticate a different
    # dependency bucket. Use that dependency's explicit key or its read config.
    key_env = dict(env)
    if package_root != project:
        key_env.pop("LAKE_CACHE_KEY", None)
    key = (ci.write_key(package, key_env) if package == "Qatlib" or package in ci.DEV_PACKAGES | ci.SECONDARY_PACKAGES else "") \
        or service.get("s3Auth", "").strip()
    (project / ".lake").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qatlas-prepare-", dir=project / ".lake") as temporary:
        manifest = Path(temporary) / "candidate.jsonl"
        with ci.authenticated({**env, "TMPDIR": temporary}, key) as auth:
            for relative in dict.fromkeys(keys):
                status = _fetch(base + "/" + relative, manifest, project, auth, package)
                if status == "404":
                    continue
                if status != "200":
                    raise CacheError(f"cache service returned HTTP {status} for {package}")
                _restore_artifacts(project, manifest, service, slug, auth, package)
                _register_mappings(["lake", "cache", "add", str(manifest), "--package=" + package,
                            "--service=" + config["defaultService"], scope_option + slug],
                           project, {**auth, "LAKE_CONFIG": str(config_file)})
                if package_root == project:
                    shutil.copyfile(manifest, project / ".lake/qatlas-root-cache.jsonl")
                    workspace.atomic_json(project / ".lake/qatlas-root-cache-kind.json", {
                        "kind": "exact" if relative in exact_keys else "ancestor", "identity": identity(project, env)})
                return relative
    print(f"CACHE_MISS package={package} (HTTP 404)", file=sys.stderr)
    if package_root == project:
        for path in (project / ".lake/qatlas-root-cache.jsonl", project / ".lake/qatlas-root-cache-kind.json"):
            if path.exists():
                workspace.remove_derived(path, env)
    return "missing"


def cold_modules(project: Path) -> int:
    """Count missing library outputs in root and locked source packages."""
    count = 0
    locked_names = _locked_package_names(project)
    for root in workspace.package_roots(project):
        config_file = root / "lakefile.toml"
        if config_file.is_file():
            config = tomllib.loads(config_file.read_text())
        elif locked_names.get(root) == "mathlib" and (root / "lakefile.lean").is_file():
            # Mathlib's known library namespace is stable. Count its source
            # tree directly instead of trying to interpret arbitrary Lean DSL.
            config = {"lean_lib": [{"name": "Mathlib"}]}
        else:
            continue
        seen = set()
        for library in config.get("lean_lib", []):
            source = root / library.get("srcDir", config.get("srcDir", "."))
            name = library["name"].split(".")[0]
            candidates = [source / (name + ".lean")]
            directory = source / name
            if directory.is_dir():
                candidates.extend(directory.rglob("*.lean"))
            for path in candidates:
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)
                output = root / ".lake/build/lib/lean" / path.relative_to(source).with_suffix(".olean")
                if not output.is_file():
                    count += 1
    return count


def _locked_package_names(project: Path) -> dict[Path, str]:
    """Use dependency identities from Lake's lock, independently of config syntax."""
    result = {}
    for package in workspace.manifest(project)["packages"]:
        name = package["name"].strip("«»")
        result[project / ".lake/packages" / name] = name
    return result


def enforce_cold_limit(project: Path, env: dict) -> int:
    count = cold_modules(project)
    maximum = int(env.get("QATLAS_MAX_COLD_MODULES", "0"))
    warning = int(env.get("QATLAS_COLD_WARN_MODULES", "100"))
    if maximum < 0 or warning < 0:
        raise ValueError("cold module thresholds must be nonnegative")
    if maximum and count > maximum:
        raise CacheError(f"{count} cold modules exceed configured limit {maximum}; run qatlib-env prepare to prewarm, or explicitly raise QATLAS_MAX_COLD_MODULES")
    if warning and count > warning:
        print(f"COLD_BUILD_WARNING modules={count}; inspect qatlib-env cache explain before compiling", file=sys.stderr)
    return count


def prepare(project: Path, env: dict, *, source=None, offline=False) -> dict:
    from qatlib_env import checkpoint
    from qatlib_env.r2_cache import package_environment
    project = Path(project)
    env = package_environment(workspace.package_name(project), env)
    cache = Path(env["LAKE_CACHE_DIR"])
    validate_cache(cache)
    (project / ".lake").mkdir(exist_ok=True)
    with workspace.cache_lock(cache):
        if offline:
            for package in workspace.manifest(project)["packages"]:
                root = project / ".lake/packages" / package["name"].strip("«»")
                if workspace.git_head(root) != package.get("rev"):
                    raise CacheError(f"offline dependency is absent or not locked: {package['name']}")
        else:
            workspace.resolve_dependencies(project, env, source)
        stamp = project / ".lake/qatlas-prepare.json"
        current = identity(project, env)
        previous = json.loads(stamp.read_text()) if stamp.is_file() else {}
        ready = previous.get("version") == PREPARE_VERSION and previous.get("identity") == current \
            and all(Path(p).is_dir() for p in previous.get("build_dirs", []))
        result = {"version": PREPARE_VERSION, "identity": current, "offline": offline, "reused": ready, "cache": str(cache)}
        if not ready and not offline:
            results = {}
            roots = workspace.package_roots(project)
            locked_names = _locked_package_names(project)
            order = {"mathlib": 0, "Physlib": 1, "Qatlib": 2, "Project": 3, "example_library": 3}
            roots = sorted(roots[1:], key=lambda p: order.get(locked_names.get(p, p.name), -1)) + roots[:1]
            for root in roots:
                if locked_names.get(root) == "mathlib":
                    _cache_run(["lake", "exe", "cache", "get"], project, env)
                    results["mathlib"] = "mathlib"
                    continue
                if not (root / "lakefile.toml").is_file():
                    continue
                name = workspace.package_name(root)
                if name == "mathlib":
                    _cache_run(["lake", "exe", "cache", "get"], project, env)
                    results[name] = "mathlib"
                else:
                    results[name] = restore_repository(project, root, env)
            result["repositories"] = results
        result["checkpoint"] = checkpoint.restore(project, env)
        result["cold_modules"] = enforce_cold_limit(project, env)
        # Offline preparation must not certify that remote caches were checked.
        if not offline:
            result["build_dirs"] = [str(root / ".lake/build") for root in workspace.package_roots(project)
                                    if (root / ".lake/build").is_dir()]
            workspace.atomic_json(stamp, result)
        return result


