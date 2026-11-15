from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from qatlib_env import ci, workspace
from qatlib_env.compiler import compiler_command, parse_dep_hash
from qatlib_env.config import EnvConfig, choose_mode, load_config
from qatlib_env.pool import Pool
from qatlib_env.cloud import unpack_bundle
from qatlib_env.backends.linux import linux_command
from qatlib_env.backends.macos import macos_limits
from qatlib_env.backends.windows import windows_limits


class EnvTest(unittest.TestCase):
    def test_compiler_flags_are_explicit_and_floor_memory(self):
        self.assertEqual(compiler_command(["lean", "Main.lean"], jobs=1, memory_mib=512)[-4:], ["-j", "1", "-M", "2048"])
        self.assertEqual(parse_dep_hash("depHash=abc123"), "abc123")

    def test_config_and_policy(self):
        config = EnvConfig.from_mapping({"default_memory_mib": 128})
        self.assertEqual(config.default_memory_mib, 2048)
        self.assertEqual(choose_mode("auto", cold=True), "cloud")
        self.assertEqual(choose_mode("auto", memory_mib=100), "local")

    def test_pool_ledger_and_stale_reap(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = Pool(budget_mib=4096, budget_cpu=2, state_dir=Path(folder), ttl=0.001)
            lease = pool.acquire(2048, 1)
            self.assertEqual(pool.status()["used_mib"], 2048)
            pool.release(lease)
            self.assertEqual(pool.status()["used_mib"], 0)

    def test_pool_denies_overcommit(self):
        with tempfile.TemporaryDirectory() as folder:
            pool = Pool(budget_mib=4096, state_dir=Path(folder))
            pool.acquire(2048)
            with self.assertRaises(RuntimeError): pool.acquire(2049, 1)

    def test_backend_builders(self):
        self.assertIn("lean", linux_command(["lean"], memory_mib=2048, cpu=2))
        self.assertEqual(windows_limits(["lean"], memory_mib=2048), ["lean"])
        self.assertTrue(macos_limits(["lean"]))

    def test_shared_compatibility_wrappers_delegate_to_generic_implementations(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            checker = project / "python/secondary_ops/check_source_imports.py"
            checker.parent.mkdir(parents=True)
            checker.write_text("# checker\n")
            with patch.object(ci.workspace, "run") as run:
                ci.source_imports(project, {"PATH": "/usr/bin"})
            run.assert_called_once()
            self.assertIn("check_source_imports.py", " ".join(run.call_args.args[0]))
        with patch.object(workspace.resources, "probe_resources",
                          return_value=SimpleNamespace(available_memory=123456)):
            self.assertEqual(workspace.available_memory_bytes(), 123456)

    def test_bundle_traversal_and_digest(self):
        import zipfile, hashlib
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); bundle = root / "bundle.zip"
            with zipfile.ZipFile(bundle, "w") as archive: archive.writestr("ok.txt", "yes")
            target = root / "out"; digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            result = unpack_bundle(bundle, target, expected_sha256=digest)
            self.assertEqual(result["files"], 1); self.assertEqual((target / "ok.txt").read_text(), "yes")
            evil = root / "evil.zip"
            with zipfile.ZipFile(evil, "w") as archive: archive.writestr("../escape", "x")
            with self.assertRaises(ValueError): unpack_bundle(evil, target)

