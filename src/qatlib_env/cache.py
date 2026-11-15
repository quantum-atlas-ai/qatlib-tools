from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path

from .workspace import fetch_lock


def cache_key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def write_cache_manifest(root: str | Path, entries: dict[str, str]) -> Path:
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    path = root / "qatlib-cache-manifest.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}, sort_keys=True, indent=2), encoding="utf-8")
    return path


def read_cache_manifest(path: str | Path) -> dict[str, str]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return dict(data.get("entries", {}))


@contextmanager
def cache_lock(root: str | Path = "."):
    with fetch_lock(root):
        yield

