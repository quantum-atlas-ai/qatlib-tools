"""One offline-first discovery entry for a Lake project and its dependencies.

Reuses proof_index search and lean_doc_source's conservative declaration lexer.
Only refresh contacts GitHub/Gitea; search updates working sources, not dependency pins.
This is source discovery, never Lean elaboration or mathematical applicability.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.parse import quote, urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search.paths import project_root
from qatlib_search.lean_source import mask_comments
from qatlib_forge.repo_api import RepoRef, RepoAPI
from qatlib_search import proof_index
from qatlib_search.lean_doc_source import END_RE, NAMESPACE_RE, SECTION_RE, parse_module_text
from qatlib_search.terminology import cjk_bridge

SCHEMA = "qatlas-unified-source-1"
CATALOG_SCHEMA = "qatlas-retrieval-catalog-1"
LIMITATIONS = [
    "SOURCE_MATCH only: inspect full Lean types, hypotheses, instances, license and actual consumers.",
    "FQNs are explicit lexical declarations, not generated/private names or elaborated types; null means source location only.",
    "Upstream observations are cached, not live; refresh explicitly. No pins, Lean build or PR validation are changed.",
    "Missing sources and unsupported syntax are coverage gaps, not evidence of absence.",
]
SKIP = {".git", ".lake", ".qatlas", ".venv", ".agent-tasks", "__pycache__", "node_modules"}
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, encoding="utf-8",
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("Git source inventory failed: " + result.stderr.strip())
    return result.stdout


def _relative(path: str) -> str:
    if (not isinstance(path, str) or not path or "\\" in path or "\x00" in path
            or PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts):
        raise ValueError("invalid relative source path")
    return PurePosixPath(path).as_posix()


def _material_roots(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Normalize caller-owned project directories; no repository policy defaults."""
    if not isinstance(values, (tuple, list)):
        raise ValueError("material_roots must be a list or tuple of project-relative directories")
    roots = set()
    for value in values:
        root = _relative(value)
        if root == "." or any(part in SKIP or part.startswith(".") for part in PurePosixPath(root).parts):
            raise ValueError("material roots must be non-hidden subdirectories inside the project")
        roots.add(root)
    return tuple(sorted(roots))


def _is_material(path: str, material_roots: tuple[str, ...]) -> bool:
    return any(PurePosixPath(path).is_relative_to(root) for root in material_roots)


def _admitted(path: str, material_roots: tuple[str, ...] = (), library_roots: tuple[str, ...] = (),
              *, all_markdown: bool = False) -> bool:
    parts = PurePosixPath(path).parts
    return (bool(parts) and not any(p in SKIP or p.startswith(".") for p in parts)
            and (path.endswith(".lean") and parts[-1] != "lakefile.lean"
                 or path.endswith(".md") and (all_markdown or len(parts) == 1 or parts[0] in {
                 "docs"}
                     or _is_material(path, material_roots + library_roots))))


def _identity(root: Path) -> str | None:
    # git_head deliberately refuses to borrow an enclosing checkout's identity.
    return proof_index.git_head(root)


def _repo_fields(ref: RepoRef) -> dict:
    return {"repository": ref.slug, "platform": ref.platform, "web_host": ref.web_host,
            "api_base": ref.api_base, "server": "https://" + ref.web_host}


def _source_ref(source: dict) -> RepoRef:
    host = source.get("web_host") or (urlsplit(source["server"]).netloc if source.get("server") else None)
    return RepoRef.parse(source["repository"], platform=source.get("platform"),
                         web_host=host, api_base=source.get("api_base"))


def _repository_fields(root: Path) -> dict:
    if _identity(root) is None:
        return {"repository": None}
    try:
        return _repo_fields(RepoRef.from_remote(root))
    except (ValueError, OSError, subprocess.SubprocessError):
        return {"repository": None}


