"""Build the actual wheel offline and inspect its versioned manual resources."""
import tests._bootstrap as _bootstrap  # noqa: F401
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest
import zipfile


class WheelResourcesTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("hatchling"), "wheel gate requires the build backend hatchling")
    def test_wheel_contains_the_complete_installed_manual(self):
        package = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory(prefix="qatlas-wheel-test-") as directory:
            subprocess.run([sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-t", "sdist", "-d", directory],
                           cwd=package, check=True, capture_output=True, text=True)
            wheels = list(Path(directory).glob("*.whl"))
            self.assertEqual(len(wheels), 1)
            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())
                for path in ("_manual.md",
                             "resources.py", "supervisor.py", "compiler.py", "runner_config.py", "prepare.py", "checkpoint.py", "r2_cache.py", "setup_runner.sh"):
                    self.assertIn("qatlib_env/" + path, names)
                self.assertEqual((package / "README.md").read_bytes(), wheel.read("qatlib_env/_manual.md"))
                for module in ("qatlib_env", "qatlib_forge", "qatlib_search"):
                    self.assertIn(module + "/__init__.py", names)
                    self.assertNotIn(module + "/pyproject.toml", names)
                for resource in ("qatlib_search/ExportSearchIndex.lean", "qatlib_search/retrieval-report.schema.json"):
                    self.assertNotIn(resource, names)
                self.assertIn("qatlib_search/retrieve.py", names)
                entrypoints, = (name for name in names if name.endswith(".dist-info/entry_points.txt"))
                entrypoint_text = wheel.read(entrypoints)
                self.assertIn(b"qatlib-env = qatlib_env.__main__:main", entrypoint_text)
                self.assertIn(b"qatlib-forge = qatlib_forge.__main__:main", entrypoint_text)
                self.assertIn(b"qatlib-retrieve = qatlib_search.retrieve:main", entrypoint_text)
                self.assertNotIn(b"qatlib-search =", entrypoint_text)
            source, = Path(directory).glob("qatlib_tools-*.tar.gz")
            with tarfile.open(source) as sdist:
                members = {"/".join(Path(name).parts[1:]) for name in sdist.getnames()}
                self.assertIn("pyproject.toml", members)
                self.assertEqual({name for name in members if name.endswith("README.md")}, {"README.md"})
                for resource in ("README.md", "src/qatlib_search/retrieve.py"):
                    self.assertIn(resource, members)
                for module in ("qatlib_env", "qatlib_forge", "qatlib_search"):
                    self.assertNotIn(module + "/pyproject.toml", members)
