"""One read-only, validated configuration pipeline for policy and execution."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Mapping

RESOURCE_KEYS = frozenset({"jobs", "lean_threads", "memory_budget", "heavy_jobs",
                           "heavy_patterns", "module_timeout", "stage_timeout"})
BUDGET_DEFAULTS = {"max_cold_modules": 15, "max_reverse_modules": 15,
                   "require_explicit_local_heavy": True}
ROUTE_DEFAULTS = {"source_roots": ["."], "cache_dir": ".lake", "local_pool_mib": 24576,
                  "local_pool_cpu": 4, "cloud_platform": "auto"}
PROFILE_KEYS = frozenset({"repository", "workflow", "library", "tests", "ref", "cache_repository",
                          "cache_bucket", "cache_key_variable", "gitea_host", "search_source_roots",
                          "search_module_roots"})
ENV_FIELDS = {
    "source_roots": ("QATLIB_SOURCE_ROOTS",), "cache_dir": ("QATLIB_CACHE_DIR",),
    "search_source_roots": ("QATLIB_SEARCH_SOURCE_ROOTS",),
    "search_module_roots": ("QATLIB_SEARCH_MODULE_ROOTS",),
    "local_pool_mib": ("QATLAS_POOL_BUDGET_MIB",), "local_pool_cpu": ("QATLAS_POOL_CPU",),
    "repository": ("QATLIB_REPOSITORY",), "workflow": ("QATLIB_WORKFLOW",),
    "cloud_platform": ("QATLIB_CLOUD_PLATFORM",), "library": ("QATLIB_LIBRARY",),
    "tests": ("QATLIB_TEST_TARGET",),
    "jobs": ("QATLAS_JOBS",), "lean_threads": ("QATLAS_LEAN_THREADS", "LEAN_NUM_THREADS"),
    "memory_budget": ("QATLAS_MEMORY_BUDGET",), "heavy_jobs": ("QATLAS_HEAVY_JOBS",),
    "heavy_patterns": ("QATLAS_HEAVY_PATTERNS",), "module_timeout": ("QATLAS_MODULE_TIMEOUT",),
    "stage_timeout": ("QATLAS_STAGE_TIMEOUT",),
    "max_cold_modules": ("QATLAS_MAX_COLD_MODULES",),
    "max_reverse_modules": ("QATLAS_MAX_REVERSE_MODULES",),
}


def memory_bytes(value: object) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("memory size must be an integer byte count or a size string")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:i?B)?\s*", str(value), re.I)
    if not match:
        raise ValueError(f"invalid memory size: {value!r}")
    result = int(float(match[1]) * 1024 ** (" KMGTPE".index(match[2].upper())))
    if result < 1:
        raise ValueError("memory size must be positive")
    return result


def local_file(project: Path, name: str) -> Path | None:
    """Select this worktree's file, then the primary checkout's (never copy it)."""
    project = project.resolve()
    path = project / name
    if path.exists():
        if not path.is_file():
            raise ValueError(f"{path}: local configuration must be a regular file")
        return path
    result = subprocess.run(["git", "-C", str(project), "rev-parse", "--git-common-dir"],
                            capture_output=True, text=True,
                            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1"})
    if result.returncode == 0:
        common = Path(result.stdout.strip())
        common = common if common.is_absolute() else project / common
        path = common.resolve().parent / name
        if path.is_file():
            return path
    return None


def _validate(values: Mapping[str, object], source: str, *, strings: bool = False) -> dict:
    result = {}
    aliases = {"default_jobs": "jobs", "default_memory_mib": "memory_budget"}
    allowed = set(ROUTE_DEFAULTS) | PROFILE_KEYS | RESOURCE_KEYS | BUDGET_DEFAULTS.keys() | aliases.keys()
    # Canonical spelling wins over aliases in the same table, regardless of order.
    for key in sorted(values, key=lambda k: k not in aliases):
        value = values[key]
        if key not in allowed:
            raise ValueError(f"{source}: unknown configuration options: {key}")
        if key in {"jobs", "default_jobs", "lean_threads", "heavy_jobs", "local_pool_mib",
                   "local_pool_cpu", "default_memory_mib", "max_cold_modules", "max_reverse_modules"}:
            if strings and isinstance(value, str) and value.isdigit():
                value = int(value)
            minimum = 0 if key in BUDGET_DEFAULTS else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{source}: {key} must be an integer >= {minimum}")
            if key == "default_memory_mib":
                value = max(2048, value) * 1024**2
        elif key in {"source_roots", "heavy_patterns", "search_source_roots", "search_module_roots"}:
            if strings and isinstance(value, str):
                value = [part.strip() for part in value.split(",") if part.strip()]
            if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
                raise ValueError(f"{source}: {key} must be an array of nonempty strings")
            if key == "source_roots" and not value:
                raise ValueError(f"{source}: source_roots must not be empty")
            value = list(value)
        elif key in {"module_timeout", "stage_timeout"}:
            if strings:
                try:
                    value = float(value)
                except (ValueError, TypeError):
                    raise ValueError(f"{source}: {key} must be a nonnegative finite number") from None
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{source}: {key} must be a nonnegative finite number")
        elif key == "require_explicit_local_heavy":
            if type(value) is not bool:
                raise ValueError(f"{source}: {key} must be a boolean")
            if not value:
                raise ValueError(f"{source}: persistent local-heavy permission is forbidden; use --allow-local-heavy")
        elif key == "memory_budget":
            value = memory_bytes(value)
        elif not isinstance(value, str) or not value.strip():
            raise ValueError(f"{source}: {key} must be a nonempty string")
        if key == "cloud_platform" and value not in {"auto", "github", "gitea"}:
            raise ValueError(f"{source}: cloud_platform must be auto, github, or gitea")
        result[aliases.get(key, key)] = value
    return result


def read_layer(path: Path, *, explicit: bool = False) -> dict:
    if not path.exists() and not explicit:
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: invalid TOML: {error}") from error
    tool = raw.get("tool", {})
    if not isinstance(tool, dict):
        raise ValueError(f"{path}: tool must be a TOML table")
    data = tool.get("qatlib-env", {}) if path.name == "pyproject.toml" else tool.get("qatlib-env", raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: qatlib-env must be a TOML table")
    result = _validate({k: v for k, v in data.items() if k not in {"resources", "budget"}}, str(path))
    for section, allowed in (("resources", RESOURCE_KEYS | {"default_jobs", "default_memory_mib"}),
                             ("budget", BUDGET_DEFAULTS.keys())):
        nested = data.get(section, {})
        if not isinstance(nested, dict):
            raise ValueError(f"{path}: {section} must be a TOML table")
        unknown = set(nested) - set(allowed)
        if unknown:
            kind = "resource" if section == "resources" else section
            raise ValueError(f"{path}: unknown {kind} options: {', '.join(sorted(unknown))}")
        result.update(_validate(nested, f"{path}.{section}"))
    return result


def layers(project: Path, env: Mapping[str, str]) -> list[tuple[str, dict]]:
    result = []
    from .project import inferred_profile
    result.append((f"lake:{project / 'lakefile.toml'}", inferred_profile(project)))
    if env.get("QATLAS_RUNNER_CONFIG"):
        path = Path(env["QATLAS_RUNNER_CONFIG"])
        result.append((f"runner:{path}", read_layer(path, explicit=True)))
    # Read-only compatibility layer. No directory or file is ever created.
    home = Path(env.get("HOME", str(Path.home())))
    path = Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "qatlib/env.toml"
    result.append((f"user:{path}", read_layer(path)))
    path = home / ".qatlib/env.toml"
    result.append((f"user:{path}", read_layer(path)))
    shared = [project / "pyproject.toml", project / "qatlib-env.toml"]
    if not any(path.exists() for path in shared):
        shared = [project / "python/pyproject.toml"]
    for path in shared:
        result.append((f"workspace:{path}", read_layer(path)))
    local = local_file(project, ".qatlib-env.local.toml")
    if local:
        result.append((f"local:{local}", read_layer(local)))
    return result


@dataclass(frozen=True)
class BudgetConfig:
    max_cold_modules: int = 15
    max_reverse_modules: int = 15
    require_explicit_local_heavy: bool = True


@dataclass(frozen=True)
class EnvConfig:
    source_roots: tuple[str, ...] = (".",)
    cache_dir: str = ".lake"
    local_pool_mib: int = 24576
    local_pool_cpu: int = 4
    default_jobs: int = 2
    default_memory_mib: int = 2048
    cloud_platform: str = "auto"
    repository: str | None = None
    workflow: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    resource_values: dict[str, object] = field(default_factory=dict)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    sources: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "EnvConfig":
        values = _validate(data, "configuration")
        defaults = {**ROUTE_DEFAULTS, **BUDGET_DEFAULTS, **values}
        return cls(source_roots=tuple(defaults["source_roots"]), cache_dir=defaults["cache_dir"],
                   local_pool_mib=defaults["local_pool_mib"], local_pool_cpu=defaults["local_pool_cpu"],
                   default_jobs=int(values.get("jobs", 2)),
                   default_memory_mib=max(2048, int(values.get("memory_budget", 2048 * 1024**2)) // 1024**2),
                   cloud_platform=defaults["cloud_platform"], repository=values.get("repository"),
                   workflow=values.get("workflow"), metadata=values,
                   resource_values={k: v for k, v in values.items() if k in RESOURCE_KEYS},
                   budget=BudgetConfig(**{k: defaults[k] for k in BUDGET_DEFAULTS}))

    def explain(self) -> dict:
        return {"routing": {k: getattr(self, k) for k in ROUTE_DEFAULTS.keys() | {"repository", "workflow"}},
                "budget": asdict(self.budget), "sources": self.sources}


def load_config(project: str | Path = ".", env: Mapping[str, str] | None = None,
                cli: Mapping[str, object] | None = None) -> EnvConfig:
    environment = os.environ if env is None else env
    merged, sources = {}, dict.fromkeys(ROUTE_DEFAULTS.keys() | BUDGET_DEFAULTS.keys(), "automatic")
    for source, values in layers(Path(project).resolve(), environment):
        merged.update(values)
        sources.update(dict.fromkeys(values, source))
    for key, variables in ENV_FIELDS.items():
        for variable in variables:
            if variable in environment:
                merged.update(_validate({key: environment[variable]}, f"environment:{variable}", strings=True))
                sources[key] = f"environment:{variable}"
                break
    for key, value in (cli or {}).items():
        if value is not None:
            parsed = _validate({key: value}, "command-line", strings=True)
            merged.update(parsed)
            sources.update(dict.fromkeys(parsed, "command-line"))
    config = EnvConfig.from_mapping(merged)
    object.__setattr__(config, "sources", sources)
    return config


def choose_mode(policy: str, *, cold: bool = False, memory_mib: int = 0,
                full_gate: bool = False, pool_mib: int = 24576) -> str:
    if policy not in {"auto", "local", "cloud"}:
        raise ValueError("policy must be auto, local, or cloud")
    # A local preference cannot cancel an unsafe/heavy recommendation.
    return "cloud" if policy == "cloud" or cold or full_gate or memory_mib > pool_mib else "local"

