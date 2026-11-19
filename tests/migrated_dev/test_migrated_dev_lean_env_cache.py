"""File- and process-backed tests of private credentials and local recovery."""
from __future__ import annotations

import tests._bootstrap as _bootstrap  # noqa: F401
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from qatlib_env import checkpoint, r2_cache
from tests._credential_fixture import environment as credential_env


class CacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def keyfile(self, name="private", key="file_access_12345:file_secret_6789012345"):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        path = directory / "secrets"
        r2_cache.save_key_file(path, key, "LAKE_DEV_CACHE_KEY")
        return path

    def project(self, name="first"):
        project = self.root / name
        project.mkdir()
        (project / "Root").mkdir()
        (project / "Root/Done.lean").write_text("def answer := 42\n")
        (project / "Root/Incomplete.lean").write_text("def later := 43\n")
        (project / "lakefile.toml").write_text('name="ConsumerLib"\n[[lean_lib]]\nname="Root"\n')
        (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
        (project / "lake-manifest.json").write_text('{"packages":[]}\n')
        return project

    def test_keys_precedence_and_private_save(self):
        explicit = self.keyfile("explicit")
        envfile = self.keyfile("environment", "other_access_12345:other_secret_6789012345")
        env = credential_env({"QATLIB_R2_KEY_FILE": str(envfile)})
        self.assertTrue(r2_cache.resolve_write_key("ConsumerLib", env=env).startswith("other_"))
        self.assertTrue(r2_cache.resolve_write_key("ConsumerLib", env=env, key_file=explicit).startswith("file_"))
        env["LAKE_CACHE_KEY"] = "generic_access_12345:generic_secret_6789012345"
        self.assertTrue(r2_cache.resolve_write_key("ConsumerLib", env=env, key_file=explicit).startswith("file_"))
        env["LAKE_DEV_CACHE_KEY"] = "dev_access_12345:dev_secret_6789012345"
        self.assertTrue(r2_cache.resolve_write_key("ConsumerLib", env=env, key_file=explicit).startswith("dev_"))
        self.assertEqual(stat.S_IMODE(explicit.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(explicit.parent.stat().st_mode), 0o700)
        self.assertEqual(r2_cache.resolve_write_key("ConsumerLib", env={"HOME": str(self.root)}), "")

    @unittest.skipIf(os.name == "nt", "POSIX credential modes")
    def test_insecure_directory_file_and_symlink_fail_without_secret_in_error(self):
        path = self.keyfile()
        secret = path.read_text().split("=", 1)[-1].strip()
        for target, mode in ((path.parent, 0o755), (path, 0o644)):
            target.chmod(mode)
            with self.assertRaises(PermissionError) as raised:
                r2_cache.resolve_write_key("ConsumerLib", env={}, key_file=path)
            self.assertNotIn(secret, str(raised.exception))
            target.chmod(0o700 if target == path.parent else 0o600)
        alias = path.parent / "alias"
        alias.symlink_to(path)
        with self.assertRaises(PermissionError):
            r2_cache.parse_key_file(alias)

    def test_invalid_environment_key_error_does_not_echo_credential(self):
        secret = "invalid_secret_value_that_must_not_appear"
        with self.assertRaises(ValueError) as raised:
            r2_cache.resolve_write_key("ConsumerLib", env=credential_env({"LAKE_DEV_CACHE_KEY": secret}))
        self.assertNotIn(secret, str(raised.exception))

    def test_failed_build_checkpoint_restores_fresh_checkout_and_is_idempotent(self):
        first = self.project()
        cache = self.root / "outside-cache"
        env = {"LAKE_CACHE_DIR": str(cache)}
        # Real child process completes one module and fails while compiling the
        # next. An orphan trace alone is deliberately not a successful module.
        code = '''from pathlib import Path
import json
base=Path('.lake/build')
for rel,data in {
 'lib/lean/Root/Done.olean': b'compiled olean',
 'lib/lean/Root/Done.ilean': b'compiled ilean',
 'ir/Root/Done.c': b'compiled c',
 'ir/Root/Done.setup.json': b'{"path":"old-checkout-only"}',
 'lib/lean/Root/Done.trace': json.dumps({'depHash':'completed-dependency-hash'}).encode(),
 'lib/lean/Root/Incomplete.trace': json.dumps({'depHash':'incomplete-hash'}).encode()
}.items():
 path=base/rel; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(data)
raise SystemExit(1)
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=first, check=False)
        self.assertEqual(result.returncode, 1)
        receipt = checkpoint.capture(first, env)
        self.assertEqual(receipt["modules"], 1)
        second = self.project("fresh-checkout")
        self.assertEqual(checkpoint.restore(second, env)["modules"], 1)
        self.assertEqual((second / ".lake/build/lib/lean/Root/Done.olean").read_bytes(), b"compiled olean")
        self.assertFalse((second / ".lake/build/lib/lean/Root/Incomplete.trace").exists())
        self.assertFalse((second / ".lake/build/ir/Root/Done.setup.json").exists())
        self.assertFalse((second / ".lake/outputs.jsonl").exists())
        self.assertFalse((cache / "revisions").exists())
        self.assertEqual(checkpoint.restore(second, env)["modules"], 1)
        (second / "Root/Done.lean").write_text("def answer := 0\n")
        self.assertEqual(checkpoint.restore(second, env)["modules"], 0)

    def test_corrupt_checkpoint_and_unwritable_cache_are_explicit_errors(self):
        project = self.project()
        cache = self.root / "cache"
        env = {"LAKE_CACHE_DIR": str(cache)}
        identity = checkpoint.local_identity(project)
        (cache / "checkpoints").mkdir(parents=True)
        (cache / "checkpoints" / (identity + ".zip")).write_bytes(b"corrupt zip")
        with self.assertRaisesRegex(ValueError, "corrupt local checkpoint"):
            checkpoint.restore(project, env)
        cache.chmod(0o500)
        with self.assertRaisesRegex(PermissionError, "cache is not writable"):
            checkpoint.restore(project, env)
        from qatlib_env import prepare
        with self.assertRaisesRegex(PermissionError, "read/write/search permission"):
            prepare.prepare(project, env, offline=True)
        cache.chmod(0o700)

    def test_two_failed_attempts_merge_only_their_newly_completed_modules(self):
        project = self.project()
        env = {"LAKE_CACHE_DIR": str(self.root / "cache")}
        script = '''from pathlib import Path
import json,sys
base=Path('.lake/build'); name=sys.argv[1]
for relative in ['lib/lean/Root/'+name+'.olean','lib/lean/Root/'+name+'.ilean','ir/Root/'+name+'.c']:
 path=base/relative; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(('compiled '+name).encode())
trace=base/('lib/lean/Root/'+name+'.trace')
trace.write_text(json.dumps({'depHash':'hash-'+name}))
raise SystemExit(1)
'''
        for index, name in enumerate(("Done", "Incomplete"), 1):
            start = time.time_ns()
            result = subprocess.run([sys.executable, "-c", script, name], cwd=project, check=False)
            self.assertEqual(result.returncode, 1)
            receipt = checkpoint.capture(project, env, since_ns=start)
            self.assertEqual(receipt["new_modules"], 1)
            self.assertEqual(receipt["modules"], index)
        # A no-progress retry preserves both earlier batches.
        receipt = checkpoint.capture(project, env, since_ns=time.time_ns())
        self.assertEqual(receipt["new_modules"], 0)
        self.assertEqual(receipt["modules"], 2)
        fresh = self.project("new-checkout")
        self.assertEqual(checkpoint.restore(fresh, env)["modules"], 2)
        for name in ("Done", "Incomplete"):
            self.assertEqual((fresh / f".lake/build/lib/lean/Root/{name}.olean").read_bytes(),
                             ("compiled " + name).encode())

    def test_manifest_rejects_partial_unsafe_and_conflicting_entries(self):
        self.assertEqual(r2_cache._manifest(b'"2026-03-17"\n')[1], {})
        for raw in (b'"2026-03-17"\n["hash","../escape.ltar"]\n',
                    b'"2026-03-17"\n["hash","0123456789abcdef.ltar"]\n["hash","fedcba9876543210.ltar"]\n'):
            with self.assertRaises(ValueError):
                r2_cache._manifest(raw)

    def test_native_mapping_promotion_refuses_symlinked_namespace_and_files(self):
        from qatlib_env import prepare
        staged = self.root / "staged"
        namespace = staged / "outputs/ConsumerLib"
        namespace.mkdir(parents=True)
        name = "1111111111111111.json"
        (namespace / name).write_text(json.dumps({"schemaVersion": "2026-02-25", "data": None, "service": "fixture"}))
        cache = self.root / "cache"
        (cache / "outputs").mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (cache / "outputs/ConsumerLib").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(prepare.CacheError, "namespace.*symlinked"):
            prepare._publish_native_mappings(staged, cache)
        self.assertFalse((outside / name).exists())
        # A second pool with a regular namespace still refuses a symlinked
        # module target instead of following it outside the cache.
        second = self.root / "second-cache"
        (second / "outputs/ConsumerLib").mkdir(parents=True)
        external = outside / "untouched.json"
        external.write_text('{"untouched":true}')
        (second / "outputs/ConsumerLib" / name).symlink_to(external)
        with self.assertRaisesRegex(prepare.CacheError, "not a regular file"):
            prepare._publish_native_mappings(staged, second)
        self.assertEqual(external.read_text(), '{"untouched":true}')


@unittest.skipUnless(shutil.which("lake"), "installed Lake is needed for integration fixture")
class LakeCacheIntegrationTests(unittest.TestCase):
    """An actual dependency-free Lean package exercises native traces and ltars."""
    def setUp(self):
        # Keep actual Lake/hash/cache I/O, isolating the admission policy tested
        # by test_budget. Subprocess CLI probes still exercise real admission.
        admission = patch('qatlib_env.budget.admit', return_value={'reasons': []})
        admission.start()
        self.addCleanup(admission.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "repo"
        self.project.mkdir()
        self.env = {**os.environ, "LAKE_CACHE_DIR": str(self.root / "cache"),
                    "LAKE_ARTIFACT_CACHE": "true", "LEAN_NUM_THREADS": "2",
                    "QATLIB_JOBS": "1", "QATLIB_LEAN_THREADS": "1", "QATLIB_MEMORY_BUDGET": str(2*1024**3),
                    "QATLIB_POOL_BUDGET_MIB": "4096", "QATLIB_POOL_CPU": "2",
                    "XDG_STATE_HOME": str(self.root / "state")}
        self.env.pop("LAKE_PKG_URL_MAP", None)
        self.env.pop("ELAN_TOOLCHAIN", None)
        for variable in {*r2_cache.KEY_VARIABLES.values(), "LAKE_CACHE_KEY", "QATLIB_R2_KEY_FILE"}:
            self.env.pop(variable, None)
        toolchain = "leanprover/lean4:v4.35.0"
        # Do not download a toolchain during tests. A missing locked installation
        # is an explicit skip, not a network operation or simulated Lake test.
        elan = Path(self.env.get("ELAN_HOME", str(Path.home() / ".elan")))
        if not (elan / "toolchains" / toolchain.replace("/", "--").replace(":", "---") / "bin/lake").is_file():
            self.skipTest("locked Lean toolchain is not installed locally")
        self.env["ELAN_TOOLCHAIN"] = toolchain
        (self.project / "lean-toolchain").write_text(toolchain + "\n")
        (self.project / "lakefile.toml").write_text('''name="ConsumerLib"
enableArtifactCache=true
platformIndependent=true
testDriver="verify"
[[lean_lib]]
name="ConsumerLib"
[[lean_lib]]
name="ConsumerChecks"
srcDir="verification"
[[lean_exe]]
name="verify"
srcDir="scripts"
''')
        (self.project / "ConsumerLib").mkdir()
        (self.project / "ConsumerLib/Done.lean").write_text("def answer : Nat := 42\n")
        (self.project / "ConsumerLib.lean").write_text("import ConsumerLib.Done\n")
        (self.project / "verification").mkdir()
        (self.project / "verification/ConsumerChecks.lean").write_text("import ConsumerLib.Done\nexample : answer = 42 := rfl\n")
        (self.project / "scripts").mkdir()
        (self.project / "scripts/verify.lean").write_text("def main : IO Unit := pure ()\n")
        (self.project / "qatlib-env.toml").write_text('''[tool.qatlib-env]
library = "ConsumerLib"
tests = "ConsumerChecks"
repository = "fixture/project"
cache_bucket = "fixture-cache"
''')
        self.command(["lake", "resolve-deps"])
        self.command(["git", "init", "-q"])
        self.command(["git", "add", "lean-toolchain", "lakefile.toml", "lake-manifest.json", "qatlib-env.toml", "ConsumerLib", "ConsumerLib.lean", "verification", "scripts"])
        # Fixture commits do not alter repository identity or hooks configuration.
        self.command(["git", "-c", "user.name=Cache fixture", "-c", "user.email=fixture@localhost", "commit", "-qm", "fixture"])

    def command(self, args, check=True):
        result = subprocess.run(args, cwd=self.project, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        if check and result.returncode:
            self.fail(f"fixture command {args} failed ({result.returncode}):\n{result.stdout}")
        return result

    def test_real_lake_upload_validation_and_failed_build_checkpoint(self):
        # Successful native build yields complete verifiable .ltar mappings.
        self.command(["lake", "build", "ConsumerLib", "ConsumerChecks", "-o", ".lake/outputs.jsonl"])
        plan = r2_cache.verified_upload_plan(self.project, self.env)
        self.assertTrue(plan["artifacts"])
        self.assertIn(b".ltar", plan["manifest"])
        descriptor, artifact = next(iter(plan["artifacts"].items()))
        corrupt = self.root / "corrupt.ltar"
        corrupt.write_bytes(b"corrupt archive; a stale .hash sidecar must not certify this")
        Path(str(corrupt) + ".hash").write_text(descriptor.removesuffix(".ltar"))
        with self.assertRaisesRegex(ValueError, "content hash"):
            r2_cache._verify_artifact_hashes(self.project, {descriptor: corrupt}, self.env)
        # A failing module leaves completed modules eligible for local recovery.
        (self.project / "ConsumerLib/Fail.lean").write_text("def fail : Nat := \"wrong\"\n")
        result = self.command(["lake", "--fail-fast", "build", "+ConsumerLib.Fail"], check=False)
        self.assertNotEqual(result.returncode, 0)
        receipt = checkpoint.capture(self.project, self.env)
        self.assertGreaterEqual(receipt["modules"], 2)
        fresh = self.root / "fresh"
        self.command(["git", "clone", "--quiet", "--local", str(self.project), str(fresh)])
        (fresh / "ConsumerLib/Fail.lean").write_text("def fail : Nat := \"wrong\"\n")
        self.assertEqual(checkpoint.restore(fresh, self.env)["modules"], receipt["modules"])
        self.assertTrue((fresh / ".lake/build/lib/lean/ConsumerLib/Done.olean").is_file())
        resumed = subprocess.run(["lake", "--no-build", "build", "+ConsumerLib.Done"],
                                 cwd=fresh, env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, check=False)
        self.assertEqual(resumed.returncode, 0, resumed.stdout)
        with self.assertRaisesRegex(ValueError, "committed, clean"):
            r2_cache.verified_upload_plan(self.project, self.env)

    def test_clean_new_revision_with_stale_manifest_must_pass_full_freshness(self):
        self.command(["lake", "build", "ConsumerLib", "ConsumerChecks", "-o", ".lake/outputs.jsonl"])
        (self.project / "ConsumerLib/Done.lean").write_text("def answer : Nat := 43\n")
        self.command(["git", "add", "ConsumerLib/Done.lean"])
        self.command(["git", "-c", "user.name=Cache fixture", "-c", "user.email=fixture@localhost",
                      "commit", "-qm", "New unbuilt source revision"])
        with self.assertRaises(subprocess.CalledProcessError):
            r2_cache.verified_upload_plan(self.project, self.env)
        self.assertTrue((self.project / ".lake/outputs.jsonl").is_file())

    def test_real_prepare_is_idempotent_and_no_build_preserves_failure(self):
        from qatlib_env import prepare
        first = prepare.prepare(self.project, self.env)
        second = prepare.prepare(self.project, self.env)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["identity"], second["identity"])
        # A separate fresh checkout has neither prepared state nor built imports.
        fresh = self.root / "unprepared"
        self.command(["git", "clone", "--quiet", "--local", str(self.project), str(fresh)])
        module_root = str(Path(__file__).resolve().parents[2] / "src")
        process_env = {**self.env, "PYTHONPATH": module_root, "QATLIB_JOBS": "1"}
        result = subprocess.run([sys.executable, "-B", "-m", "qatlib_env", "--project", str(fresh),
                                 "run", "--", "lake", "--no-build", "lean", "ConsumerLib.lean"],
                                cwd=fresh, env=process_env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((fresh / ".lake/qatlas-prepare.json").exists(), result.stdout)
        self.assertFalse((fresh / ".lake/build/lib/lean/ConsumerLib/Done.olean").exists(), result.stdout)
        # --no-prepare must keep the same skip guarantee for build commands.
        command = [sys.executable, "-B", "-m", "qatlib_env", "--project", str(fresh),
                   "run", "--no-prepare", "--", "lake", "build", "+ConsumerLib.Done"]
        result = subprocess.run(command, cwd=fresh, env=process_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((fresh / ".lake/qatlas-prepare.json").exists())
        self.assertTrue((fresh / ".lake/build/lib/lean/ConsumerLib/Done.olean").is_file())

    def test_remote_cache_miss_authentication_and_corruption_are_distinct(self):
        import http.server
        import threading
        from qatlib_env import prepare
        state = {"status": 404, "body": b"missing"}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(state["status"])
                self.end_headers()
                self.wfile.write(state["body"])
            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}/revisions"
        (self.project / ".lake").mkdir(exist_ok=True)
        (self.project / "lake-cache.toml").write_text(f'''[cache]
defaultService="fixture"
[[cache.service]]
name="fixture"
revisionEndpoint="{endpoint}"
''')
        self.assertEqual(prepare.restore_repository(self.project, self.project, self.env), "missing")
        state["status"] = 403
        with self.assertRaisesRegex(prepare.CacheError, "HTTP 403"):
            prepare.restore_repository(self.project, self.project, self.env)
        state["status"], state["body"] = 200, b"not a Lake manifest"
        with self.assertRaisesRegex(prepare.CacheError, "corrupt cache manifest"):
            prepare.restore_repository(self.project, self.project, self.env)

    def test_physlib_uses_native_verbatim_scope_and_drops_other_bucket_credentials(self):
        import http.server
        import threading
        from qatlib_env import prepare
        requests = []
        state = {"body": None}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                self.send_response(200 if state["body"] is not None else 404)
                self.end_headers()
                if state["body"] is not None:
                    self.wfile.write(state["body"])
            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}"
        (self.project / "lakefile.toml").write_text('name="Physlib"\n')
        (self.project / "lake-cache.toml").write_text(f'''[cache]
defaultService="fixture"
[[cache.service]]
name="fixture"
kind="s3"
artifactEndpoint="{endpoint}/artifacts"
revisionEndpoint="{endpoint}/revisions"
''')
        environment = {**self.env, "LAKE_CACHE_KEY": "other_access_12345:other_secret_6789012345"}
        self.assertEqual(prepare.restore_repository(self.project, self.project, environment), "missing")
        self.assertTrue(requests, "the scoped manifest must reach the configured endpoint")
        self.assertTrue(all("/physlib-master/leanprover-lean4-v4.35.0/physlib/" in path for path, _ in requests))
        self.assertTrue(all(authorization is None for _, authorization in requests))
        # A hit uses the selected dependency read key, including for native
        # mapping registration, and never inherits the root job's generic key.
        state["body"] = b'"2026-03-17"\n["3333333333333333",{"flag":true,"nothing":null}]\n'
        config = self.project / "lake-cache.toml"
        config.write_text(config.read_text() + 's3Auth="physlib_access_12345:physlib_secret_6789012345"\n')
        offset = len(requests)
        self.assertTrue(prepare.restore_repository(self.project, self.project, environment).endswith(".jsonl"))
        self.assertTrue(all("Credential=physlib_access_12345/" in (authorization or "") for _, authorization in requests[offset:]))

    def test_native_duplicate_descriptors_preserve_readonly_artifacts_and_register_complete_map(self):
        import http.server
        import threading
        from qatlib_env import prepare
        self.command(["lake", "build", "ConsumerLib", "ConsumerChecks", "-o", ".lake/outputs.jsonl"])
        plan = r2_cache.verified_upload_plan(self.project, self.env)
        self.assertGreaterEqual(len(plan["artifacts"]), 3)
        (existing_name, existing), (remote_name, remote), (second_name, second) = list(plan["artifacts"].items())[:3]
        existing.chmod(0o444)
        untouched = (existing.stat().st_ino, existing.stat().st_mtime_ns, existing.read_bytes())
        remote_hash = remote_name.split(".", 1)[0]
        second_hash = second_name.split(".", 1)[0]
        output = {"ltar": existing_name, "duplicates": [existing_name, existing_name],
                  "other": [remote_hash + ".alias", remote_hash, int(remote_hash, 16), second_hash + ".secondary"],
                  "boolean": True, "nothing": None}
        mapping = {"raw": ('"2026-03-17"\n' + json.dumps(["1111111111111111", output]) + "\n").encode()}
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith("/revisions/"):
                    body, code = mapping["raw"], 200
                elif self.path.endswith("/" + remote_hash + ".art"):
                    body, code = remote.read_bytes(), 200
                elif self.path.endswith("/" + second_hash + ".art"):
                    body, code = second.read_bytes(), 200
                else:
                    body, code = b"absent artifact", 404
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        endpoint = f"http://127.0.0.1:{server.server_port}"
        (self.project / "lake-cache.toml").write_text(f'''[cache]
defaultService="fixture"
[[cache.service]]
name="fixture"
kind="s3"
artifactEndpoint="{endpoint}/artifacts"
revisionEndpoint="{endpoint}/revisions"
''')
        restored = prepare.restore_repository(self.project, self.project, self.env)
        self.assertTrue(restored.endswith(".jsonl"))
        self.assertEqual(sum(path.startswith("/artifacts/") for path in requests), 2)
        cache = Path(self.env["LAKE_CACHE_DIR"])
        aliases = [cache / "artifacts" / (remote_hash + extension) for extension in (".alias", "", ".art")]
        self.assertEqual(len({path.stat().st_ino for path in aliases}), 3)
        self.assertTrue(all(path.read_bytes() == remote.read_bytes() for path in aliases))
        self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o444 for path in aliases))
        records = list((cache / "outputs").rglob("1111111111111111.json"))
        self.assertTrue(records, "native cache add must register the unchanged full mapping")
        record = json.loads(records[0].read_text())
        self.assertEqual(record["data"], output)
        self.assertEqual(record["service"], "fixture")
        self.assertEqual(record["repo"], "fixture/project")
        # Exercise the real native writer with large updates while a concurrent
        # Lake-style reader ignores the Python fetch lock. Each module JSON must
        # remain readable across the private native write and atomic promotion.
        stop = threading.Event()
        observations, reader_errors = [], []
        record_file = records[0]
        def read_mappings():
            while not stop.is_set():
                try:
                    value = json.loads(record_file.read_bytes())
                    if value.get("schemaVersion") != record["schemaVersion"] or "data" not in value:
                        reader_errors.append("incomplete schema")
                    observations.append(len(value["data"]["duplicates"]))
                except Exception as error:
                    reader_errors.append(repr(error))
                stop.wait(0.001)
        reader = threading.Thread(target=read_mappings, daemon=True)
        reader.start()
        try:
            for length in (40000, 40001):
                updated = {**output, "duplicates": [existing_name] * length}
                mapping["raw"] = ('"2026-03-17"\n' + json.dumps(["1111111111111111", updated]) + "\n").encode()
                prepare.restore_repository(self.project, self.project, self.env)
        finally:
            stop.set()
            reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        self.assertTrue(observations, "concurrent reader must actually inspect shared module JSON")
        self.assertIn(40000, observations)
        self.assertFalse(reader_errors, reader_errors[:3])
        self.assertEqual(len(json.loads(record_file.read_bytes())["data"]["duplicates"]), 40001)
        self.assertEqual(sum(path.startswith("/artifacts/") for path in requests), 2)
        self.assertEqual((existing.stat().st_ino, existing.stat().st_mtime_ns, existing.read_bytes()), untouched)
        # Readability errors are distinct from the duplicate-descriptor bug.
        existing.chmod(0o000)
        try:
            with self.assertRaisesRegex(PermissionError, "not readable"):
                prepare.restore_repository(self.project, self.project, self.env)
        finally:
            existing.chmod(0o444)
        aliases[0].chmod(0o644)
        aliases[0].write_bytes(b"corrupt existing artifact")
        aliases[0].chmod(0o444)
        with self.assertRaisesRegex(prepare.CacheError, "corrupt cache artifact content"):
            prepare.restore_repository(self.project, self.project, self.env)
        # HTTP 404 for an artifact is a corrupt manifest, never a cache miss.
        mapping["raw"] = b'"2026-03-17"\n["2222222222222222","1234567890abcdef.ltar"]\n'
        with self.assertRaisesRegex(prepare.CacheError, "artifact returned HTTP 404"):
            prepare.restore_repository(self.project, self.project, self.env)
        self.assertFalse(list((cache / "outputs").rglob("2222222222222222.json")))


if __name__ == "__main__":
    unittest.main()
