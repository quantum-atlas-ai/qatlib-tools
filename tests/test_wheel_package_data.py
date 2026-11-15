from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


class WheelPackageDataTests(unittest.TestCase):
    def test_built_wheel_contains_runtime_docs_and_manifests(self):
        root = Path(__file__).parents[1]
        temporary = None
        try:
            wheels = sorted((root / "dist").glob("qatlib_tools-*.whl"))
            if not wheels:
                temporary = Path(tempfile.mkdtemp(prefix="qatlib-wheel-package-data-"))
                subprocess.run(["uv", "build", "--wheel", "--out-dir", str(temporary)],
                               cwd=root, check=True, capture_output=True, text=True)
                wheels = sorted(temporary.glob("qatlib_tools-*.whl"))
            self.assertTrue(wheels, "uv build did not produce a wheel")
            with zipfile.ZipFile(wheels[-1]) as archive:
                names = set(archive.namelist())
                setup_runner = archive.read("qatlib_env/setup_runner.sh")
                self.assertIn("qatlib_env/docs/cache.md", names)
                self.assertIn("qatlib_env/docs/resources.md", names)
                self.assertIn("qatlib_env/docs/runners.md", names)
                self.assertIn(b"github.com", setup_runner)
                self.assertIn(b"ssh.github.com", setup_runner)
                self.assertIn(b"GITHUB_ED25519_KEY", setup_runner)
                self.assertIn("qatlib_search/lean_source.py", names)
                self.assertFalse(any(name.endswith((".pyc", "/__pycache__/")) for name in names))
        finally:
            if temporary is not None:
                subprocess.run(["trash-put", str(temporary)], check=False)

