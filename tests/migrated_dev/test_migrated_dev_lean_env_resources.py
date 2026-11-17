"""Resource fixtures exercise real cgroup files and configuration layers."""
import tests._bootstrap as _bootstrap  # noqa: F401
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from qatlib_env import resources


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.proc = self.root / "proc"
        (self.proc / "self").mkdir(parents=True)
        self.cgroup = self.root / "cgroup"
        self.cgroup.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home)}

    def fixture(self, cpus=48, total=125, available=105):
        (self.proc / "meminfo").write_text(f"MemTotal: {total * 1024 ** 2} kB\nMemAvailable: {available * 1024 ** 2} kB\n")
        (self.proc / "self/cgroup").write_text("0::/lane/instance\n")
        (self.cgroup / "lane/instance").mkdir(parents=True)
        return dict(proc_root=self.proc, cgroup_root=self.cgroup, cpu_count=cpus, affinity=set(range(cpus)))

    def test_adaptive_examples_and_available_memory(self):
        for cpus, total, available, jobs in [(8, 16, 12, 2), (32, 62, 47, 10),
                                              (48, 125, 105, 23), (128, 440, 440, 64)]:
            # Reuse real fixture directories rather than mocking the probe.
            (self.proc / "meminfo").write_text(f"MemTotal: {total * 1024 ** 2} kB\nMemAvailable: {available * 1024 ** 2} kB\n")
            (self.proc / "self/cgroup").write_text("")
            host = resources.probe_resources(proc_root=self.proc, cgroup_root=self.cgroup,
                                             cpu_count=cpus, affinity=set(range(cpus)))
            config = resources.resolve_config(self.repo, env=self.env, probe=host)
            self.assertEqual(config.values["jobs"], jobs)
            self.assertEqual(config.values["lean_threads"], 2)

    def test_cgroup_v2_all_ancestors_memory_current_quota_and_cpuset(self):
        kwargs = self.fixture()
        (self.cgroup / "memory.high").write_text(str(48 * resources.GIB))
        (self.cgroup / "memory.current").write_text(str(8 * resources.GIB))
        (self.cgroup / "lane/memory.max").write_text(str(80 * resources.GIB))
        (self.cgroup / "lane/memory.current").write_text("0")
        (self.cgroup / "lane/instance/memory.max").write_text("max")
        (self.cgroup / "cpu.max").write_text("750000 100000")
        (self.cgroup / "lane/cpu.max").write_text("1200000 100000")
        (self.cgroup / "lane/instance/cpuset.cpus.effective").write_text("0-3,8-11")
        host = resources.probe_resources(**kwargs)
        self.assertEqual(host.effective_cpus, 7)
        self.assertEqual(host.memory_headroom, 40 * resources.GIB)
        self.assertEqual(resources.resolve_config(self.repo, env=self.env, probe=host).values["jobs"], 3)
        self.assertEqual(len(host.limits["cgroups"]), 3)

    def test_formula_with_all_physical_memory_available(self):
        for cpus, total, jobs in [(8, 16, 3), (32, 62, 13), (48, 125, 24), (128, 440, 64)]:
            host = resources.HostResources(total * resources.GIB, total * resources.GIB,
                                           cpus, cpus, total * resources.GIB)
            self.assertEqual(resources.resolve_config(self.repo, env=self.env, probe=host).values["jobs"], jobs)

    def test_host_reserve_does_not_consume_a_smaller_cgroup_allocation(self):
        kwargs = self.fixture(cpus=16, total=440, available=420)
        (self.cgroup / "lane/memory.high").write_text(str(40 * resources.GIB))
        (self.cgroup / "lane/memory.current").write_text(str(28 * resources.GIB))
        host = resources.probe_resources(**kwargs)
        self.assertGreater(host.reserve, 40 * resources.GIB)
        self.assertEqual(host.memory_headroom, 12 * resources.GIB)
        self.assertEqual(host.memory_budget, 12 * resources.GIB)
        automatic = resources.resolve_config(self.repo, env=self.env, probe=host)
        self.assertEqual(automatic.configuration.local_pool_mib, 12 * 1024)
        explicit = resources.resolve_config(self.repo, {"local_pool_mib": 1024}, self.env, probe=host)
        self.assertEqual(explicit.configuration.local_pool_mib, 1024)

    def test_host_pressure_and_cgroup_headroom_still_bound_automatic_budgets(self):
        host = resources.HostResources(440 * resources.GIB, 50 * resources.GIB,
                                        16, 16, 12 * resources.GIB)
        self.assertEqual(host.memory_budget, 6 * resources.GIB)
        pressure = resources.HostResources(440 * resources.GIB, 2 * resources.GIB,
                                            16, 16, 12 * resources.GIB)
        self.assertEqual(pressure.memory_budget, 1)
        bounded = resources.HostResources(440 * resources.GIB, 420 * resources.GIB,
                                           16, 16, 512 * 1024**2)
        self.assertEqual(bounded.memory_budget, 512 * 1024**2)

    def test_config_explain_cli_reports_real_file_layers_without_preparing(self):
        (self.repo / "qatlib-env.toml").write_text('[resources]\njobs=4\nlean_threads=3\n')
        inherited = {key: value for key, value in os.environ.items()
                     if key not in {variable for variables in resources.ENV_KEYS.values() for variable in variables}
                     and key not in {"QATLAS_RUNNER_CONFIG", "XDG_CONFIG_HOME"}}
        env = {**inherited, "HOME": str(self.home), "QATLAS_JOBS": "5",
               "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
        result = subprocess.run([sys.executable, "-B", "-m", "qatlib_env.workspace", "--project", str(self.repo),
                                 "config", "explain", "--jobs", "6"], env=env, text=True,
                                 capture_output=True, check=True)
        report = json.loads(result.stdout)
        self.assertEqual(report["values"]["jobs"], 6)
        self.assertEqual(report["sources"]["jobs"], "command-line")
        self.assertTrue(report["sources"]["lean_threads"].startswith("workspace:"))
        self.assertIn("cgroups", report["resources"]["limits"])
        self.assertFalse((self.repo / ".lake").exists())

    def test_cgroup_v1_limits_and_affinity_intersection(self):
        kwargs = self.fixture(cpus=16, total=32, available=30)
        (self.proc / "self/cgroup").write_text("2:cpu,cpuacct:/lane\n3:memory:/lane\n4:cpuset:/lane\n")
        for name in ("cpu,cpuacct", "memory", "cpuset"):
            (self.cgroup / name / "lane").mkdir(parents=True)
        (self.cgroup / "cpu,cpuacct/cpu.cfs_quota_us").write_text("350000")
        (self.cgroup / "cpu,cpuacct/cpu.cfs_period_us").write_text("100000")
        (self.cgroup / "memory/memory.limit_in_bytes").write_text(str(20 * resources.GIB))
        (self.cgroup / "memory/memory.usage_in_bytes").write_text(str(4 * resources.GIB))
        (self.cgroup / "cpuset/lane/cpuset.cpus").write_text("2-5")
        kwargs["affinity"] = {0, 1, 2, 3}
        host = resources.probe_resources(**kwargs)
        self.assertEqual(host.effective_cpus, 2)
        self.assertEqual(host.memory_headroom, 16 * resources.GIB)

    def test_explicit_priority_each_layer_and_explain_sources(self):
        kwargs = self.fixture()
        runner = self.root / "runner.toml"
        runner.write_text('[resources]\njobs=3\nlean_threads=3\n')
        self.env["QATLAS_RUNNER_CONFIG"] = str(runner)
        user = self.home / ".config/qatlib/env.toml"
        user.parent.mkdir(parents=True)
        user.write_text('[resources]\njobs=4\n')
        workspace = self.repo / "qatlib-env.toml"
        workspace.write_text('[resources]\njobs=5\nmemory_budget="20GiB"\n')
        host = resources.probe_resources(**kwargs)
        config = resources.resolve_config(self.repo, env=self.env, probe=host)
        self.assertEqual(config.values["jobs"], 5)
        self.assertTrue(config.sources["jobs"].startswith("workspace:"))
        self.assertTrue(config.sources["lean_threads"].startswith("runner:"))
        env = {**self.env, "LEAN_NUM_THREADS": "6", "QATLAS_HEAVY_JOBS": "2"}
        config = resources.resolve_config(self.repo, {"jobs": 7}, env, probe=host)
        self.assertEqual(config.values["jobs"], 7)
        self.assertEqual(config.values["heavy_jobs"], 2)
        self.assertEqual(config.sources["jobs"], "command-line")
        self.assertEqual(config.sources["heavy_jobs"], "environment:QATLAS_HEAVY_JOBS")
        exported = config.environment({})
        self.assertEqual(config.values["lean_threads"], 6)
        self.assertEqual(exported["LEAN_NUM_THREADS"], "6")
        self.assertEqual(exported["QATLAS_LEAN_THREADS"], "6")
        self.assertEqual(config.explain()["resources"]["reserve"], host.reserve)

    def test_explicit_hard_limit_conflicts_warn_without_truncation(self):
        config = resources.resolve_config(self.repo, {"jobs": 100, "lean_threads": 8,
                                           "memory_budget": "200G"}, self.env,
                                           probe=resources.HostResources(16 * resources.GIB,
                                             12 * resources.GIB, 8, 4, 10 * resources.GIB))
        self.assertEqual(config.values["jobs"], 100)
        self.assertEqual(config.values["memory_budget"], 200 * resources.GIB)
        self.assertEqual(len(config.warnings), 3)

    def test_invalid_values_and_unknown_keys_fail(self):
        for value in [True, -1, 0, "3.5"]:
            with self.assertRaises(ValueError):
                resources.resolve_config(self.repo, {"jobs": value}, self.env)
        (self.repo / "qatlib-env.toml").write_text('[resources]\njobz=2\n')
        with self.assertRaisesRegex(ValueError, "unknown resource options"):
            resources.resolve_config(self.repo, env=self.env)


if __name__ == "__main__":
    unittest.main()
