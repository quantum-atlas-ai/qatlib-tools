from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import tomllib


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

    @classmethod
    def from_mapping(cls, data: dict[str, object]) -> "EnvConfig":
        def integer(name: str, fallback: int, minimum: int = 1) -> int:
            try:
                value = int(data.get(name, fallback))
            except (TypeError, ValueError):
                value = fallback
            return max(minimum, value)
        roots = data.get("source_roots", ["."])
        if not isinstance(roots, list | tuple) or not roots:
            roots = ["."]
        return cls(tuple(str(x) for x in roots), str(data.get("cache_dir", ".lake")),
                   integer("local_pool_mib", 24576), integer("local_pool_cpu", 4),
                   integer("default_jobs", 2), max(2048, integer("default_memory_mib", 2048)),
                   str(data.get("cloud_platform", "auto")),
                   str(data["repository"]) if data.get("repository") else None,
                   str(data["workflow"]) if data.get("workflow") else None, dict(data))


def load_config(project: str | Path = ".") -> EnvConfig:
    root = Path(project).resolve()
    merged: dict[str, object] = {}
    for path in (root / "pyproject.toml", root / "qatlib-env.toml", root / ".qatlib-env.local.toml"):
        if not path.is_file():
            continue
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        section = raw.get("tool", {}).get("qatlib-env", {}) if path.name == "pyproject.toml" else raw.get("tool", {}).get("qatlib-env", raw)
        if isinstance(section, dict):
            merged.update(section)
    return EnvConfig.from_mapping(merged)


def choose_mode(policy: str, *, cold: bool = False, memory_mib: int = 0,
                full_gate: bool = False, pool_mib: int = 24576) -> str:
    policy = policy.lower()
    if policy in {"local", "cloud"}:
        return policy
    if policy != "auto":
        raise ValueError("policy must be auto, local, or cloud")
    return "cloud" if cold or full_gate or memory_mib > pool_mib else "local"