def _web_url(repository: str | None, version: str | None, path: str, line: int,
             server: str | None = None) -> str | None:
    if not repository or not version:
        return None
    if not server:
        return None
    base = server.rstrip("/")
    style = "/blob/" if urlsplit(base).hostname == "github.com" else "/src/commit/"
    return base + "/" + repository + style + quote(version, safe="") + "/" + quote(path, safe="/") + "#L" + str(line)


def _layout(config: str | None) -> list[dict]:
    """Read declared TOML library roots, never evaluate a dynamic lakefile."""
    value = tomllib.loads(config) if config else {}
    libraries = []
    for lib in value.get("lean_lib", []):
        directory = lib.get("srcDir", value.get("srcDir", "."))
        directory = "." if directory == "." else _relative(directory)
        roots = lib.get("roots", [lib["name"]])
        if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
            raise ValueError("Lean library roots must be a string array")
        libraries.append({"srcDir": directory, "roots": roots})
    return sorted(libraries, key=lambda lib: (-len(PurePosixPath(lib["srcDir"]).parts), lib["srcDir"]))


def _library_roots(layout: list[dict]) -> tuple[str, ...]:
    return tuple(sorted({PurePosixPath(lib["srcDir"], *root.split(".")).as_posix()
                         for lib in layout for root in lib["roots"]}))


def _module(path: str, layout: list[dict]) -> str | None:
    relative = PurePosixPath(path)
    for library in layout:
        prefix = PurePosixPath(library["srcDir"])
        if relative.is_relative_to(prefix):
            parts = relative.relative_to(prefix).with_suffix("").parts
            # Do not fabricate an import for slugs/hyphens or generated names.
            if not all(re.fullmatch(r"[^\W\d][\w']*", part) for part in parts):
                return None
            module = ".".join(parts)
            if any(module == root or module.startswith(root + ".") for root in library["roots"]):
                return module
    return None


def _declarations(path: str, text: str):
    """Read Lean 4.35 declarations with shared module visibility handling."""
    masked = mask_comments(text, preserve_escaped_identifiers=True)
    if re.search(r"^[ \t]+(?:namespace|(?:(?:public|noncomputable|meta)\s+)*section|end)\b",
                 masked, re.MULTILINE):
        raise ValueError(f"{path}: indented scopes need a Lean index; source locations only")
    return parse_module_text(text, Path(path), Path(".")).decls


def _records(path: str, text: str, layout: list[dict], material_roots: tuple[str, ...] = ()) -> dict:
    """Retain source fallback even where the existing lexer cannot name declarations."""
    material = _is_material(path, material_roots)
    if path.endswith(".md"):
        return {"records": [{"path": path, "line": 1, "kind": "doc", "text": text,
                              "fqn": None, "module": None, "entry_kind": "document",
                              "source_role": "research-material" if material else "documentation"}], "warnings": []}
    module = None if material else _module(path, layout)
    records = [{"path": path, "line": 1, "kind": "source", "text": text,
                "fqn": None, "module": module, "entry_kind": "source"}]
    if module:
        records.append({"path": path, "line": 1, "location_line": 1, "kind": "source",
                        "text": "import " + module, "fqn": None, "module": module, "entry_kind": "module"})
    warnings = []
    try:
        for decl in _declarations(path, text):
            # Synthetic search text keeps the original declaration location in
            # location_line; it is not an elaborated Lean signature.
            body = decl.fqn + "\n" + decl.signature + "\n" + decl.doc
            for kind in ("source", "doc"):
                records.append({"path": path, "line": decl.line, "location_line": decl.line,
                                "kind": kind, "text": body, "fqn": decl.fqn, "module": module,
                                "declaration_kind": decl.kind, "signature": decl.signature,
                                "variable_context": list(decl.variable_context), "entry_kind": "declaration"})
    except ValueError as error:
        warnings.append(str(error))
    try:
        for line, body in proof_index.doc_comments(text):
            records.append({"path": path, "line": line, "kind": "doc", "text": body,
                            "fqn": None, "module": module, "entry_kind": "documentation"})
    except ValueError as error:
        warnings.append(f"{path}: {error}")
    role = "research-material" if material else "formal-library" if module else "source-only"
    for record in records:
        record["source_role"] = role
    return {"records": records, "warnings": warnings}


