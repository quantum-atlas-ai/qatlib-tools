"""Regression tests for the shared qatlib_env R2 uploader."""
from __future__ import annotations

import tests._bootstrap as _bootstrap  # noqa: F401

import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from qatlib_env import r2_cache as MOD
from tests._credential_fixture import environment as credential_env


class PushR2CacheTests(unittest.TestCase):
    def _tmp_dir(self) -> Path:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        return Path(td.name)

    def test_default_key_file_is_user_qatlib_secrets(self) -> None:
        kf = MOD.default_key_file({})
        self.assertEqual(kf.parts[-2:], (".qatlib", "secrets"))

    def test_save_key_file_creates_0600_and_roundtrips(self) -> None:
        d = self._tmp_dir()
        kf = d / ".qatlib" / "secrets"
        MOD.save_key_file(kf, "saved_ak_12345:saved_sk_6789012345", "LAKE_DEV_CACHE_KEY")
        self.assertTrue(kf.is_file())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(kf.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(kf.parent.stat().st_mode), 0o700)
        self.assertEqual(
            MOD.resolve_write_key("example_library", env=credential_env(), key_file=kf, allow_prompt=False),
            "saved_ak_12345:saved_sk_6789012345",
        )

    def test_key_file_resolution_and_bucket_specific_override(self) -> None:
        d = self._tmp_dir()
        kf = d / ".qatlib" / "secrets"
        kf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kf.write_text(
            "# Maintainer R2 write credentials\n"
            "LAKE_CACHE_KEY=shared_ak_12345:shared_sk_6789012345\n"
            "LAKE_DEV_CACHE_KEY='dev_ak_12345:dev_sk_6789012345'\n",
            encoding="utf-8",
        )
        os.chmod(kf, 0o600)

        self.assertEqual(
            MOD.resolve_write_key("example_library", env=credential_env(), key_file=kf, allow_prompt=False),
            "dev_ak_12345:dev_sk_6789012345",
        )
        self.assertEqual(
            MOD.resolve_write_key("Qatlib", env=credential_env(), key_file=kf, allow_prompt=False),
            "shared_ak_12345:shared_sk_6789012345",
        )
        # Explicit env var wins over file
        self.assertEqual(
            MOD.resolve_write_key(
                "example_library",
                env=credential_env({"LAKE_DEV_CACHE_KEY": "env_ak:env_sk_1234567890"}),
                key_file=kf,
                allow_prompt=False,
            ),
            "env_ak:env_sk_1234567890",
        )

    @unittest.skipIf(os.name == "nt", "POSIX file mode check only")
    def test_world_readable_key_file_is_rejected(self) -> None:
        d = self._tmp_dir()
        kf = d / ".qatlib" / "secrets"
        kf.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kf.write_text("LAKE_CACHE_KEY=ak:sk_1234567890123456\n", encoding="utf-8")
        os.chmod(kf, 0o644)
        with self.assertRaises(PermissionError):
            MOD.resolve_write_key("example_library", env={}, key_file=kf, allow_prompt=False)

    def test_qatlib_profile_uses_ci_flag(self) -> None:
        d = self._tmp_dir()
        (d / "lakefile.toml").write_text('name = "Qatlib"\n', encoding="utf-8")
        (d / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n", encoding="utf-8")
        (d / "qatlib-env.toml").write_text('[tool.qatlib-env]\ncache_bucket="public-cache"\nrepository="fixture/public"\n', encoding="utf-8")
        prof = MOD.detect_repo_profile(d)
        self.assertEqual(prof["build_args"], ["-KCI", "Qatlib"])
        self.assertEqual(prof["bucket"], "public-cache")

    def test_secondary_question_profile_uses_canonical_repo_and_toolchain_path(self) -> None:
        for pkg, expected_lib, expected_tests in [
            ("secondary_package", "SecondaryLibrary", "SecondaryLibraryTests"),
            ("SecondaryProject", "SecondaryProject", "SecondaryProjectTests"),
        ]:
            d = self._tmp_dir()
            (d / "lakefile.toml").write_text(f'name = "{pkg}"\n', encoding="utf-8")
            (d / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n", encoding="utf-8")
            (d / "qatlib-env.toml").write_text(f'[tool.qatlib-env]\ncache_bucket="secondary-cache"\nrepository="fixture/secondary"\nlibrary="{expected_lib}"\ntests="{expected_tests}"\n', encoding="utf-8")
            prof = MOD.detect_repo_profile(d)
            self.assertEqual(prof["bucket"], "secondary-cache")
            self.assertEqual(prof["repo_slug"], "fixture/secondary")
            self.assertEqual(prof["tc_dir"], "leanprover--lean4---v4.35.0")
            self.assertEqual(prof["build_args"], [expected_lib, expected_tests])

    def test_example_library_pascalcase_profile(self) -> None:
        d = self._tmp_dir()
        (d / "lakefile.toml").write_text('name = "ExampleLibrary"\n', encoding="utf-8")
        (d / "lean-toolchain").write_text("leanprover/lean4:v4.35.0\n", encoding="utf-8")
        (d / "qatlib-env.toml").write_text('[tool.qatlib-env]\ncache_bucket="dev-cache"\nrepository="fixture/dev"\nlibrary="ExampleLibrary"\ntests="QatlasDevTests"\n', encoding="utf-8")
        prof = MOD.detect_repo_profile(d)
        self.assertEqual(prof["package"], "ExampleLibrary")
        self.assertEqual(prof["bucket"], "dev-cache")
        self.assertEqual(prof["build_args"], ["ExampleLibrary", "QatlasDevTests"])

    def test_baseline_lookup_skips_header_only_manifests(self) -> None:
        repo = self._tmp_dir()
        profile = {"bucket": "bucket", "repo_slug": "owner/repo", "tc_dir": "tc"}
        with patch.object(MOD, "_run", return_value="new\nold\n"), \
             patch.object(MOD, "s3_get_optional", side_effect=[b'"2026-03-17"\n',
                                                                  b'"2026-03-17"\n["hash", "0123456789abcdef.ltar"]\n']):
            self.assertEqual(MOD.find_baseline_ltars(repo, profile, "ak", "sk", {"QATLAS_R2_ENDPOINT": "https://cache.example"}), {"0123456789abcdef.ltar"})


if __name__ == "__main__":
    unittest.main()
