"""Cache selection, build requirements, and CI environment isolation."""

import tests._bootstrap as _bootstrap  # noqa: F401
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tests._git_fixture import snapshot

ENV_DIR = Path(__file__).resolve().parents[3] / "src/qatlib_env"
sys.path.insert(0, str(ENV_DIR))
from qatlib_env import ci
from tests._credential_fixture import environment as credential_env



class CITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_candidates_prefer_revision_then_input_tree_then_nearest_ancestor(self):
        for expected, hit, calls in [("exact", "head.jsonl", 1), ("exact", "inputs/tree.jsonl", 2),
                                      ("ancestor", "parent.jsonl", 3), ("ancestor", "older.jsonl", 4),
                                      ("none", "absent", 4)]:
            seen = []
            def fetch(key):
                seen.append(key)
                return key == hit
            self.assertEqual(ci.select_candidate("head", "tree", ["parent", "older"], fetch), expected)
            self.assertEqual(len(seen), calls)

    def test_header_only_manifest_is_not_a_candidate(self):
        candidate = self.root / "candidate.jsonl"
        candidate.write_text('"2026-03-17"\n')
        self.assertFalse(ci.manifest_is_usable(candidate))
        candidate.write_text('"2026-03-17"\n["hash", "artifact.ltar"]\n')
        self.assertTrue(ci.manifest_is_usable(candidate))

    def test_official_toolchain_directory_encoding(self):
        self.assertEqual(ci.toolchain_dir("leanprover/lean4:v4.35.0"),
                         "leanprover--lean4---v4.35.0")

    def test_dev_dependency_dir_follows_manifest_over_stale_directory(self):
        (self.root / "lake-manifest.json").write_text(json.dumps({
            "packages": [{"name": "ConsumerLib"}]
        }))
        (self.root / ".lake/packages/consumer_lib").mkdir(parents=True)
        self.assertEqual(ci.dev_dependency_dir(self.root),
                         self.root / ".lake/packages/ConsumerLib")
        (self.root / "lake-manifest.json").write_text(json.dumps({
            "packages": [{"name": "consumer_lib"}]
        }))
        self.assertEqual(ci.dev_dependency_dir(self.root),
                         self.root / ".lake/packages/consumer_lib")

    def test_dev_dependency_dir_falls_back_to_existing_directory_without_manifest_entry(self):
        (self.root / "lake-manifest.json").write_text(json.dumps({"packages": []}))
        stale = self.root / ".lake/packages/consumer_lib"
        stale.mkdir(parents=True)
        self.assertEqual(ci.dev_dependency_dir(self.root), stale)

    def test_input_tree_includes_root_aggregate_for_both_packages(self):
        for package in ["ConsumerLib", "consumer_lib", "SecondaryProject", "secondary_package"]:
            library, _, _ = ci.profile(package)
            with patch.object(ci.subprocess, "check_output", return_value=b"tree\n") as command:
                self.assertEqual(ci.input_tree(self.root, library), hashlib.sha256(b"tree\n").hexdigest())
            self.assertIn("HEAD:" + library + ".lean", command.call_args.args[0])

    def test_source_import_gate_supports_dev_and_secondary_checkers(self):
        dev = self.root / "dev"
        (dev / "python/dev_metadata").mkdir(parents=True)
        (dev / "python/dev_metadata/check_source_imports.py").write_text("# checker\n")
        self.assertIn("dev_metadata/check_source_imports.py", str(ci.source_import_gate(dev)))

        secondary = self.root / "secondary"
        (secondary / "python/secondary_ops").mkdir(parents=True)
        (secondary / "python/secondary_ops/check_source_imports.py").write_text("# checker\n")
        self.assertIn("secondary_ops/check_source_imports.py", str(ci.source_import_gate(secondary)))

    def test_lint_runs_source_import_gate_before_other_checks(self):
        project = self.root / "secondary"
        (project / "python/secondary_ops").mkdir(parents=True)
        (project / "python/secondary_ops/check_source_imports.py").write_text("# checker\n")
        commands = []
        with patch.object(ci.workspace, "ensure_pkg_mirror"), \
             patch.object(ci.workspace, "run", side_effect=lambda command, **kwargs: commands.append(command)), \
             patch.object(ci.workspace, "package_name", return_value="secondary_package"), \
             patch.object(ci.shutil, "which", return_value=None):
            ci.lint(project, {})
        self.assertIn("secondary_ops/check_source_imports.py", str(commands[0]))
        self.assertEqual(commands[1], ["git", "diff", "--check"])

    def test_only_exact_success_skips_build(self):
        output = self.root / ".lake/outputs.jsonl"
        output.parent.mkdir()
        candidate = self.root / "candidate.jsonl"
        candidate.write_text('header\n["hash", "artifact.ltar"]\n')
        def build(*args, **kwargs):
            output.write_text("new build\n")
        for kind, probe_rc, expected_builds in [("exact", 0, 0), ("exact", 1, 1), ("ancestor", 0, 1), ("none", 0, 1)]:
            summary = self.root / ("summary-" + kind + str(probe_rc))
            with patch.object(ci.supervisor, "run", return_value=subprocess.CompletedProcess([], probe_rc)) as probe, \
                 patch.object(ci.workspace, "run", side_effect=build) as command, \
                 patch.object(ci, "restore_ltars") as restore:
                ci.compile_targets(self.root, {"LAKE_CACHE_DIR": str(self.root / "cache"),
                                               "GITHUB_STEP_SUMMARY": str(summary)}, ["Library"], kind, candidate)
            timings = summary.read_text(encoding="utf-8")
            result = ("rejected" if probe_rc else "verified") if kind == "exact" else "skipped"
            self.assertIn(f"cache_kind={kind} · result={result}", timings)
            self.assertEqual("fallback-build" in timings, bool(expected_builds))
            self.assertEqual(command.call_count, expected_builds)
            self.assertEqual(restore.call_count, expected_builds)
            self.assertEqual(probe.call_count, int(kind == "exact"))
            if expected_builds:
                self.assertEqual(command.call_args.args[0], ["lake", "--fail-fast", "build", "Library", "-o", ".lake/outputs.jsonl"])
            else:
                self.assertEqual(output.read_bytes(), candidate.read_bytes())

    def test_upload_service_keeps_single_endpoint_compatibility(self):
        read, write = {"name": "read"}, {"name": "write"}
        config = {"defaultService": "read", "service": [read, write]}
        self.assertIs(ci.cache_service(config, upload=True), read)
        config["defaultUploadService"] = "write"
        self.assertIs(ci.cache_service(config), read)
        self.assertIs(ci.cache_service(config, upload=True), write)

    def test_archive_restoration_links_by_trace_hash(self):
        cache, build = self.root / "cache", self.root / "build"
        archive = cache / "artifacts/0123456789abcdef.ltar"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"archive")
        trace = build / "lib/lean/Library/Module.trace"
        trace.parent.mkdir(parents=True)
        trace.write_text('{"depHash":"input-hash"}')
        candidate = self.root / "candidate.jsonl"
        candidate.write_text('header\n["input-hash", "0123456789abcdef.ltar"]\n')
        ci.restore_ltars(candidate, cache, build)
        self.assertTrue(os.path.samefile(archive, build / "ir/Library/Module.ltar"))

    def test_archive_restoration_uses_local_pool_without_remote_candidate(self):
        cache, build = self.root / "cache", self.root / "build"
        archive = cache / "artifacts/0123456789abcdef.ltar"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"archive")
        mapping = cache / "outputs/ConsumerLib/hash.json"
        mapping.parent.mkdir(parents=True)
        mapping.write_text(json.dumps({"data": archive.name}))
        trace = build / "lib/lean/Module.trace"
        trace.parent.mkdir(parents=True)
        trace.write_text('{"depHash":"hash"}')
        ci.restore_ltars(self.root / "absent", cache, build)
        self.assertTrue(os.path.samefile(archive, build / "ir/Module.ltar"))

    def test_complete_outputs_repairs_cached_ltar_mapping_and_archive(self):
        source = self.root / "Root/Module.lean"
        source.parent.mkdir(parents=True)
        source.write_text("def answer := 42\n")
        build = self.root / ".lake/build"
        trace = build / "lib/lean/Root/Module.trace"
        trace.parent.mkdir(parents=True)
        trace.write_text(json.dumps({"depHash": "module-input"}))
        archive = build / "ir/Root/Module.ltar"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"archive")
        cache = self.root / "cache"
        cached = cache / "outputs/scope/module-input.json"
        cached.parent.mkdir(parents=True)
        cached.write_text(json.dumps({"data": {"ltar": "0123456789abcdef.ltar", "m": True}}))
        output = self.root / ".lake/outputs.jsonl"
        output.write_text('"2026-03-17"\n')
        self.assertEqual(ci.complete_outputs_manifest(self.root, output, cache), 1)
        self.assertEqual(output.read_text().splitlines()[1], '["module-input","0123456789abcdef.ltar"]')
        self.assertTrue(os.path.samefile(archive, cache / "artifacts/0123456789abcdef.ltar"))

    def test_orphan_trace_without_build_artifacts_is_ignored(self):
        source = self.root / "Root/Orphan.lean"
        source.parent.mkdir(parents=True)
        source.write_text("def orphan := 0\n")
        trace = self.root / ".lake/build/lib/lean/Root/Orphan.trace"
        trace.parent.mkdir(parents=True)
        trace.write_text(json.dumps({"depHash": "orphan-input"}))
        output = self.root / ".lake/outputs.jsonl"
        output.write_text('"2026-03-17"\n')
        self.assertEqual(ci.complete_outputs_manifest(self.root, output, self.root / "cache"), 0)
        self.assertEqual(output.read_text(), '"2026-03-17"\n')

    def test_delta_uses_all_current_entries_but_removes_prior_rows(self):
        output, candidate = self.root / "output", self.root / "candidate"
        output.write_text('header\n["b", "new"]\n["a", "old"]\n')
        candidate.write_text('header\n["a", "old"]\n')
        self.assertEqual(ci.delta_manifest(output, candidate), ('header\n["b", "new"]\n', 1))

    def test_publish_requires_main_allowed_event_and_write_key_on_both_providers(self):
        for provider in ["GITHUB_", "GITEA_"]:
            env = credential_env({provider + "REF": "refs/heads/main", provider + "EVENT_NAME": "push", "LAKE_DEV_CACHE_KEY": "key"})
            self.assertTrue(ci.may_publish(env, "ConsumerLib"))
            self.assertTrue(ci.may_publish({**env, provider + "EVENT_NAME": "workflow_dispatch"}, "ConsumerLib"))
            self.assertFalse(ci.may_publish({**env, provider + "EVENT_NAME": "pull_request"}, "ConsumerLib"))
            self.assertFalse(ci.may_publish({**env, provider + "REF": "refs/heads/topic"}, "ConsumerLib"))
            self.assertFalse(ci.may_publish({**env, "LAKE_DEV_CACHE_KEY": " "}, "ConsumerLib"))
            self.assertTrue(ci.may_publish({**env, "LAKE_CACHE_KEY": " ",
                                            "LAKE_DEV_CACHE_KEY": "dev-key"}, "consumer_lib"))
            self.assertTrue(ci.may_publish({**env, "LAKE_CACHE_KEY": " ",
                                            "LAKE_DEV_CACHE_KEY": "dev-key"}, "ConsumerLib"))
            self.assertTrue(ci.may_publish({**env, "LAKE_CACHE_KEY": " ",
                                            "LAKE_SECONDARY_CACHE_KEY": "secondary-key"},
                                           "secondary_package"))
            self.assertTrue(ci.may_publish({**env, "LAKE_CACHE_KEY": " ",
                                            "LAKE_SECONDARY_CACHE_KEY": "secondary-key"},
                                           "SecondaryProject"))

    def test_all_runner_temporary_paths_stay_on_runner_volume(self):
        runner = self.root / "runner"
        env = ci.runner_environment(self.root, {"RUNNER_TEMP": str(runner), "TMPDIR": "/unwanted"})
        for key in ["TMPDIR", "TMP", "TEMP", "XDG_DATA_HOME", "QATLIB_TEST_ROOT"]:
            self.assertTrue(Path(env[key]).is_relative_to(runner))
            self.assertTrue(Path(env[key]).is_dir())

    def public_source_fixture(self):
        origin = self.root / "public-source"
        origin.mkdir()
        subprocess.run(["git", "init", "-q", str(origin)], check=True)
        source = origin / "Qatlib/Concepts/Example.lean"
        source.parent.mkdir(parents=True)
        source.write_text("namespace Qatlib.Concepts.Example\nend Qatlib.Concepts.Example\n")
        locked = snapshot(origin)
        (self.root / "lake-manifest.json").write_text(json.dumps({
            "packages": [{"name": "Qatlib", "rev": locked}],
        }))
        return origin, locked

    def clone_public_fixture(self, origin, destination, *options):
        subprocess.run(["git", "clone", "-q", *options, origin.as_uri(), str(destination)],
                       check=True, capture_output=True)
        return destination

    def test_public_repo_uses_shallow_local_sources_before_object_mirror(self):
        origin, _locked = self.public_source_fixture()
        local = self.clone_public_fixture(origin, self.root / ".lake/packages/Qatlib", "--depth=1")
        self.assertEqual(subprocess.check_output(
            ["git", "-C", str(local), "rev-parse", "--is-shallow-repository"], text=True,
        ).strip(), "true")
        with patch.object(ci.workspace, "usable_git_mirror", return_value=False) as object_mirror:
            self.assertEqual(ci.public_repo(self.root, {"QATLIB_PKG_MIRROR": str(self.root / "mirror")}),
                             local)
        object_mirror.assert_not_called()

    def test_public_repo_rejects_no_checkout_mirror(self):
        origin, locked = self.public_source_fixture()
        mirror = self.root / "mirror"
        checkout = self.clone_public_fixture(origin, mirror / ".lake/packages/Qatlib", "--no-checkout")
        self.assertEqual(ci.workspace.git_head(checkout), locked)
        with self.assertRaisesRegex(ValueError, "materialized Lean sources"):
            ci.public_repo(self.root, {"QATLIB_PKG_MIRROR": str(mirror)})
        self.assertFalse((checkout / "Qatlib").exists())

    def test_public_repo_rejects_sources_at_stale_head(self):
        origin, locked = self.public_source_fixture()
        (origin / "new-file.txt").write_text("new revision\n")
        newer = snapshot(origin)
        self.assertNotEqual(newer, locked)
        local = self.clone_public_fixture(origin, self.root / ".lake/packages/Qatlib")
        self.assertEqual(ci.workspace.git_head(local), newer)
        with self.assertRaisesRegex(ValueError, locked):
            ci.public_repo(self.root, {})

    def test_public_repo_falls_back_to_materialized_locked_mirror(self):
        origin, _locked = self.public_source_fixture()
        self.clone_public_fixture(origin, self.root / ".lake/packages/Qatlib", "--no-checkout")
        mirror = self.root / "mirror"
        checkout = self.clone_public_fixture(origin, mirror / ".lake/packages/Qatlib")
        self.assertEqual(ci.public_repo(self.root, {"QATLIB_PKG_MIRROR": str(mirror)}), checkout)

    def test_public_repo_fails_when_locked_sources_are_unavailable(self):
        _origin, locked = self.public_source_fixture()
        with self.assertRaisesRegex(ValueError, locked):
            ci.public_repo(self.root, {})

    def test_public_repo_returns_none_without_qatlib_lock(self):
        (self.root / "lake-manifest.json").write_text(json.dumps({"packages": []}))
        self.assertIsNone(ci.public_repo(self.root, {}))

    def test_build_does_not_require_copied_qatlib_env(self):
        project = self.root / "qatlas-shared-extension"
        project.mkdir()
        environment = {"LAKE_CACHE_DIR": str(self.root / "cache")}
        commands = []

        class StopBuild(Exception):
            pass

        def run(command, **kwargs):
            commands.append(command)

        def stop_after_sync(*args, **kwargs):
            raise StopBuild

        for has_qatlib_env in (False, True):
            commands.clear()
            dependency = project / ".lake/packages/consumer_lib/python/qatlib_env"
            if has_qatlib_env:
                dependency.mkdir(parents=True)
            with patch.object(ci.workspace, "environment", return_value=environment), \
                 patch("qatlib_env.prepare.prepare"), \
                 patch.object(ci.workspace, "ensure_pkg_mirror"), \
                 patch.object(ci.workspace, "cache_lock", return_value=ci.nullcontext()), \
                 patch.object(ci.workspace, "package_name", return_value="secondary_package"), \
                 patch.object(ci, "profile", return_value=("Library", ["Library"], "repo")), \
                 patch.object(ci.workspace, "run", side_effect=run), \
                 patch.object(ci, "input_tree", side_effect=stop_after_sync):
                with self.assertRaises(StopBuild):
                    ci.build(project, {})
            sync = [command for command in commands if command[-2:] == ["sync", "--check"]]
            self.assertFalse(sync, commands)
            for gate in ("check_source_imports.py", "check_promoted_duplicates.py",
                         "check_references.py", "check_meta_v2.py", "gen_v2_docs.py"):
                self.assertFalse(any(gate in str(command) for command in commands),
                                 (gate, commands))

    def test_build_runs_source_import_gate_before_cache_restore(self):
        project = self.root / "qatlas-shared-extension"
        project.mkdir()
        checker = project / "python/secondary_ops/check_source_imports.py"
        checker.parent.mkdir(parents=True)
        checker.write_text("# checker\n")
        environment = {"LAKE_CACHE_DIR": str(self.root / "cache")}
        commands = []

        class StopBuild(Exception):
            pass

        def run(command, **kwargs):
            commands.append(command)

        def stop_after_cache(*args, **kwargs):
            raise StopBuild

        with patch.object(ci.workspace, "environment", return_value=environment), \
             patch("qatlib_env.prepare.prepare", side_effect=lambda *a, **kw: commands.append(["prepare"])) as prepare, \
             patch.object(ci.workspace, "ensure_pkg_mirror"), \
             patch.object(ci.workspace, "cache_lock", return_value=ci.nullcontext()), \
             patch.object(ci.workspace, "package_name", return_value="secondary_package"), \
             patch.object(ci, "profile", return_value=("Library", ["Library"], "repo")), \
             patch.object(ci.workspace, "run", side_effect=run), \
             patch.object(ci, "input_tree", side_effect=stop_after_cache):
            with self.assertRaises(StopBuild):
                ci.build(project, {})
        self.assertIn("secondary_ops/check_source_imports.py", str(commands[0]))
        self.assertEqual(commands[1], ["prepare"])
        prepare.assert_called_once_with(project, environment)


if __name__ == "__main__":
    unittest.main()
