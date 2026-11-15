from __future__ import annotations

import json
from contextlib import nullcontext
import hashlib
import io
from pathlib import Path
import tempfile
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

from qatlib_env.cloud import CloudLoop, CloudRequest, unpack_bundle
from qatlib_forge.ci_logs import read_run_zip
from qatlib_env.pool import Pool, Lease
from qatlib_env import pool as pool_module, workspace
from qatlib_forge.repo_api import RepoAPI, RepoRef


class _FakeAPI:
    def __init__(self, response=None):
        self.response = response
        self.calls = []

    def dispatch(self, workflow, ref, inputs):
        self.calls.append(("dispatch", workflow, ref, inputs))
        return self.response

    def run(self, run_id):
        self.calls.append(("run", run_id))
        return {"id": run_id, "status": "completed", "conclusion": "success"}


class _Dispatch204API(_FakeAPI):
    def workflow_runs(self, workflow, *, branch=None):
        self.calls.append(("workflow_runs", workflow, branch))
        return {"workflow_runs": [{"id": 43, "head_sha": "abc"}]}


class _TimestampAPI(_FakeAPI):
    def workflow_runs(self, workflow, *, branch=None):
        self.calls.append(("workflow_runs", workflow, branch))
        return {"workflow_runs": [
            {"id": 41, "head_sha": "a" * 40, "created_at": "2020-01-01T00:00:00Z"},
            {"id": 42, "head_sha": "a" * 40, "created_at": "2999-01-01T00:00:00Z"},
        ]}


