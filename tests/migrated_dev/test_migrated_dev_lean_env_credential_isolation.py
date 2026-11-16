"""Bucket-specific credentials, real private files and native child boundaries."""
from __future__ import annotations

import tests._bootstrap as _bootstrap  # noqa: F401
from tests._git_fixture import snapshot
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from qatlib_env import ci, prepare, r2_cache, workspace
from tests._credential_fixture import environment as credential_env

DEV = "dev_fixture_access_12345:dev_fixture_secret_67890"
SECONDARY = "secondary_fixture_access_12345:secondary_fixture_secret_67890"
PUBLIC = "public_fixture_access_12345:public_fixture_secret_67890"
GENERIC = "generic_fixture_access_12345:generic_fixture_secret_67890"
PRIVATE_PACKAGES = (("ExampleLibrary", "LAKE_DEV_CACHE_KEY", DEV),
                    ("example_library", "LAKE_DEV_CACHE_KEY", DEV),
                    ("SecondaryProject", "LAKE_SECONDARY_CACHE_KEY", SECONDARY),
                    ("secondary_package", "LAKE_SECONDARY_CACHE_KEY", SECONDARY))


class CredentialIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="qatlas-credential-isolation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)

    def private_file(self, text: str, name="credentials") -> Path:
        directory = self.root / name
        directory.mkdir(mode=0o700)
        path = directory / "secrets"
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_private_aliases_ignore_generic_and_other_bucket_environment(self):
        for package, variable, expected in PRIVATE_PACKAGES:
            for generic in (GENERIC, "malformed generic value"):
                with self.subTest(package=package, generic_valid=generic == GENERIC):
                    env = credential_env({"HOME": str(self.home), "LAKE_CACHE_KEY": generic,
                                          "LAKE_PUBLIC_CACHE_KEY": PUBLIC})
                    other_variable, other_key = ("LAKE_SECONDARY_CACHE_KEY", SECONDARY) if variable == "LAKE_DEV_CACHE_KEY" else ("LAKE_DEV_CACHE_KEY", DEV)
                    env[other_variable] = other_key
                    self.assertEqual(r2_cache.resolve_write_key(package, env=env), "")
                    self.assertEqual(ci.write_key(package, env), "")
                    env[variable] = expected
                    self.assertEqual(r2_cache.resolve_write_key(package, env=env), expected)
                    self.assertEqual(ci.write_key(package, env), expected)
                    self.assertEqual(env["LAKE_CACHE_KEY"], generic)

    def test_private_file_selection_does_not_consume_generic_entries(self):
        generic_only = self.private_file(f"LAKE_CACHE_KEY={GENERIC}\n", "generic-only")
        dedicated = self.private_file(f"LAKE_DEV_CACHE_KEY={DEV}\nLAKE_SECONDARY_CACHE_KEY={SECONDARY}\n"
                                      "LAKE_CACHE_KEY=malformed generic value\n", "dedicated")
        self.assertEqual(r2_cache.resolve_write_key("Qatlib", env=credential_env(), key_file=generic_only), GENERIC)
        for package, variable, expected in PRIVATE_PACKAGES:
            env = credential_env({"HOME": str(self.home), "LAKE_CACHE_KEY": GENERIC,
                                  "QATLAS_R2_KEY_FILE": str(generic_only)})
            self.assertEqual(r2_cache.resolve_write_key(package, env=env), "")
            self.assertEqual(r2_cache.resolve_write_key(package, env=env, key_file=dedicated), expected)
            alternate = "override_fixture_access:override_fixture_secret"
            self.assertEqual(r2_cache.resolve_write_key(package, env={**env, variable: alternate},
                                                        key_file=dedicated), alternate)

    def test_public_community_environment_file_and_raw_line_compatibility(self):
        path = self.private_file(f"LAKE_PUBLIC_CACHE_KEY={PUBLIC}\nLAKE_CACHE_KEY={GENERIC}\n")
        self.assertEqual(r2_cache.resolve_write_key("Qatlib", env=credential_env(), key_file=path), PUBLIC)
        self.assertEqual(r2_cache.resolve_write_key("Qatlib", env=credential_env({"LAKE_CACHE_KEY": GENERIC}), key_file=path), GENERIC)
        env = credential_env({"LAKE_PUBLIC_CACHE_KEY": PUBLIC, "LAKE_CACHE_KEY": "malformed generic value"})
        self.assertEqual(r2_cache.resolve_write_key("Qatlib", env=env), PUBLIC)
        self.assertEqual(ci.write_key("Qatlib", env), PUBLIC)
        raw = self.private_file(GENERIC + "\n", "raw-public")
        self.assertEqual(r2_cache.resolve_write_key("Qatlib", env=credential_env(), key_file=raw), GENERIC)
        for package, _variable, _expected in PRIVATE_PACKAGES:
            self.assertEqual(r2_cache.resolve_write_key(package, env={}, key_file=raw), "")

    def test_save_key_uses_explicit_bucket_names_in_real_files(self):
        for package, variable, expected in (*PRIVATE_PACKAGES, ("Qatlib", "LAKE_PUBLIC_CACHE_KEY", PUBLIC)):
            project = self.root / package
            project.mkdir()
            (project / "lakefile.toml").write_text(f"name={json.dumps(package)}\n")
            (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
            path = project / "private/secrets"
            with patch.object(r2_cache.getpass, "getpass", return_value=expected), \
                    patch.dict(os.environ, credential_env()):
                self.assertEqual(r2_cache.main(["--repo", str(project), "--save-key", "--key-file", str(path)]), 0)
            self.assertEqual(r2_cache.parse_key_file(path), {variable: expected})
            self.assertNotIn("LAKE_CACHE_KEY=", path.read_text())
        default = self.root / "default-public/secrets"
        r2_cache.save_key_file(default, PUBLIC, "LAKE_PUBLIC_CACHE_KEY")
        self.assertEqual(r2_cache.parse_key_file(default), {"LAKE_PUBLIC_CACHE_KEY": PUBLIC})
        with self.assertRaisesRegex(ValueError, "unsupported R2 credential variable"):
            r2_cache.save_key_file(default, GENERIC, "OTHER_SECRET")

    def test_ci_publication_requires_matching_private_identity(self):
        for provider in ("GITHUB_", "GITEA_"):
            base = credential_env({provider + "REF": "refs/heads/main", provider + "EVENT_NAME": "push",
                                   "LAKE_CACHE_KEY": GENERIC, "LAKE_PUBLIC_CACHE_KEY": PUBLIC})
            self.assertFalse(ci.may_publish(base))
            for package, variable, expected in PRIVATE_PACKAGES:
                self.assertFalse(ci.may_publish(base, package))
                selected = {**base, variable: expected}
                self.assertTrue(ci.may_publish(selected, package))
                self.assertFalse(ci.may_publish({**selected, provider + "EVENT_NAME": "pull_request"}, package))
            self.assertTrue(ci.may_publish(base, "Qatlib"))
            self.assertTrue(ci.may_publish({**base, "LAKE_PUBLIC_CACHE_KEY": ""}, "Qatlib"))

    def test_workspace_clears_external_generic_before_real_child_execution(self):
        for package in ("ExampleLibrary", "SecondaryProject", "Qatlib"):
            project = self.root / package
            project.mkdir()
            (project / "lakefile.toml").write_text(f"name={json.dumps(package)}\n")
            (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
            (project / "lake-manifest.json").write_text('{"packages":[]}\n')
            incoming = credential_env({"HOME": str(self.home), "LAKE_CACHE_DIR": str(project / "cache"),
                                       "LAKE_CACHE_KEY": GENERIC})
            env = workspace.environment(project, base_env=incoming)
            self.assertNotIn("LAKE_CACHE_KEY", env)
            if package == "Qatlib":
                self.assertEqual(env["LAKE_PUBLIC_CACHE_KEY"], GENERIC)
            else:
                self.assertNotIn("LAKE_PUBLIC_CACHE_KEY", env)
            child = subprocess.run([sys.executable, "-B", "-c", "import os; assert 'LAKE_CACHE_KEY' not in os.environ"],
                                   env=env, capture_output=True, text=True, check=False)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(incoming["LAKE_CACHE_KEY"], GENERIC)

    def test_native_adapter_scopes_selected_key_to_real_child(self):
        incoming = {"TMPDIR": str(self.root), "LAKE_CACHE_KEY": GENERIC}
        for selected in (DEV, ""):
            with ci.authenticated(incoming, selected) as child_env:
                expected = selected or None
                child = subprocess.run([sys.executable, "-B", "-c",
                                        "import os; assert os.environ.get('LAKE_CACHE_KEY') == " + repr(expected)],
                                       env=child_env, capture_output=True, text=True, check=False)
                self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(incoming["LAKE_CACHE_KEY"], GENERIC)

    def test_prepare_root_and_dependency_requests_use_selected_bucket_only(self):
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.headers.get("Authorization"))
                self.send_response(404)
                self.end_headers()
            def log_message(self, *_args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        project = self.root / "request-fixture"
        project.mkdir()
        (project / "lakefile.toml").write_text('name="ExampleLibrary"\n')
        (project / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n")
        (project / "lake-manifest.json").write_text('{"packages":[]}\n')
        (project / "lake-cache.toml").write_text(f'''[cache]
defaultService="fixture"
[[cache.service]]
name="fixture"
kind="s3"
revisionEndpoint="http://127.0.0.1:{server.server_port}/revisions"
s3Auth="{PUBLIC}"
''')
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "remote", "add", "origin", "https://github.com/fixture/library.git"], check=True)
        snapshot(project)
        env = credential_env({"ELAN_TOOLCHAIN": "leanprover/lean4:v4.35.0", "LAKE_CACHE_DIR": str(self.root / "cache"),
                              "LAKE_CACHE_KEY": "malformed generic value"})
        self.assertEqual(prepare.restore_repository(project, project, env), "missing")
        self.assertTrue(requests)
        self.assertTrue(all("Credential=public_fixture_access_12345/" in (value or "") for value in requests))
        requests.clear()
        self.assertEqual(prepare.restore_repository(project, project, {**env, "LAKE_DEV_CACHE_KEY": DEV}), "missing")
        self.assertTrue(all("Credential=dev_fixture_access_12345/" in (value or "") for value in requests))
        # A public dependency must use its own read config; the private root's
        # generic/native value is never a public fallback on this boundary.
        requests.clear()
        (project / "lakefile.toml").write_text('name="Qatlib"\n')
        consumer = self.root / "consumer"
        consumer.mkdir()
        self.assertEqual(prepare.restore_repository(consumer, project, env), "missing")
        self.assertTrue(all("Credential=public_fixture_access_12345/" in (value or "") for value in requests))


if __name__ == "__main__":
    unittest.main()
