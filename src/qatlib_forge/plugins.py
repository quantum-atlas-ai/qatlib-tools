from __future__ import annotations

from importlib import metadata


def discover() -> dict[str, object]:
    found: dict[str, object] = {}
    for entry in metadata.entry_points(group="qatlib_forge.plugins"):
        if entry.name in found: raise RuntimeError(f"duplicate plugin: {entry.name}")
        found[entry.name] = entry.load()
    return found

