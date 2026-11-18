"""Resource discovery and explicit, explainable build configuration.

This module never changes cgroups or silently lowers a user's configured values.
All sizes in the Python API are bytes and all timeouts are seconds (zero disables).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping

GIB = 1024 ** 3
MIB = 1024 ** 2


def memory_bytes(value: Any) -> int:
    """Parse a byte count or a binary size such as ``32GiB`` or ``32G``."""
    if isinstance(value, bool):
        raise ValueError("memory sizes cannot be booleans")
    if isinstance(value, int):
        result = value
    else:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTPE]?)(?:i?B)?\s*", str(value), re.I)
        if not match:
            raise ValueError(f"invalid memory size: {value!r}")
        result = int(float(match[1]) * 1024 ** (" KMGTPE".index(match[2].upper())))
    if result <= 0:
        raise ValueError("memory size must be positive")
    return result


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return None


def _cpus(value: str) -> set[int]:
    result: set[int] = set()
    for part in value.split(","):
        if not part:
            continue
        ends = part.split("-")
        result.update(range(int(ends[0]), int(ends[-1]) + 1))
    return result


@dataclass(frozen=True)
class HostResources:
    physical_memory: int
    available_memory: int
    cpu_count: int
    effective_cpus: int
    memory_headroom: int
    limits: dict[str, Any] = field(default_factory=dict)

    @property
    def reserve(self) -> int:
        return max(2 * GIB, self.physical_memory // 10)

    @property
    def memory_budget(self) -> int:
        # Reserve OS headroom at the host level. A cgroup already expresses a
        # bounded allocation: subtracting a large host's reserve from that quota
        # can turn an otherwise usable runner into a one-byte automatic pool.
        return max(1, min(self.physical_memory - self.reserve,
                          self.available_memory - self.reserve, self.memory_headroom))


def _ancestors(root: Path, relative: str) -> list[Path]:
    # Cgroup namespaces can expose a host-relative membership while mounting only
    # the namespace's root. In that case inspect the visible root, never escape it.
    child = root / relative.lstrip("/")
    if ".." in child.parts or not child.exists():
        child = root
    result = []
    while True:
        result.append(child)
        if child == root:
            return result
        child = child.parent


def _memory_headrooms(directory: Path, entry: dict[str, Any], *, v2: bool) -> list[int]:
    """Keep charged memory as the admission bound, including file cache.

    LRU counters do not promise reclaimable capacity. Missing or invalid usage
    must not turn a known finite limit into an entirely unused allocation.
    These reads are a snapshot, not a reservation; kernel caps still apply.
    """
    finite = []
    try:
        names = ("memory.max", "memory.high") if v2 else ("memory.limit_in_bytes", "memory.soft_limit_in_bytes")
        for name in names:
            raw = _read(directory / name)
            if raw is None or raw == "max":
                continue
            if not raw.isascii() or not raw.isdecimal():
                raise ValueError(f"invalid {name}")
            limit = int(raw)
            if not v2 and (limit == 0 or limit >= 2 ** 60):
                continue
            finite.append(limit)
            entry[name] = limit
        if not finite:
            return []
        usage = "memory.current" if v2 else "memory.usage_in_bytes"
        raw = _read(directory / usage)
        if raw is None or not raw.isascii() or not raw.isdecimal():
            raise ValueError(f"missing or invalid {usage}")
        current = int(raw)
        entry["memory.current"] = current
        return [max(0, value - current) for value in finite]
    except (OSError, ValueError) as error:
        entry["memory.error"] = str(error)
        return [0]


def probe_resources(*, proc_root: Path = Path("/proc"),
                    cgroup_root: Path = Path("/sys/fs/cgroup"),
                    cpu_count: int | None = None, affinity: set[int] | None = None) -> HostResources:
    """Read memory, affinity, cpusets and *all ancestor* cgroup v1/v2 limits.

    ``proc_root`` and ``cgroup_root`` allow tests to use real filesystem fixtures;
    explicit ``cpu_count``/``affinity`` isolate fixture values from the test host.
    """
    memory = {}
    for line in (_read(proc_root / "meminfo") or "").splitlines():
        key, _, value = line.partition(":")
        fields = value.split()
        if fields:
            memory[key] = int(fields[0]) * (1024 if len(fields) > 1 and fields[1] == "kB" else 1)
    if "MemTotal" not in memory:
        # Non-Linux platforms have no /proc. Avoid an aggressive CPU-only default.
        try:
            import psutil
            virtual = psutil.virtual_memory()
            memory = {"MemTotal": virtual.total, "MemAvailable": virtual.available}
        except Exception:
            memory = {"MemTotal": 4 * GIB, "MemAvailable": 4 * GIB}
    physical = memory["MemTotal"]
    available = memory.get("MemAvailable", memory.get("MemFree", physical))
    count = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    if affinity is None:
        affinity = set(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else set(range(count))
    allowed = set(affinity)
    quotas: list[float] = []
    headrooms = [physical]
    limits: dict[str, Any] = {"affinity": sorted(affinity), "cgroups": []}
    memberships = []
    for line in (_read(proc_root / "self/cgroup") or "").splitlines():
        number, controllers, relative = line.split(":", 2)
        memberships.append((controllers.split(",") if controllers else [], relative))
    for controllers, relative in memberships:
        v2 = not controllers
        roots = [(cgroup_root, {"cpu", "memory", "cpuset"})] if v2 else [
            (cgroup_root / controller, {controller}) for controller in controllers
        ]
        # The usual combined v1 mount is cpu,cpuacct rather than cpu.
        if not v2 and "cpu" in controllers and not (cgroup_root / "cpu").exists():
            roots = [(cgroup_root / "cpu,cpuacct" if root.name == "cpu" else root, kinds)
                     for root, kinds in roots]
        for root, kinds in roots:
            for directory in _ancestors(root, relative):
                entry: dict[str, Any] = {"path": str(directory)}
                if "cpu" in kinds:
                    if v2:
                        raw = _read(directory / "cpu.max")
                        if raw and raw.split()[0] != "max":
                            quota, period = map(int, raw.split())
                            if quota > 0 and period > 0:
                                quotas.append(quota / period)
                                entry["cpu_quota"] = quota / period
                    else:
                        quota = _read(directory / "cpu.cfs_quota_us")
                        period = _read(directory / "cpu.cfs_period_us")
                        if quota and period and int(quota) > 0 and int(period) > 0:
                            quotas.append(int(quota) / int(period))
                            entry["cpu_quota"] = int(quota) / int(period)
                if "cpuset" in kinds:
                    raw = (_read(directory / "cpuset.cpus.effective") or _read(directory / "cpuset.cpus"))
                    if raw:
                        cpuset = _cpus(raw)
                        allowed.intersection_update(cpuset)
                        entry["cpuset"] = sorted(cpuset)
                if "memory" in kinds:
                    headrooms.extend(_memory_headrooms(directory, entry, v2=v2))
                if len(entry) > 1:
                    limits["cgroups"].append(entry)
    quota_count = math.floor(min(quotas)) if quotas else count
    effective = max(1, min(count, len(allowed), quota_count))
    limits["effective_cpuset"] = sorted(allowed)
    limits["cpu_quota"] = min(quotas) if quotas else None
    return HostResources(physical, available, count, effective, min(headrooms), limits)


ENV_KEYS = {
    "jobs": ("QATLAS_JOBS",),
    "lean_threads": ("QATLAS_LEAN_THREADS", "LEAN_NUM_THREADS"),
    "memory_budget": ("QATLAS_MEMORY_BUDGET",),
    "heavy_jobs": ("QATLAS_HEAVY_JOBS",),
    "module_timeout": ("QATLAS_MODULE_TIMEOUT",),
    "stage_timeout": ("QATLAS_STAGE_TIMEOUT",),
    "heavy_patterns": ("QATLAS_HEAVY_PATTERNS",),
}


@dataclass
class ResolvedConfig:
    values: dict[str, Any]
    sources: dict[str, str]
    host: HostResources
    warnings: list[str]
    configuration: Any = None
    automatic: dict[str, Any] = field(default_factory=dict)

    def explain(self) -> dict[str, Any]:
        return {"values": self.values, "sources": self.sources, "warnings": self.warnings,
                "automatic": self.automatic,
                "configuration": self.configuration.explain() if self.configuration else {},
                "resources": {"physical_memory": self.host.physical_memory,
                              "available_memory": self.host.available_memory,
                              "memory_headroom": self.host.memory_headroom,
                              "reserve": self.host.reserve,
                              "logical_cpus": self.host.cpu_count,
                              "effective_cpus": self.host.effective_cpus,
                              "limits": self.host.limits}}

    def environment(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        result = dict(os.environ if base_env is None else base_env)
        result["LEAN_NUM_THREADS"] = str(self.values["lean_threads"])
        for name, variables in ENV_KEYS.items():
            value = self.values[name]
            result[variables[0]] = ",".join(value) if isinstance(value, list) else str(value)
        if self.configuration is not None:
            result["QATLAS_POOL_BUDGET_MIB"] = str(self.configuration.local_pool_mib)
            result["QATLAS_POOL_CPU"] = str(self.configuration.local_pool_cpu)
            result["QATLAS_MAX_COLD_MODULES"] = str(self.configuration.budget.max_cold_modules)
            result["QATLAS_MAX_REVERSE_MODULES"] = str(self.configuration.budget.max_reverse_modules)
        return result


def _config_file(path: Path, *, explicit: bool = False) -> dict[str, Any]:
    from .config import read_layer
    return {key: value for key, value in read_layer(path, explicit=explicit).items() if key in ENV_KEYS}


def _task_defaults(cpu: int, memory: int, explicit: Mapping[str, Any]) -> dict[str, Any]:
    # Leave capacity for other builds when the machine has room. Small pools
    # can devote their whole capacity to one task. Hard limits still apply.
    task_cpu = min(cpu, max(2, cpu // 2), 8)
    task_memory = min(memory, max(4 * GIB, memory // 2), 16 * GIB)
    budget = explicit.get("memory_budget", task_memory)
    threads = explicit.get("lean_threads", min(2, max(1, task_cpu // explicit.get("jobs", 1))))
    jobs = explicit.get("jobs", max(1, min(task_cpu // threads, budget // (4 * GIB))))
    return {"jobs": jobs, "lean_threads": threads, "memory_budget": budget,
            "heavy_jobs": 1, "module_timeout": 1800.0, "stage_timeout": 10800.0,
            "heavy_patterns": ["BalancedGram"]}


def resolve_config(repo: Path, cli: Mapping[str, Any] | None = None,
                   env: Mapping[str, str] | None = None, *,
                   probe: HostResources | None = None) -> ResolvedConfig:
    """Resolve CLI > environment > local > shared > compatibility > defaults."""
    from .config import load_config
    environment = os.environ if env is None else env
    host = probe if probe is not None else probe_resources()
    configuration = load_config(repo, environment, cli)
    pool_mib = max(1, host.memory_budget // MIB)
    pool_cpu = host.effective_cpus
    automatic = {**_task_defaults(pool_cpu, min(host.memory_budget, pool_mib * MIB), {}),
                 "local_pool_mib": pool_mib, "local_pool_cpu": pool_cpu}
    # Unconfigured pools adapt to host capacity; an explicit pool is never
    # enlarged by either a host probe or an individual task request.
    configuration = replace(configuration,
        local_pool_mib=pool_mib if configuration.sources["local_pool_mib"] == "automatic" else configuration.local_pool_mib,
        local_pool_cpu=pool_cpu if configuration.sources["local_pool_cpu"] == "automatic" else configuration.local_pool_cpu)
    values = _task_defaults(min(pool_cpu, configuration.local_pool_cpu),
                            min(host.memory_budget, configuration.local_pool_mib * MIB),
                            configuration.resource_values)
    sources = dict.fromkeys(values, "automatic")
    values.update(configuration.resource_values)
    sources.update({key: configuration.sources[key] for key in configuration.resource_values})
    for key in ("jobs", "lean_threads", "heavy_jobs"):
        if key not in values:
            continue
        value = values[key]
        if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
            raise ValueError(f"{key} must be a positive integer ({sources[key]})")
        values[key] = int(value)
    values["memory_budget"] = memory_bytes(values["memory_budget"])
    for key in ("module_timeout", "stage_timeout"):
        value = values[key]
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a nonnegative number")
        values[key] = float(value)
        if not math.isfinite(values[key]) or values[key] < 0:
            raise ValueError(f"{key} must be a nonnegative finite number")
    patterns = values["heavy_patterns"]
    if isinstance(patterns, str):
        patterns = [part.strip() for part in patterns.split(",") if part.strip()]
    if not isinstance(patterns, list) or not all(isinstance(part, str) and part for part in patterns):
        raise ValueError("heavy_patterns must be an array of nonempty strings")
    values["heavy_patterns"] = patterns
    warnings = []
    if values["jobs"] * values["lean_threads"] > min(host.effective_cpus, configuration.local_pool_cpu):
        warnings.append("jobs × lean_threads exceeds effective or pool CPU capacity")
    if values["memory_budget"] > min(host.physical_memory, host.available_memory, host.memory_headroom,
                                      configuration.local_pool_mib * MIB):
        warnings.append("memory_budget exceeds available, cgroup or pool memory")
    elif min(host.memory_budget, configuration.local_pool_mib * MIB) < 2 * GIB:
        warnings.append("less than 2 GiB is available for the minimum build request")
    if values["jobs"] * 4 * GIB > values["memory_budget"]:
        warnings.append("jobs exceed the 4 GiB/job memory estimate")
    if values["heavy_jobs"] > values["jobs"]:
        warnings.append("heavy_jobs exceeds jobs; explicit values were retained")
    return ResolvedConfig(values, sources, host, warnings, configuration, automatic)