def _cache(project: Path, source: dict) -> Path:
    # Hash only the small source descriptor, not files or compilation artifacts.
    key = hashlib.sha256(proof_index.json_bytes({"engine": "unified-discovery-4", "source": source})).hexdigest()[:24]
    return project / ".qatlas/retrieval" / (key + ".json")


def _save(value: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents readers seeing a half-written snapshot. On
    # failure leave the temporary evidence for inspection, rather than cleanup.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".retrieval-", delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("files"), dict):
        raise ValueError("unsupported unified source index")
    for field in ("version", "repository", "server"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError("malformed unified source identity")
    for path, item in value["files"].items():
        _relative(path)
        if not isinstance(item, dict) or not isinstance(item.get("records"), list):
            raise ValueError("malformed unified source records")
        for record in item["records"]:
            if (not isinstance(record, dict) or record.get("path") != path
                    or not isinstance(record.get("text"), str)
                    or record.get("kind") not in {"doc", "source"}
                    or record.get("source_role") not in {"research-material", "documentation", "formal-library", "source-only"}
                    or type(record.get("line")) is not int or record["line"] < 1
                    or "location_line" in record and (type(record["location_line"]) is not int or record["location_line"] < 1)
                    or record.get("fqn") is not None and not isinstance(record["fqn"], str)
                    or record.get("module") is not None and not isinstance(record["module"], str)):
                raise ValueError("malformed unified source record")
    return value


def _symlink_components(root: Path, relative: str) -> bool:
    parts = PurePosixPath(relative).parts
    return any(root.joinpath(*parts[:end]).is_symlink() for end in range(1, len(parts) + 1))


def _readme_alias(root: Path, relative: str) -> bool:
    """Recognize only the conventional docs/README.md -> ../README.md alias."""
    if relative != "docs/README.md":
        return False
    path = root / relative
    target = root / "README.md"
    return (path.is_symlink() and not _symlink_components(root, "docs")
            and path.readlink() == Path("../README.md")
            and not target.is_symlink() and target.is_file())


def _walk(root: Path, material_roots: tuple[str, ...] = (), library_roots: tuple[str, ...] = ()) -> list[str]:
    """Used only on initial build/explicit refresh without a Git inventory."""
    found = []
    def visit(directory: Path):
        for path in sorted(directory.iterdir()):
            if path.name in SKIP or path.name.startswith("."):
                continue
            if path.is_symlink():
                relative = path.relative_to(root).as_posix()
                if _readme_alias(root, relative):
                    continue
                if _admitted(relative, material_roots, library_roots) or path.is_dir():
                    raise ValueError("source path is symlinked or escapes checkout: " + relative)
                continue
            if path.is_dir():
                visit(path)
            elif _admitted(path.relative_to(root).as_posix(), material_roots, library_roots):
                found.append(path.relative_to(root).as_posix())
    visit(root)
    return found


def _dirty(root: Path, material_roots: tuple[str, ...] = (), library_roots: tuple[str, ...] = ()) -> list[str]:
    raw = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    return sorted({entry[3:] for entry in raw.split("\0") if entry and _admitted(entry[3:], material_roots, library_roots)})


def _local(project: Path, source: dict, *, refresh: bool = False) -> dict:
    root = Path(source["root"]).resolve()
    if not root.is_dir():
        raise ValueError("source checkout unavailable; restore the project's Lake dependencies (no separate Dev checkout needed)")
    head = _identity(root)
    cache = _cache(project, source)
    prior = _load(cache) if cache.is_file() and not refresh else None
    config_path = root / "lakefile.toml"
    config = config_path.read_text(encoding="utf-8") if config_path.is_file() else None
    layout = _layout(config)
    material_roots = _material_roots(source.get("material_roots", ())) if source["kind"] == "project" else ()
    library_roots = _library_roots(layout)
    mutable = source["kind"] == "project" or source.get("incremental", False)
    if prior and prior.get("version") == head and prior.get("layout") == layout and (not mutable or not head):
        return prior
    dirty = _dirty(root, material_roots, library_roots) if head else []
    if prior and head and prior.get("version") and prior.get("layout") == layout:
        changed = set(dirty) | set(prior.get("dirty", []))
        if head != prior["version"]:
            changed.update(_git(root, "diff", "--name-only", "--no-renames", "-z", prior["version"], head).split("\0"))
        files = prior["files"].copy()
    else:
        changed = set(_git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")) if head else set(_walk(root, material_roots, library_roots))
        files = {}
    changed = {p for p in changed if _admitted(p, material_roots, library_roots)}
    if "README.md" in changed and (root / "docs/README.md").is_symlink():
        changed.add("docs/README.md")
    if "docs/README.md" in changed and _readme_alias(root, "docs/README.md"):
        changed.add("README.md")  # keep the canonical document, even if ignored by Git
    if prior and not changed and prior.get("version") == head and prior.get("layout") == layout:
        return prior
    for relative in sorted(changed):
        path = root / _relative(relative)
        if _readme_alias(root, relative):
            files.pop(relative, None)
        elif _symlink_components(root, relative) or not path.resolve().is_relative_to(root):
            raise ValueError("source path is symlinked or escapes checkout: " + relative)
        elif not path.exists():
            files.pop(relative, None)
        else:
            files[relative] = _records(relative, path.read_text(encoding="utf-8"), layout, material_roots)
    if not any(p.endswith(".lean") for p in files):
        raise ValueError("no Lean sources found in this source inventory")
    value = {"schema": SCHEMA, "version": head, "repository": source.get("repository"),
             "files": files, "layout": layout, "material_roots": list(material_roots), "dirty": dirty,
             "observation": "working-tree" if mutable else "fixed-dependency-snapshot"}
    if not head:
        value["note"] = "No Git inventory: source snapshot changes require explicit build --refresh."
    _save(value, cache)
    return value


def discover(project: Path, catalog: Path | None = None, *,
             material_roots: tuple[str, ...] | list[str] = ()) -> list[dict]:
    project = project_root(project)
    material_roots = _material_roots(material_roots)
    if not any((project / p).is_file() for p in ("lakefile.toml", "lakefile.lean")):
        raise ValueError("--project must identify a Lake project")
    sources = [{"id": "project", "kind": "project", "root": str(project),
                **_repository_fields(project), "material_roots": list(material_roots)}]
    upstream = []
    manifest = project / "lake-manifest.json"
    if manifest.is_file():
        value = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("packages"), list):
            raise ValueError("Lake manifest must contain a packages array")
        packages_dir = project / _relative(value.get("packagesDir", ".lake/packages"))
        for package in value.get("packages", []):
            name = package["name"].strip("«»")
            _relative(name)
            if "/" in name:
                raise ValueError("invalid Lake package name")
            if package.get("type") == "path":
                root = (project / package["dir"]).resolve()
            else:
                root = packages_dir / name
            if package.get("subDir"):
                root = root / _relative(package["subDir"])
            url = package.get("url", "")
            try:
                ref = RepoRef.parse(url)
                context = _repo_fields(ref)
            except ValueError:
                ref, context = None, {"repository": None}
            sources.append({"id": "dependency:" + name, "kind": "dependency", "root": str(root.resolve()),
                            **context, "locked_version": package.get("rev"),
                            "incremental": name in {"Project", "Qatlib"} or package.get("type") == "path"})
            if name in {"Project", "Qatlib", "qatlas_frozen"} and ref:
                upstream.append({"id": "upstream:" + name, "kind": "upstream", **context, "ref": "main"})
    else:
        sources.append({"id": "dependencies", "kind": "unavailable", "error": "lake-manifest.json is missing; dependency coverage unknown"})
    if catalog is None:
        default = project / "retrieval-catalog.json"
        catalog = default if default.is_file() else None
    if catalog:
        value = json.loads(catalog.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != CATALOG_SCHEMA or not isinstance(value.get("sources"), list):
            raise ValueError("unsupported retrieval catalog")
        for raw in value["sources"]:
            source = dict(raw)
            if "material_roots" in source:
                raise ValueError("material_roots configures the current project, not catalog sources")
            if not isinstance(source.get("id"), str) or not source["id"].strip():
                raise ValueError("catalog sources need a nonempty id")
            source.setdefault("kind", "upstream")
            if source["kind"] not in {"upstream", "pull-request"}:
                raise ValueError("catalog kind must be upstream or pull-request; local dependencies are discovered from Lake")
            providers = sum(key in source for key in ("root", "index"))
            if providers > 1:
                raise ValueError("catalog source selects root OR index OR repository/ref")
            for key in ("root", "index"):
                if key in source:
                    source[key] = str((catalog.parent / source[key]).resolve())
            if not providers:
                source.update(_repo_fields(_source_ref(source)))
            if "pr" in source:
                if type(source["pr"]) is not int or source["pr"] < 1:
                    raise ValueError("pr must be a positive integer")
                source["kind"] = "pull-request"
            source.setdefault("ref", "main")
            sources.append(source)
    else:
        sources.extend(upstream)
    ids = [source["id"] for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source id in retrieval catalog")
    return sources


def _import_index(path: Path) -> dict:
    # Official Frozen source-git-2 archives retain their own integrity checks;
    # importing local bytes does not claim that a live Release was authenticated.
    with (gzip.open(path, "rt", encoding="utf-8") if path.suffix == ".gz" else path.open(encoding="utf-8")) as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("source index must be an object")
    if value.get("schema") == SCHEMA:
        if path.suffix == ".gz":
            raise ValueError("unified snapshots use plain JSON")
        return _load(path)
    proof_index.admit(value, None)
    files = {}
    for record in value["records"]:
        if record["kind"] == "source" or record["path"].endswith(".md"):
            # The existing Frozen/Dev proof catalog schema admits only these
            # declared package module roots, not arbitrary research directories.
            layout = [{"srcDir": ".", "roots": list(proof_index.index_identity.MODULE_ROOTS)}]
            files[record["path"]] = _records(record["path"], record["text"], layout)
    return {"schema": SCHEMA, "files": files, "repository": value.get("repository"),
            "version": value.get("git_head"), "observation": "imported-proof-catalog",
            "publication_verified": False}


def _indexed(project: Path, source: dict, *, refresh: bool = False) -> dict:
    path = Path(source["index"])
    stat = path.stat()
    stamp = [stat.st_mtime_ns, stat.st_size]
    cache = _cache(project, source)
    if cache.is_file() and not refresh:
        prior = _load(cache)
        if prior.get("input_stamp") == stamp:
            return prior
    value = _import_index(path)
    value["input_stamp"] = stamp
    _save(value, cache)
    return value


def _archive(api, repository: str, revision: str) -> bytes:
    if api.ref.slug != repository:
        raise ValueError("archive client does not match target repository")
    return api.archive(revision, max_bytes=MAX_ARCHIVE)


def _archive_files(payload: bytes) -> dict[str, str]:
    # Never extract filesystem entries: symlinks, traversal, executables and
    # unrelated files do not become a checkout or an executable tool snapshot.
    files, total, count, tops = {}, 0, 0, set()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r|gz") as archive:
        for member in archive:
            count += 1
            total += member.size
            if count > 100000 or total > MAX_EXPANDED or member.size > 16 * 1024 * 1024:
                raise ValueError("source archive exceeds expanded budget")
            name = _relative(member.name)
            parts = PurePosixPath(name).parts
            tops.add(parts[0])
            if len(tops) != 1:
                raise ValueError("source archive has multiple top-level directories")
            if member.issym() or member.islnk():
                continue
            if not member.isfile() or len(parts) < 2:
                continue
            relative = PurePosixPath(*parts[1:]).as_posix()
            # Read bounded Markdown data before its Lake layout is known;
            # _remote selects owned documents after reading the archive config.
            if _admitted(relative, all_markdown=True) or relative == "lakefile.toml":
                if relative in files:
                    raise ValueError("source archive contains duplicate paths")
                stream = archive.extractfile(member)
                if stream is None:
                    raise ValueError("archive member could not be read")
                files[relative] = stream.read().decode("utf-8")
    return files


def _remote(project: Path, source: dict, *, refresh: bool = False, api=None) -> dict:
    cache = _cache(project, source)
    if not refresh:
        if not cache.is_file():
            raise ValueError("upstream not cached; run refresh with this catalog to fetch read-only sources")
        return _load(cache)
    ref = _source_ref(source)
    if api is None:
        api = RepoAPI(ref)
    elif api.ref != ref:
        api = api.for_repo(ref)
    repository = source["repository"]
    if "pr" in source:
        pr = api.request(f"repos/{repository}/pulls/{source['pr']}")
        merged = pr.get("merged") is True
        if merged:
            revision = pr.get("merge_commit_sha")
        else:
            revision = pr.get("head", {}).get("sha")
            head_repo = pr.get("head", {}).get("repo") or {}
            target = head_repo.get("clone_url") or head_repo.get("ssh_url") or head_repo.get("html_url")
            if target:
                target_ref = RepoRef.parse(target, platform=ref.platform)
            else:
                # A slug from an authenticated PR response has explicit host
                # context; a catalog's bare slug never acquires one implicitly.
                target_ref = RepoRef.parse(head_repo.get("full_name", repository),
                                           platform=ref.platform, web_host=ref.web_host, api_base=ref.api_base)
            if target_ref != ref:
                api = api.for_repo(target_ref)
                ref = target_ref
            repository = ref.slug
        observation = {"pr": source["pr"], "merged": merged, "url": pr.get("html_url")}
    else:
        info = api.request(f"repos/{repository}")
        branch_ref = source.get("ref", info.get("default_branch", "main"))
        commit = api.request(f"repos/{repository}/git/commits/{quote(branch_ref, safe='')}")
        revision = commit.get("sha")
        # Only the default branch is described as merged upstream. Arbitrary
        # feature refs remain candidates rather than being silently promoted.
        merged = branch_ref == info.get("default_branch")
        observation = {"ref": branch_ref, "merged": merged, "url": info.get("html_url")}
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise ValueError("repository API returned invalid source identity")
    raw = _archive_files(_archive(api, repository, revision))
    layout = _layout(raw.pop("lakefile.toml", None))
    files = {path: _records(path, text, layout) for path, text in sorted(raw.items())
             if _admitted(path, library_roots=_library_roots(layout))}
    if not any(path.endswith(".lean") for path in files):
        raise ValueError("upstream archive contains no Lean sources")
    value = {"schema": SCHEMA, "version": revision, "repository": repository,
             **_repo_fields(ref), "files": files, "observation": observation,
             "fetched_at": datetime.now(timezone.utc).isoformat(), "merged": merged}
    _save(value, cache)
    return value


def collect(project: Path, catalog: Path | None = None, *, refresh: bool = False,
            refresh_local: bool = False, api=None,
            material_roots: tuple[str, ...] | list[str] = ()) -> tuple[list[dict], list[dict]]:
    project = project_root(project)
    sources = discover(project, catalog, material_roots=material_roots)
    corpora, records = [], []
    for source in sources:
        try:
            if source["kind"] == "unavailable":
                raise ValueError(source["error"])
            if "root" in source:
                index = _local(project, source, refresh=refresh_local)
            elif "index" in source:
                index = _indexed(project, source, refresh=refresh_local)
            else:
                index = _remote(project, source, refresh=refresh, api=api)
            local = source["kind"] in {"project", "dependency"}
            availability = ("local-checkable" if local else "unmerged-candidate"
                            if source["kind"] == "pull-request" and not index.get("merged")
                            or index.get("merged") is False else "dependency-update-required")
            version = index.get("version") or source.get("version")
            repository = index.get("repository") or source.get("repository")
            warnings = [warning for item in index["files"].values() for warning in item.get("warnings", [])]
            if source.get("locked_version") and version != source["locked_version"]:
                warnings.append("Local checkout differs from the Lake lock (or has no Git identity); discovery is not lock validation.")
            evidence = {**source, "version": version, "repository": repository, "availability": availability,
                        "server": index.get("server") or source.get("server"),
                        "platform": index.get("platform") or source.get("platform"),
                        "web_host": index.get("web_host") or source.get("web_host"),
                        "consumption": "current-project" if source["kind"] == "project" else
                                       "locked-dependency" if source["kind"] == "dependency" else "candidate",
                        "observation": index.get("observation"), "fetched_at": index.get("fetched_at"),
                        "dirty_paths": index.get("dirty", []), "warnings": warnings,
                        "status": "scanned", "files": len(index["files"]), "records": 0,
                        "index_path": str(_cache(project, source))}
            if index.get("note"):
                evidence["note"] = index["note"]
            for path, item in sorted(index["files"].items()):
                for position, record in enumerate(item["records"]):
                    # IDs are assigned by this adapter, never trusted from catalogs.
                    record_id = json.dumps([source["id"], path, position], ensure_ascii=False)
                    records.append({**record, "record_id": record_id, "source_id": source["id"]})
                    evidence["records"] += 1
            corpora.append(evidence)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, tarfile.TarError) as error:
            corpora.append({**source, "status": "unavailable", "error": str(error)})
    installed = {(c.get("platform"), c.get("web_host"), c.get("repository"), c.get("version")): c["id"] for c in corpora
                 if c.get("availability") == "local-checkable" and c.get("repository") and c.get("version")}
    for corpus in corpora:
        key = (corpus.get("platform"), corpus.get("web_host"), corpus.get("repository"), corpus.get("version"))
        if corpus.get("availability") == "dependency-update-required" and key in installed:
            corpus["availability"] = "local-checkable"
            corpus["available_in_source"] = installed[key]
    return corpora, records


def search(project: Path, query: str, *, catalog: Path | None = None, mode: str = "literal",
           limit: int = 20, regex_seconds: float = 2.0,
           material_roots: tuple[str, ...] | list[str] = ()) -> dict:
    proof_index.validate_query(query, mode, limit, regex_seconds)
    corpora, records = collect(project_root(project), catalog, material_roots=material_roots)
    evidence = {item["id"]: item for item in corpora}
    # Literal/regex have no statistical score: exact FQN, short name, lexical
    # name fragment, then source locations. Local/Frozen wins only relevance ties.
    def rank(record):
        fqn = record.get("fqn") or ""
        name_rank = 0 if fqn == query else 1 if fqn.rsplit(".", 1)[-1] == query else 2 if query in fqn else 3
        source = evidence[record["source_id"]]
        locality = 0 if source.get("availability") == "local-checkable" else 1
        frozen = 0 if "frozen" in source["id"] else 1
        return name_rank, locality, frozen, record["path"], record["line"], record["record_id"]
    records.sort(key=rank)
    mapped = {record["record_id"]: record for record in records}
    terms, untranslated = cjk_bridge(query) if mode == "doc" else ([], [])
    effective_query = query + " " + " ".join(terms) if terms else query
    # doc mode scores all supplied corpora together, not separate top-k lists.
    answer = proof_index.search({"records": records}, effective_query, mode, limit, regex_seconds)
    hits = []
    for hit in answer["results"]:
        record = mapped[hit.pop("record_id")]
        source = evidence[record["source_id"]]
        line = record.get("location_line", hit["line"])
        if "location_line" in record:
            hit.pop("column", None)  # synthetic declaration text has no source column
        module = record.get("module")
        hits.append({**hit, "line": line, "fqn": record.get("fqn"), "module": module,
                     "import": "import " + module if module else None, "source_id": source["id"],
                     "repository": source.get("repository"), "version": source.get("version"),
                     "locked_version": source.get("locked_version"), "availability": source["availability"],
                     "entry_kind": record.get("entry_kind"), "source_role": record["source_role"],
                     "lean_type_checked": False,
                     "source_signature": record.get("signature"),
                     "variable_context": record.get("variable_context", []),
                     "available_in_source": source.get("available_in_source"),
                     "local_path": str(Path(source["root"]) / hit["path"]) if "root" in source else None,
                     "source_url": _web_url(source.get("repository"), source.get("version"), hit["path"], line,
                                            source.get("server")),
                     "source_modified": hit["path"] in source.get("dirty_paths", []),
                     "location_note": "Declaration header/doc excerpt; line points to the declaration" if record.get("fqn") else "Source location only"})
    return {"schema": "qatlas-unified-retrieval-1", "query": query, "mode": mode, "limit": limit,
            "project": str(project_root(project)), "results": hits, "corpora": corpora,
            "incomplete": any(c["status"] == "unavailable" for c in corpora),
            "truncated": answer["truncated"], "validation": "not-checked",
            "upstream_coverage": "configured-catalog-only; no automatic global or PR search",
            "terminology_bridge": terms, "untranslated_terms": untranslated, "limitations": LIMITATIONS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", "--root", type=Path, help="consumer Lake project, not the Python installation")
    parser.add_argument("--catalog", type=Path, help="optional source catalog (default: project/retrieval-catalog.json)")
    parser.add_argument("--material-root", action="append", default=[], metavar="RELATIVE_DIR",
                        help="project-only source material directory; repeatable, no import claims, includes Markdown")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sources", help="list selected sources without indexing or network")
    build = commands.add_parser("build", help="prepare local snapshots; no network")
    build.add_argument("--refresh", action="store_true", help="rebuild fixed/non-Git source snapshots")
    commands.add_parser("refresh", help="fetch configured upstream sources using read-only Gitea API")
    find = commands.add_parser("search")
    find.add_argument("query")
    find.add_argument("--mode", choices=("literal", "doc", "regex"), default="literal")
    find.add_argument("--limit", type=int, default=20)
    find.add_argument("--regex-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        root = project_root(args.project)
        catalog = args.catalog.resolve() if args.catalog else None
        if args.command == "sources":
            result = {"project": str(root), "sources": discover(root, catalog, material_roots=args.material_root),
                      "upstream_coverage": "configured-catalog-only; refresh explicitly"}
        elif args.command == "search":
            result = search(root, args.query, catalog=catalog, mode=args.mode, limit=args.limit,
                            regex_seconds=args.regex_seconds, material_roots=args.material_root)
        else:
            corpora, _ = collect(root, catalog, refresh=args.command == "refresh",
                                 refresh_local=args.command == "build" and args.refresh,
                                 material_roots=args.material_root)
            result = {"project": str(root), "corpora": corpora,
                      "incomplete": any(c["status"] == "unavailable" for c in corpora),
                      "validation": "not-checked"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("incomplete") else 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print("unified_retrieval: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
