from __future__ import annotations

import os
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]
SAFETY_PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE [K]EY-----"),
    "access token": re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{30,}\b"),
    "absolute personal path": re.compile(r"(?<![\w:/])(?:/(?:home|Users)/|[A-Z]:[\\/]Users[\\/])[\w.-]+[\\/]", re.I),
    "credentials in HTTP URL": re.compile(r"https?://[^\s:/@<>]+:[^\s/@<>]+@[^\s/<>]+", re.I),
}
SKIP_DIRS = {".git", ".venv", "dist", "build", ".pytest_cache", "__pycache__"}
SITE_OUTPUTS = {ROOT / "site" / name for name in ("node_modules", "src", ".vitepress/cache")}


def denylist_pattern(literal: str) -> re.Pattern:
    escaped = re.escape(literal)
    if len(literal) <= 2 and literal.isalnum():
        # Short names are tokens. Longer words and encoded hashes can contain them by chance.
        escaped = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    return re.compile(escaped, re.I)


class PublicHygieneTest(unittest.TestCase):
    def test_repository_has_no_private_literals(self):
        """Set QATLIB_HYGIENE_DENYLIST to additional literals, one per line."""
        patterns = list(SAFETY_PATTERNS.items())
        extra = os.environ.get("QATLIB_HYGIENE_DENYLIST", "")
        patterns.extend(("denylist literal", denylist_pattern(line.strip()))
                        for line in extra.splitlines() if line.strip())
        offenders = []
        for directory, subdirectories, filenames in os.walk(ROOT):
            subdirectories[:] = [name for name in subdirectories
                                 if name not in SKIP_DIRS and not name.startswith(".wheel-venv")
                                 and Path(directory, name) not in SITE_OUTPUTS]
            for name in filenames:
                path = Path(directory) / name
                if name == ".git" or path.is_symlink() or not path.is_file():
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                offenders.extend(f"{path.relative_to(ROOT)} matches {rule}"
                                 for rule, pattern in patterns if pattern.search(content))
        self.assertEqual([], offenders)

