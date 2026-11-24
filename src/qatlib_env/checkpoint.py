"""Local-only recovery of completed root modules, independent of CI checkouts.

These checkpoints are deliberately separate from Lake revision manifests.  A
failed build may produce useful modules but can never certify a publishable
revision.  Exact input fingerprints and Lake's own traces protect restoration.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import tomllib
import zipfile

SCHEMA = 1


def _text_digest(path: Path) -> str:
    """Apply Lake's CRLF-to-LF rule to source and configuration inputs."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _artifact_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def source_roots(project: Path) -> list[Path]:
    """Find root and test source directories declared by the root Lake package."""
    config = tomllib.loads((project / "lakefile.toml").read_text(encoding="utf-8"))
    roots = [project]
    for kind in ("lean_lib", "lean_exe"):
        for target in config.get(kind, []):
            root = (project / target.get("srcDir", ".")).resolve()
            if not root.is_relative_to(project.resolve()):
                raise ValueError("source directory outside the project is unsupported")
            if root not in roots:
                roots.append(root)
    return roots


def local_identity(project: Path) -> str:
    """Fingerprint actual root sources and dependency lock without a checkout path."""
    project = project.resolve()
    config = tomllib.loads((project / "lakefile.toml").read_text(encoding="utf-8"))
    inputs = {project / name for name in ("lakefile.toml", "lean-toolchain", "lake-manifest.json")}
    for kind in ("lean_lib", "lean_exe"):
        for target in config.get(kind, []):
            root = project / target.get("srcDir", ".")
            name = target.get("name", "")
            # Libraries can use namespace globs or srcDir; include all Lean
            # sources in dedicated source roots, but never .lake dependencies.
            scan = root / name.replace(".", "/")
            if root != project:
                scan = root
            if scan.is_dir():
                inputs.update(scan.rglob("*.lean"))
            aggregate = root / (name.replace(".", "/") + ".lean")
            if aggregate.is_file():
                inputs.add(aggregate)
    # Include local imported files outside target namespaces too. Prune build
    # and VCS directories before walking, including enormous dependency trees.
    for directory, folders, files in os.walk(project):
        folders[:] = [name for name in folders if name not in {".lake", ".git", ".agent-tasks", ".venv"}
                      and not (Path(directory) / name).is_symlink()]
        inputs.update(Path(directory) / name for name in files if name.endswith(".lean"))
    digest = hashlib.sha256()
    for path in sorted(inputs):
        if path.is_symlink():
            raise ValueError(f"refusing symlinked checkpoint input: {path}")
        digest.update(path.relative_to(project).as_posix().encode() + b"\0")
        digest.update((_text_digest(path) if path.is_file() else "absent").encode() + b"\0")
    return digest.hexdigest()


def _pool(env: dict[str, str]) -> Path:
    raw = env.get("LAKE_CACHE_DIR", "").strip()
    if not raw:
        raise ValueError("local checkpoint requires LAKE_CACHE_DIR")
    cache = Path(raw).expanduser()
    if cache.is_symlink():
        raise ValueError(f"refusing symlinked cache storage: {cache}")
    cache.mkdir(parents=True, exist_ok=True)
    # os.access alone reports writable for root even when a fixture is 0500.
    if not cache.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError(f"cache is not writable: {cache}")
    pool = cache / "checkpoints"
    if pool.is_symlink():
        raise ValueError(f"refusing symlinked checkpoint storage: {pool}")
    pool.mkdir(exist_ok=True)
    return pool


def _source(project: Path, relative: Path, roots: list[Path]) -> Path | None:
    for root in roots:
        source = root / relative.with_suffix(".lean")
        if source.is_file():
            return source
    return None


