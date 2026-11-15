"""Index local Qatlib and current Dev source/document evidence.

The default public corpus is the exact checkout locked in lake-manifest.json.
An explicit --public-repo selects a separate local checkout and records whether
it matches that lock. Searches never contact release servers or build Lean.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import os
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search import index_identity, proof_index


SHA = re.compile(r"[0-9a-f]{40}\Z")
PUBLIC_REPOSITORIES = set(filter(None, os.environ.get(
    'QATLIB_PUBLIC_REPOSITORIES', 'quantum-atlas-ai/qatlib,quantum-atlas-ai/qatlib-staging').split(',')))


def repository_identity(url: str) -> tuple[str, str]:
    """Return a forge repository name and its browser URL, without credentials."""
    ssh = re.fullmatch(r"git@([^:/]+):([^/]+/[^/]+?)(?:\.git)?", url)
    if ssh:
        host, repository = ssh.groups()
    else:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"https", "ssh"} or parsed.hostname is None
                or parsed.password or parsed.query or parsed.fragment
                or parsed.username not in {None, "git"}):
            raise ValueError("unsupported Git repository URL")
        host, repository = parsed.hostname, parsed.path.strip("/")
        repository = repository.removesuffix(".git")
    allowed_hosts = set(filter(None, os.environ.get('QATLIB_FORGE_HOSTS', 'github.com').split(',')))
    if host not in allowed_hosts or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("unsupported Git repository identity")
    return repository, "https://" + host + "/" + repository


def current_repository_identity(root: Path) -> tuple[str, str]:
    remotes = index_identity.git_output(root, ["remote"]).decode().split()
    for remote in sorted(remotes, key=lambda name: (name != "origin", name)):
        url = index_identity.git_output(root, ["config", f"remote.{remote}.url"]).decode().strip()
        try:
            return repository_identity(url)
        except ValueError:
            continue
    raise ValueError("no supported GitHub or Gitea repository remote found")


def current_repository(root: Path) -> str:
    return current_repository_identity(root)[0]


def qatlib_lock(root: Path) -> tuple[dict, dict]:
    manifest = json.loads((root / "lake-manifest.json").read_text(encoding="utf-8"))
    matches = [p for p in manifest["packages"] if p["name"].strip("«»") == "Qatlib"]
    if len(matches) != 1:
        raise ValueError("lock must identify exactly one Qatlib dependency")
    package = matches[0]
    repository, web = repository_identity(package.get("url", ""))
    if (package.get("type") != "git" or repository not in PUBLIC_REPOSITORIES
            or not web.startswith("https://github.com/")
            or not SHA.fullmatch(package.get("rev", ""))):
        raise ValueError("Qatlib lock repository or revision is invalid")
    return manifest, package


def qatlib_pin(root: Path, public_repo: Path | None = None) -> tuple[Path, str]:
    """Resolve the locked checkout, or an explicitly selected local public repo."""
    root = Path(root).resolve()
    manifest, package = qatlib_lock(root)
    selected = (Path(public_repo).expanduser() if public_repo is not None
                else root / manifest["packagesDir"] / "Qatlib")
    if selected.is_symlink():
        raise ValueError("Qatlib checkout must not be a directory symlink")
    directory = selected.resolve()
    if public_repo is None and not directory.is_relative_to(root):
        raise ValueError("Qatlib source checkout path escapes the consumer repository")
    if not directory.is_dir() or not (directory / "Qatlib").is_dir():
        raise ValueError("Qatlib source checkout is unavailable; prepare local Git sources")
    revision = proof_index.git_head(directory)
    if revision is None or not SHA.fullmatch(revision):
        raise ValueError("Qatlib source must be its own committed Git checkout")
    if public_repo is None and revision != package["rev"]:
        raise ValueError("Qatlib source checkout differs from the dependency lock")
    if (directory / "lean-toolchain").read_bytes() != (root / "lean-toolchain").read_bytes():
        raise ValueError("Qatlib and consumer toolchains differ")
    return directory, revision


def local_index(root: Path, output: Path, *, source_scope: str = "source-and-docs") -> dict:
    if output.is_file():
        try:
            admitted = proof_index.load(output, root)
            if admitted.get("source_scope", "source-and-docs") == source_scope:
                return admitted
        except ValueError:
            pass
    value = proof_index.build(root, source_scope=source_scope)
    proof_index.save(value, output)
    return value


def qatlib_index(root: Path, *, public_repo: Path | None = None,
                 offline: bool = False, api=None) -> tuple[dict, dict, Path]:
    """Build/reuse an authenticated local source catalog; offline/api are legacy options."""
    directory, revision = qatlib_pin(root, public_repo)
    _, package = qatlib_lock(root)
    repository, repository_url = repository_identity(package["url"])
    cache = root / ".qatlas" / "qatlib-proof-index" / (revision + ".json")
    evidence = {
        "corpus": "qatlib", "repository": repository, "repository_url": repository_url,
        "commit": revision, "dependency_revision": package["rev"],
        "matches_dependency_pin": revision == package["rev"],
        "origin": "explicit-local-source" if public_repo is not None else "pinned-local-source",
        "publication_verified": False, "checkout_verified": True,
        "source_scope": "qatlib-lean",
    }
    value = local_index(directory, cache, source_scope="qatlib-lean")
    proof_index._validate_current(value, directory)
    if value.get("git_head") != revision or qatlib_pin(root, public_repo) != (directory, revision):
        raise ValueError("Qatlib source selection changed while admitting catalog")
    return value, evidence, directory


def search(root: Path, query: str, *, mode="literal", limit=10, regex_seconds=2.0,
           all_matches=False, scope="all", offline=False, api=None,
           public_repo: Path | None = None) -> dict:
    root = Path(root).resolve()
    proof_index.validate_query(query, mode, limit, regex_seconds)
    if scope not in {"all", "qatlib", "frozen", "dev-docs"}:
        raise ValueError("unsupported cross-repository scope")
    if scope == "frozen":
        scope = "qatlib"
    corpora, hits, snapshots = [], [], []
    selected = ["qatlib", "dev"] if scope == "all" else ["qatlib" if scope == "qatlib" else "dev"]
    for name in selected:
        try:
            if name == "qatlib":
                value, evidence, directory = qatlib_index(
                    root, public_repo=public_repo, offline=offline, api=api)
                version = evidence["commit"]
            else:
                repository, repository_url = current_repository_identity(root)
                value = local_index(root, root / ".qatlas/proof-source-index-git-2.json")
                directory, version = root, value["git_head"]
                evidence = {
                    "corpus": "dev-docs" if scope == "dev-docs" else "dev",
                    "repository": repository, "repository_url": repository_url,
                    "commit": version, "origin": "current-source-and-supplementary-docs",
                    "publication_verified": False,
                }
                if scope == "dev-docs":
                    value = {**value, "records": [r for r in value["records"] if r["path"].endswith(".md")]}
            answer = proof_index.search(value, query, mode, limit, regex_seconds, all_matches=all_matches)
            proof_index._validate_current(value, directory)
            if name == "qatlib" and qatlib_pin(root, public_repo) != (directory, version):
                raise ValueError("Qatlib source selection changed during search")
            separator = "/blob/" if evidence["repository_url"].startswith("https://github.com/") else "/src/commit/"
            base = evidence["repository_url"] + separator + version + "/"
            for hit in answer["results"]:
                hits.append({
                    **hit, "corpus": evidence["corpus"], "repository": evidence["repository"],
                    "commit": version, "origin": evidence["origin"],
                    "evidence_kind": "supplementary-document" if hit["path"].endswith(".md") else "source-location",
                    "source_url": base + hit["path"] + "#L" + str(hit["line"]),
                    "source_path": str(directory / hit["path"]),
                })
            corpora.append({
                **evidence, "status": "ok", "results": len(answer["results"]),
                "truncated": answer["truncated"], "warnings": answer["warnings"],
            })
            snapshots.append((len(corpora) - 1, value, directory, name, version))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            corpora.append({"corpus": name, "status": "error", "error": str(error)})
    for position, value, directory, name, version in snapshots:
        try:
            proof_index._validate_current(value, directory)
            if name == "qatlib" and qatlib_pin(root, public_repo) != (directory, version):
                raise ValueError("Qatlib source selection changed before the combined result was returned")
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            corpus = corpora[position]["corpus"]
            corpora[position] = {"corpus": corpus, "status": "error", "error": str(error)}
            hits = [hit for hit in hits if hit["corpus"] != corpus]
    return {
        "scope": scope, "query": query, "mode": mode, "limit_per_corpus": limit,
        "corpora": corpora, "results": hits,
        "incomplete": any(c["status"] == "error" for c in corpora),
        "truncated": any(c.get("truncated", False) for c in corpora),
        "validation": "not-checked",
        "limitations": [
            "Lexical source/document locations; verify full Lean types, hypotheses and actual consumers.",
            "Local committed source identity is checked; publication and Lean applicability are not.",
            "A corpus error or missing checkout is not an empty search result.",
        ],
    }
