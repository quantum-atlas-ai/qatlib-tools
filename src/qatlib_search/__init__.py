"""Shared Lean lexical and promotion APIs, plus read-only source retrieval."""

from .lean_source import import_header, imports, mask_comments

__all__ = ["import_header", "imports", "mask_comments"]
