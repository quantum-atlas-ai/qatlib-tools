"""Private Lean workspaces backed by Lake's shared artifact cache (Python 3.11)."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from urllib.parse import quote, unquote, urlparse

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qatlib_env import resources, supervisor

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no POSIX flock.
    fcntl = None

ENV_KEYS = ("ELAN_TOOLCHAIN", "LAKE_CACHE_DIR", "LAKE_ARTIFACT_CACHE",
            "LEAN_NUM_THREADS", "MATHLIB_NO_CACHE_ON_UPDATE")
ARTIFACT = re.compile(r"[0-9a-f]{16}\.[A-Za-z0-9.]+\Z")
GIB = 1024 ** 3
MEMORY_PER_JOB = 4 * GIB


def _is_lake(argv):
    return bool(argv) and Path(argv[0]).stem.casefold() == "lake"


def command_runtime_environment(project, env, argv):
    effective = dict(os.environ if env is None else env)
    if _is_lake(argv):
        effective = command_environment(project, effective, argv)
    executable = Path(argv[0]).stem if argv else ""
    if effective.get("QATLAS_LEAN_THREADS") and (_is_lake(argv) or executable.startswith(("python", "pypy")) or executable in {"bash", "sh"}):
        from qatlib_env.compiler import environment as compiler_environment
        effective = compiler_environment(project, effective)
    return effective


@contextmanager
def _pool_scope(effective, *, building):
    """Admit builds to the machine pool and renew the lease while running."""
    if not building or effective.get("QATLAS_POOL_BYPASS") == "1":
        yield
        return
    from qatlib_env.pool import Pool
    raw_memory = int(float(effective.get("QATLAS_MEMORY_BUDGET", 4 * GIB)))
    memory_mib = max(2048, raw_memory // (1024 * 1024) if raw_memory > 65536 else raw_memory)
    cpu = max(1, int(effective.get("QATLAS_JOBS", effective.get("LEAN_NUM_THREADS", "1"))))
    pool = Pool(budget_mib=max(memory_mib, int(effective.get("QATLAS_POOL_BUDGET_MIB", "24576"))),
                budget_cpu=max(cpu, int(effective.get("QATLAS_POOL_CPU", "4"))))
    deadline = time.monotonic() + float(effective.get("QATLAS_POOL_WAIT_TIMEOUT", "300"))
    lease = None
    while lease is None:
        try:
            lease = pool.acquire(memory_mib, cpu, owner=str(Path.cwd()))
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise TimeoutError("local build pool admission timed out")
            time.sleep(min(1.0, max(0.05, deadline - time.monotonic())))
    stop = threading.Event()
    def renew():
        while not stop.wait(max(1.0, pool.ttl / 3)):
            try:
                pool.heartbeat(lease)
            except RuntimeError:
                return
    heartbeat = threading.Thread(target=renew, name="qatlib-pool-heartbeat", daemon=True)
    heartbeat.start()
    try:
        yield
    finally:
        stop.set(); heartbeat.join(timeout=2)
        pool.release(lease)


def _backend_command(argv, effective, *, building):
    if not building or effective.get("QATLAS_USE_SYSTEMD_SCOPE", "1") != "1":
        return list(argv)
    raw_memory = int(float(effective.get("QATLAS_MEMORY_BUDGET", 4 * GIB)))
    memory_mib = max(2048, raw_memory // (1024 * 1024) if raw_memory > 65536 else raw_memory)
    cpu = max(1, int(effective.get("QATLAS_JOBS", effective.get("LEAN_NUM_THREADS", "1"))))
    if sys.platform.startswith("linux"):
        from qatlib_env.backends.linux import linux_command
        return linux_command(list(argv), memory_mib=memory_mib, cpu=cpu,
                             unit="qatlib-lean-build")
    if sys.platform == "darwin":
        from qatlib_env.backends.macos import macos_limits
        return macos_limits(list(argv), qos=effective.get("QATLIB_MACOS_QOS", "utility"))
    return list(argv)


def run(argv, *, cwd, env=None, capture=False):
    """Supervise every command, including Python/shell drivers spawning Lake."""
    project = Path(cwd)
    effective = command_runtime_environment(project, env, argv)
    building = lake_build_command(argv) and "--no-build" not in argv
    build_started_ns = time.time_ns() if building else None
    command = _backend_command(argv, effective, building=building)
    job = None
    process_callback = None
    if building and sys.platform == "win32" and effective.get("QATLAS_USE_JOB_OBJECT", "1") == "1":
        from qatlib_env.backends.windows import JobObject
        job = JobObject("qatlib-lean-build")
        raw_memory = int(float(effective.get("QATLAS_MEMORY_BUDGET", 4 * GIB)))
        memory_bytes = raw_memory if raw_memory > 65536 else raw_memory * 1024 * 1024
        cpu_percent = max(1, int(effective.get("QATLAS_CPU_PERCENT", "100")))
        job.configure(memory_bytes=max(2048 * 1024 * 1024, memory_bytes), cpu_percent=cpu_percent)
        process_callback = lambda child: job.attach_pid(child.pid)
    try:
        with _pool_scope(effective, building=building):
            return supervisor.run(
                command, cwd=cwd, env=effective, check=True, text=True,
                stdout=subprocess.PIPE if capture else None,
                timeout=float(effective.get("QATLAS_STAGE_TIMEOUT", "10800")) or None,
                module_timeout=float(effective.get("QATLAS_MODULE_TIMEOUT", "1800")) or None,
                process_callback=process_callback,
            ).stdout
    finally:
        if job is not None:
            job.close()
        if building and (project / "lean-toolchain").is_file():
            from qatlib_env import checkpoint
            try:
                result = checkpoint.capture(project, effective, since_ns=build_started_ns)
                print("LOCAL_CHECKPOINT " + json.dumps(result), file=sys.stderr)
            except Exception as error:
                # Preserve the build/cancellation exception; never publish here.
                print("LOCAL_CHECKPOINT_FAILED: " + str(error), file=sys.stderr)


def package_name(project):
    return tomllib.loads((project / "lakefile.toml").read_text())["name"]


def manifest(project):
    data = json.loads((project / "lake-manifest.json").read_text())
    if data.get("packagesDir", ".lake/packages") != ".lake/packages":
        raise ValueError("dependencies must use private .lake/packages")
    for pkg in data["packages"]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", pkg["name"].strip("«»")):
            raise ValueError("invalid dependency name")
    return data


def package_dir(project, names):
    """Resolve a dependency directory from the lockfile before stale paths."""
    lockfile = project / "lake-manifest.json"
    if lockfile.is_file():
        data = json.loads(lockfile.read_text())
        for package in data.get("packages", []):
            name = package.get("name", "").strip("«»")
            if name in names:
                return project / ".lake/packages" / name
    for name in names:
        candidate = project / ".lake/packages" / name
        if candidate.exists():
            return candidate
    return project / ".lake/packages" / names[0]


def package_roots(project):
    return [project, *(project / ".lake/packages" / p["name"].strip("«»")
                       for p in manifest(project)["packages"])]


def redirected_directory(path):
    if path.is_symlink():
        return True
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) &
                    getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def primary_checkout(project):
    result = supervisor.run(["git", "rev-parse", "--git-common-dir"], cwd=project,
                            text=True, capture_output=True)
    return (project / result.stdout.strip()).resolve().parent if result.returncode == 0 else project


def agents_local_path(project):
    """Find local preferences, falling back to the primary checkout and home."""
    project = Path(project).resolve()
    local = project / "AGENTS.local.md"
    if local.is_file():
        return local
    result = supervisor.run(["git", "-C", str(project), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip())
        if not common.is_absolute():
            common = project / common
        primary = common.resolve().parent / "AGENTS.local.md"
        if primary.is_file():
            return primary
    global_path = Path.home() / ".agents" / "AGENTS.local.md"
    return global_path if global_path.is_file() else None


def candidate_sources(project, source, env):
    roots = [source, env.get("QATLAS_SOURCE_ROOT"), env.get("QATLAS_PKG_MIRROR"),
             primary_checkout(project), *sorted(project.parent.iterdir())]
    return list(dict.fromkeys(Path(p).expanduser().absolute() for p in roots
                              if p and Path(p).is_dir() and Path(p).expanduser().absolute() != project.absolute()))


def source_packages(root, name):
    if (root / "lakefile.toml").is_file() and package_name(root).strip("«»") == name:
        yield root
    yield root / ".lake/packages" / name


def git_head(path):
    result = supervisor.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _git_common_dir(path: Path, env: dict[str, str]):
    result = supervisor.run(["git", "-C", str(path), "rev-parse", "--git-common-dir"],
                            env=env, capture_output=True, text=True)
    if result.returncode:
        return None
    raw = Path(result.stdout.strip())
    return (path / raw if not raw.is_absolute() else raw).resolve()


def _local_origin(path: Path, env: dict[str, str]):
    result = supervisor.run(["git", "-C", str(path), "config", "--get", "remote.origin.url"],
                            env=env, capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        return None
    value = result.stdout.strip()
    if value.startswith("file://"):
        from urllib.parse import unquote, urlparse
        parsed = urlparse(value)
        if parsed.netloc not in ("", "localhost"):
            return None
        return Path(unquote(parsed.path)).expanduser().resolve()
    if "://" in value or (":" in value and not value.startswith(("./", "../", "/"))):
        return None
    local = Path(value)
    return (path / local if not local.is_absolute() else local).expanduser().resolve()


def _local_url(value: str, base: Path):
    """Resolve a manifest URL only when it names a local filesystem path."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("file://"):
        parsed = urlparse(value)
        if parsed.netloc not in ("", "localhost"):
            return None
        return Path(unquote(parsed.path)).expanduser().resolve()
    if "://" in value or (":" in value and not value.startswith(("./", "../", "/"))):
        return None
    return (base / value if not Path(value).is_absolute() else Path(value)).expanduser().resolve()


