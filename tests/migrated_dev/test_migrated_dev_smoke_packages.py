"""Exercise the single wheel and editable distribution outside the checkout."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    source = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="qatlas-package-smoke-") as directory:
        temporary = Path(directory)
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}}
        env.update(PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(temporary),
                   PIP_CACHE_DIR=str(temporary / "pip-cache"))
        venv = temporary / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=temporary, env=env, check=True)
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        def run(*args: str) -> None:
            subprocess.run([str(python), *args], cwd=temporary, env=env, check=True)
        run("-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", "hatchling", "editables")
        wheels = temporary / "wheels"
        run("-m", "pip", "wheel", "--disable-pip-version-check", "--no-cache-dir",
            "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), str(source))
        wheel, = wheels.glob("qatlib_tools-*.whl")
        run("-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir", str(wheel))
        code = '''
from importlib import import_module, metadata, resources
from pathlib import Path
import json, pkgutil, shutil, sys
for package in ("qatlib_env", "qatlib_forge", "qatlib_search"):
    module = import_module(package)
    location = Path(module.__file__).resolve()
    if sys.argv[1] == "wheel":
        assert location.is_relative_to(Path(sys.prefix).resolve()), location
    else:
        assert location.is_relative_to(Path(sys.argv[2]).resolve()), location
    for entry in pkgutil.iter_modules(module.__path__):
        if entry.name != "__main__": import_module(package + "." + entry.name)
assets = resources.files("qatlib_search")
assert "import Lean" in assets.joinpath("ExportSearchIndex.lean").read_text()
assert isinstance(json.loads(assets.joinpath("retrieval-report.schema.json").read_text()), dict)
manual = resources.files("qatlib_env")
assert "qatlib-env" in manual.joinpath("README.md").read_text()
for name in ("cache.md", "resources.md", "runners.md"):
    assert manual.joinpath("docs", name).is_file(), name
for package in ("qatlib_forge", "qatlib_search", "dev_metadata"):
    assert resources.files(package).joinpath("README.md").is_file()
assert resources.files("qatlib_env").joinpath("setup_runner.sh").is_file()
assert resources.files("dev_metadata").joinpath("demoted_pending_public_sync.txt").is_file()
import jsonschema
assert jsonschema.Draft202012Validator is not None
from qatlib_search.build_search_index import write_driver
write_driver(Path.cwd(), ["Example.Main"], Path("driver.lean"))
assert "import Example.Main" in Path("driver.lean").read_text()
assert metadata.requires("example-library") == ["jsonschema<5,>=4.18"]
entrypoints = {entry.name: entry for entry in metadata.distribution("example-library").entry_points}
for name, value in {"qatlib-env": "qatlib_env.workspace:cli_main",
                    "qatlas-ci": "qatlib_env.ci:cli_main",
                    "qatlas-qatlib_forge": "qatlib_forge.cli:main"}.items():
    assert entrypoints[name].value == value
    assert callable(entrypoints[name].load())
from example_library_metadata import check_meta_v2
target = Path.cwd() / ("minimal-" + sys.argv[1])
schemas = target / "metadata/schemas"
shutil.copytree(Path(sys.argv[2]).parent / "metadata/schemas", schemas)
for index in ("units", "papers"):
    (target / "metadata" / (index + ".json")).write_text("{}\\n")
result = check_meta_v2.run(target)
assert not result.errors(), result.as_json()
# A target-only constraint must take effect in both installed modes. This
# catches validators that silently load schemas relative to site-packages.
unit_schema = schemas / "units-index.schema.json"
constraint = json.loads(unit_schema.read_text())
constraint["minProperties"] = 1
unit_schema.write_text(json.dumps(constraint))
result = check_meta_v2.run(target)
assert any(f.code == "index.schema" and f.file == "metadata/units.json"
           for f in result.errors()), result.as_json()
print("PACKAGE_SMOKE_OK:", sys.argv[1], "four modules, dependency, CLI entries, assets and target schemas")
'''
        for mode in ("wheel", "editable"):
            if mode == "editable":
                run("-m", "pip", "uninstall", "--yes", "example-library")
                run("-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
                    "--no-deps", "--no-build-isolation", "--editable", str(source))
            run("-I", "-B", "-m", "qatlib_env", "--help")
            run("-I", "-B", "-m", "qatlib_forge", "--help")
            for cli in ("qatlib-env", "qatlas-ci", "qatlas-qatlib_forge"):
                subprocess.run([str(bin_dir / cli), "--help"], cwd=temporary, env=env, check=True)
            run("-I", "-B", "-m", "dev_metadata.format_lean_style", "--help")
            run("-I", "-B", "-c", code, mode, str(source))
    print("PACKAGE_SMOKE_TEMP_CLEANED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