def _completed(project: Path, since_ns: int | None = None) -> list[dict]:
    build = project / ".lake/build"
    lean = build / "lib/lean"
    roots = source_roots(project)
    modules = []
    for trace in sorted(lean.rglob("*.trace")):
        if since_ns is not None and trace.stat().st_mtime_ns < since_ns:
            continue
        relative = trace.relative_to(lean)
        source = _source(project, relative, roots)
        if source is None or source.is_symlink():
            continue
        try:
            data = json.loads(trace.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        dep_hash = data.get("depHash") if isinstance(data, dict) else None
        if not isinstance(dep_hash, str) or not dep_hash:
            continue
        # Lake only writes the trace after a successful lean invocation.  An
        # older trace left behind by an interrupted rebuild is not completion.
        required = [lean / relative.with_suffix(ext) for ext in (".olean", ".ilean")]
        required.append(build / "ir" / relative.with_suffix(".c"))
        if not all(path.is_file() and not path.is_symlink() for path in required):
            continue
        if trace.stat().st_mtime_ns < source.stat().st_mtime_ns:
            continue
        files = [trace]
        for directory in (lean, build / "ir"):
            prefix = directory / relative.with_suffix("")
            files.extend(path for path in prefix.parent.glob(prefix.name + ".*")
                         if path.is_file() and not path.is_symlink() and path != trace
                         and not path.name.endswith(".setup.json"))
        modules.append({"source": source.relative_to(project).as_posix(),
                        "source_sha256": _text_digest(source), "dep_hash": dep_hash,
                        "files": sorted(set(files))})
    return modules


def capture(project: Path, env: dict[str, str], *, since_ns: int | None = None) -> dict:
    """Save completed modules after success, failure or cancellation; never build."""
    from .workspace import cache_lock
    # Serialize same-identity retries from independent worktrees. Atomic rename
    # protects readers, while the lock prevents concurrent writers losing a
    # previously confirmed batch when they merge the same prior checkpoint.
    with cache_lock(_pool(env)):
        return _capture(project, env, since_ns=since_ns)


def _capture(project: Path, env: dict[str, str], *, since_ns: int | None = None) -> dict:
    project = Path(project).resolve()
    identity = local_identity(project)
    modules = _completed(project, since_ns)
    pool = _pool(env)
    previous_path = pool / (identity + ".zip")
    previous = None
    if previous_path.is_symlink():
        raise ValueError(f"refusing symlinked local checkpoint: {previous_path}")
    if previous_path.is_file():
        try:
            with zipfile.ZipFile(previous_path) as archive:
                previous = json.loads(archive.read("checkpoint.json"))
                if previous.get("schema") != SCHEMA or previous.get("identity") != identity \
                        or previous.get("local_only") is not True:
                    raise ValueError("local checkpoint identity/schema mismatch")
                for relative, checksum in previous["files"].items():
                    if Path(relative).is_absolute() or ".." in Path(relative).parts:
                        raise ValueError("unsafe path in local checkpoint")
                    if hashlib.sha256(archive.read(relative)).hexdigest() != checksum:
                        raise ValueError(f"corrupt local checkpoint artifact: {relative}")
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
            raise ValueError(f"corrupt local checkpoint: {previous_path}") from error
    if not modules:
        return {"identity": identity, "modules": len(previous["modules"]) if previous else 0,
                "new_modules": 0, "local_only": True}
    metadata = {"schema": SCHEMA, "identity": identity, "local_only": True,
                "created": time.time(), "modules": [], "files": {}}
    # The final path is atomically replaced; readers never observe a partial zip.
    with tempfile.NamedTemporaryFile(prefix="checkpoint-", suffix=".zip", dir=pool, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_STORED) as archive:
            new_paths = {path.relative_to(project / ".lake/build").as_posix()
                         for module in modules for path in module["files"]}
            if previous:
                # Keep previously confirmed modules when a later retry only
                # finishes a few more. New output bytes win on overlapping paths.
                new_sources = {module["source"] for module in modules}
                metadata["modules"] = [module for module in previous["modules"]
                                       if module["source"] not in new_sources]
                with zipfile.ZipFile(previous_path) as old:
                    for relative, checksum in previous["files"].items():
                        if relative not in new_paths:
                            archive.writestr(relative, old.read(relative))
                            metadata["files"][relative] = checksum
            for module in modules:
                paths = module.pop("files")
                for path in paths:
                    relative = path.relative_to(project / ".lake/build").as_posix()
                    if relative in metadata["files"]:
                        continue
                    checksum = _artifact_digest(path)
                    archive.write(path, relative)
                    # Detect a simultaneous build changing an artifact mid-copy.
                    if checksum != _artifact_digest(path):
                        raise ValueError(f"artifact changed while checkpointing: {path}")
                    metadata["files"][relative] = checksum
                metadata["modules"].append(module)
            if local_identity(project) != identity:
                raise ValueError("source inputs changed while checkpointing")
            archive.writestr("checkpoint.json", json.dumps(metadata, sort_keys=True))
        temporary_path.replace(pool / (identity + ".zip"))
    except BaseException:
        # Keep an incomplete checkpoint out of the restore namespace; normal
        # cache GC can dispose of it.  Never erase user files on an error path.
        raise
    return {"identity": identity, "modules": len(metadata["modules"]),
            "new_modules": len(modules), "local_only": True}


def restore(project: Path, env: dict[str, str]) -> dict:
    """Restore a verified local checkpoint into a fresh private .lake/build tree."""
    project = Path(project).resolve()
    identity = local_identity(project)
    path = _pool(env) / (identity + ".zip")
    result = {"identity": identity, "modules": 0, "local_only": True}
    if not path.exists():
        return result
    if path.is_symlink():
        raise ValueError(f"refusing symlinked local checkpoint: {path}")
    build = project / ".lake/build"
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read("checkpoint.json"))
            if metadata.get("schema") != SCHEMA or metadata.get("identity") != identity \
                    or metadata.get("local_only") is not True:
                raise ValueError("local checkpoint identity/schema mismatch")
            for module in metadata["modules"]:
                source = project / module["source"]
                if not source.resolve().is_relative_to(project) or _text_digest(source) != module["source_sha256"]:
                    raise ValueError("local checkpoint source mismatch")
            # Verify every byte before writing anything; corrupt caches are
            # errors, not remote cache misses and not permission fallbacks.
            for relative, expected in metadata["files"].items():
                target = build / relative
                if Path(relative).is_absolute() or ".." in Path(relative).parts \
                        or not target.resolve().is_relative_to(build.resolve()):
                    raise ValueError("unsafe path in local checkpoint")
                if hashlib.sha256(archive.read(relative)).hexdigest() != expected:
                    raise ValueError(f"corrupt local checkpoint artifact: {relative}")
            for relative in metadata["files"]:
                target = build / relative
                if target.is_symlink():
                    raise ValueError(f"refusing symlinked build artifact: {target}")
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(archive.read(relative))
            result["modules"] = len(metadata["modules"])
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as error:
        raise ValueError(f"corrupt local checkpoint: {path}") from error
    return result


def describe(project: Path, env: dict[str, str]) -> dict:
    """Explain the exact local recovery key without exposing any credentials."""
    identity = local_identity(Path(project))
    cache = Path(env["LAKE_CACHE_DIR"]).expanduser()
    path = cache / "checkpoints" / (identity + ".zip")
    return {"cache_directory": str(cache), "checkpoint": str(path),
            "checkpoint_present": path.is_file(), "identity": identity,
            "local_only": True, "publishes_revision_manifest": False}

