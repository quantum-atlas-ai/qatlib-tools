"""Read-only import-graph estimates and fail-closed local admission.

No Lake command is run here. Cached outputs are conservatively screened using
trace presence, source/config mtimes, dependency invalidation and lock identity;
this is an estimate, not a substitute for Lake's native depHash validation.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Mapping

from .config import load_config
from . import resources
from qatlib_search.lean_source import import_header, mask_comments

MIB = 1024**2
_permission: ContextVar[bool] = ContextVar("local_heavy_permission", default=False)
_full_gate: ContextVar[bool] = ContextVar("full_gate_operation", default=False)


@contextmanager
def permission(allowed: bool, *, full_gate: bool = False):
    """In-process, invocation-scoped permission. Never exported to child env."""
    token = _permission.set(bool(allowed))
    gate_token = _full_gate.set(full_gate or _full_gate.get())
    try:
        yield
    finally:
        _full_gate.reset(gate_token)
        _permission.reset(token)


def git(root: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"})
    return result.stdout.strip() if result.returncode == 0 else None


def _sources(root: Path):
    for parent, directories, files in os.walk(root):
        directories[:] = sorted(d for d in directories if not d.startswith(".") and d not in
                                {"node_modules", "build", "dist"})
        for name in sorted(files):
            if name.endswith(".lean"):
                yield Path(parent) / name


@dataclass(frozen=True)
class Module:
    path: Path | None
    output: Path
    package: str
    root: Path
    toolchain: bool = False


class SourceGraph:
    def __init__(self, project: Path, env: Mapping[str, str]):
        self.project, self.env = project, env
        self.modules: dict[str, Module] = {}
        self.libraries: dict[str, set[str]] = {}
        self.executables: dict[str, str] = {}
        self.project_modules: set[str] = set()
        self.edges: dict[str, set[str]] = {}
        self.errors: set[str] = set()
        self.module_errors: dict[str, set[str]] = defaultdict(set)
        self.lock_mismatches: list[dict] = []
        self.warm_cache: dict[str, bool] = {}
        self.package_stamp: dict[Path, int] = {}
        self.conf = self._toml(project / "lakefile.toml")
        self.add_package(project, "project", self.conf)
        manifest_path = project / "lake-manifest.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text())
                if not isinstance(manifest, dict) or not isinstance(manifest.get("packages"), list):
                    raise ValueError("packages must be an array")
            except (ValueError, OSError) as error:
                raise ValueError(f"{manifest_path}: invalid dependency lock: {error}") from error
        else:
            manifest = {"packages": []}
            self.errors.add("<missing lake-manifest.json>")
        packages_dir = (project / manifest.get("packagesDir", ".lake/packages")).resolve()
        if not packages_dir.is_relative_to(project):
            raise ValueError("dependency packagesDir must stay inside the project")
        seen = set()
        for entry in manifest["packages"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise ValueError(f"{manifest_path}: malformed package entry")
            name = entry["name"].strip("«»")
            if name in seen or not re.fullmatch(r"[\w.-]+", name):
                raise ValueError(f"{manifest_path}: duplicate or unsafe package name: {name}")
            seen.add(name)
            base = packages_dir / name
            expected = entry.get("rev")
            actual = git(base, "rev-parse", "HEAD") if base.is_dir() else None
            checkout = git(base, "rev-parse", "--show-toplevel") if base.is_dir() else None
            if checkout is None or Path(checkout).resolve() != base.resolve():
                actual = None
            dirty = git(base, "status", "--porcelain=v1", "--untracked-files=all", "--",
                        "*.lean", "lakefile.toml", "lakefile.lean", "lean-toolchain") if base.is_dir() else None
            if entry.get("type", "git") != "git" or not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{40}", expected) or actual != expected or dirty:
                self.lock_mismatches.append({"package": name, "expected": expected,
                                             "actual": actual, "modified_sources": bool(dirty)})
            if base.is_dir():
                conf = self._toml(base / "lakefile.toml")
                self.add_package(base, name, conf)
        self.add_toolchain()

    @staticmethod
    def _toml(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            return tomllib.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise ValueError(f"{path}: invalid TOML: {error}") from error

    def add(self, name: str, path: Path | None, base: Path, package: str, *, toolchain=False):
        output = base / ("lib/lean" if toolchain else ".lake/build/lib/lean") / (name.replace(".", "/") + ".olean")
        module = Module(path, output, package, base, toolchain)
        if name in self.modules and self.modules[name] != module:
            self.module_errors[name].add("<ambiguous provider: " + name + ">")
            return
        self.modules[name] = module
        if package == "project":
            self.project_modules.add(name)

    @staticmethod
    def _dsl_libraries(base: Path) -> list[dict]:
        """Recognize library names and literal source directories without Lake.

        Executable declarations and package configuration are not importable
        libraries. Dynamic DSL expressions stay unresolved when imported.
        """
        path = base / "lakefile.lean"
        if not path.is_file():
            return []
        try:
            text = path.read_text()
            masked = mask_comments(text, preserve_escaped_identifiers=True)
        except (OSError, UnicodeError, ValueError):
            return []
        declarations = list(re.finditer(
            r"(?m)^[ \t]*(?:@\[[^\n]*\][ \t]*)?(package|lean_lib|lean_exe)"
            r"\s+(«[^»\n]+»|[\w.]+)", masked))

        def source_directory(start: int, end: int, default: str | None) -> str | None:
            field = re.search(r"(?m)^[ \t]*srcDir[ \t]*:=", masked[start:end])
            if field is None:
                return default
            raw = text[start + field.end():end].lstrip()
            try:
                value, consumed = json.JSONDecoder().raw_decode(raw)
                tail = mask_comments(raw[consumed:].split("\n", 1)[0]).strip()
            except ValueError:
                return None
            return value if isinstance(value, str) and not tail else None

        default_source = "."
        libraries = []
        for index, declaration in enumerate(declarations):
            end = declarations[index + 1].start() if index + 1 < len(declarations) else len(text)
            kind, name = declaration.groups()
            if kind == "package":
                default_source = source_directory(declaration.end(), end, ".")
            elif kind == "lean_lib":
                source = source_directory(declaration.end(), end, default_source)
                if source is not None:
                    libraries.append({"name": name.strip("«»"), "srcDir": source})
        return libraries

    def add_package(self, base: Path, package: str, conf: dict):
        libraries = conf.get("lean_lib", [])
        if not isinstance(libraries, list):
            raise ValueError(f"{base}: lean_lib must be an array")
        if not libraries:
            if package != "project":
                libraries = self._dsl_libraries(base)
                if libraries:
                    self.add_package(base, package, {"lean_lib": libraries})
                return
            # Project targets may include standalone files. Missing TOML
            # declarations still prevent certifying a complete project plan.
            for path in _sources(base):
                self.add(".".join(path.relative_to(base).with_suffix("").parts), path, base, package)
            self.errors.add("<missing TOML lean_lib declarations>")
            return
        for library in libraries:
            name = library["name"]
            source = base / library.get("srcDir", conf.get("srcDir", "."))
            if not source.resolve().is_relative_to(base.resolve()):
                raise ValueError(f"{base}: library srcDir escapes package")
            members = set()
            globs = library.get("globs", [name, name + ".+"])
            for glob in globs:
                recursive = glob.endswith(".+")
                direct = glob.endswith(".*")
                prefix = glob[:-2] if recursive or direct else glob
                location = source / prefix.replace(".", "/")
                candidates = _sources(location) if recursive else location.glob("*.lean") if direct else [location.with_suffix(".lean")]
                for path in candidates:
                    if path.is_file():
                        module = ".".join(path.relative_to(source).with_suffix("").parts)
                        self.add(module, path, base, package)
                        members.add(module)
            if package == "project":
                self.libraries[name] = members
        for executable in conf.get("lean_exe", []) if package == "project" else []:
            source = base / executable.get("srcDir", conf.get("srcDir", "."))
            name = executable.get("root", executable["name"])
            path = source / (name.replace(".", "/") + ".lean")
            if path.is_file():
                self.add(name, path, base, package)
                self.executables[executable["name"]] = name

    def add_toolchain(self):
        tc_file = self.project / "lean-toolchain"
        tc = tc_file.read_text().strip() if tc_file.is_file() else ""
        if not re.fullmatch(r"leanprover/lean4:v4\.\d+\.\d+(?:-[\w.-]+)?", tc):
            self.errors.add("<missing or unpinned lean-toolchain>")
            return
        explicit = self.env.get("QATLAS_REAL_SYSROOT") or self.env.get("LEAN_SYSROOT")
        elan = Path(self.env.get("ELAN_HOME", str(Path.home() / ".elan")))
        prefix = Path(explicit) if explicit else elan / "toolchains" / tc.replace("/", "--").replace(":", "---")
        for source in [prefix / "src/lean", prefix / "src/lean/lake"]:
            for name in ("Init", "Lean", "Std", "Lake"):
                paths = [source / (name + ".lean"), *_sources(source / name)]
                for path in paths:
                    if path.is_file():
                        module = ".".join(path.relative_to(source).with_suffix("").parts)
                        if module not in self.modules or not self.modules[module].toolchain:
                            self.add(module, path, prefix, "toolchain", toolchain=True)
        # Some distributions omit sources. Their precompiled modules are valid
        # boundaries, but the absent source-level closure is reported explicitly.
        for path in (prefix / "lib/lean").rglob("*.olean") if (prefix / "lib/lean").is_dir() else []:
            module = ".".join(path.relative_to(prefix / "lib/lean").with_suffix("").parts)
            if module not in self.modules or not self.modules[module].toolchain:
                self.add(module, None, prefix, "toolchain", toolchain=True)

    def deps(self, name: str) -> set[str]:
        if name not in self.edges:
            module = self.modules.get(name)
            result = set()
            if module and module.path:
                try:
                    text = module.path.read_text()
                    dependencies, end = import_header(text)
                    result = {name.replace("«", "").replace("»", "") for name in dependencies}
                    if not re.match(r"\s*(?:module\s+)?prelude\b", mask_comments(text[:end])) and name != "Init":
                        result.add("Init")
                except (ValueError, OSError, UnicodeError) as error:
                    self.module_errors[name].add(f"<cannot parse {name}: {error}>")
            elif module and module.toolchain:
                self.module_errors[name].add(f"<toolchain source unavailable: {name}>")
            self.edges[name] = result
        return self.edges[name]

    @staticmethod
    def walk(seeds, next_nodes) -> set[str]:
        seen, pending = set(), list(seeds)
        while pending:
            name = pending.pop()
            if name not in seen:
                seen.add(name)
                pending.extend(next_nodes(name) - seen)
        return seen

    def targets(self, targets: list[str] | tuple[str, ...]) -> tuple[set[str], bool]:
        explicit = bool(targets)
        targets = list(targets or self.conf.get("defaultTargets", list(self.libraries)))
        selected, library_target = set(), False
        for target in targets:
            if target in self.libraries:
                selected.update(self.libraries[target])
                library_target = True
            elif target in self.executables:
                selected.add(self.executables[target])
            elif target in self.modules:
                selected.add(target)
            elif target.endswith(".lean"):
                path = (self.project / target).resolve()
                matches = {n for n, m in self.modules.items() if m.path and m.path.resolve() == path}
                if matches:
                    selected.update(matches)
                elif path.is_file() and path.is_relative_to(self.project):
                    name = ".".join(path.relative_to(self.project).with_suffix("").parts)
                    self.add(name, path, self.project, "project")
                    selected.add(name)
                else:
                    self.errors.add(f"<unknown target: {target}>")
            else:
                self.errors.add(f"<unknown target: {target}>")
        if not selected:
            self.errors.add("<no source targets>")
        return selected, not explicit or library_target

    def warm(self, name: str) -> bool:
        if name not in self.warm_cache:
            self.warm_cache[name] = self._warm(name)
        return self.warm_cache[name]

    def _warm(self, name: str) -> bool:
        module = self.modules[name]
        output = module.output
        try:
            stat = output.stat()
            if not output.is_file() or stat.st_size == 0:
                return False
            if module.toolchain:
                return True
            trace = output.with_suffix(".trace")
            stamp = min(trace.stat().st_mtime_ns, stat.st_mtime_ns)
            if module.root not in self.package_stamp:
                paths = [module.root / p for p in ("lakefile.toml", "lakefile.lean", "lean-toolchain", "lake-manifest.json")]
                self.package_stamp[module.root] = max((p.stat().st_mtime_ns for p in paths if p.exists()), default=0)
            if self.package_stamp[module.root] > stamp or module.path and module.path.stat().st_mtime_ns > stamp:
                return False
            data = json.loads(trace.read_text())
            if not isinstance(data, dict) or not data.get("depHash"):
                return False
            return True
        except (OSError, ValueError, TypeError):
            return False


def remote_cache(project: Path, head: str | None, config) -> dict:
    """Fetch at most one exact manifest into memory. Never restore/register it."""
    path = project / "lake-cache.toml"
    if not path.is_file() or not head:
        return {"status": "unconfigured", "module_hit_rate": None}
    cache = SourceGraph._toml(path).get("cache", {})
    service = next((s for s in cache.get("service", []) if s.get("name") == cache.get("defaultService")), {})
    endpoint = service.get("revisionEndpoint")
    repository = config.metadata.get("cache_repository") or config.repository
    if not endpoint or not repository:
        return {"status": "unconfigured", "module_hit_rate": None}
    from urllib.parse import urlsplit, quote
    slug = urlsplit(repository).path.strip("/") if "://" in repository else repository
    tc = (project / "lean-toolchain").read_text().strip().replace("/", "--").replace(":", "---")
    url = endpoint.rstrip("/") + "/" + quote(slug, safe="/") + "/tc/" + tc + "/" + head + ".jsonl"
    options = ['silent', 'show-error', 'connect-timeout = 5', 'max-time = 15',
               'max-filesize = 4000000', 'url = ' + json.dumps(url)]
    if service.get("s3Auth"):
        options += ['aws-sigv4 = "aws:amz:auto:s3"', 'user = ' + json.dumps(service["s3Auth"])]
    try:
        result = subprocess.run(["curl", "-q", "--config", "-", "--write-out", "\nHTTP=%{http_code}"],
                                input="\n".join(options), capture_output=True, text=True, timeout=20)
        body, _, status = result.stdout.rpartition("\nHTTP=")
        response = {"status": "reachable" if not result.returncode and status == "200" else "missing" if status == "404" else "unknown",
                    "http": status, "module_hit_rate": None}
        if response["status"] == "reachable":
            rows = [json.loads(line) for line in body.splitlines() if line.strip()]
            if not rows or not isinstance(rows[0], str) or not all(isinstance(row, list) and len(row) >= 2 for row in rows[1:]):
                raise ValueError("invalid manifest")
            response["manifest_entries"] = len(rows) - 1
        return response
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unknown", "module_hit_rate": None}


def estimate(project: str | Path, targets=(), *, env: Mapping[str, str] | None = None,
             cli=None, policy="auto", cold=False, full_gate=False, memory_mib=0,
             check_remote_cache=False) -> dict:
    if policy not in {"auto", "local", "cloud"}:
        raise ValueError("policy must be auto, local, or cloud")
    if memory_mib < 0:
        raise ValueError("memory-mib must be nonnegative")
    project = Path(project).resolve()
    environment = os.environ if env is None else env
    runtime = resources.resolve_config(project, cli, environment)
    config = runtime.configuration
    graph = SourceGraph(project, environment)
    selected, entire = graph.targets(targets)
    full_gate = full_gate or entire
    # Build project reverse edges even for modules outside the selected closure.
    reverse = defaultdict(set)
    for name in graph.project_modules:
        for dep in graph.deps(name):
            reverse[dep].add(name)
    closure = graph.walk(selected, graph.deps)
    changed_names = set()
    paths = (git(project, "diff", "--name-only", "HEAD", "--", "*.lean") or "").splitlines()
    paths += (git(project, "ls-files", "--others", "--exclude-standard", "--", "*.lean") or "").splitlines()
    changed_paths = {str((project / p).resolve()) for p in paths}
    for name, module in graph.modules.items():
        if module.package == "project" and module.path and str(module.path.resolve()) in changed_paths:
            changed_names.add(name)
    initial = {n for n in closure & graph.modules.keys() if not graph.warm(n)} | (changed_names & closure)
    if cold:
        initial |= {n for n in closure & graph.modules.keys() if not graph.modules[n].toolchain}
    affected = graph.walk(initial, lambda n: reverse[n])
    closure |= graph.walk(affected, graph.deps)
    known = closure & graph.modules.keys()
    package_nodes = {n for n in known if not graph.modules[n].toolchain}
    # Missing/stale dependency outputs invalidate their users, transitively.
    invalid = {n for n in known if not graph.warm(n)} | (changed_names & known) | (affected & known)
    if cold:
        invalid |= package_nodes
    users = defaultdict(set)
    for name in known:
        for dependency in graph.deps(name):
            users[dependency].add(name)
            dep = graph.modules.get(dependency)
            if dep and dep.output.is_file() and graph.modules[name].output.is_file() and dep.output.stat().st_mtime_ns > graph.modules[name].output.stat().st_mtime_ns:
                invalid.add(name)
    invalid = graph.walk(invalid | (closure - known), lambda n: users[n]) & known
    reachable_errors = {error for name in closure for error in graph.module_errors.get(name, ())}
    unresolved = sorted((closure - graph.modules.keys()) | graph.errors | reachable_errors)
    head = git(project, "rev-parse", "HEAD")
    if head is None:
        unresolved.append("<missing Git HEAD>")
    if graph.lock_mismatches:
        # Outputs from mismatched dependencies are never certified warm.
        invalid |= package_nodes
    rebuilt = invalid & package_nodes
    reverse_count = len((affected & graph.project_modules) - selected)
    jobs = runtime.values["jobs"]
    process_mib = max(2048, runtime.values["memory_budget"] // MIB // jobs)
    heavy = sorted(n for n in rebuilt if any(fnmatch.fnmatch(n, p) or p in n or
                   p in str(graph.modules[n].path) for p in runtime.values["heavy_patterns"]))
    concurrent = min(jobs, max(1, len(rebuilt)))
    heavy_count = min(concurrent, runtime.values["heavy_jobs"], len(heavy))
    peak = max(memory_mib, (concurrent - heavy_count) * process_mib + heavy_count * max(8192, process_mib))
    seconds = [math.ceil(len(rebuilt) * 5 / max(1, jobs)), len(rebuilt) * 60]
    reasons = []
    def reason(code, **details): reasons.append({"code": code, **details})
    if full_gate: reason("full_gate")
    if cold: reason("cold_requested")
    if len(rebuilt) > config.budget.max_cold_modules:
        reason("cold_limit", actual=len(rebuilt), limit=config.budget.max_cold_modules)
    if reverse_count > config.budget.max_reverse_modules:
        reason("reverse_limit", actual=reverse_count, limit=config.budget.max_reverse_modules)
    if graph.lock_mismatches: reason("dependency_lock_mismatch", packages=len(graph.lock_mismatches))
    if unresolved: reason("unresolved_imports", count=len(unresolved))
    cpu = jobs * runtime.values["lean_threads"]
    if peak > config.local_pool_mib or math.ceil(runtime.values["memory_budget"] / MIB) > config.local_pool_mib:
        reason("pool_memory_exceeded", requested_mib=max(peak, math.ceil(runtime.values["memory_budget"] / MIB)), limit_mib=config.local_pool_mib)
    if cpu > config.local_pool_cpu:
        reason("pool_cpu_exceeded", requested_cpu=cpu, limit_cpu=config.local_pool_cpu)
    task_mib = math.ceil(runtime.values["memory_budget"] / MIB)
    if peak > task_mib:
        reason("task_memory_exceeded", requested_mib=peak, limit_mib=task_mib)
    host_mib = min(runtime.host.physical_memory, runtime.host.available_memory, runtime.host.memory_headroom) // MIB
    if max(peak, task_mib) > host_mib:
        reason("host_memory_exceeded", requested_mib=max(peak, task_mib), limit_mib=host_mib)
    if cpu > runtime.host.effective_cpus:
        reason("host_cpu_exceeded", requested_cpu=cpu, limit_cpu=runtime.host.effective_cpus)
    if policy == "cloud": reason("cloud_requested")
    return {"schema_version": 1, "head": head, "targets": list(targets), "target_modules": len(selected),
            "closure_package_modules": len(package_nodes), "closure_toolchain_modules": len(known - package_nodes),
            "reverse_invalidated_modules": reverse_count, "warm_modules": len(package_nodes - rebuilt),
            "cold_or_rebuild_modules": len(rebuilt), "unresolved_imports": unresolved,
            "dependency_lock_mismatches": graph.lock_mismatches, "heavy_modules": heavy,
            "estimated_peak_mib": peak, "estimated_seconds_range": seconds,
            "estimate_basis": {"seconds_per_module": [5, 60], "per_process_mib": process_mib,
                               "jobs": jobs, "lean_threads": runtime.values["lean_threads"],
                               "cache_validation": "trace/mtime/lock screening; native depHash not evaluated",
                               "confidence": "heuristic; excludes linking and audit overhead"},
            "full_gate": bool(full_gate), "mode": "cloud" if reasons else "local", "reasons": reasons,
            "configuration": config.explain(),
            "remote_cache": remote_cache(project, head, config) if check_remote_cache else {"status": "not_checked", "module_hit_rate": None}}


class AdmissionError(ValueError):
    def __init__(self, plan: dict, *, hard=False):
        self.plan = plan
        prefix = "本机资源硬上限拒绝准入" if hard else "默认拒绝本机重任务"
        super().__init__(prefix + ": " + json.dumps(plan, ensure_ascii=False, sort_keys=True) +
                         "；请使用 qatlib-env cloud submit，或获用户当次明确允许后传 --allow-local-heavy（不能绕过硬上限）。")


def admit(project, targets=(), *, env=None, cli=None, policy="auto", full_gate=False,
          allow_local_heavy=False) -> dict:
    plan = estimate(project, targets, env=env, cli=cli, policy=policy, full_gate=full_gate or _full_gate.get())
    allowed = bool(allow_local_heavy or _permission.get())
    plan["allow_local_heavy"] = allowed
    if any(r["code"] in {"pool_memory_exceeded", "pool_cpu_exceeded", "task_memory_exceeded",
                         "host_memory_exceeded", "host_cpu_exceeded"} for r in plan["reasons"]):
        raise AdmissionError(plan, hard=True)
    if plan["mode"] == "cloud" and (policy == "cloud" or not allowed):
        raise AdmissionError(plan)
    return plan


def command_request(argv, project: Path) -> tuple[bool, list[str], bool]:
    """Classify direct checks and opaque drivers; parsing never executes them."""
    if not argv:
        return False, [], False
    executable = Path(argv[0]).stem.casefold()
    if executable == "elan" and len(argv) > 3 and argv[1] == "run":
        # A toolchain override is an opaque/full operation, even if the nested
        # command would otherwise be a small target.
        active, targets, _ = command_request(argv[3:], project)
        return active, targets, active
    if executable.startswith(("python", "pypy")) or executable in {"bash", "sh", "zsh", "pwsh", "powershell", "cmd"}:
        return True, [], True
    if executable == "lean":
        sources = [arg for arg in argv[1:] if arg.endswith(".lean")]
        query = len(argv) == 2 and argv[1] in {"--version", "--help", "--githash", "--print-prefix"}
        return not query, sources, not sources and not query
    if executable != "lake":
        # Arbitrary drivers and wrappers cannot certify that they will not
        # launch Lean. Only the control plane's transport/maintenance commands
        # have a non-compiling contract here.
        return (False, [], False) if executable in {"git", "curl", "ssh", "pgrep", "trash-put", "gio"} else (True, [], True)
    takes_value = {"-d", "--dir", "-f", "--file", "--packages", "--config", "--toolchain",
                   "--log-level", "--fail-level", "-K"}
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in takes_value:
            index += 2
        elif arg == "--":
            index += 1
            break
        elif arg.startswith(("-", "+")):
            index += 1
        else:
            break
    if index >= len(argv):
        return False, [], False
    verb, rest = argv[index], argv[index + 1:]
    if verb not in {"build", "lean", "lint", "test", "exe", "env"}:
        return (False, [], False) if verb in {"update", "upgrade", "resolve-deps", "cache", "help", "--help", "--version"} else (True, [], True)
    for index, arg in enumerate(argv[1:index], 1):
        directory = argv[index + 1] if arg in {"-d", "--dir"} and index + 1 < len(argv) else arg.split("=", 1)[1] if arg.startswith("--dir=") else arg[2:] if arg.startswith("-d") and len(arg) > 2 else None
        if directory and (project / directory).resolve() != project.resolve():
            raise ValueError("Lake directory override must match --project; select the project explicitly")
        toolchain = argv[index + 1] if arg == "--toolchain" and index + 1 < len(argv) else arg.split("=", 1)[1] if arg.startswith("--toolchain=") else arg[1:] if arg.startswith("+") else None
        if toolchain:
            pinned = project / "lean-toolchain"
            if not pinned.is_file() or toolchain != pinned.read_text().strip():
                raise ValueError("Lake toolchain override must match the pinned lean-toolchain")
        alternate = argv[index + 1] if arg in {"-f", "--file"} and index + 1 < len(argv) else arg.split("=", 1)[1] if arg.startswith("--file=") else arg[2:] if arg.startswith("-f") and len(arg) > 2 else None
        if alternate and (project / alternate).resolve() != (project / "lakefile.toml").resolve():
            raise ValueError("alternate Lake configuration cannot bypass the planned project inputs")
    if verb == "env":
        # Environment wrappers can spawn an unrestricted compiler/driver.
        return True, [], True
    if verb == "lean":
        return True, [arg for arg in rest if arg.endswith(".lean")], False
    if verb == "build":
        targets = []
        skip = False
        for arg in rest:
            if skip:
                skip = False
            elif arg in {"-o", "--out", "-j", "--jobs"}:
                skip = True
            elif not arg.startswith("-"):
                targets.append(arg.split(":", 1)[0].removeprefix("+"))
        return True, targets, not targets
    # Include a declared driver in its own import closure as well as flagging
    # its implicit audit behavior.
    conf = SourceGraph._toml(project / "lakefile.toml")
    driver = rest[0] if verb == "exe" and rest else conf.get("lintDriver" if verb == "lint" else "testDriver")
    return True, [driver] if isinstance(driver, str) else [], True
