"""Real Lake regressions for mathlib's Lean-configured dependency and cache exe."""
from __future__ import annotations

import tests._bootstrap as _bootstrap  # noqa: F401
from tests._git_fixture import snapshot
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from qatlib_env import prepare


@unittest.skipUnless(shutil.which("lake"), "the installed Lake toolchain is required")
class MathlibPrepareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qatlas-mathlib-prepare-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        toolchain = "leanprover/lean4:v4.35.0"
        elan = Path(os.environ.get("ELAN_HOME", str(Path.home() / ".elan")))
        if not (elan / "toolchains" / toolchain.replace("/", "--").replace(":", "---") / "bin/lake").is_file():
            self.skipTest("the locked Lean toolchain must already be installed; tests never download it")
        self.receipt = self.root / "cache-invocations.txt"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("LAKE_") and key != "QATLAS_STAGE_DEADLINE"}
        self.env.update(ELAN_TOOLCHAIN=toolchain, LAKE_CACHE_DIR=str(self.root / "native-cache"),
                        LAKE_CACHE_KEY="ignored_fixture_generic_access:ignored_fixture_generic_secret",
                        LAKE_ARTIFACT_CACHE="true", LEAN_NUM_THREADS="2", QATLAS_LEAN_THREADS="2",
                        MATHLIB_NO_CACHE_ON_UPDATE="1", GIT_TERMINAL_PROMPT="0",
                        MATHLIB_FIXTURE_RECEIPT=str(self.receipt))
        self.source = self.root / "mathlib-source"
        self.source.mkdir()
        (self.source / "lean-toolchain").write_text(toolchain + "\n")
        (self.source / "lakefile.lean").write_text('''import Lake
open Lake DSL
package mathlib
lean_lib Mathlib
lean_exe cache where
  root := `CacheMain
''')
        (self.source / "Mathlib").mkdir()
        (self.source / "Mathlib/Tiny.lean").write_text("def mathlibFixture : Nat := 7\n")
        (self.source / "Mathlib.lean").write_text("import Mathlib.Tiny\n")
        (self.source / "CacheMain.lean").write_text('''import Lake
def main (args : List String) : IO Unit := do
  if (← IO.getEnv "LAKE_CACHE_KEY").isSome then
    throw (IO.userError "external generic credential leaked into mathlib cache child")
  unless args == ["get"] do
    throw (IO.userError "fixture cache expected get")
  let some receipt ← IO.getEnv "MATHLIB_FIXTURE_RECEIPT"
    | throw (IO.userError "missing fixture receipt")
  let path := System.FilePath.mk receipt
  let previous ← if ← path.pathExists then IO.FS.readFile path else pure ""
  IO.FS.writeFile path (previous ++ "cache get\\n")
''')
        self.run_command(self.source, ["git", "init", "-q"])
        revision = snapshot(self.source, self.env)
        self.project = self.root / "consumer"
        self.project.mkdir()
        (self.project / "lean-toolchain").write_text(toolchain + "\n")
        (self.project / "lakefile.toml").write_text(f'''name="ExampleLibrary"
[[require]]
name="mathlib"
git={json.dumps(self.source.as_uri())}
rev="{revision}"
[[lean_lib]]
name="Root"
''')
        (self.project / "Root.lean").write_text("import Mathlib.Tiny\ndef consumerFixture : Nat := mathlibFixture\n")
        self.run_command(self.project, ["git", "init", "-q"])
        snapshot(self.project, self.env)
        self.run_command(self.project, ["lake", "resolve-deps"])
        self.mathlib = self.project / ".lake/packages/mathlib"
        self.assertTrue((self.mathlib / "lakefile.lean").is_file())
        self.assertFalse((self.mathlib / "lakefile.toml").exists())

    def run_command(self, directory: Path, args: list[str]):
        result = subprocess.run(args, cwd=directory, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        self.assertEqual(result.returncode, 0, f"fixture command failed: {args}\n{result.stdout}")
        return result

    def test_lean_configured_mathlib_runs_cache_and_invalidates_old_empty_stamp(self):
        current = prepare.identity(self.project, self.env)
        # The old writer omitted a format version and could stamp an empty
        # build_dirs list after silently skipping Lean-configured mathlib.
        stamp = self.project / ".lake/qatlas-prepare.json"
        stamp.write_text(json.dumps({"identity": current, "build_dirs": [], "reused": False}))
        first = prepare.prepare(self.project, self.env)
        self.assertFalse(first["reused"])
        self.assertEqual(first["repositories"]["mathlib"], "mathlib")
        self.assertEqual(first["version"], prepare.PREPARE_VERSION)
        self.assertEqual(self.receipt.read_text().splitlines(), ["cache get"])
        self.assertTrue((self.mathlib / ".lake/build/bin/cache").is_file(),
                        "the actual dependency executable must have been compiled and run")
        second = prepare.prepare(self.project, self.env)
        third = prepare.prepare(self.project, self.env)
        self.assertTrue(second["reused"])
        self.assertTrue(third["reused"])
        self.assertEqual(self.receipt.read_text().splitlines(), ["cache get"])
        self.assertEqual(json.loads(stamp.read_text())["version"], prepare.PREPARE_VERSION)

    def test_known_mathlib_sources_are_counted_until_real_oleans_exist(self):
        # Root, Mathlib and Mathlib.Tiny are cold; CacheMain is an executable
        # module and must not inflate the known Mathlib library count.
        self.assertEqual(prepare.cold_modules(self.project), 3)
        self.run_command(self.project, ["lake", "build", "+Mathlib.Tiny"])
        self.assertEqual(prepare.cold_modules(self.project), 2)
        self.run_command(self.project, ["lake", "build", "+Mathlib", "+Root"])
        self.assertEqual(prepare.cold_modules(self.project), 0)
        (self.mathlib / "Mathlib/Additional.lean").write_text("def additionalFixture := 1\n")
        (self.mathlib / "Mathlib/Other.lean").write_text("def otherFixture := 2\n")
        with self.assertRaisesRegex(prepare.CacheError, "cold modules exceed configured limit"):
            prepare.enforce_cold_limit(self.project, {"QATLAS_MAX_COLD_MODULES": "1"})


if __name__ == "__main__":
    unittest.main()
