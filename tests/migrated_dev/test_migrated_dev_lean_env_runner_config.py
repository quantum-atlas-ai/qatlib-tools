"""Render two real instance configs without modifying host systemd services."""
import tests._bootstrap as _bootstrap  # noqa: F401
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest

from qatlib_env import resources, runner_config


class RunnerConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / "ci"
        self.systemd = self.root / "systemd"
        common = self.base / "dev/runners/.env"
        common.parent.mkdir(parents=True)
        common.write_text("HOME=/lane\nLAKE_CACHE_DIR=/lane/lake-cache\nLEAN_NUM_THREADS=8\n")

    def test_two_instances_have_independent_environment_and_resource_dropins(self):
        first = runner_config.render(self.base, self.systemd, "dev", "lint-1",
                                      values={"jobs": 2, "lean_threads": 2, "memory_budget": "12G"},
                                      cpu_quota="400%", memory_high="16G", memory_max="20G")
        second = runner_config.render(self.base, self.systemd, "dev", "build-1",
                                       values={"jobs": 6, "lean_threads": 3, "memory_budget": "28G"},
                                       cpu_quota="1800%", memory_high="32G", memory_max="36G")
        self.assertNotEqual(first["environment"], second["environment"])
        for item, jobs in [(first, 2), (second, 6)]:
            environment = Path(item["environment"]).read_text()
            self.assertIn("QATLAS_RUNNER_CONFIG=" + item["config"], environment)
            self.assertNotIn("LEAN_NUM_THREADS=", environment)
            self.assertEqual(tomllib.loads(Path(item["config"]).read_text())["resources"]["jobs"], jobs)
            self.assertEqual(stat.S_IMODE(Path(item["config"]).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(Path(item["environment"]).stat().st_mode), 0o600)
            config = resources.resolve_config(self.root, env={"HOME": str(self.root / "home"),
                                                              "QATLAS_RUNNER_CONFIG": item["config"]})
            self.assertEqual(config.values["jobs"], jobs)
            self.assertTrue(config.sources["jobs"].startswith("runner:"))
        self.assertIn("CPUQuota=400%", Path(first["systemd"]).read_text())
        self.assertIn("MemoryMax=36G", Path(second["systemd"]).read_text())
        original = Path(first["config"]).read_text()
        runner_config.render(self.base, self.systemd, "dev", "build-1", values={"jobs": 7})
        self.assertEqual(Path(first["config"]).read_text(), original)

    def test_cli_renderer_writes_only_selected_sandbox_paths(self):
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        result = subprocess.run([sys.executable, "-B", "-m", "qatlib_env.runner_config", "--base", str(self.base),
                                 "--systemd-dir", str(self.systemd), "--lane", "secondary", "--instance", "build-2",
                                 "--jobs", "4", "--lean-threads", "2", "--module-timeout", "600"],
                                env=env, text=True, capture_output=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(tomllib.loads(Path(report["config"]).read_text())["resources"]["module_timeout"], 600)
        self.assertTrue(all(Path(path).is_relative_to(self.root) for path in report.values()))

    def test_invalid_resources_fail_before_writing_instance(self):
        for arguments in [dict(instance="../escape"), dict(instance="bad", cpu_quota="20G"),
                          dict(instance="bad", memory_max="50BAD"), dict(instance="bad", values={"jobs": 1.5}),
                          dict(instance="bad", values={"stage_timeout": "nan"})]:
            with self.assertRaises(ValueError):
                runner_config.render(self.base, self.systemd, "dev", **arguments)
        self.assertFalse((self.base / "dev/runners/bad").exists())


if __name__ == "__main__":
    unittest.main()
