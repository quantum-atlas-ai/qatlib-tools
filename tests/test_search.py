from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from qatlib_search import SearchIndex, parse_imports


class SearchTest(unittest.TestCase):
    def test_unicode_and_quoted_imports(self):
        self.assertEqual(parse_imports('import «Mathlib.Algebra»\nimport "Foo.Bar"'), ["Mathlib.Algebra", "Foo.Bar"])

    def test_index_query_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / "Main.lean"
            source.write_text("theorem hello : True := by trivial\ndef helper := 1\n", encoding="utf-8")
            index = SearchIndex([root]); index.build(); self.assertEqual(index.query("HELLO")[0].kind, "theorem")
            output = root / "index.json"; index.save(output); self.assertEqual(len(SearchIndex.load(output).records), 2)

