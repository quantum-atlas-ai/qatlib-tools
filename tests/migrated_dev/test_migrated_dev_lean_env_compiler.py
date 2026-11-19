"""Runtime admission and real Lake traces stay independent of thread counts."""
import tests._bootstrap as _bootstrap  # noqa: F401
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from qatlib_env import compiler, supervisor


@unittest.skipUnless(os.name == "posix", "POSIX compiler overlay")
class CompilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def test_heavy_slots_are_acquired_before_compiler_start_and_threads_reach_argv(self):
        sysroot = self.root / "toolchain"
        (sysroot / "bin").mkdir(parents=True)
        (sysroot / "lib/lean").mkdir(parents=True)
        (sysroot / "lib/lean/Init.olean").write_text("fixture")
        real = sysroot / "bin/lean"
        real.write_text(f'''#!{sys.executable}
import json, os, sys, time
with open(os.environ["EVENTS"], "a") as stream:
    stream.write(json.dumps(["start", time.monotonic(), sys.argv[1:]]) + "\\n")
time.sleep(.15)
with open(os.environ["EVENTS"], "a") as stream:
    stream.write(json.dumps(["end", time.monotonic(), sys.argv[1:]]) + "\\n")
''')
        real.chmod(0o700)
        events = self.root / "events"
        env = {**os.environ, "QATLIB_REAL_LEAN": str(real), "QATLIB_LEAN_THREADS": "3",
               "QATLIB_HEAVY_JOBS": "1", "QATLIB_HEAVY_PATTERNS": "Heavy", "EVENTS": str(events)}
        env = compiler.environment(self.repo, env)
        overlay = Path(env["LEAN_SYSROOT"])
        self.assertEqual(compiler.environment(self.repo, env), env)
        launcher = ("import subprocess,sys\n"
                    "children=[subprocess.Popen([sys.argv[1], 'Heavy'+str(i)+'.lean']) for i in range(3)]\n"
                    "raise SystemExit(max(p.wait() for p in children))\n")
        supervisor.run([sys.executable, "-c", launcher, str(overlay / "bin/lean")],
                       env=env, check=True, timeout=5)
        data = [json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual([item[0] for item in data], ["start", "end"] * 3)
        self.assertTrue(all(item[2][0] == "-j3" for item in data))
        self.assertTrue((overlay / "lib").is_symlink())
        self.assertFalse(real.is_symlink())

    @unittest.skipUnless(shutil.which("lake") and shutil.which("lean"), "installed Lean toolchain")
    def test_real_lake_dephash_is_identical_native_and_runtime_thread_counts(self):
        (self.repo / "lakefile.toml").write_text('name="runtime_probe"\n[[lean_lib]]\nname="RuntimeProbe"\n')
        (self.repo / "RuntimeProbe.lean").write_text('"""Runtime thread probe."""\ntheorem probe : (1 : Nat) = 1 := rfl\n'.replace('"""Runtime thread probe."""', '/-- Runtime thread probe. -/'))
        # Pin the installed compiler so Elan never downloads a toolchain.
        try:
            probe = supervisor.run(["lean", "--version"], capture_output=True, text=True, check=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("Lean toolchain is not configured on this host")
        if "4.35.0" not in probe.stdout:
            self.skipTest("trace stability fixture is pinned to Lake 4.35.0")
        (self.repo / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
        env = {**os.environ, "LAKE_ARTIFACT_CACHE": "false", "LAKE_CACHE_DIR": str(self.root / "cache"),
               "LEAN_NUM_THREADS": "2", "QATLIB_HEAVY_PATTERNS": "", "QATLIB_HEAVY_JOBS": "1"}
        for key in ("LEAN_SYSROOT", "LAKE_OVERRIDE_LEAN", "QATLIB_REAL_LEAN", "QATLIB_REAL_SYSROOT"):
            env.pop(key, None)
        hashes = []
        for index, threads in enumerate((None, "1", "3")):
            actual = env if threads is None else compiler.environment(self.repo, {**env, "QATLIB_LEAN_THREADS": threads})
            result = supervisor.run(["lake", "--no-cache", "build", "RuntimeProbe"], cwd=self.repo, env=actual,
                                    text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            trace = json.loads((self.repo / ".lake/build/lib/lean/RuntimeProbe.trace").read_text())
            hashes.append(trace["depHash"])
            (self.repo / ".lake/build").rename(self.repo / f".lake/build-{index}")
        self.assertEqual(hashes, [hashes[0]] * 3)


if __name__ == "__main__":
    unittest.main()
