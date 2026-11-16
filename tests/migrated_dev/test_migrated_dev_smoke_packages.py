"""Exercise the single wheel and editable distribution outside the checkout."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="qatlib-package-smoke-") as directory:
        temporary = Path(directory)
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        venv = temporary / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], env=env, check=True)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        wheel_dir = temporary / "wheels"
        subprocess.run(["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
                       cwd=source, env=env, check=True)
        wheel, = wheel_dir.glob("qatlib_tools-*.whl")
        code = """
from importlib import import_module, metadata, resources
from pathlib import Path
import json, pkgutil, sys
for package in ("qatlib_env", "qatlib_forge", "qatlib_search"):
    module = import_module(package)
    location = Path(module.__file__).resolve()
    expected = Path(sys.prefix) if sys.argv[1] == "wheel" else Path(sys.argv[2])
    assert location.is_relative_to(expected.resolve()), location
    for entry in pkgutil.iter_modules(module.__path__):
        if entry.name != "__main__": import_module(package + "." + entry.name)
assets = resources.files("qatlib_search")
assert "import Lean" in assets.joinpath("ExportSearchIndex.lean").read_text()
assert isinstance(json.loads(assets.joinpath("retrieval-report.schema.json").read_text()), dict)
assert resources.files("qatlib_env").joinpath("setup_runner.sh").is_file()
from qatlib_env.workspace import main
assert main(["docs"]) == 0
entrypoints = {entry.name: entry for entry in metadata.distribution("qatlib-tools").entry_points}
for name in ("qatlib-env", "qatlib-forge", "qatlib-search"):
    assert callable(entrypoints[name].load())
print("PACKAGE_SMOKE_OK:", sys.argv[1])
"""
        for mode in ("wheel", "editable"):
            install = ["--editable", str(source)] if mode == "editable" else [str(wheel)]
            subprocess.run(["uv", "pip", "install", "--python", str(python), *install],
                           cwd=temporary, env=env, check=True)
            subprocess.run([str(python), "-I", "-B", "-c", code, mode, str(source)],
                           cwd=temporary, env=env, check=True)
            result = subprocess.run([str(bin_dir / "qatlib-env"), "docs"], cwd=temporary,
                                    env=env, check=True, capture_output=True)
            assert result.stdout == (source / "README.md").read_bytes() + b"\n"
            for cli in ("qatlib-env", "qatlib-forge", "qatlib-search"):
                subprocess.run([str(bin_dir / cli), "--help"], cwd=temporary, env=env, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