def _git_result(args, *, env, timeout=300, cwd=None, check=False):
    return supervisor.run(["git", *map(str, args)], cwd=cwd, env=env, check=check,
                          capture_output=True, text=True, timeout=timeout)


def _mirror_repo_state(repo: Path, env: dict[str, str]):
    """Return whether a mirror is a complete clone and its local origin."""
    if not repo.is_dir() or redirected_directory(repo):
        return False, None
    common = _git_result(["-C", repo, "rev-parse", "--git-common-dir"], env=env)
    if common.returncode:
        return False, None
    shallow = _git_result(["-C", repo, "rev-parse", "--is-shallow-repository"], env=env)
    if shallow.returncode or shallow.stdout.strip() != "false":
        return False, None
    partial = _git_result(["-C", repo, "config", "--get", "extensions.partialClone"], env=env)
    if partial.returncode == 0 and partial.stdout.strip():
        return False, None
    promisor = _git_result(["-C", repo, "config", "--get-regexp",
                            r"^remote\..*\.promisor$"], env=env)
    if promisor.returncode == 0 and promisor.stdout.strip():
        return False, None
    return True, _local_origin(repo, env)


def ensure_pkg_mirror(project: Path, env: dict[str, str]) -> None:
    """Refresh locked Git dependencies in the lane's local package mirror.

    Mirror failures are deliberately warnings: Lake can still fetch the package
    directly, which keeps a transient network outage from failing CI.
    """
    env = network_environment(project, env)
    raw_mirror = env.get("QATLAS_PKG_MIRROR", "").strip()
    if not raw_mirror:
        return
    project, mirror = Path(project).resolve(), Path(raw_mirror).expanduser().resolve()
    packages = []
    for pkg in manifest(project)["packages"]:
        url, rev = pkg.get("url"), pkg.get("rev")
        if (pkg.get("type", "git") == "git" or url is not None) and url and rev:
            packages.append((pkg["name"].strip("«»"), str(url), str(rev)))
    if not packages:
        return
    lock_path = mirror / ".lock"
    try:
        mirror.mkdir(parents=True, exist_ok=True)
        lock_path.touch(exist_ok=True)
    except OSError as error:
        for name, _, rev in packages:
            print(f"Warning: pkg-mirror refresh skipped for {name} ({rev[:12]}): {error}",
                  file=sys.stderr)
        return
    for name, url, rev in packages:
        target = mirror / ".lake" / "packages" / name
        try:
            if usable_git_mirror(target, rev, project, env):
                continue
            local_url = _local_url(url, project)
            if local_url is not None and local_url in {target.resolve(), project.resolve()}:
                continue
            @contextmanager
            def locked_context():
                if fcntl is None:
                    yield
                    return
                with lock_path.open("a+") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            with locked_context():
                if usable_git_mirror(target, rev, project, env):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                net_env = {**env, "GIT_TERMINAL_PROMPT": "0",
                           "GIT_HTTP_LOW_SPEED_LIMIT": "1024",
                           "GIT_HTTP_LOW_SPEED_TIME": "30"}
                if not target.exists():
                    temporary = target.parent / ("." + name + ".clone-" + uuid.uuid4().hex)
                    try:
                        _git_result(["clone", "--quiet", "--no-checkout", url, temporary],
                                    env=net_env, cwd=project, check=True)
                        _git_result(["-C", temporary, "config", "uploadpack.allowFilter", "true"],
                                    env=net_env, check=True)
                        temporary.replace(target)
                    except Exception:
                        if temporary.exists():
                            remove_derived(temporary, net_env)
                        raise
                else:
                    valid, origin = _mirror_repo_state(target, net_env)
                    if not valid or origin in {target.resolve(), project.resolve()}:
                        continue
                    _git_result(["-C", target, "config", "uploadpack.allowFilter", "true"],
                                env=net_env, check=True)
                    fetched = _git_result(["-C", target, "fetch", "--quiet", "origin", rev],
                                          env=net_env, timeout=300)
                    if fetched.returncode:
                        _git_result(["-C", target, "fetch", "--quiet", "--tags", "--prune", "origin"],
                                    env=net_env, check=True)
        except (OSError, subprocess.SubprocessError) as error:
            print(f"Warning: pkg-mirror refresh skipped for {name} ({rev[:12]}): {error}",
                  file=sys.stderr)


