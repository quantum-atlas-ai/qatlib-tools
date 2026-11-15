"""Keep resource/execution failures distinct from proven artifact corruption."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import tests._bootstrap  # noqa: F401
from qatlib_env import budget, prepare, r2_cache


class CacheValidationErrors(unittest.TestCase):
    def test_restore_preserves_admission_and_execution_failures(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            cache = project / "cache"
            artifacts = cache / "artifacts"
            artifacts.mkdir(parents=True)
            (artifacts / "0123456789abcdef.ltar").write_bytes(b"existing")
            manifest = project / "outputs.jsonl"
            manifest.write_text('"2026-03-17"\n["1111111111111111","0123456789abcdef.ltar"]\n')
            env = {"LAKE_CACHE_DIR": str(cache)}
            failures = (
                budget.AdmissionError({"reasons": [{"code": "host_cpu_exceeded"}]}, hard=True),
                subprocess.CalledProcessError(1, ["lake", "--no-build", "lean"]),
                ValueError("heavy local execution requires a delegated systemd user CPU/memory scope"),
            )
            for failure in failures:
                with self.subTest(failure=type(failure).__name__), \
                     patch.object(r2_cache, "_verify_artifact_hashes", side_effect=failure):
                    with self.assertRaises(type(failure)) as raised:
                        prepare._restore_artifacts(project, manifest, {}, "fixture", env, "Dependency")
                    self.assertIs(raised.exception, failure)
            with patch.object(r2_cache, "_verify_artifact_hashes",
                              side_effect=r2_cache.ArtifactHashMismatch("bytes differ")):
                with self.assertRaisesRegex(prepare.CacheError, "corrupt cache artifact content"):
                    prepare._restore_artifacts(project, manifest, {}, "fixture", env, "Dependency")

    def test_only_completed_native_hash_comparisons_report_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / ".lake").mkdir()
            artifact = project / "artifact"
            artifact.write_bytes(b"bytes")
            paths = {"0123456789abcdef.ltar": artifact}
            with patch.object(r2_cache, "_run", return_value="fedcba9876543210\n"):
                with self.assertRaises(r2_cache.ArtifactHashMismatch):
                    r2_cache._verify_artifact_hashes(project, paths, {})
            failure = budget.AdmissionError({"reasons": []}, hard=True)
            with patch.object(r2_cache, "_run", side_effect=failure):
                with self.assertRaises(budget.AdmissionError) as raised:
                    r2_cache._verify_artifact_hashes(project, paths, {})
                self.assertIs(raised.exception, failure)