class PoolCloudTests(unittest.TestCase):
    def test_lease_heartbeat_and_dead_pid_reaping(self):
        with tempfile.TemporaryDirectory() as directory:
            pool = Pool(budget_mib=4096, budget_cpu=2, state_dir=Path(directory), ttl=90)
            lease = pool.acquire(2048, owner="test")
            old = lease.heartbeat
            pool.heartbeat(lease)
            self.assertGreaterEqual(lease.heartbeat, old)
            data = json.loads(pool.ledger.read_text())
            data["leases"].append(Lease("dead", 99999999, "never", 2048, 1, time.time(), "dead").__dict__)
            pool.ledger.write_text(json.dumps(data))
            status = pool.status()
            self.assertEqual(status["used_mib"], 2048)
            pool.release(lease)
            self.assertEqual(pool.status()["used_mib"], 0)

    def test_lease_identity_rejects_pid_reuse_on_each_supported_platform(self):
        lease = Lease("token", 4242, "creation-old", 2048, 1, time.time(), "test")
        for platform in ("linux", "darwin", "win32"):
            with self.subTest(platform=platform), patch.object(pool_module.sys, "platform", platform), \
                 patch.object(pool_module.os, "kill"), patch.object(pool_module, "_identity", return_value="creation-new"):
                self.assertFalse(lease.alive())

    def test_linux_identity_reads_proc_start_time(self):
        text = "4242 (qatlib) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20"
        with patch.object(pool_module.sys, "platform", "linux"), patch.object(pool_module.Path, "read_text", return_value=text):
            self.assertEqual(pool_module._identity(4242), "19")

    def test_darwin_identity_reads_process_start_time(self):
        with patch.object(pool_module.sys, "platform", "darwin"), patch.object(pool_module.subprocess, "check_output", return_value="Wed Jan  1 00:00:00 2026\n"):
            self.assertEqual(pool_module._identity(4242), "Wed Jan  1 00:00:00 2026")

    def test_backend_command_and_run_use_darwin_taskpolicy(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch('qatlib_env.budget.admit', return_value={'reasons': []}), \
             patch.object(workspace, '_pool_scope', return_value=nullcontext()), \
             patch.object(workspace, 'command_runtime_environment', side_effect=lambda p, e, a: e), \
             patch.object(workspace.sys, "platform", "darwin"), \
             patch("qatlib_env.backends.macos.macos_limits", return_value=["taskpolicy", "--", "lake", "build"]) as limits, \
             patch.object(workspace.supervisor, "run", return_value=__import__("subprocess").CompletedProcess(["lake", "build"], 0, stdout="")) as supervised:
            workspace.run(["lake", "build"], cwd=directory, env={}, capture=True)
            limits.assert_called_once()
            self.assertEqual(supervised.call_args.args[0][:2], ["taskpolicy", "--"])

    def test_backend_command_and_run_attach_windows_job_object(self):
        jobs = []
        class FakeJob:
            def __init__(self, name): self.name = name; jobs.append(self)
            def configure(self, **kwargs): self.configuration = kwargs
            def attach_pid(self, pid): self.attached = pid; return True
            def close(self): self.closed = True
        def supervise(command, **kwargs):
            kwargs["process_callback"](SimpleNamespace(pid=4242))
            return __import__("subprocess").CompletedProcess(command, 0, stdout="")
        with tempfile.TemporaryDirectory() as directory, \
             patch('qatlib_env.budget.admit', return_value={'reasons': []}), \
             patch.object(workspace, '_pool_scope', return_value=nullcontext()), \
             patch.object(workspace, 'command_runtime_environment', side_effect=lambda p, e, a: e), \
             patch.object(workspace.sys, "platform", "win32"), \
             patch("qatlib_env.backends.windows.JobObject", FakeJob), \
             patch.object(workspace.supervisor, "run", side_effect=supervise):
            workspace.run(["lake", "build"], cwd=directory, env={}, capture=True)
        self.assertEqual(len(jobs), 1)
        self.assertGreaterEqual(jobs[0].configuration["memory_bytes"], 2048 * 1024 * 1024)
        self.assertEqual(jobs[0].attached, 4242)
        self.assertTrue(jobs[0].closed)

    def test_cloud_loop_persists_dispatch_state_and_waits_for_run(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _FakeAPI({"workflow_run_id": 42})
            loop = CloudLoop(api, Path(directory) / "state.json")
            request = CloudRequest("github", "acme/tools", "build.yml", "abc", {})
            state = loop.submit(request)
            self.assertEqual(state["run_id"], 42)
            self.assertEqual(loop.wait(timeout=0.2, interval=0), {"id": "42", "status": "completed", "conclusion": "success"})
            self.assertEqual(json.loads(loop.state_path.read_text())["request"]["head"], "abc")

    def test_cloud_loop_handles_empty_204_dispatch_response(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _FakeAPI(None)
            loop = CloudLoop(api, Path(directory) / "state.json")
            state = loop.submit(CloudRequest("github", "acme/tools", "build.yml", "abc", {}))
            self.assertNotIn("run_id", state)
            self.assertIsNone(state["response"])

    def test_cloud_loop_correlates_204_by_exact_head(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _Dispatch204API(None)
            state = CloudLoop(api, Path(directory) / "state.json").submit(
                CloudRequest("github", "acme/tools", "build.yml", "abc", {}))
            self.assertEqual(state["run_id"], 43)
            self.assertEqual(api.calls[-1], ("workflow_runs", "build.yml", "abc"))

    def test_cloud_loop_rejects_stale_iso_timestamp_and_does_not_use_sha_as_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            api = _TimestampAPI(None)
            state = CloudLoop(api, Path(directory) / "state.json").submit(
                CloudRequest("github", "acme/tools", "build.yml", "a" * 40, {}, ref="main"))
            self.assertEqual(state["run_id"], 42)
            self.assertEqual(api.calls[-1], ("workflow_runs", "build.yml", None))

    def test_cloud_fetch_verifies_remote_manifest_and_digest(self):
        payload = b"verified payload\n"
        manifest = json.dumps({
            "schema_version": 1,
            "head_sha": "abc",
            "lean_toolchain": "toolchain",
            "entries": {"payload.txt": {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}},
        }).encode()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("manifest.json", manifest)
            archive.writestr("payload.txt", payload)
        archive_bytes = stream.getvalue()

        class ArtifactAPI(_FakeAPI):
            def artifacts(self, run_id):
                self.calls.append(("artifacts", run_id))
                return {"artifacts": [{"id": 7, "name": "verification", "sha256": hashlib.sha256(archive_bytes).hexdigest()}]}
            def download_artifact(self, artifact_id):
                self.calls.append(("download_artifact", artifact_id))
                return 200, {}, archive_bytes

        with tempfile.TemporaryDirectory() as directory:
            api = ArtifactAPI({"run_id": 7})
            loop = CloudLoop(api, Path(directory) / "state.json")
            loop.submit(CloudRequest("github", "acme/tools", "build.yml", "abc", {}))
            result = loop.fetch(Path(directory) / "destination")
            self.assertEqual((Path(result["destination"]) / "payload.txt").read_bytes(), payload)

    def test_repo_api_log_urls_and_range_reads_cover_github_and_gitea(self):
        class Response:
            status = 206
            headers = {"Content-Range": "bytes 0-10/11"}
            def __init__(self): self.data = b"Main.lean:1:1: error: bad"
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size=-1): return self.data

        for ref, expected in (
            (RepoRef.parse("https://github.com/acme/tools"), "/actions/jobs/8/logs"),
            (RepoRef.parse("https://mirror.test/acme/tools", platform="gitea"), "/actions/runs/3/jobs/8/logs"),
        ):
            with self.subTest(platform=ref.platform):
                client = RepoAPI(ref, token="test")
                self.assertTrue(client.job_log_url(3, 8).endswith(expected))
                with patch.object(client, "_action_redirect", return_value=(302, {"Location": "https://logs.test/job"}, b"")), \
                     patch("qatlib_forge.ci_logs.RangeLogReader.read", return_value=(Response().data, {":status": "206", "content-range": "bytes 0-10/11"})):
                    data, headers = client.read_job_log_range(3, 8, start=0, end=10)
                self.assertIn(b"Main.lean", data)
                self.assertEqual(headers[":status"], "206")

    def test_run_zip_rejects_absolute_parent_drive_and_symlink_members(self):
        for name in ("../escape.txt", "/absolute.txt", "C:/drive.txt"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive: archive.writestr(name, "x")
            with self.assertRaises(ValueError): read_run_zip(stream.getvalue())
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            info = zipfile.ZipInfo("link"); info.external_attr = 0o120777 << 16; archive.writestr(info, "target")
        with self.assertRaises(ValueError): read_run_zip(stream.getvalue())

    def test_bundle_rejects_absolute_parent_and_drive_members(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("../escape.txt", "/absolute.txt", "C:/drive.txt"):
                bundle = root / "bundle.zip"
                with zipfile.ZipFile(bundle, "w") as archive: archive.writestr(name, "x")
                with self.assertRaises(ValueError): unpack_bundle(bundle, root / "out")