def maintain_mirror(mirror: Path, env: dict[str, str], *, gc_cache=False, project=None):
    """Refresh mirrors, prewarm locked Lean toolchains, then optionally GC cache."""
    mirror = Path(mirror).expanduser().resolve()
    packages_dir = mirror / ".lake" / "packages"
    mirror.mkdir(parents=True, exist_ok=True)
    lock_path = mirror / ".lock"
    lock_path.touch(exist_ok=True)
    with lock_path.open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            if packages_dir.is_dir():
                for repo in sorted(packages_dir.iterdir()):
                    if repo.is_symlink():
                        print(f"Warning: pkg-mirror maintenance skipped symlink {repo}", file=sys.stderr)
                        continue
                    try:
                        valid, origin = _mirror_repo_state(repo, {**env, "GIT_NO_LAZY_FETCH": "1"})
                        if not valid or origin in {repo.resolve()}:
                            print(f"Warning: pkg-mirror maintenance skipped incomplete/self-origin repo {repo}",
                                  file=sys.stderr)
                            continue
                        _git_result(["-C", repo, "config", "uploadpack.allowFilter", "true"],
                                    env=env, check=True)
                        _git_result(["-C", repo, "fetch", "--quiet", "--tags", "--prune", "origin"],
                                    env={**env, "GIT_TERMINAL_PROMPT": "0",
                                         "GIT_HTTP_LOW_SPEED_LIMIT": "1024",
                                         "GIT_HTTP_LOW_SPEED_TIME": "30"}, check=True)
                    except (OSError, subprocess.SubprocessError) as error:
                        print(f"Warning: pkg-mirror maintenance failed for {repo.name}: {error}",
                              file=sys.stderr)
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    prewarm_toolchains(mirror, env)
    if gc_cache and env.get("LAKE_CACHE_DIR"):
        _maintain_gc(Path(env["LAKE_CACHE_DIR"]), project or mirror, env)


def prewarm_toolchains(mirror: Path, env: dict[str, str]) -> None:
    """Install toolchains named by mirror/runner workspaces before CI needs them."""
    elan_home = Path(env.get("ELAN_HOME") or Path(env.get("HOME", str(Path.home()))) / ".elan")
    elan = elan_home / "bin" / "elan"
    if not elan.is_file():
        print(f"Warning: elan not installed; toolchain prewarm skipped under {mirror}", file=sys.stderr)
        return
    roots = [Path(mirror).expanduser().resolve(), Path(mirror).expanduser().resolve().parent]
    toolchains: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = root.rglob("lean-toolchain")
        except OSError:
            continue
        for path in candidates:
            if any(part in {".git", ".lake", "node_modules"} for part in path.parts):
                continue
            try:
                value = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if value:
                toolchains.add(value)
    toolchains.add(env.get("QATLAS_DEFAULT_TOOLCHAIN", "leanprover/lean4:v4.35.0"))
    child_env = {**env, "ELAN_HOME": str(elan_home), "PATH": str(elan.parent) + os.pathsep + env.get("PATH", "")}
    for toolchain in sorted(toolchains):
        try:
            supervisor.run([str(elan), "toolchain", "install", toolchain], cwd=mirror,
                           env=child_env, check=True, stdout=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError) as error:
            print(f"Warning: toolchain prewarm failed for {toolchain}: {error}", file=sys.stderr)


