"""Fetch exact public Lean dependencies in an isolated, anonymous Git process."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import re
import subprocess
import uuid
from urllib.parse import urlsplit

from qatlib_env import supervisor
from qatlib_env.envvars import env_value

# Match complete manifest URLs, including the spelling used by Lake.
PUBLIC_REPOS = frozenset(
    f"https://github.com/{slug}{suffix}"
    for slug in (
        "quantum-atlas-ai/qatlib",
        "leanprover/lean4-cli",
        "leanprover-community/physlib",
        "leanprover-community/mathlib4",
        "leanprover-community/plausible",
        "leanprover-community/LeanSearchClient",
        "leanprover-community/import-graph",
        "leanprover-community/ProofWidgets4",
        "leanprover-community/aesop",
        "leanprover-community/quote4",
        "leanprover-community/batteries",
    )
    for suffix in ("", ".git")
)
REVISION = re.compile(r"[0-9a-f]{40}")
KEY = "QATLIB_GITHUB_PREFIX"


def prefix(env: dict[str, str]) -> str:
    value = env_value(env, KEY, "")
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        parsed.port
        if (not value.startswith("https://") or not value.endswith("/")
                or not parsed.hostname or not re.fullmatch(r"[A-Za-z0-9.:-]+", parsed.hostname)
                or re.search(r"[\s?#@\\%\x00-\x1f\x7f]", value)):
            raise ValueError
    except ValueError:
        raise ValueError(f"{KEY} / --gh-proxy requires an HTTPS prefix ending in / "
                         "without credentials, query, or fragment") from None
    return value


def add_options(parser, *, dest="gh_proxy"):
    def parse_url(value):
        try:
            if not value:
                raise ValueError("--gh-proxy requires a URL")
            return prefix({KEY: value})
        except ValueError as error:
            raise argparse.ArgumentTypeError(str(error)) from None

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--gh-proxy", metavar="URL", dest=dest, type=parse_url,
                       help="prefix for anonymous locked public Lean Git fetches")
    group.add_argument("--no-gh-proxy", action="store_const", const="", dest=dest,
                       help="disable the Tools GitHub prefix for this invocation")


def anonymous_environment(env: dict[str, str], home: Path) -> dict[str, str]:
    # Start with a process environment allowlist. Git config, credential
    # helpers, .netrc, extra headers, proxy credentials and trace outputs are
    # also excluded on Windows, where Git can read USERPROFILE and APPDATA.
    keys = {"PATH", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL"}
    child = {key: value for key, value in env.items() if key.upper() in keys}
    null = home / "empty.gitconfig"
    return {
        **child, "HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(home),
        "APPDATA": str(home), "LOCALAPPDATA": str(home), "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(null), "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
        "GIT_ALLOW_PROTOCOL": "https", "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "http.followRedirects",
        "GIT_CONFIG_VALUE_0": "false", "GIT_CONFIG_KEY_1": "core.longpaths",
        "GIT_CONFIG_VALUE_1": "true",
    }


def anonymous_git(home: Path, env: dict[str, str], *args: str):
    return supervisor.run(["git", *args], cwd=home, env=anonymous_environment(env, home),
                          capture_output=True, text=True, errors="replace", timeout=600)


def has_revision(repo: Path, rev: str, home: Path, env: dict[str, str]) -> bool:
    result = anonymous_git(home, env, "--git-dir", str(repo), "rev-parse",
                           "--verify", "refs/heads/locked^{commit}")
    return result.returncode == 0 and result.stdout.strip() == rev


def cached_repo(project: Path, url: str, rev: str, mirror: str, env: dict[str, str]) -> Path:
    from qatlib_env.workspace import cache_lock, redirected_directory, remove_derived

    if url not in PUBLIC_REPOS or not isinstance(rev, str) or not REVISION.fullmatch(rev):
        raise ValueError("Lean Git mirror requires an allowlisted URL and full locked commit SHA")
    root = project / ".lake" / "qatlib-git"
    if redirected_directory(root.parent) or redirected_directory(root):
        raise ValueError("Lean Git mirror cache conflicts with a redirected workspace path")
    digest = sha256(url.encode()).hexdigest()[:20]
    target = root / f"{digest}-{rev}.git"
    host = urlsplit(mirror).hostname
    with cache_lock(root):
        if redirected_directory(target):
            raise ValueError("Lean Git mirror cache conflicts with a redirected repository path")
        if target.exists():
            if not has_revision(target, rev, root, env):
                raise ValueError("Lean Git mirror cache conflicts with the locked SHA")
            return target
        job = root / f".fetch-{uuid.uuid4().hex}"
        job.mkdir(mode=0o700)
        repo = job / "repo.git"
        try:
            if anonymous_git(job, env, "init", "--bare", "--template=", str(repo)).returncode:
                raise ValueError("Lean Git mirror could not initialize its isolated repository")
            git_dir = ("--git-dir", str(repo))
            result = anonymous_git(job, env, *git_dir, "fetch", "--quiet", "--no-tags", "--",
                                   mirror + url, f"{rev}:refs/heads/locked")
            if result.returncode:
                status = re.search(r"\b(?:returned error:\s*|HTTP(?:/\S+)?\s+)([45]\d\d)\b",
                                   result.stderr)
                detail = f"HTTP {status[1]}" if status else f"Git exit {result.returncode}"
                raise ValueError(f"Lean Git mirror fetch from {host} failed ({detail}); "
                                 "use --no-gh-proxy to try the original public source")
            if not has_revision(repo, rev, job, env):
                raise ValueError(f"Lean Git mirror fetch from {host} did not return the locked SHA")
            if anonymous_git(job, env, *git_dir, "symbolic-ref", "HEAD", "refs/heads/locked").returncode:
                raise ValueError("Lean Git mirror could not select the locked revision")
            if anonymous_git(job, env, *git_dir, "config", "uploadpack.allowFilter", "true").returncode:
                raise ValueError("Lean Git mirror could not enable local filtered reads")
            repo.rename(target)
        except subprocess.TimeoutExpired:
            raise ValueError(f"Lean Git mirror fetch from {host} timed out; use --no-gh-proxy") from None
        finally:
            remove_derived(job, env)
    return target
