"""Host-scoped GitHub and Gitea transport primitives."""

from .repo_api import RepoAPI, RepoRef, RepoError
from .ci_logs import Diagnostic, RangeLogReader, extract_diagnostics

__all__ = ["Diagnostic", "RangeLogReader", "RepoAPI", "RepoError", "RepoRef", "extract_diagnostics"]

