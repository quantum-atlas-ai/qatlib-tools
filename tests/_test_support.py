"""Use this checkout's self-contained tools, including child processes."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIRECTORIES = {name: "python/" + name for name in ("qatlib_env", "qatlib_forge", "qatlib_search")}


def activate_packages() -> dict:
    python_root = str(ROOT / "python")
    if python_root not in sys.path:
        sys.path.insert(0, python_root)
    return {name: importlib.import_module(name) for name in PACKAGE_DIRECTORIES}


def subprocess_environment() -> dict[str, str]:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env["PYTHONPATH"] = str(ROOT / "python") + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


PACKAGES = activate_packages()
