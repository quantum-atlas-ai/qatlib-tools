"""Shared GitHub/Gitea CI entry point driven entirely by repository profile."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qatlib_env import workspace, supervisor
from qatlib_env.envvars import env_value


def edited_build_needed(event, run_id, request, endpoint, *, workflow="build.yml"):
    """Decide whether an edited, non-draft change needs a build."""
    pull = event.get("pull_request", {})
    if pull.get("draft"):
        return False
    if event.get("action") != "edited":
        return True
    sha = pull.get("head", {}).get("sha")
    if not sha:
        return True
    payload = request(f"{endpoint}/actions/workflows/{workflow}/runs?event=pull_request&head_sha={sha}")
    rows = payload.get("workflow_runs", payload if isinstance(payload, list) else [])
    for run_record in rows:
        if run_record.get("id", 0) >= run_id or run_record.get("head_sha") != sha:
            continue
        jobs = request(f"{endpoint}/actions/runs/{run_record['id']}/jobs")
        job_rows = jobs.get("jobs", jobs if isinstance(jobs, list) else [])
        if any(job.get("name") == "build" and job.get("conclusion") != "skipped" for job in job_rows):
            return False
    return True


def build_gate(env=None):
    """Write a build decision for a light-lane CI job."""
    env = dict(os.environ if env is None else env)
    event_path = env.get("GITHUB_EVENT_PATH") or env.get("GITEA_EVENT_PATH")
    output_path = env.get("GITHUB_OUTPUT") or env.get("GITEA_OUTPUT")
    if not event_path:
        raise ValueError("CI event path is required")
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    decision = not event.get("pull_request", {}).get("draft", False)
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as stream:
            stream.write("build=" + str(decision).lower() + "\n")
    print("Build gate: " + ("build required" if decision else "draft skipped"))
    return decision


def ci_value(env, name):
    return env.get("GITHUB_" + name) or env.get("GITEA_" + name, "")


def runner_environment(project, base=None):
    env = dict(os.environ if base is None else base)
    temporary = Path(env["RUNNER_TEMP"]).resolve()
    for key, subdir in {"TMPDIR": "tmp", "TMP": "tmp", "TEMP": "tmp",
                        "XDG_DATA_HOME": "xdg-data", "QATLIB_TEST_ROOT": "tests"}.items():
        path = temporary / subdir
        path.mkdir(parents=True, exist_ok=True)
        env[key] = str(path)
    python_root = str(project / "src" if (project / "src").is_dir() else project / "python")
    env["PYTHONPATH"] = python_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


# Historical extension points retained for adapters, without built-in identities.
DEV_PACKAGES = set()
SECONDARY_PACKAGES = set()
DEV_PACKAGE_NAMES = ()
SOURCE_IMPORT_CHECKERS = ()


def dev_dependency_dir(project):
    entries = workspace.manifest(project)["packages"]
    names = tuple(p["name"].strip("«»") for p in entries if not p.get("inherited"))
    if not names:
        paths = sorted(path.name for path in (project / ".lake/packages").glob("*") if path.is_dir())
        names = tuple(paths) if len(paths) == 1 else ()
    if len(names) != 1:
        raise ValueError("dependency selection is ambiguous; use an explicit project adapter")
    return workspace.package_dir(project, names)


def source_import_gate(project):
    """Select the project's declared CLI package, or its sole source checker."""
    candidates = sorted({path for root in (project / "python", project / "src")
                         for path in root.glob("*/check_source_imports.py")})
    if not candidates:
        return None
    preferred = set()
    for configuration in (project / "pyproject.toml", project / "python/pyproject.toml"):
        if configuration.is_file():
            scripts = tomllib.loads(configuration.read_text()).get("project", {}).get("scripts", {})
            preferred.update(value.split(":", 1)[0].split(".", 1)[0] for value in scripts.values()
                             if isinstance(value, str))
    selected = [path for path in candidates if path.parent.name in preferred]
    if len(selected) == 1:
        checker = selected[0]
    elif len(candidates) == 1:
        checker = candidates[0]
    else:
        raise ValueError("multiple source-import checkers; declare a unique project CLI package")

    dependencies = []
    manifest = project / "lake-manifest.json"
    if manifest.is_file():
        for dependency in workspace.manifest(project)["packages"]:
            name = dependency["name"].strip("«»")
            dependency_path = project / workspace.manifest(project).get("packagesDir", ".lake/packages") / name
            if (dependency_path / "lakefile.toml").is_file():
                dependencies += ["--dependency-repo", str(dependency_path)]

    return [sys.executable, "-B", str(checker), "--repo", ".", *dependencies]


