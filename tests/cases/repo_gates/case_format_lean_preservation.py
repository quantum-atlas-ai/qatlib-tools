"""Regression tests for the comment-safe Lean formatter."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

import tests._bootstrap as _bootstrap  # noqa: F401

ROOT = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location(
    "lean_formatter", ROOT / "python/dev_metadata/format_lean_style.py"
)
assert SPEC and SPEC.loader
formatter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(formatter)


class DocumentationPreservationTests(unittest.TestCase):
    def test_header_insertion_keeps_nested_mathematical_documentation(self):
        doc = '''/-! # Pilot scope

This excludes peak workspace.


/- A nested explanation. -/
set_option trace.example true
-/'''
        source = doc + "\n/-- The zero identity. -/\ntheorem zero_identity : 0 = 0 := rfl\n"
        result = formatter.format_lean_text(source)
        self.assertIn(doc, result)
        self.assertIn("/-- The zero identity. -/", result)
        self.assertTrue(result.startswith("/-\nCopyright"))

    def test_raw_strings_and_escaped_identifiers_remain_byte_identical(self):
        source = '''def text := r##"/- -- set_option trace.example true

←x ; this is literal text"##
def «value/-literal» := 1
'''
        self.assertIn(source, formatter.format_lean_text(source))

    def test_original_author_attribution_is_preserved(self):
        header = "/-\nCopyright (c) 2020 Original Author.\nAuthors: Original Author\n-/\n"
        self.assertTrue(formatter.format_lean_text(header + "def x := 1\n").startswith(header))

    def test_tactic_continuation_does_not_merge_into_a_preserved_comment(self):
        source = "theorem t : True := by\n  -- A mathematical explanation.\n  ; trivial\n"
        result = formatter.format_lean_text(source)
        self.assertIn("  -- A mathematical explanation.", result)
        self.assertNotIn("QATLIB_PRESERVED_LINE_", result)

    def test_formatter_never_relabels_existing_build_traces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Qatlib/A/B/C.lean"
            trace = root / ".lake/build/lib/lean/Qatlib/A/B/C.trace"
            source.parent.mkdir(parents=True)
            trace.parent.mkdir(parents=True)
            source.write_text("/-- An explained declaration. -/\ndef value := 1  \n")
            original = b'{"depHash":"original","inputs":[["C.lean","original"]]}'
            trace.write_bytes(original)
            formatter.process_repo(root)
            self.assertEqual(trace.read_bytes(), original)
            self.assertIn("/-- An explained declaration. -/", source.read_text())

    def test_obsolete_strip_request_fails_instead_of_destroying_docs(self):
        with self.assertRaisesRegex(ValueError, "retired"):
            formatter.format_lean_text("/-- Preserve me. -/\ndef x := 1\n", strip_comments=True)


if __name__ == "__main__":
    unittest.main()