def _maintain_gc(cache: Path, project: Path, env: dict[str, str]):
    cache.mkdir(parents=True, exist_ok=True)
    lock = cache / ".fetch.lock"
    with lock.open("a+") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Warning: cache GC skipped because the lane is busy", file=sys.stderr)
                return
        try:
            uid = str(os.getuid())
            running = any(supervisor.run(["pgrep", "-u", uid, "-x", name],
                                         stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL).returncode == 0
                          for name in ("lake", "lean"))
            if running:
                print("Warning: cache GC skipped because lake/lean is running", file=sys.stderr)
                return
            if cache.stat().st_dev != project.stat().st_dev:
                return
            _gc_unlocked(cache, workspace=project, apply=True,
                         age=float(env.get("QATLAS_GC_AGE", 14 * 86400)), scope="*")
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def usable_git_mirror(candidate: Path, rev: str, project: Path, env: dict[str, str]) -> bool:
    """Validate a complete local Git mirror before exposing it to Lake.

    Non-update Lake commands run ``Workspace.materializeDeps`` and
    ``PackageEntry.materialize``, honoring ``LAKE_PKG_URL_MAP`` without
    rewriting ``lake-manifest.json``.  ``lake update`` runs
    ``Workspace.updateAndMaterialize`` and rewrites that manifest, so
    ``command_environment`` strips the map on update/upgrade and never exports
    it to interactive shells.

    Lake fetches with ``--filter=tree:0``.  Local ``git-upload-pack`` runs
    with ``GIT_NO_LAZY_FETCH=1``; shallow, promisor, partial, and
    self-referential mirrors therefore fail or recursively spawn upload-pack.
    Only a complete, non-shallow clone with an independent origin is usable.
    """
    candidate, project = Path(candidate), Path(project)
    local_env = {**env, "GIT_NO_LAZY_FETCH": "1"}
    if not candidate.is_dir() or redirected_directory(candidate):
        return False
    try:
        if candidate.resolve() == project.resolve():
            return False
    except OSError:
        return False
    candidate_common = _git_common_dir(candidate, local_env)
    project_common = _git_common_dir(project, local_env)
    if candidate_common is None or project_common is None or candidate_common == project_common:
        return False
    origin = _local_origin(candidate, local_env)
    if origin is not None and origin in {candidate.resolve(), project.resolve()}:
        return False
    shallow = supervisor.run(["git", "-C", str(candidate), "rev-parse", "--is-shallow-repository"],
                             env=local_env, capture_output=True, text=True)
    if shallow.returncode != 0 or shallow.stdout.strip() != "false":
        return False
    partial = supervisor.run(["git", "-C", str(candidate), "config", "--get", "extensions.partialClone"],
                             env=local_env, capture_output=True, text=True)
    if partial.returncode == 0 and partial.stdout.strip():
        return False
    promisor = supervisor.run(["git", "-C", str(candidate), "config", "--get-regexp",
                               r"^remote\..*\.promisor$"], env=local_env,
                              capture_output=True, text=True)
    if promisor.returncode == 0 and promisor.stdout.strip():
        return False
    commit = supervisor.run(["git", "-C", str(candidate), "cat-file", "-e", rev + "^{commit}"],
                            env=local_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if commit.returncode:
        return False
    objects = supervisor.run(["git", "-C", str(candidate), "rev-list", "--objects", "--no-walk",
                              "--missing=print", rev], env=local_env, capture_output=True, text=True)
    return objects.returncode == 0 and not any(line.startswith("?") for line in objects.stdout.splitlines())


def official_cache(env):
    home = Path(env.get("USERPROFILE" if os.name == "nt" else "HOME", str(Path.home())))
    elan = Path(env.get("ELAN_HOME", str(home / ".elan")))
    lake = shutil.which("lake", path=env.get("PATH", ""))
    shim = lake and Path(lake).parent.resolve() == (elan / "bin").resolve()
    if env.get("ELAN", "elan").strip() and (env.get("ELAN_HOME") or shim):
        tc = env["ELAN_TOOLCHAIN"].replace("/", "--").replace(":", "---")
        return elan / "toolchains" / tc / "lake/cache"
    return Path(env.get("XDG_CACHE_HOME", str(home / ".cache"))) / "lake"


def available_memory_bytes() -> int | None:
    """Return the resource probe's available memory for compatibility callers."""
    try:
        return int(resources.probe_resources().available_memory)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def default_jobs():
    return resources.resolve_config(Path.cwd()).values["jobs"]


@contextmanager
def cache_lock(cache_dir: Path):
    """Serialize cache downloads and GC while leaving Lake builds concurrent."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    with (cache_dir / ".fetch.lock").open("a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def cache_directory(project, env):
    if env.get("LAKE_CACHE_DIR"):
        return Path(env["LAKE_CACHE_DIR"]).expanduser().resolve()
    env = {**env, "ELAN_TOOLCHAIN": (project / "lean-toolchain").read_text().strip()}
    return official_cache(env)


def dependency_url_map(project, source=None, env=None):
    env = dict(os.environ if env is None else env)
    try:
        urls = json.loads(env.get("LAKE_PKG_URL_MAP", "{}"))
    except json.JSONDecodeError as error:
        raise ValueError("LAKE_PKG_URL_MAP must contain a JSON object") from error
    if not isinstance(urls, dict):
        raise ValueError("LAKE_PKG_URL_MAP must contain a JSON object")
    for pkg in manifest(project)["packages"]:
        if pkg.get("type", "git") != "git" or "rev" not in pkg:
            continue
        name = pkg["name"].strip("«»")
        target = project / ".lake/packages" / name
        if git_head(target) == pkg["rev"]:
            continue
        if name in {key.strip("«»") for key in urls}:
            continue
        for root in candidate_sources(project, source, env):
            for candidate in source_packages(root, name):
                if usable_git_mirror(candidate, pkg["rev"], project, env):
                    urls[pkg["name"]] = str(candidate)
                    break
            if name in {key.strip("«»") for key in urls}:
                break
    return urls


def lake_update_command(argv):
    """Recognize Lake globals without mistaking their values for subcommands.

    --update forces dependency updates for any command. A literal -- terminates
    global options; options after the subcommand belong to that command.
    """
    if not argv or Path(argv[0]).stem != "lake":
        return False
    if any(arg == "--update" or arg.startswith("--update=") for arg in argv[1:]):
        return True
    takes_value = {"-d", "--dir", "-f", "--file", "--packages", "--config", "--toolchain", "--log-level", "--fail-level"}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--update" or arg.startswith("--update="):
            return True
        if arg == "--":
            return i + 1 < len(argv) and argv[i + 1] in {"update", "upgrade"}
        if arg in takes_value or arg == "-K":
            i += 2
            continue
        if arg.startswith("-") or arg.startswith("+"):
            i += 1
            continue
        return arg in {"update", "upgrade"}
    return False


def network_environment(project, env):
    """Apply child-local Git URL routing without mutating caller config."""
    effective = dict(env)
    prefix = effective.get("QATLAS_GITHUB_PREFIX", "").strip()
    if prefix:
        if not re.fullmatch(r"https://[^\s?#@]+/", prefix):
            raise ValueError("QATLAS_GITHUB_PREFIX must be an HTTPS prefix ending in /")
        key = "url." + prefix + "https://github.com/.insteadOf"
        count = int(effective.get("GIT_CONFIG_COUNT", "0"))
        if not any(effective.get(f"GIT_CONFIG_KEY_{i}") == key for i in range(count)):
            effective.update(GIT_CONFIG_COUNT=str(count + 1))
            effective[f"GIT_CONFIG_KEY_{count}"] = key
            effective[f"GIT_CONFIG_VALUE_{count}"] = "https://github.com/"
    if "QATLAS_PKG_MIRROR" not in effective:
        mirror = Path(effective.get("HOME", str(Path.home()))) / "pkg-mirror"
        if mirror.is_dir():
            effective["QATLAS_PKG_MIRROR"] = str(mirror)
    return effective


def command_environment(project, env, argv, source=None):
    # Non-update Lake commands materialize locked deps from LAKE_PKG_URL_MAP
    # without rewriting lake-manifest.json.  lake update/upgrade rewrites that
    # file, so keep the map out of those commands and out of interactive envs.
    effective = network_environment(project, env)
    effective.pop("LAKE_PKG_URL_MAP", None)
    if not lake_update_command(argv) and (project / "lake-manifest.json").is_file():
        urls = dependency_url_map(project, source, env)
        if urls:
            effective["LAKE_PKG_URL_MAP"] = json.dumps(urls)
    return effective


def environment(project, source=None, base_env=None, cli=None):
    from qatlib_env.r2_cache import package_environment
    package = package_name(project) if (project / "lakefile.toml").is_file() else ""
    env = network_environment(project, package_environment(package, os.environ if base_env is None else base_env))
    env.pop("LAKE_PKG_URL_MAP", None)
    tc = (project / "lean-toolchain").read_text().strip()
    if not re.fullmatch(r"leanprover/lean4:v4\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", tc):
        raise ValueError("lean-toolchain must select an explicit Lean 4 release")
    if env.get("ELAN_TOOLCHAIN", tc) != tc:
        raise ValueError("ELAN_TOOLCHAIN differs from lean-toolchain")
    env["ELAN_TOOLCHAIN"] = tc
    for root in package_roots(project):
        for path in [root, root / ".lake", root / ".lake/build", root / ".lake/packages", root / ".lake/qatlas-tmp"]:
            if redirected_directory(path):
                raise ValueError(f"shared/symlinked working state is forbidden: {path}")
    cache = cache_directory(project, env)
    cache.mkdir(parents=True, exist_ok=True)
    env.update(LAKE_CACHE_DIR=str(cache), LAKE_ARTIFACT_CACHE="true")
    config = resources.resolve_config(project, cli, env)
    env = config.environment(env)
    if config.values["stage_timeout"] and "QATLAS_STAGE_DEADLINE" not in env:
        env["QATLAS_STAGE_DEADLINE"] = str(time.monotonic() + config.values["stage_timeout"])
    for warning in config.warnings:
        print("RESOURCE_WARNING: " + warning, file=sys.stderr)
    env.setdefault("MATHLIB_NO_CACHE_ON_UPDATE", "1")
    return env


def bootstrap(project, env, source=None):
    from qatlib_env.prepare import prepare
    return prepare(project, env, source=source)


def resolve_dependencies(project, env, source=None):
    """Materialize the existing dependency lock without updating it."""
    # Credentials live only in child environment variables, never ~/.gitconfig.
    env = dict(env)
    token = env.get("GITEA_TOKEN", "").strip()
    if token:
        host = env.get("GITEA_HOST", "").strip()
        if not host:
            raise ValueError("GITEA_HOST is required when GITEA_TOKEN is configured")
        if not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", host):
            raise ValueError("invalid GITEA_HOST")
        import base64
        auth = base64.b64encode(("token:" + token).encode()).decode()
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        env.update({"GIT_CONFIG_COUNT": str(count + 1),
                    f"GIT_CONFIG_KEY_{count}": f"http.https://{host}/.extraheader",
                    f"GIT_CONFIG_VALUE_{count}": "Authorization: Basic " + auth})
    ensure_pkg_mirror(project, env)
    # resolve-deps materializes the existing lock before executables are built.
    command = ["lake", "resolve-deps"]
    run(command, cwd=project, env=command_environment(project, env, command, source))


def lake_build_command(argv):
    if not _is_lake(argv):
        return False
    takes_value = {"-d", "--dir", "-f", "--file", "--packages", "--config", "--toolchain", "--log-level", "--fail-level", "-K"}
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in takes_value:
            index += 2
        elif arg == "--":
            return index + 1 < len(argv) and argv[index + 1] == "build"
        elif arg.startswith(("-", "+")):
            index += 1
        else:
            return arg == "build"
    return False


def check(project, env, modules=()):
    """Run the consumer's own import gate, lint, build and kernel audit."""
    gate = project / "python/dev_metadata/check_source_imports.py"
    if gate.is_file():
        dependencies = []
        for package in manifest(project)["packages"]:
            if package["name"].strip("«»") in {"Qatlib", "Project", "example_library"}:
                dependencies += ["--dependency-repo", str(project / ".lake/packages" / package["name"].strip("«»"))]
        run([sys.executable, "-B", "-m", "dev_metadata.check_source_imports", "--repo", str(project), *dependencies], cwd=project, env=env)
    run(["lake", "lint"], cwd=project, env=env)
    if modules:
        run(["lake", "build", *modules], cwd=project, env=env)
        run(["lake", "exe", "test", *modules], cwd=project, env=env)
    else:
        config = tomllib.loads((project / "lakefile.toml").read_text())
        targets = [lib["name"] for lib in config.get("lean_lib", [])]
        if not targets:
            raise ValueError("lakefile.toml must declare root and test libraries")
        run(["lake", "build", *targets], cwd=project, env=env)
        run(["lake", "test"], cwd=project, env=env)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def _restore_writable(path: Path) -> None:
    """Clear read-only attributes before a Windows fallback move."""
    if os.name != "nt":
        return
    candidates = [path]
    if path.is_dir() and not path.is_symlink():
        try:
            candidates.extend(path.rglob("*"))
        except OSError:
            pass
    for candidate in candidates:
        try:
            candidate.chmod(candidate.stat().st_mode | stat.S_IWRITE)
        except OSError:
            pass


def _freedesktop_trash(path: Path, env_or_os=None) -> None:
    """Move *path* to a FreeDesktop Trash directory using only the stdlib."""
    environment = os.environ if env_or_os is None else env_or_os
    data_home = Path(environment.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    files = data_home / "Trash" / "files"
    info = data_home / "Trash" / "info"
    files.mkdir(parents=True, exist_ok=True)
    info.mkdir(parents=True, exist_ok=True)

    original = Path(path)
    base_name = original.name or "unnamed"
    deletion_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    encoded_path = quote(str(original.absolute()), safe="/")
    for index in range(1000000):
        name = base_name if index == 0 else f"{base_name}.{index}"
        destination = files / name
        info_path = info / f"{name}.trashinfo"
        if os.path.lexists(destination) or os.path.lexists(info_path):
            continue
        try:
            with info_path.open("x", encoding="utf-8") as descriptor:
                descriptor.write("[Trash Info]\n")
                descriptor.write(f"Path={encoded_path}\n")
                descriptor.write(f"DeletionDate={deletion_date}\n")
            _restore_writable(original)
            shutil.move(original, destination)
            return
        except Exception:
            try:
                info_path.unlink()
            except OSError:
                pass
            raise
    raise FileExistsError(f"unable to choose a unique Trash name for {original}")


def remove_derived(path, env_or_os=None):
    """Move a derived file to Trash, falling back to the FreeDesktop layout."""
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        return
    trash = shutil.which("trash-put")
    if trash is not None:
        supervisor.run([trash, str(path)], check=True)
        return
    _freedesktop_trash(Path(path), env_or_os)


def active_lean_processes(project):
    """Best-effort Linux guard; the operator must still ensure the workspace is idle."""
    project, processes = Path(project).resolve(), []
    proc = Path("/proc")
    if proc.is_dir():
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                command = (item / "cmdline").read_bytes().split(b"\0")
                if not command or Path(os.fsdecode(command[0])).stem not in {"lean", "leanir"}:
                    continue
                if (item / "cwd").resolve().is_relative_to(project):
                    processes.append(int(item.name))
            except (OSError, ValueError):
                continue
    return processes


def clean_setup(project, apply=False, include_dependencies=False):
    # --apply is an explicit operator assertion of idleness. /proc catches
    # visible local jobs, but cannot prevent another job starting after the scan.
    if apply and active_lean_processes(project):
        raise ValueError("clean-setup requires an idle workspace (active lean/leanir)")
    roots = package_roots(project)
    files = []
    for root in roots if include_dependencies else [project]:
        for path in (root, root / ".lake", root / ".lake/build", root / ".lake/build/ir", root / ".lake/qatlas-tmp"):
            if redirected_directory(path):
                raise ValueError("refusing a shared/symlinked build directory")
        files.extend(p for p in (root / ".lake/build/ir").rglob("*.setup.json")
                     if p.is_file() and not p.is_symlink())
    for path in (project / ".lake/qatlas-tmp").glob("*"):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 64 * 1024 ** 2:
            continue
        try:
            setup = json.loads(path.read_text())
            if isinstance(setup, dict) and {"importArts", "isModule", "name"} <= setup.keys():
                files.append(path)
        except (ValueError, UnicodeError):
            pass
    result = {"files": len(files), "bytes": sum(p.stat().st_size for p in files), "applied": apply}
    if apply:
        for path in files:
            remove_derived(path)
    return result


def descriptors(value):
    if isinstance(value, str) and ARTIFACT.fullmatch(value):
        return {value}
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else []
    return set().union(*(descriptors(v) for v in items))


def _gc_unlocked(cache, *, workspace, apply=False, age=86400, scope=None):
    result = {"maps": 0, "artifacts": 0, "logical_bytes": 0, "applied": apply,
              "physical_bytes_freed_estimate": 0}
    if age < 0:
        raise ValueError("GC age must be nonnegative")
    if cache.stat().st_dev != workspace.stat().st_dev:
        return {**result, "skipped": "cross-filesystem pool"}
    scope = scope or package_name(workspace)
    cutoff, maps, candidates = time.time() - age, {}, set()
    for path in (cache / "outputs").glob("*/*.json"):
        if any(redirected_directory(p) for p in [path, path.parent, cache / "outputs", cache / "artifacts"]):
            raise ValueError("refusing symlinked cache storage")
        saved = json.loads(path.read_text())
        if not isinstance(saved, dict) or "data" not in saved:
            raise ValueError(f"unsupported cache output schema: {path}")
        data = saved["data"]
        maps[path] = descriptors(data)
        if ((scope != "*" and path.parent.name != scope) or not isinstance(data, dict) or
                not isinstance(data.get("i"), str) or not data.get("o") or path.stat().st_mtime > cutoff):
            continue
        artifacts = [cache / "artifacts" / name for name in maps[path]]
        if artifacts and all(p.is_file() and not p.is_symlink() and p.stat().st_nlink == 1
                             and p.stat().st_mtime <= cutoff for p in artifacts):
            candidates.add(path)
    retained = set().union(*(arts for p, arts in maps.items() if p not in candidates))
    removable = set().union(*(maps[p] for p in candidates)) - retained
    artifacts = [cache / "artifacts" / name for name in removable]
    stats = [p.stat() for p in artifacts]
    result.update(maps=len(candidates), artifacts=len(artifacts), logical_bytes=sum(s.st_size for s in stats))
    if apply:
        for path in [*candidates, *artifacts]:
            remove_derived(path)
        # The Trash backend preserves data until the operator empties it; do
        # not claim those blocks have already been released to the filesystem.
        result["bytes_moved_to_trash_estimate"] = sum(getattr(s, "st_blocks", 0) * 512 for s in stats)
    return result


def gc(cache, *, workspace, apply=False, age=86400, scope=None):
    cache, workspace = Path(cache), Path(workspace)
    try:
        cache_stat, workspace_stat = cache.stat(), workspace.stat()
    except OSError as error:
        raise ValueError("cache and workspace must be directories") from error
    if hasattr(cache_stat, "st_mode") and not stat.S_ISDIR(cache_stat.st_mode):
        raise ValueError("cache and workspace must be directories")
    if hasattr(workspace_stat, "st_mode") and not stat.S_ISDIR(workspace_stat.st_mode):
        raise ValueError("cache and workspace must be directories")
    if cache_stat.st_dev != workspace_stat.st_dev:
        return {"maps": 0, "artifacts": 0, "logical_bytes": 0, "applied": apply,
                "physical_bytes_freed_estimate": 0, "skipped": "cross-filesystem pool"}
    # Hold the lock across the complete scan and unlink phase.  Lake cache get
    # writes .art.tmp and revision JSONL non-atomically, so GC must not race it.
    with cache_lock(cache):
        return _gc_unlocked(cache, workspace=workspace, apply=apply, age=age, scope=scope)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="qatlib-env", description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--source-root", type=Path)
    resource_options = ("jobs", "lean_threads", "memory_budget", "heavy_jobs", "module_timeout", "stage_timeout", "heavy_patterns")
    def options(target, *, defaults=False):
        for name in resource_options:
            target.add_argument("--" + name.replace("_", "-"), default=None if defaults else argparse.SUPPRESS)
        target.add_argument("--offline", action="store_true", default=False if defaults else argparse.SUPPRESS)
        target.add_argument("--no-prepare", action="store_true", default=False if defaults else argparse.SUPPRESS)
    options(parser, defaults=True)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("env", "shell", "bootstrap", "prepare"):
        options(commands.add_parser(name))
    config = commands.add_parser("config")
    config.add_argument("action", choices=("explain",))
    options(config)
    cache = commands.add_parser("cache")
    cache.add_argument("action", choices=("explain", "upload"))
    cache.add_argument("args", nargs=argparse.REMAINDER)
    manual = commands.add_parser("docs")
    manual.add_argument("--path", action="store_true", help="print installed manual location")
    execute = commands.add_parser("run")
    options(execute)
    execute.add_argument("args", nargs=argparse.REMAINDER)
    clean = commands.add_parser("clean-setup")
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--include-dependencies", action="store_true")
    collect = commands.add_parser("gc")
    collect.add_argument("--apply", action="store_true")
    collect.add_argument("--age", type=float, default=86400)
    collect.add_argument("--scope")
    maintain = commands.add_parser("maintain-mirror")
    maintain.add_argument("--mirror", type=Path)
    maintain.add_argument("--gc-cache", action="store_true")
    tree = commands.add_parser("worktree")
    tree.add_argument("path", type=Path)
    tree.add_argument("branch")
    tree.add_argument("--no-bootstrap", action="store_true",
                      help="create a lightweight worktree without restoring Lean caches")
    options(tree)
    validate = commands.add_parser("check")
    options(validate)
    validate.add_argument("modules", nargs="*")
    pool = commands.add_parser("pool")
    pool.add_argument("action", choices=("status", "prune"), nargs="?", default="status")
    plan = commands.add_parser("plan")
    plan.add_argument("--policy", choices=("auto", "local", "cloud"), default="auto")
    plan.add_argument("--cold", action="store_true")
    plan.add_argument("--full-gate", action="store_true")
    plan.add_argument("--memory-mib", type=int, default=0)
    cloud = commands.add_parser("cloud")
    cloud.add_argument("action", choices=("submit", "status", "logs", "wait", "fetch"))
    cloud.add_argument("--head", default="HEAD")
    cloud.add_argument("--state", type=Path, default=Path(".qatlib-env/cloud-state.json"))
    cloud.add_argument("--job-id")
    cloud.add_argument("--bundle", type=Path)
    cloud.add_argument("--destination", type=Path)
    cloud.add_argument("--sha256")
    args = parser.parse_args(argv)
    project = args.project.resolve()
    cli = {name: getattr(args, name, None) for name in resource_options if getattr(args, name, None) is not None}
    if args.command == "docs":
        manual_path = Path(__file__).with_name("README.md")
        print(manual_path if args.path else manual_path.read_text())
        return 0
    if args.command == "config":
        print(json.dumps(resources.resolve_config(project, cli).explain(), indent=2))
        return 0
    if args.command in {"pool", "plan", "cloud"}:
        if args.command == "pool":
            from qatlib_env.pool import Pool
            result = Pool().prune() if args.action == "prune" else Pool().status()
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "plan":
            from qatlib_env.config import choose_mode, load_config
            config = load_config(project)
            mode = choose_mode(args.policy, cold=args.cold, memory_mib=args.memory_mib,
                               full_gate=args.full_gate, pool_mib=config.local_pool_mib)
            print(json.dumps({"mode": mode, "head": "HEAD"}, indent=2))
            return 0
        from qatlib_env.cloud import CloudLoop, make_request, unpack_bundle
        if args.action == "fetch" and args.bundle:
            if not args.bundle or not args.destination:
                raise ValueError("cloud fetch requires --bundle and --destination")
            print(json.dumps(unpack_bundle(args.bundle, args.destination, expected_sha256=args.sha256), indent=2))
            return 0
        from qatlib_env.config import load_config
        from qatlib_forge.repo_api import RepoAPI, RepoRef
        config = load_config(project)
        if not config.repository or not config.workflow:
            raise ValueError("cloud mode requires repository and workflow in qatlib-env.toml")
        request = make_request(config, head=args.head, policy="cloud", cold=True)
        ref = RepoRef.parse(config.repository, platform=config.cloud_platform if config.cloud_platform in {"github", "gitea"} else None)
        loop = CloudLoop(RepoAPI(ref, token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GITEA_TOKEN")), args.state)
        if args.action == "submit":
            result = loop.submit(request)
        elif args.action == "status":
            result = loop.status()
        elif args.action == "wait":
            result = loop.wait()
        elif args.action == "fetch":
            if not args.destination:
                raise ValueError("cloud fetch requires --destination")
            result = loop.fetch(args.destination)
        else:
            state = json.loads(args.state.read_text(encoding="utf-8"))
            job_id = args.job_id or state.get("job_id")
            if not job_id:
                raise ValueError("cloud logs requires --job-id or a saved job id")
            from qatlib_forge.ci_logs import RangeLogReader, extract_diagnostics
            run_id = state.get("run_id")
            if run_id is None:
                raise ValueError("cloud logs requires a saved run_id")
            reader = RangeLogReader(loop.api.job_log_url(str(run_id), str(job_id)))
            body, headers = reader.read(0, 128 * 1024 - 1)
            result = {"headers": headers, "diagnostics": [d.__dict__ for d in extract_diagnostics(body.decode("utf-8", "replace"))]}
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.command == "cache" and args.action == "upload":
        from qatlib_env.r2_cache import main as upload
        return upload(["--repo", str(project), *args.args])
    if args.command == "maintain-mirror":
        mirror = args.mirror or (Path(os.environ["QATLAS_PKG_MIRROR"])
                                 if os.environ.get("QATLAS_PKG_MIRROR") else None)
        if mirror is None:
            raise ValueError("maintain-mirror needs --mirror or QATLAS_PKG_MIRROR")
        maintain_mirror(mirror, dict(os.environ), gc_cache=args.gc_cache, project=project)
        return 0
    env = environment(project, args.source_root, cli=cli)
    if args.command == "env":
        print(json.dumps({k: env[k] for k in ENV_KEYS}, indent=2))
    elif args.command == "shell":
        for key in ENV_KEYS:
            print(f"export {key}={shlex.quote(env[key])}")
    elif args.command in {"bootstrap", "prepare"}:
        from qatlib_env.prepare import prepare
        result = ({"skipped": "explicit --no-prepare"} if args.no_prepare else
                  prepare(project, env, source=args.source_root, offline=args.offline))
        print(json.dumps(result, indent=2))
    elif args.command == "cache":
        from qatlib_env import checkpoint
        from qatlib_env.prepare import identity, cold_modules
        print(json.dumps({"toolchain": env["ELAN_TOOLCHAIN"], "identity": identity(project, env),
                          "cold_modules": cold_modules(project), "checkpoint": checkpoint.describe(project, env),
                          "resources": resources.resolve_config(project, cli).explain()}, indent=2))
    elif args.command == "check":
        if not args.no_prepare:
            from qatlib_env.prepare import prepare
            prepare(project, env, source=args.source_root, offline=args.offline)
        check(project, env, args.modules)
    elif args.command == "run":
        command = args.args[1:] if args.args[:1] == ["--"] else args.args
        if not command:
            raise ValueError("run needs a command after --")
        if lake_build_command(command) and "--no-build" not in command and not args.no_prepare:
            from qatlib_env.prepare import prepare
            prepare(project, env, source=args.source_root, offline=args.offline)
        run_env = {**env, **({"LAKE_PKG_URL_MAP": os.environ["LAKE_PKG_URL_MAP"]}
                             if "LAKE_PKG_URL_MAP" in os.environ else {})}
        # A finished child does not imply an idle workspace: another Lean or
        # leanir process may still consume setup files. Only clean-setup sweeps.
        run(command, cwd=project, env=run_env)
    elif args.command == "worktree":
        path = args.path.expanduser().resolve()
        run(["git", "worktree", "add", "-b", args.branch, str(path)], cwd=project)
        visible = agents_local_path(path)
        print("AGENTS.local.md: " + (str(visible) if visible else "repository defaults"), file=sys.stderr)
        if not (args.no_bootstrap or args.no_prepare):
            from qatlib_env.prepare import prepare
            prepare(path, environment(path, project, cli=cli), source=project, offline=args.offline)
    elif args.command == "clean-setup":
        print(json.dumps(clean_setup(project, args.apply, args.include_dependencies)))
    elif args.command == "gc":
        print(json.dumps(gc(Path(env["LAKE_CACHE_DIR"]), workspace=project,
                            apply=args.apply, age=args.age, scope=args.scope)))
    return 0


def cli_main(argv=None):
    try:
        return main(argv)
    except supervisor.ProcessCancelled as error:
        print("WORKSPACE_CANCELLED: " + str(error), file=sys.stderr)
        return error.returncode
    except subprocess.TimeoutExpired as error:
        print("WORKSPACE_TIMEOUT: " + str(error), file=sys.stderr)
        return 124
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print("WORKSPACE_FAILED: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli_main())