def source_imports(project, env=None):
    """Run the configured source-import checker for a downstream workspace.

    This compatibility entry point keeps the historical call shape while
    delegating checker selection to the generic ``source_import_gate``.
    """
    project = Path(project).resolve()
    command = source_import_gate(project)
    if command is None:
        return None
    return workspace.run(command, cwd=project, env=env)


def profile(package, env=None, project=None):
    values = os.environ if env is None else env
    from .config import load_config
    configured = load_config(project, values).metadata if project is not None else {}
    library = str(configured.get("library", values.get("QATLIB_LIBRARY", package))).strip()
    tests = str(configured.get("tests", values.get("QATLIB_TEST_TARGET", ""))).strip()
    repository = str(configured.get("cache_repository", configured.get("repository", values.get("QATLIB_REPOSITORY", "")))).strip()
    if not repository and project is not None:
        from .project import repository_slug
        repository = repository_slug(project)
    if "://" in repository:
        from urllib.parse import urlsplit
        repository = urlsplit(repository).path.strip("/").removesuffix(".git")
    return library, [library, tests] if tests else [library], repository


def write_key(package, env):
    # CI consumes injected credentials only; local key-file/prompt resolution
    # belongs to the explicit cache-upload command, with the same variable map.
    from qatlib_env.r2_cache import credential_variables
    return next((env.get(name, "").strip() for name in credential_variables(package, env)
                 if env.get(name, "").strip()), "")


def source_inputs(project, library):
    config = tomllib.loads((project / "lakefile.toml").read_text()) if (project / "lakefile.toml").is_file() else {}
    if config.get("lean_lib"):
        from .project import libraries
        paths = []
        for lib in libraries(project):
            for root in lib.roots:
                path = lib.src_dir / root.replace(".", "/")
                for candidate in (path, path.with_suffix(".lean")):
                    if (project / candidate).exists():
                        paths.append(candidate.as_posix())
    else:
        paths = [library, library + ".lean"]
        if (project / "verification").is_dir():
            paths.append("verification")
    return list(dict.fromkeys([*paths, "lakefile.toml", "lean-toolchain", "lake-manifest.json"]))


def input_tree(project, library):
    paths = source_inputs(project, library)
    data = subprocess.check_output(["git", "rev-parse", *("HEAD:" + p for p in paths)], cwd=project)
    return hashlib.sha256(data).hexdigest()


def toolchain_dir(raw):
    return raw.replace("/", "--").replace(":", "---")


def _source_dependency(project, env):
    entries = workspace.manifest(project)["packages"]
    selected = env.get("QATLIB_SOURCE_PACKAGE", "").strip()
    if selected:
        matches = [p for p in entries if p["name"].strip("«»") == selected]
        if len(matches) != 1:
            raise ValueError("QATLIB_SOURCE_PACKAGE must name one locked dependency")
        return matches[0]
    direct = [p for p in entries if not p.get("inherited")]
    return direct[0] if len(direct) == 1 else None


def public_repo(project, env):
    """Select checked-out sources at the locked revision for read-only gates.

    A usable object mirror may have no checkout, while a shallow dependency
    checkout is sufficient for source validation. Do not conflate the two.
    """
    entry = _source_dependency(project, env)
    if entry is None:
        return None
    locked, name = entry["rev"], entry["name"].strip("«»")
    roots = [project]
    mirror = env_value(env, "QATLIB_PKG_MIRROR", None)
    if mirror:
        roots.append(Path(mirror))
    candidates = []
    for root in roots:
        candidate = root / workspace.manifest(project).get("packagesDir", ".lake/packages") / name
        candidates.append(str(candidate))
        if source_checkout(candidate, locked, env):
            return candidate
    raise ValueError(
        f"{name} {locked}: source checks require materialized Lean sources at the locked HEAD; "
        f"no matching checkout found in {', '.join(candidates)}"
    )


