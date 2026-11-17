"""Real Git tree-filtered materializations are sources; bare mirrors are not."""
from __future__ import annotations

import tests._bootstrap as _bootstrap  # noqa: F401
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from qatlib_env import ci, workspace


class SourceCheckoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        self.origin = self.root / "origin"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q")
        (self.origin / "Qatlib").mkdir()
        (self.origin / "Qatlib/Basic.lean").write_text("def answer := 42\n")
        (self.origin / "Qatlib.lean").write_text("import Qatlib.Basic\n")
        (self.origin / "docs").mkdir()
        (self.origin / "docs/references.bib").write_text("@article{fixture, title={Fixture}}\n")
        self.git(self.origin, "add", "Qatlib", "Qatlib.lean", "docs")
        self.git(self.origin, "-c", "user.name=Source fixture", "-c", "user.email=fixture@localhost",
                 "commit", "-qm", "Initial source fixture")
        self.first = self.git(self.origin, "rev-parse", "HEAD").strip()
        (self.origin / "Qatlib/Basic.lean").write_text("def answer := 43\n")
        self.git(self.origin, "add", "Qatlib/Basic.lean")
        self.git(self.origin, "-c", "user.name=Source fixture", "-c", "user.email=fixture@localhost",
                 "commit", "-qm", "Update source fixture")
        self.locked = self.git(self.origin, "rev-parse", "HEAD").strip()
        self.git(self.origin, "config", "uploadpack.allowFilter", "true")
        self.project = self.root / "project"
        self.project.mkdir()
        self.git(self.project, "init", "-q")
        (self.project / "lake-manifest.json").write_text(json.dumps({
            "packages": [{"name": "Qatlib", "rev": self.locked}]}))
        self.package = self.project / ".lake/packages/Qatlib"
        self.package.parent.mkdir(parents=True)
        self.git(self.root, "clone", "--quiet", "--filter=tree:0", self.origin.as_uri(), str(self.package))
        self.mirror = self.root / "mirror"
        self.bare = self.mirror / ".lake/packages/Qatlib"
        self.bare.parent.mkdir(parents=True)
        self.git(self.root, "clone", "--quiet", "--bare", str(self.origin), str(self.bare))
        self.env["QATLAS_PKG_MIRROR"] = str(self.mirror)

    def git(self, directory, *args):
        result = subprocess.run(["git", *args], cwd=directory, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode:
            self.fail(f"Git fixture command {args} failed: {result.stderr}")
        return result.stdout

    def test_tree_filtered_materialized_checkout_is_preferred_to_bare_mirror(self):
        self.assertEqual(self.git(self.package, "config", "--get", "remote.origin.promisor").strip(), "true")
        self.assertEqual(self.git(self.package, "config", "--get", "remote.origin.partialclonefilter").strip(), "tree:0")
        self.assertTrue((self.package / "Qatlib/Basic.lean").is_file())
        # A partial clone is unsuitable for upload-pack mirror reuse, but its
        # materialized locked sources are exactly what metadata checks require.
        self.assertFalse(workspace.usable_git_mirror(self.package, self.locked, self.project, self.env))
        self.assertEqual(ci.public_repo(self.project, self.env), self.package)
        self.assertEqual(ci.bibliography(self.project, self.env), self.package / "docs/references.bib")
        self.assertEqual(self.git(self.root, "--git-dir=" + str(self.bare), "rev-parse", "--is-bare-repository").strip(), "true")

    def test_stale_checkout_does_not_fall_back_to_a_bare_mirror(self):
        (self.project / "lake-manifest.json").write_text(json.dumps({
            "packages": [{"name": "Qatlib", "rev": self.first}]}))
        with self.assertRaisesRegex(ValueError, "materialized Lean sources"):
            ci.public_repo(self.project, self.env)
        self.assertIsNone(ci.bibliography(self.project, self.env))

    def test_bare_repository_remains_rejected_even_with_loose_source_shaped_files(self):
        (self.bare / "Qatlib").mkdir()
        (self.bare / "Qatlib/Basic.lean").write_text("def misleading := 0\n")
        (self.bare / "Qatlib.lean").write_text("import Qatlib.Basic\n")
        self.assertFalse(ci.source_checkout(self.bare, self.locked, self.env))


if __name__ == "__main__":
    unittest.main()
