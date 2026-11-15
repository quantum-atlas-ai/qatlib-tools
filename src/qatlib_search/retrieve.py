#!/usr/bin/env python3
"""Unified Lean/source discovery using the repository's own tools."""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv=None) -> int:
    from qatlib_search.unified_retrieval import main as retrieve
    from qatlib_search.paths import ROOT
    return retrieve(["--project", str(ROOT),
                     *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