def source_checkout(candidate, locked, env):
    """Materialized locked sources are usable even from a tree-filtered clone."""
    config = tomllib.loads((candidate / "lakefile.toml").read_text()) if (candidate / "lakefile.toml").is_file() else {}
    if config.get("lean_lib"):
        from .project import libraries
        roots = [candidate / lib.src_dir / root.replace(".", "/")
                 for lib in libraries(candidate) for root in lib.roots]
    else:
        roots = [path for path in candidate.glob("*") if not path.name.startswith(".")]
    if not any((root.is_file() and root.suffix == ".lean") or
               (root.is_dir() and any(root.rglob("*.lean"))) for root in roots):
        return False
    result = supervisor.run(["git", "-C", str(candidate), "rev-parse", "--show-toplevel", "HEAD"],
                            env={**env, "GIT_NO_LAZY_FETCH": "1"}, capture_output=True, text=True)
    lines = result.stdout.splitlines()
    return result.returncode == 0 and len(lines) == 2 \
        and Path(lines[0]).resolve() == candidate.resolve() and lines[1] == locked


def bibliography(project, env):
    entry = _source_dependency(project, env)
    if entry is None:
        return None
    locked, name = entry["rev"], entry["name"].strip("«»")
    mirror = env_value(env, "QATLIB_PKG_MIRROR", None)
    roots = [Path(mirror)] if mirror else []
    for root in [project, *roots]:
        candidate = root / workspace.manifest(project).get("packagesDir", ".lake/packages") / name
        bib = candidate / "docs/references.bib"
        file_at_rev = supervisor.run(
            ["git", "-C", str(candidate), "cat-file", "-e", f"{locked}:docs/references.bib"],
            env={**env, "GIT_NO_LAZY_FETCH": "1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) if locked else None
        if locked and bib.is_file() and file_at_rev and file_at_rev.returncode == 0 \
                and source_checkout(candidate, locked, env):
            return bib
    return None


def metadata_checks(run, public, module):
    flags = ["--public-repo", str(public)] if public else []
    for check, option in [("check_meta_v2", "--strict"), ("gen_v2_docs", "--check")]:
        run([sys.executable, "-B", "-m", module + "." + check, "--repo", ".", option, *flags])


def mirror_main(project, env):
    key = env.get("GITEA_MIRROR_SSH_KEY", "").strip()
    if not key or ci_value(env, "EVENT_NAME") != "push" or ci_value(env, "REF") != "refs/heads/main":
        return
    configured = {}
    for path in (Path(project) / "qatlib-env.toml", Path(project) / "pyproject.toml", Path(project) / ".qatlib-env.local.toml"):
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = raw.get("tool", {}).get("qatlib-env", {}) if path.name == "pyproject.toml" else raw.get("tool", {}).get("qatlib-env", raw)
        if isinstance(section, dict):
            configured.update(section)
    mirror_url = env.get("GITEA_MIRROR_URL", "").strip() or str(configured.get("gitea_mirror_url", "")).strip()
    known_value = env.get("GITEA_KNOWN_HOST", "").strip() or str(configured.get("gitea_known_host", "")).strip()
    known_file = env.get("SSH_KNOWN_HOSTS", "").strip()
    if not mirror_url:
        print("Gitea mirror skipped: GITEA_MIRROR_URL is unset", file=sys.stderr)
        return
    if not known_value and not known_file:
        print("Gitea mirror skipped: GITEA_KNOWN_HOST or SSH_KNOWN_HOSTS is unset", file=sys.stderr)
        return
    try:
        with tempfile.TemporaryDirectory(dir=env["TMPDIR"]) as directory:
            path = Path(directory) / "key"
            path.write_text(key + "\n")
            path.chmod(0o600)
            known = Path(known_file) if known_file else Path(directory) / "known_hosts"
            if known_value:
                known.write_text(known_value.rstrip() + "\n")
            command = shlex.join(["ssh", "-i", str(path), "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=15",
                                  "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(known)])
            workspace.run(["git", "push", "--force", mirror_url,
                           "HEAD:refs/heads/main", "refs/tags/*:refs/tags/*"], cwd=project,
                          env={**env, "GIT_SSH_COMMAND": command})
    except (OSError, subprocess.CalledProcessError) as error:
        print("::warning::Gitea mirror failed: " + str(error), file=sys.stderr)


def lint(project, env):
    from qatlib_env.r2_cache import package_environment
    package = workspace.package_name(project)
    env = package_environment(package, env, project=project)
    workspace.ensure_pkg_mirror(project, env)
    def run(command):
        workspace.run(command, cwd=project, env=env)
    source_gate = source_import_gate(project)
    if source_gate:
        run(source_gate)
    run(["git", "diff", "--check"])
    adapters = sorted(path.parent for path in (project / "python").glob("*/check_meta_v2.py"))
    if len(adapters) > 1:
        raise ValueError("multiple metadata adapters; select checks in the project CI entry point")
    metadata = adapters[0].name if adapters else None
    if metadata and (adapters[0] / "ci_static.py").is_file():
        run([sys.executable, "-B", "-m", metadata + ".ci_static"])
    if (project / "python/tests").is_dir():
        run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "python/tests", "-p", "test_*.py"])
    if shutil.which("lake", path=env.get("PATH")):
        run(["lake", "lint"])
    if metadata:
        if (adapters[0] / "check_dev_imports.py").is_file():
            run([sys.executable, "-B", "-m", metadata + ".check_dev_imports"])
        public = public_repo(project, env)
        if public:
            run([sys.executable, "-B", "-m", metadata + ".check_promoted_duplicates", "--repo", ".",
                 "--public-repo", str(public)])
        bib = bibliography(project, env)
        run([sys.executable, "-B", "-m", metadata + ".check_references", *(["--bib", str(bib)] if bib else [])])
        metadata_checks(run, public, metadata)
    mirror_main(project, env)


def cache_service(project, env, package):
    config = tomllib.loads((project / "lake-cache.toml").read_text())["cache"]
    service = next(s for s in config["service"] if s["name"] == config["defaultService"])
    key = write_key(package, env) or service.get("s3Auth", "").strip()
    return service["revisionEndpoint"].rstrip("/"), key


@contextmanager
def authenticated(env, key):
    from qatlib_env.r2_cache import native_key_environment
    with tempfile.TemporaryDirectory(dir=env["TMPDIR"]) as directory:
        path = Path(directory) / ".curlrc"
        options = 'connect-timeout = 15\nmax-time = 120\nretry = 3\nretry-all-errors\n'
        if key:
            options += 'aws-sigv4 = "aws:amz:auto:s3"\nuser = ' + json.dumps(key) + '\n'
        path.write_text(options, encoding="utf-8", newline="\n")
        path.chmod(0o600)
        yield {**native_key_environment(env, key), "CURL_HOME": directory}


def select_candidate(sha, tree, ancestors, fetch):
    for key, kind in [(sha + ".jsonl", "exact"), ("inputs/" + tree + ".jsonl", "exact"),
                      *((ancestor + ".jsonl", "ancestor") for ancestor in ancestors)]:
        if fetch(key):
            return kind
    return "none"


def _manifest_rows(path):
    """Read the valid mapping rows from a Lake JSONL manifest."""
    if not path.is_file():
        return "", []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
             if line.strip()]
    if not lines:
        return "", []
    rows = []
    for line in lines[1:]:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, list) and len(row) >= 2 and isinstance(row[0], str) and row[0]:
            rows.append(row)
    return lines[0], rows


