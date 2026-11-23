"""Portable Lean workspace and repository infrastructure (standard library only)."""

from importlib.metadata import version


def __getattr__(name: str) -> str:
    if name == "__version__":
        return version("qatlib-tools")
    raise AttributeError(name)

