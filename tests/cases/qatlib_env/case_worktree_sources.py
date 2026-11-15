"""Regression coverage for native Lake URL-map materialization in worktrees."""

import tests._bootstrap as _bootstrap  # noqa: F401
from tests._git_fixture import snapshot, refresh_bare
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[3] / "src/qatlib_env"
from qatlib_env import workspace as lake



class LocalSourceTest(unittest.TestCase):
    def setUp(self):
        directory = Path(os.environ.get("QATLAS_TEST_ROOT", tempfile.gettempdir()))
        directory.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=directory)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "primary"
        self.project.mkdir()
        self.env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "LAKE_CACHE_DIR": str(self.root / "cache")}
        self.git("init", "-q", "-b", "main", cwd=self.project)
        upstream = self.root / "upstream"
        upstream.mkdir()
        self.git("init", "-q", "-b", "main", cwd=upstream)
        (upstream / "Pkg.lean").write_text("def marker : Nat := 7\n")
        (upstream / "lakefile.toml").write_text('name = "Pkg"\n')
        self.commit(upstream)
        self.revision = self.git("rev-parse", "HEAD", cwd=upstream)
        bare = self.root / "upstream.git"
        self.git("clone", "-q", "--bare", str(upstream), str(bare), cwd=self.root)
        self.git("--git-dir=" + str(bare), "config", "uploadpack.allowFilter", "true", cwd=self.root)
        self.source = self.project / ".lake/packages/Pkg"
        self.source.parent.mkdir(parents=True)
        self.env.pop("GIT_NO_LAZY_FETCH", None)
        # The mirror advertised to Lake must be complete; Lake's own
        # --filter=tree:0 request is served by uploadpack.allowFilter on it.
        self.git("clone", "-q", "--no-local", bare.as_uri(), str(self.source), cwd=self.root)
        self.env["GIT_NO_LAZY_FETCH"] = "1"
        self.url = bare.as_uri()
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0")
        (self.project / "lakefile.toml").write_text('name = "example_library"\n')
        (self.project / "lake-manifest.json").write_text(
            json.dumps({"packages": [{"name": "Pkg", "rev": self.revision, "url": self.url}]})
        )
        (self.project / ".gitignore").write_text(".lake/\n")
        self.commit(self.project)
        self.child = self.root / "child"
        self.git("worktree", "add", "-q", "-b", "child", str(self.child), cwd=self.project)

    def git(self, *args, cwd):
        process = subprocess.run(["git", *args], cwd=cwd, env=self.env,
                                 capture_output=True, text=True)
        if process.returncode:
            raise AssertionError(process.stderr)
        return process.stdout.strip()

    def commit(self, path):
        return snapshot(path, self.env)

    def test_environment_is_clean_and_command_gets_native_map(self):
        env = lake.environment(self.child, source=self.project, base_env=self.env)
        self.assertNotIn("LAKE_PKG_URL_MAP", env)
        command_env = lake.command_environment(
            self.child, env, ["lake", "exe", "get_cache"], source=self.project
        )
        self.assertEqual(json.loads(command_env["LAKE_PKG_URL_MAP"]), {"Pkg": str(self.source)})
        self.assertEqual((self.child / "lake-manifest.json").read_bytes(),
                         (self.project / "lake-manifest.json").read_bytes())

    def test_update_command_never_gets_local_map(self):
        env = lake.environment(self.child, source=self.project, base_env=self.env)
        command_env = lake.command_environment(
            self.child, env, ["lake", "update", "Pkg"], source=self.project
        )
        self.assertNotIn("LAKE_PKG_URL_MAP", command_env)

    def test_independent_worktrees_keep_packages_private_and_share_cache(self):
        other = self.root / "other"
        self.git("worktree", "add", "-q", "-b", "other", str(other), cwd=self.project)
        environments = [lake.environment(child, source=self.project, base_env=self.env)
                        for child in [self.child, other]]
        self.assertEqual(environments[0]["LAKE_CACHE_DIR"], environments[1]["LAKE_CACHE_DIR"])
        (self.child / ".lake").mkdir(exist_ok=True)
        (other / ".lake").mkdir(exist_ok=True)
        self.assertNotEqual((self.child / ".lake").stat().st_ino, (other / ".lake").stat().st_ino)

    def test_wrong_revision_source_is_not_used(self):
        manifest = json.loads((self.child / "lake-manifest.json").read_text())
        manifest["packages"][0]["rev"] = "a" * 40
        (self.child / "lake-manifest.json").write_text(json.dumps(manifest))
        env = lake.environment(self.child, source=self.project, base_env=self.env)
        command_env = lake.command_environment(
            self.child, env, ["lake", "exe", "get_cache"], source=self.project
        )
        self.assertNotIn("LAKE_PKG_URL_MAP", command_env)

    def test_shallow_mirror_is_rejected(self):
        shallow = self.root / "shallow"
        self.git("clone", "-q", "--depth=1", self.url, str(shallow), cwd=self.root)
        self.assertFalse(lake.usable_git_mirror(shallow, self.revision, self.child, self.env))

    def test_partial_promisor_mirror_is_rejected_with_filter_modes(self):
        for allow_filter in ("false", "true"):
            bare = self.root / ("partial-" + allow_filter)
            self.git("clone", "-q", "--bare", str(self.root / "upstream"), str(bare), cwd=self.root)
            self.git("--git-dir=" + str(bare), "config", "uploadpack.allowFilter", allow_filter, cwd=self.root)
            partial = self.root / ("partial-clone-" + allow_filter)
            result = subprocess.run(["git", "clone", "-q", "--no-local", "--filter=tree:0", bare.as_uri(), str(partial)],
                                    cwd=self.root, env=self.env, capture_output=True, text=True)
            if partial.is_dir():
                self.assertFalse(lake.usable_git_mirror(partial, self.revision, self.child, self.env))
            else:
                self.assertNotEqual(result.returncode, 0)

    def test_self_referential_origin_is_rejected(self):
        self.git("config", "remote.origin.url", str(self.source), cwd=self.source)
        self.assertFalse(lake.usable_git_mirror(self.source, self.revision, self.child, self.env))
        self.git("config", "remote.origin.url", self.source.as_uri(), cwd=self.source)
        self.assertFalse(lake.usable_git_mirror(self.source, self.revision, self.child, self.env))

    def test_complete_mirror_materializes_offline(self):
        self.assertTrue(lake.usable_git_mirror(self.source, self.revision, self.child, self.env))
        mapped = lake.dependency_url_map(self.child, source=self.project, env=self.env)
        self.assertEqual(mapped["Pkg"], str(self.source))

    def test_ensure_pkg_mirror_clones_complete_repo_and_maps_it(self):
        mirror = self.root / "pkg-mirror"
        env = {**self.env, "QATLAS_PKG_MIRROR": str(mirror)}
        lake.ensure_pkg_mirror(self.child, env)
        target = mirror / ".lake/packages/Pkg"
        self.assertTrue(lake.usable_git_mirror(target, self.revision, self.child, env))
        self.assertEqual(self.git("-C", target, "config", "--get", "uploadpack.allowFilter", cwd=self.root), "true")
        self.assertEqual(lake.dependency_url_map(self.child, env=env)["Pkg"], str(target))

    def test_ensure_pkg_mirror_fetches_new_revision_under_lock(self):
        mirror = self.root / "pkg-mirror"
        env = {**self.env, "QATLAS_PKG_MIRROR": str(mirror)}
        lake.ensure_pkg_mirror(self.child, env)
        upstream = self.root / "upstream"
        (upstream / "Pkg.lean").write_text("def marker : Nat := 8\n")
        self.commit(upstream)
        rev2 = self.git("rev-parse", "HEAD", cwd=upstream)
        refresh_bare(self.url, upstream, self.env)
        manifest = json.loads((self.child / "lake-manifest.json").read_text())
        manifest["packages"][0]["rev"] = rev2
        (self.child / "lake-manifest.json").write_text(json.dumps(manifest))
        lake.ensure_pkg_mirror(self.child, env)
        target = mirror / ".lake/packages/Pkg"
        self.assertTrue(lake.usable_git_mirror(target, rev2, self.child, env))
        self.assertEqual(lake.dependency_url_map(self.child, env=env)["Pkg"], str(target))

    def test_unreachable_pkg_mirror_warns_and_is_omitted(self):
        mirror = self.root / "pkg-mirror"
        env = {**self.env, "QATLAS_PKG_MIRROR": str(mirror)}
        manifest = json.loads((self.child / "lake-manifest.json").read_text())
        manifest["packages"][0]["url"] = (self.root / "missing.git").as_uri()
        manifest["packages"][0]["rev"] = "b" * 40
        (self.child / "lake-manifest.json").write_text(json.dumps(manifest))
        import contextlib
        from io import StringIO
        warning = StringIO()
        with contextlib.redirect_stderr(warning):
            lake.ensure_pkg_mirror(self.child, env)
        self.assertIn("Warning: pkg-mirror refresh skipped for Pkg", warning.getvalue())
        self.assertNotIn("Pkg", lake.dependency_url_map(self.child, env=env))

    def test_maintain_mirror_refreshes_existing_repositories(self):
        mirror = self.root / "pkg-mirror"
        target = mirror / ".lake/packages/Pkg"
        target.parent.mkdir(parents=True)
        self.git("clone", "-q", "--no-local", self.url, str(target), cwd=self.root)
        upstream = self.root / "upstream"
        (upstream / "Pkg.lean").write_text("def marker : Nat := 9\n")
        self.commit(upstream)
        rev2 = self.git("rev-parse", "HEAD", cwd=upstream)
        refresh_bare(self.url, upstream, self.env)
        lake.maintain_mirror(mirror, self.env)
        self.assertEqual(self.git("-C", target, "cat-file", "-t", rev2, cwd=self.root), "commit")


if __name__ == "__main__":
    unittest.main()