def manifest_is_usable(path):
    """Reject Lake's header-only (13-byte) manifest as a cache candidate."""
    _header, rows = _manifest_rows(path)
    return bool(rows)


def _ltar_descr(value):
    """Extract a plain ``<hash>.ltar`` descriptor from Lake cache JSON."""
    if isinstance(value, dict):
        value = value.get("ltar")
    if isinstance(value, str) and value.endswith(".ltar") and Path(value).name == value:
        return value
    return None


def _manifest_map(path):
    _header, rows = _manifest_rows(path)
    result = {}
    for row in rows:
        descr = _ltar_descr(row[1])
        if descr:
            result[row[0]] = descr
    return result


def _cache_output_map(cache):
    """Read local ``outputs/<scope>/<depHash>.json`` entries.

    Lake 4.35.0 has emitted both ``data: "hash.ltar"`` and
    ``data: {"ltar": "hash.ltar", ...}`` forms over its supported cache
    formats, so accept both here.
    """
    result = {}
    outputs = cache / "outputs"
    if not outputs.is_dir():
        return result
    for path in outputs.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        descr = _ltar_descr(payload.get("data"))
        if descr:
            result[path.stem] = descr
    return result


def _root_traces(project, build):
    """Return traces for modules belonging to ``project``'s root package only."""
    lean = build / "lib/lean"
    if not lean.is_dir():
        return []
    traces = []
    for trace in sorted(lean.rglob("*.trace")):
        relative = trace.relative_to(lean)
        config_file = project / "lakefile.toml"
        config = tomllib.loads(config_file.read_text()) if config_file.is_file() else {}
        roots = [project, *(project / lib.get("srcDir", config.get("srcDir", "."))
                            for lib in config.get("lean_lib", []))]
        source = next((root / relative.with_suffix(".lean") for root in roots
                       if (root / relative.with_suffix(".lean")).is_file()), project / relative.with_suffix(".lean"))
        # Dependencies have their own .lake/build tree.  The source check also
        # excludes stale traces left for deleted root-package modules.
        if not source.is_file() or ".lake/packages" in trace.parts:
            continue
        # A hot runner can retain an orphan trace after its other outputs were
        # cleaned.  It was not covered by this build target and cannot be
        # staged as an archive, so require at least one real build artifact.
        if not (lean / relative.with_suffix(".olean")).is_file() \
                and not (build / "ir" / relative.with_suffix(".c")).is_file() \
                and not (build / "ir" / relative.with_suffix(".ltar")).is_file():
            continue
        try:
            payload = json.loads(trace.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        dep_hash = payload.get("depHash")
        if isinstance(dep_hash, str) and dep_hash:
            traces.append((trace, source, dep_hash))
    return traces


def _archive_hash(path, project, env=None):
    """Return Lake's 16-hex content hash, using its sidecar or Lean fallback."""
    sidecar = Path(str(path) + ".hash")
    if sidecar.is_file():
        value = sidecar.read_text(encoding="utf-8").strip()
        if len(value) == 16 and all(c in "0123456789abcdefABCDEF" for c in value):
            return value.lower() + ".ltar"
    # A sidecar is normally written by Lake.  Keep a fallback for historical
    # unpacked caches that have an archive but lost its .hash file.
    with tempfile.TemporaryDirectory(prefix="qatlib-lake-hash-") as directory:
        helper = Path(directory) / "hash.lean"
        helper.write_text(
            "import Lake\n"
            "open Lake System\n"
            "#eval show IO Unit from do\n"
            "  let file? ← IO.getEnv \"HASH_FILE\"\n"
            "  match file? with\n"
            "  | some file =>\n"
            "    let h ← computeBinFileHash (FilePath.mk file)\n"
            "    IO.println h.hex\n"
            "  | none => pure ()\n",
            encoding="utf-8",
        )
        try:
            value = workspace.run(
                ["lake", "--no-build", "lean", str(helper)], cwd=project,
                env={**(os.environ if env is None else env), "HASH_FILE": str(path)}, capture=True,
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    if len(value) == 16 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower() + ".ltar"
    return None


def _optional_build(command, project, env):
    try:
        workspace.run(command, cwd=project, env=env)
    except subprocess.CalledProcessError:
        pass


def _ensure_cached_ltar(project, build, cache, trace, source, descr, env):
    """Ensure the descriptor named by a mapping has a real cache artifact."""
    artifact = cache / "artifacts" / descr
    if artifact.is_file():
        return
    lean = build / "lib/lean"
    archive = build / "ir" / trace.relative_to(lean).with_suffix(".ltar")
    if not archive.is_file():
        # A historical cache can contain only scattered outputs.  Rebuilding
        # this root module with cache downloads disabled makes Lake run
        # ``packLtar`` and leaves the archive in the normal IR location.
        module = source.relative_to(project).as_posix()
        local_env = {**os.environ, **(env or {}), "LAKE_ARTIFACT_CACHE": "true"}
        _optional_build(["lake", "--no-cache", "build", module + ":ltar"], project, local_env)
        if not archive.is_file():
            module_name = ".".join(trace.relative_to(lean).with_suffix("").parts)
            _optional_build(["lake", "--no-cache", "build", "+" + module_name + ":ltar"], project, local_env)
    if not archive.is_file():
        _pack_ltar_direct(project, build, trace, env)
    if not archive.is_file():
        raise ValueError(f"missing .ltar for root module {source}: {descr}")
    cache_artifacts = cache / "artifacts"
    cache_artifacts.mkdir(parents=True, exist_ok=True)
    try:
        os.link(archive, artifact)
    except FileExistsError:
        pass
    except OSError:
        shutil.copy2(archive, artifact)


def _pack_ltar_direct(project, build, trace, env=None):
    """Pack an archive directly when a historical cache has only loose outputs."""
    lean = build / "lib/lean"
    ir = build / "ir"
    relative = trace.relative_to(lean)
    archive = ir / relative.with_suffix(".ltar")
    required = [lean / relative.with_suffix(ext) for ext in (".olean", ".ilean")]
    required.append(ir / relative.with_suffix(".c"))
    if not all(path.is_file() for path in required):
        return
    archive.parent.mkdir(parents=True, exist_ok=True)
    args = ["lake", "env", "leantar", "-s", "-C", str(lean), "-C", str(ir), str(archive),
            str(relative.with_suffix(".trace"))]
    for ext in (".olean", ".olean.server", ".olean.private", ".ilean", ".ir.sig", ".ir"):
        if (lean / relative.with_suffix(ext)).is_file():
            args += ["-i", "0", str(relative.with_suffix(ext))]
    for ext in (".c", ".bc"):
        if (ir / relative.with_suffix(ext)).is_file():
            args += ["-i", "1", str(relative.with_suffix(ext))]
    _optional_build(args, project, dict(os.environ if env is None else env))


def complete_outputs_manifest(project, output, cache, candidate=None, env=None):
    """Repair Lake 4.35.0's header-only ``-o`` output for platform-independent packages.

    The manifest is reconstructed from root-package traces, the remote candidate,
    and local cache output records.  Every final mapping is checked against a
    real artifact in ``$LAKE_CACHE_DIR/artifacts`` before it is written.
    """
    header, rows = _manifest_rows(output)
    if not header:
        header = "2026-03-17"
    mapping = {}
    for row in rows:
        descr = _ltar_descr(row[1])
        if descr:
            mapping[row[0]] = descr
    candidate_map = _manifest_map(candidate) if candidate and candidate.is_file() else {}
    local_map = _cache_output_map(cache)
    traces = _root_traces(project, build := project / ".lake/build")
    if not traces:
        # Unit-test/fake build directories and targets that produce no Lean
        # modules have nothing to repair; preserve Lake's original bytes.
        return len(mapping)
    for trace, source, dep_hash in traces:
        descr = mapping.get(dep_hash) or candidate_map.get(dep_hash) or local_map.get(dep_hash)
        archive = build / "ir" / trace.relative_to(build / "lib/lean").with_suffix(".ltar")
        if not descr and not archive.is_file():
            # A stale local cache may have only scattered outputs.  Materialize
            # this module once with cache downloads disabled so Lake runs
            # ``packLtar`` before we try to infer its descriptor.
            module = source.relative_to(project).as_posix()
            local_env = {**os.environ, **(env or {}), "LAKE_ARTIFACT_CACHE": "true"}
            _optional_build(["lake", "--no-cache", "build", module + ":ltar"], project, local_env)
            if not archive.is_file():
                module_name = ".".join(trace.relative_to(build / "lib/lean").with_suffix("").parts)
                _optional_build(["lake", "--no-cache", "build", "+" + module_name + ":ltar"], project, local_env)
            if not archive.is_file():
                _pack_ltar_direct(project, build, trace, env)
            if archive.is_file():
                descr = _ltar_descr(_archive_hash(archive, project, env))
        if not descr and archive.is_file():
            descr = _ltar_descr(_archive_hash(archive, project, env))
        if not descr:
            raise ValueError(f"no .ltar descriptor for root module {source} ({dep_hash})")
        mapping[dep_hash] = descr
        _ensure_cached_ltar(project, build, cache, trace, source, descr, env)
    if traces and len(mapping) < len(traces):
        raise ValueError(
            f"incomplete root-package output manifest: {len(mapping)} mappings for {len(traces)} traces"
        )
    if traces and any(dep_hash not in mapping for _trace, _source, dep_hash in traces):
        raise ValueError("root-package trace missing from final output manifest")
    if traces and not mapping:
        raise ValueError("root package has built modules but output manifest is empty")
    output.write_text(
        header + "\n" + "\n".join(
            json.dumps([dep_hash, mapping[dep_hash]], separators=(",", ":"))
            for dep_hash in sorted(mapping)
        ) + ("\n" if mapping else ""),
        encoding="utf-8",
    )
    return len(mapping)


def restore_ltars(candidate, cache, build):
    ltars = {}
    if candidate.is_file():
        for line in candidate.read_text().splitlines()[1:]:
            if line.strip():
                key, value, *_ = json.loads(line)
                descr = _ltar_descr(value)
                if descr:
                    ltars[key] = descr
    lean = build / "lib/lean"
    for trace in lean.rglob("*.trace"):
        dep_hash = json.loads(trace.read_text()).get("depHash")
        ltar = ltars.get(dep_hash)
        if not ltar and dep_hash:
            for mapping in (cache / "outputs").rglob(dep_hash + ".json"):
                value = json.loads(mapping.read_text()).get("data")
                ltar = _ltar_descr(value)
                if ltar:
                    break
        if not ltar:
            continue
        src = cache / "artifacts" / ltar
        dst = build / "ir" / trace.relative_to(lean).with_suffix(".ltar")
        if src.is_file() and not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(src, dst)
            except OSError:
                shutil.copy2(src, dst)


def compile_targets(project, env, targets, kind, candidate):
    output = project / ".lake/outputs.jsonl"
    command = ["lake", "--no-build", "build", *targets]
    if kind == "exact" and supervisor.run(
        command, cwd=project, env=workspace.command_runtime_environment(project, env, command),
        timeout=float(env_value(env, "QATLIB_STAGE_TIMEOUT", "10800")) or None,
        module_timeout=float(env_value(env, "QATLIB_MODULE_TIMEOUT", "1800")) or None
    ).returncode == 0:
        shutil.copyfile(candidate, output)
    else:
        restore_ltars(candidate, Path(env["LAKE_CACHE_DIR"]), project / ".lake/build")
        workspace.run(["lake", "--fail-fast", "build", *targets, "-o", ".lake/outputs.jsonl"],
                      cwd=project, env=env)
    if not output.is_file() or not output.stat().st_size:
        raise ValueError("build produced no output manifest")
    complete_outputs_manifest(project, output, Path(env["LAKE_CACHE_DIR"]), candidate, env)
    if _root_traces(project, project / ".lake/build") and not manifest_is_usable(output):
        raise ValueError("build produced an unusable output manifest")
    return output


def delta_manifest(output, candidate):
    lines = output.read_text().splitlines()
    previous = set(candidate.read_text().splitlines()[1:]) if candidate.is_file() else set()
    delta = sorted(set(lines[1:]) - previous)
    return "\n".join([lines[0], *delta]) + "\n", len(delta)


def may_publish(env, package=None):
    if package is None:
        return False
    key = write_key(package, env)
    return (ci_value(env, "REF") == "refs/heads/main" and
            ci_value(env, "EVENT_NAME") in {"push", "workflow_dispatch"} and bool(key))


def build(project, env):
    from qatlib_env.r2_cache import package_environment, native_key_environment
    package = workspace.package_name(project)
    env = package_environment(package, env, project=project)
    env = workspace.environment(project, base_env=env)
    workspace.ensure_pkg_mirror(project, env)
    env["LAKE_CONFIG"] = str(project / "lake-cache.toml")
    def run(command):
        workspace.run(command, cwd=project, env=env)
    source_gate = source_import_gate(project)
    if source_gate:
        run(source_gate)
    library, targets, repo_slug = profile(package, env, project)
    from qatlib_env.prepare import prepare
    prepare(project, env)
    sha = workspace.git_head(project)
    tree = input_tree(project, library)
    tc_raw = env["ELAN_TOOLCHAIN"]
    tc_dir = toolchain_dir(tc_raw)
    endpoint, key = cache_service(project, env, package)
    canonical_base = endpoint + "/" + repo_slug + "/tc/" + tc_dir
    with tempfile.TemporaryDirectory(dir=env["TMPDIR"]) as directory:
        candidate = Path(directory) / "candidate.jsonl"
        cached = project / ".lake/qatlas-root-cache.jsonl"
        state = project / ".lake/qatlas-root-cache-kind.json"
        kind = "none"
        if cached.is_file() and state.is_file():
            from qatlib_env.prepare import identity
            details = json.loads(state.read_text())
            if details.get("identity") == identity(project, env) and manifest_is_usable(cached):
                shutil.copyfile(cached, candidate)
                kind = details["kind"]
        print("CACHE_HIT kind=" + kind, flush=True)
        output = compile_targets(project, env, targets, kind, candidate)
        run(["lake", "lint"])
        run(["lake", "test"])
        if env.get("CI_BASE_SHA") and ci_value(env, "EVENT_NAME") == "pull_request":
            run([sys.executable, "-B", "-m", "qatlib_env.check_scoped_env_lint", "--base", env["CI_BASE_SHA"]])
        if may_publish(env, package):
            selected_key = write_key(package, env)
            publish_env = native_key_environment(env, selected_key)
            delta, count = delta_manifest(output, candidate)
            print(f"CACHE_DELTA entries={count}", flush=True)
            delta_file = Path(directory) / "delta.jsonl"
            delta_file.write_text(delta)
            if count:
                stage = Path(directory) / "staging"
                run(["lake", "cache", "stage", str(delta_file), str(stage)])
                workspace.run(["lake", "cache", "put-staged", str(stage), "--repo=" + repo_slug,
                     "--toolchain=" + tc_raw,
                     "--rev=delta-" + sha + "-" + (ci_value(env, "RUN_ID") or uuid.uuid4().hex)],
                     cwd=project, env=publish_env)
            with authenticated(env, selected_key) as auth:
                for relative in ["inputs/" + tree + ".jsonl", sha + ".jsonl"]:
                    workspace.run(["curl", "-fsS", "-X", "PUT", "-T", str(output), "-H",
                                   "Content-Type: application/vnd.reservoir.outputs+json-lines",
                                   canonical_base + "/" + relative], cwd=project, env=auth)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="qatlib-env ci", description=__doc__)
    parser.add_argument("command", choices=("lint", "build", "build-gate", "mirror"))
    parser.add_argument("--allow-local-heavy", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "build-gate":
        build_gate(os.environ)
        return 0
    project = Path.cwd().resolve()
    from qatlib_env.budget import admit, permission
    if args.command in {"lint", "build"}:
        admit(project, full_gate=True, allow_local_heavy=args.allow_local_heavy)
    temporary = os.environ.get("RUNNER_TEMP")
    with permission(args.allow_local_heavy, full_gate=True), (nullcontext(temporary) if temporary else tempfile.TemporaryDirectory(prefix="qatlas-ci-")) as directory:
        base_env = runner_environment(project, {**os.environ, "RUNNER_TEMP": directory})
        if args.command == "mirror":
            mirror_main(project, base_env)
        else:
            env = workspace.environment(project, base_env=base_env)
            (lint if args.command == "lint" else build)(project, env)


def cli_main(argv=None):
    try:
        main(argv)
        return 0
    except supervisor.ProcessCancelled as error:
        print("CI_CANCELLED: " + str(error), file=sys.stderr)
        return error.returncode
    except subprocess.TimeoutExpired as error:
        print("CI_TIMEOUT: " + str(error), file=sys.stderr)
        return 124
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print("CI_FAILED: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())


