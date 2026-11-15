from __future__ import annotations

import io
import unittest

from qatlib_forge.ci_logs import Diagnostic, RangeLogReader, extract_diagnostics
from qatlib_forge.plugins import discover
from qatlib_forge.repo_api import RepoRef


class FakeHeaders(dict):
    def items(self): return super().items()


class FakeResponse:
    status = 206
    headers = FakeHeaders({"Content-Range": "bytes 0-50/51"})
    def __init__(self, data): self.data = data
    def read(self, n=-1): return self.data
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def getcode(self): return self.status


class ForgeTest(unittest.TestCase):
    def test_repo_parse_and_plugin_group(self):
        ref = RepoRef.parse("https://github.com/acme/tools")
        self.assertEqual(ref.slug, "acme/tools")
        # The shared package has no built-in downstream extension. Discovery is
        # still a valid empty mapping until a consumer registers one.
        self.assertIsInstance(discover(), dict)

    def test_diagnostics(self):
        result = extract_diagnostics("Main.lean:4:9: error: bad\nwarning: Other.lean:2:1: note")
        self.assertEqual([(x.path, x.line, x.column) for x in result], [("Main.lean", 4, 9), ("Other.lean", 2, 1)])

    def test_range_reader(self):
        reader = RangeLogReader("https://logs.test/log", opener=lambda request, timeout: FakeResponse(b"Main.lean:1:2: error: no"))
        data, headers = reader.read(0, 50)
        self.assertIn(b"Main.lean", data); self.assertEqual(headers[":status"], "206")

