"""Regression coverage for deletion, cross-package retention and native-Lake defaults."""

import tests._bootstrap as _bootstrap  # noqa: F401
from tests._git_fixture import snapshot
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[3] / "src/qatlib_env/workspace.py"
TOOLCHAIN = "leanprover/lean4:v4.35.0"
HOST_ENV = {name: os.environ[name] for name in ["PATH", "SystemRoot", "WINDIR"] if name in os.environ}
from qatlib_env import workspace as lake



class CacheWorkflowTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ.get("QATLAS_TEST_ROOT", tempfile.gettempdir()))
        root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "lean-toolchain").write_text(TOOLCHAIN)
        (self.project / "lake-manifest.json").write_text('{"packages": []}')
        (self.project / "lakefile.toml").write_text('name="example_library"\n')
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.env = {**HOST_ENV, "LAKE_CACHE_DIR": str(self.cache)}

    def artifact(self, name):
        path = self.cache / "artifacts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")
        os.utime(path, (1, 1))
        return path

    def output(self, key="b" * 16, scope="example_library"):
        names = ["a" * 16 + ".olean", "c" * 16 + ".ilean", "d" * 16 + ".c"]
        for name in names:
            if not (self.cache / "artifacts" / name).exists():
                self.artifact(name)
        path = self.cache / "outputs" / scope / (key + ".json")
        lake.atomic_json(path, {"data": {"o": [names[0]], "i": names[1], "c": names[2]}})
        os.utime(path, (1, 1))
        return path, [self.cache / "artifacts" / name for name in names]

    def collect(self, **kwargs):
        return lake.gc(self.cache, workspace=self.project, age=0, **kwargs)

    def test_remove_derived_prefers_path_trash_put(self):
        path = self.root / "derived"
        path.write_text("payload")
        with patch.object(lake.shutil, "which", return_value="/opt/bin/trash-put"), \
             patch.object(lake.supervisor, "run") as command:
            lake.remove_derived(path, {"XDG_DATA_HOME": str(self.root / "xdg")})
        command.assert_called_once_with(["/opt/bin/trash-put", str(path)], check=True)
        self.assertTrue(path.exists())

    def test_remove_derived_missing_file_does_not_call_trash(self):
        with patch.object(lake.supervisor, 'run') as command:
            lake.remove_derived(self.root / 'missing')
        command.assert_not_called()

    def test_remove_derived_falls_back_to_freedesktop_trash_and_avoids_collisions(self):
        path = self.root / "derived"
        path.write_text("payload")
        xdg = self.root / "xdg"
        files = xdg / "Trash/files"
        files.mkdir(parents=True)
        (files / path.name).write_text("existing")
        with patch.object(lake.shutil, "which", return_value=None):
            lake.remove_derived(path, {"XDG_DATA_HOME": str(xdg)})
        destination = files / (path.name + ".1")
        info = xdg / "Trash/info" / (destination.name + ".trashinfo")
        self.assertFalse(path.exists())
        self.assertEqual(destination.read_text(), "payload")
        self.assertIn("[Trash Info]\n", info.read_text())
        self.assertIn("Path=", info.read_text())
        self.assertIn("DeletionDate=", info.read_text())

    def init_worktree(self):
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.project, check=True)
        snapshot(self.project)
        child = self.root / "nested/worktrees/child"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "child", str(child)],
                       cwd=self.project, check=True)
        return child

    def test_agents_local_falls_back_to_primary_and_allows_worktree_override(self):
        child = self.init_worktree()
        home = self.root / 'home'
        global_path = home / '.agents/AGENTS.local.md'
        global_path.parent.mkdir(parents=True)
        with patch.object(lake.Path, 'home', return_value=home):
            self.assertIsNone(lake.agents_local_path(child))
            global_path.write_text('general preferences')
            self.assertEqual(lake.agents_local_path(child), global_path)
            primary = self.project / 'AGENTS.local.md'
            primary.write_text('repository preferences')
            self.assertEqual(lake.agents_local_path(child), primary)
            self.assertEqual(lake.agents_local_path(self.project), primary)
            local = child / 'AGENTS.local.md'
            local.write_text('worktree preferences')
            self.assertEqual(lake.agents_local_path(child), local)

    def test_gc_dry_run_then_trash_without_claiming_disk_reclamation(self):
        output, artifacts = self.output()
        self.assertEqual(self.collect()["artifacts"], 3)
        self.assertTrue(output.exists())
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg")}, clear=False):
            result = self.collect(apply=True)
        self.assertEqual(result["artifacts"], 3)
        self.assertEqual(result["physical_bytes_freed_estimate"], 0)
        self.assertGreater(result["bytes_moved_to_trash_estimate"], 0)
        self.assertFalse(output.exists())
        self.assertFalse(any(p.exists() for p in artifacts))
        self.assertFalse((self.cache / ".qatlas").exists())

    def test_linked_ilean_keeps_entire_module_without_registered_workspace(self):
        output, artifacts = self.output()
        os.link(artifacts[1], self.root / "editor.ilean")
        self.assertEqual(artifacts[1].stat().st_nlink, 2)
        self.assertEqual(self.collect(apply=True)["maps"], 0)
        self.assertTrue(output.exists())
        self.assertTrue(all(p.exists() for p in artifacts))

    def test_maintain_mirror_gc_cleans_expired_outputs_from_any_package(self):
        output, artifacts = self.output(scope="secondary_package")
        original_run = lake.supervisor.run

        def no_running_lake(argv, **kwargs):
            if argv[:1] == ["pgrep"]:
                return subprocess.CompletedProcess(argv, 1)
            return original_run(argv, **kwargs)

        with patch.object(lake.supervisor, "run", side_effect=no_running_lake):
            lake.maintain_mirror(self.root / "pkg-mirror", self.env, gc_cache=True, project=self.project)
        self.assertFalse(output.exists())
        self.assertTrue(all(not artifact.exists() for artifact in artifacts))

    def test_cross_scope_reference_keeps_shared_artifacts(self):
        output, artifacts = self.output()
        self.output("e" * 16, "qatlib")
        result = self.collect(apply=True)
        self.assertEqual(result["maps"], 1)
        self.assertEqual(result["artifacts"], 0)
        self.assertFalse(output.exists())
        self.assertTrue(all(p.exists() for p in artifacts))

    def test_recent_map_or_artifact_is_retained(self):
        output, artifacts = self.output()
        os.utime(output, None)
        self.assertEqual(lake.gc(self.cache, workspace=self.project)["maps"], 0)
        os.utime(output, (1, 1))
        os.utime(artifacts[1], None)
        self.assertEqual(lake.gc(self.cache, workspace=self.project)["maps"], 0)

    def test_archive_only_map_is_retained(self):
        artifact = self.artifact("f" * 16 + ".ltar")
        lake.atomic_json(self.cache / "outputs/example_library/archive.json",
                         {"data": artifact.name})
        self.assertEqual(self.collect(apply=True)["maps"], 0)
        self.assertTrue(artifact.exists())

    def test_invalid_map_aborts_before_any_deletion(self):
        output, artifacts = self.output()
        lake.atomic_json(self.cache / "outputs/other/unknown.json", {"unknown": []})
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.collect(apply=True)
        self.assertTrue(output.exists())
        self.assertTrue(all(p.exists() for p in artifacts))

    def test_cross_volume_gc_skips_before_reading_maps(self):
        with patch.object(Path, "stat") as metadata:
            metadata.side_effect = [type("Stat", (), {"st_dev": 1})(),
                                    type("Stat", (), {"st_dev": 2})()]
            self.assertIn("skipped", self.collect(apply=True))

    def test_completed_setup_is_deleted_and_other_files_remain(self):
        setup = self.project / ".lake/build/ir/Mod.setup.json"
        setup.parent.mkdir(parents=True)
        setup.write_text("setup input")
        other = setup.with_name("Mod.c")
        other.write_text("C output")
        self.assertEqual(lake.clean_setup(self.project)["files"], 1)
        self.assertTrue(setup.exists())
        lake.clean_setup(self.project, apply=True)
        self.assertFalse(setup.exists())
        self.assertTrue(other.exists())
        self.assertFalse((self.project / ".lake/qatlas-quarantine").exists())

    def test_anonymous_setup_cleanup_keeps_unrelated_temp_files(self):
        directory = self.project / ".lake/qatlas-tmp"
        directory.mkdir(parents=True)
        setup = directory / "anonymous"
        setup.write_text('{"name":"Probe","isModule":false,"importArts":{}}')
        other = directory / "user.json"
        other.write_text('{"name":"user data"}')
        self.assertEqual(lake.clean_setup(self.project, apply=True)["files"], 1)
        self.assertFalse(setup.exists())
        self.assertTrue(other.exists())

    def test_dependency_escape_rejected_before_cleanup(self):
        (self.project / "lake-manifest.json").write_text('{"packagesDir":"../escape","packages":[]}')
        with self.assertRaises(ValueError):
            lake.clean_setup(self.project, apply=True)

    def test_same_volume_uses_official_default(self):
        default = self.root / "official-cache"
        with patch.object(lake, "official_cache", return_value=default):
            env = lake.environment(self.project, base_env=HOST_ENV)
        self.assertEqual(Path(env["LAKE_CACHE_DIR"]), default)

    def test_retrieval_preflight_loads_workspace_without_creating_cache(self):
        from qatlib_env.lean_runtime import prepared_environment
        (self.project / "python/qatlib_env").mkdir(parents=True)
        (self.project / "python/qatlib_env/workspace.py").write_bytes(SCRIPT.read_bytes())
        cache = self.root / "not-created"
        result = prepared_environment(self.project, {"LAKE_CACHE_DIR": str(cache)})
        self.assertEqual(result["LAKE_CACHE_DIR"], str(cache))
        self.assertFalse(cache.exists())

    def test_elan_and_standalone_defaults_match_official_layout(self):
        home = self.root / "home"
        env = {"HOME": str(home), "USERPROFILE": str(home), "ELAN_HOME": str(home / ".elan"),
               "ELAN_TOOLCHAIN": TOOLCHAIN}
        self.assertEqual(lake.official_cache(env), home / ".elan/toolchains/leanprover--lean4---v4.35.0/lake/cache")
        env["ELAN"] = ""
        env["XDG_CACHE_HOME"] = str(home / "xdg")
        self.assertEqual(lake.official_cache(env), home / "xdg/lake")

    def test_explicit_cache_is_respected(self):
        env = lake.environment(self.project, base_env=self.env)
        self.assertEqual(env["LAKE_CACHE_DIR"], str(self.cache))

    def test_default_jobs_reserves_memory_per_lean_process(self):
        host = lake.resources.HostResources(125 * lake.GIB, 105 * lake.GIB, 48, 48, 125 * lake.GIB)
        with patch.object(lake.resources, "probe_resources", return_value=host), \
             patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(lake.default_jobs(), 23)

    def test_explicit_lean_thread_limit_is_preserved(self):
        env = lake.environment(self.project, base_env={**self.env, "LEAN_NUM_THREADS": "2"})
        self.assertEqual(env["LEAN_NUM_THREADS"], "2")

    def test_run_captures_real_child_output(self):
        output = lake.run([sys.executable, "-c", "print('supervised')"], cwd=self.project,
                          env=self.env, capture=True)
        self.assertEqual(output, "supervised\n")

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process-tree ownership")
    def test_run_failure_reaps_real_detached_descendant(self):
        pidfile = self.root / "detached.pid"
        script = ("import subprocess,sys,time\n"
                  "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'], start_new_session=True)\n"
                  "open(sys.argv[1],'w').write(str(child.pid))\n"
                  "time.sleep(.1)\nraise SystemExit(9)\n")
        with self.assertRaises(subprocess.CalledProcessError) as context:
            lake.run([sys.executable, "-c", script, str(pidfile)], cwd=self.project, env=self.env)
        self.assertEqual(context.exception.returncode, 9)
        self.assertFalse(Path(f"/proc/{pidfile.read_text()}").exists())

    def test_cache_lock_creates_and_serializes_fetches(self):
        entered = threading.Event()
        release = threading.Event()
        acquired = threading.Event()

        def holder():
            with lake.cache_lock(self.cache):
                entered.set()
                release.wait(2)

        def waiter():
            with lake.cache_lock(self.cache):
                acquired.set()

        first = threading.Thread(target=holder)
        second = threading.Thread(target=waiter)
        first.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue((self.cache / ".fetch.lock").is_file())
        second.start()
        self.assertFalse(acquired.wait(0.1))
        release.set()
        first.join(2)
        second.join(2)
        self.assertTrue(acquired.is_set())

    def test_project_toolchain_selects_its_own_official_cache(self):
        (self.project / "lean-toolchain").write_text("leanprover/lean4:v4.36.0")
        elan = self.root / "elan"
        env = lake.environment(self.project, base_env={**HOST_ENV, "ELAN_HOME": str(elan)})
        self.assertEqual(env["ELAN_TOOLCHAIN"], "leanprover/lean4:v4.36.0")
        self.assertEqual(Path(env["LAKE_CACHE_DIR"]),
                         elan / "toolchains/leanprover--lean4---v4.36.0/lake/cache")
        with self.assertRaisesRegex(ValueError, "ELAN_TOOLCHAIN differs"):
            lake.environment(self.project, base_env={**self.env, "ELAN_TOOLCHAIN": TOOLCHAIN})

    def test_environment_never_exports_package_url_map(self):
        env = lake.environment(self.project, base_env={**self.env, "LAKE_PKG_URL_MAP": "{}"})
        self.assertNotIn("LAKE_PKG_URL_MAP", env)
        self.assertNotIn("LAKE_PKG_URL_MAP", lake.ENV_KEYS)

    def test_run_calls_lake_once_without_maintenance_scan(self):
        with patch.object(lake, "environment", return_value=self.env), \
             patch.object(lake, "run") as command, \
             patch.object(lake, "clean_setup", return_value={}):
            lake.main(["--project", str(self.project), "run", "--no-prepare", "--", "lake", "build", "Probe"])
        self.assertEqual(command.call_count, 1)

    def test_bootstrap_leaves_manifest_untouched_when_lake_fails(self):
        env = dict(self.env)
        lock = self.project / "lake-manifest.json"
        original = lock.read_bytes()
        def fail(*args, **kwargs):
            lock.write_text('{"packages": [{"type": "path", "dir": "local"}]}')
            raise subprocess.CalledProcessError(1, ["lake"])
        with patch.object(lake, "run", side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                lake.bootstrap(self.project, env)
        self.assertNotEqual(lock.read_bytes(), original)

    def test_bootstrap_uses_shared_root_cache_restoration(self):
        env = dict(self.env)
        (self.project / "scripts").mkdir(parents=True, exist_ok=True)
        (self.project / "scripts/get_cache.lean").write_text("-- stub\n")
        lake.atomic_json(self.cache / "outputs/mathlib/local-build.json", {})
        with patch.object(lake, "run"), \
             patch('qatlib_env.prepare.restore_repository', return_value="missing") as restore:
            lake.bootstrap(self.project, env)
        restore.assert_called_once_with(self.project, self.project, env)

    def test_failed_command_preserves_setup_for_concurrent_leanir(self):
        failure = subprocess.CalledProcessError(1, ["lake", "build", "Probe"])
        with patch.object(lake, "environment", return_value=self.env), \
             patch.object(lake, "run", side_effect=failure), \
             patch.object(lake, "clean_setup", return_value={}) as cleanup:
            with self.assertRaises(subprocess.CalledProcessError):
                lake.main(["--project", str(self.project), "run", "--", "lake", "build", "Probe"])
        cleanup.assert_not_called()

    def test_shared_packages_parent_is_rejected(self):
        directory = self.project / ".lake"
        directory.mkdir()
        target = self.root / "shared"
        target.mkdir()
        try:
            (directory / "packages").symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("this account cannot create directory symlinks")
        with self.assertRaisesRegex(ValueError, "shared/symlinked"):
            lake.environment(self.project, base_env=self.env)

    @unittest.skipUnless(os.name == "nt", "native Windows junction test")
    def test_windows_junction_build_is_rejected(self):
        directory = self.project / ".lake"
        directory.mkdir()
        shared = self.root / "shared-build"
        shared.mkdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(directory / "build"), str(shared)],
                       check=True, capture_output=True)
        with self.assertRaisesRegex(ValueError, "shared/symlinked"):
            lake.environment(self.project, base_env=self.env)


if __name__ == "__main__":
    unittest.main()
