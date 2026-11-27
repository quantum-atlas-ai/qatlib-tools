"""Build and read an explicitly curated, portable literature snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import version
from importlib.resources import files
import json
from pathlib import Path
import re
import sqlite3
from tempfile import mkdtemp
from urllib.parse import quote, unquote

from . import lean_declarations, retrieve
from .retrieve_corpus import digest, discover

SCHEMA_VERSION = 1
INDEX_NAME = "index.jsonl"
MANIFEST_NAME = "manifest.json"
ARXIV = re.compile(r"(?P<id>[0-9]{4}\.[0-9]{4,5}|[a-z][a-z0-9.-]*/[0-9]{7})(?P<version>v[1-9][0-9]{0,8})?")
DOI = re.compile(r"10\.[0-9]{4,}/[^\s?#]+")
MANIFEST_FIELDS = {"generator_version", "record_count", "sha256", "snapshot_id"}
SOURCE_FIELDS = {"schema_version", "source_commit", "source_repository", "source_date",
                 "lean_toolchain", "dependency_lock_sha256", "scope"}


class LiteratureError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(data: bytes):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)


@lru_cache
def validator(schema_name: str):
    from jsonschema import Draft202012Validator

    schema = json.loads(files("qatlib_search").joinpath(schema_name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate(value, schema) -> None:
    error = next(schema.iter_errors(value), None)
    if error:
        raise ValueError(f"{error.json_path}: failed {error.validator} validation")


def parse_identifier(value: str) -> tuple[str, str | None, str]:
    """Return a normalized identity key, embedded version and identifier kind."""
    text = unquote(value.strip()).casefold()
    for prefix in ("https://arxiv.org/abs/", "https://arxiv.org/pdf/", "arxiv:"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix).split("#", 1)[0].removesuffix(".pdf")
            if not ARXIV.fullmatch(text):
                raise ValueError("invalid arXiv identifier")
            break
    for prefix in ("https://doi.org/", "https://dx.doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
            if not DOI.fullmatch(text):
                raise ValueError("invalid DOI")
            break
    # The arXiv-issued DOI names the arXiv work. Publisher DOIs keep their own namespace.
    match = ARXIV.fullmatch(text.removeprefix("10.48550/arxiv."))
    if match:
        return "arxiv:" + match["id"], match["version"], "arxiv"
    if DOI.fullmatch(text):
        return "doi:" + text, None, "doi"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]*", text):
        raise ValueError("invalid paper identifier")
    return "paper:" + text, None, "paper"


def paper_identifiers(paper: dict) -> list[str]:
    return [paper["paper_id"], "arXiv:" + paper["arxiv_id"], *paper["dois"], *paper["aliases"]]


def _keyed(rows: list[dict], field: str, label: str) -> dict:
    result = {row[field]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"duplicate {label}")
    return result


def version_order(value: str) -> int:
    return int(value[1:])


def _normalize(data: dict) -> tuple[list[dict], dict]:
    validate(data, validator("literature_schema.json"))
    papers = sorted(data["papers"], key=lambda paper: paper["arxiv_id"])
    identities = {}
    record_ids = set()
    for paper in papers:
        for identifier in paper_identifiers(paper):
            key, _, _ = parse_identifier(identifier)
            existing = identities.get(key)
            if existing is not None and existing is not paper:
                raise LiteratureError("AMBIGUOUS_ID", "identifier maps to multiple papers")
            identities[key] = paper
        versions = _keyed(paper["sources"], "version", "arXiv source version")
        for source in versions.values():
            expected = f"https://arxiv.org/pdf/{paper['arxiv_id']}{source['version']}"
            if source["url"] != expected:
                raise ValueError("source URL does not match the pinned arXiv version")
        interfaces = _keyed(paper["interfaces"], "fqn", "interface")
        for interface in interfaces.values():
            lean_declarations.name_parts(interface["fqn"])
            lean_declarations.name_parts(interface["module"])
            if interface["type"] is not None:
                raise ValueError("this index format has no attestation for elaborated Lean types")
        for record in [*paper["claims"], *paper["errata"]]:
            if record["id"] in record_ids:
                raise ValueError("duplicate claim or erratum ID")
            record_ids.add(record["id"])
        for claim in paper["claims"]:
            if claim["version"] not in versions or set(claim["interfaces"]) - interfaces.keys():
                raise ValueError("claim must reference an indexed version and declared interfaces")
        for erratum in paper["errata"]:
            affected = _keyed(erratum["affected"], "version", "affected version")
            fixed = _keyed(erratum["fixed_in"], "version", "fixed version")
            if ((affected.keys() | fixed.keys()) - versions.keys() or
                    fixed and max(map(version_order, affected)) >= min(map(version_order, fixed))):
                raise ValueError("erratum must reference indexed versions with repairs after affected versions")
            if erratum["correction_fqn"] is not None and erratum["correction_fqn"] not in interfaces:
                raise ValueError("erratum correction must reference a declared interface")
            for field in ("affected", "fixed_in"):
                erratum[field].sort(key=lambda location: version_order(location["version"]))
        paper["aliases"].sort(key=str.casefold)
        paper["dois"].sort(key=str.casefold)
        paper["sources"].sort(key=lambda source: version_order(source["version"]))
        for field, key in (("claims", "id"), ("interfaces", "fqn"), ("errata", "id")):
            paper[field].sort(key=lambda item: item[key])
    return papers, identities


def source_link(manifest: dict, interface: dict) -> str:
    path = quote(interface["path"], safe="/")
    return f"{manifest['source_repository']}/blob/{manifest['source_commit']}/{path}#L{interface['line']}"


def verify_source(data: dict, project, db) -> None:
    """Recheck the portable source fields against the existing local retrieval cache."""
    try:
        corpus = discover(project)
        root = corpus.project
        if (corpus.repos[0]["revision"] != data["source_commit"] or
                corpus.inputs[str(root / "lake-manifest.json")] != data["dependency_lock_sha256"] or
                (root / "lean-toolchain").read_text(encoding="utf-8").strip() != data["lean_toolchain"]):
            raise ValueError("source commit, dependency lock or Lean toolchain differs")
        for paper in data["papers"]:
            for item in paper["interfaces"]:
                result = retrieve.query(item["fqn"], root, db, limit=100, repo=corpus.repos[0]["repo"])
                matches = [row for row in result["results"] if row["source_fqn"] == item["fqn"]
                           and row["revision"] == data["source_commit"]]
                if len(matches) != 1:
                    raise ValueError("expected one exact declaration in the pinned repository")
                hit = matches[0]
                if (hit["dirty"] or hit["parse_warning"] or
                        Path(hit["path"]).relative_to(root).as_posix() != item["path"] or
                        any(hit[key] != item[key] for key in ("module", "line", "source_hash",
                                                              "source_signature", "variable_context"))):
                    raise ValueError("portable source evidence differs from the pinned cache")
    except (OSError, ValueError, sqlite3.Error) as error:
        raise LiteratureError("INDEX_STALE", "cannot verify the curated source against the pinned retrieval cache") from error


def export_index(source: Path, output: Path, *, project=None, db=None) -> dict:
    try:
        data = read_json(source.read_bytes())
        papers, _ = _normalize(data)
    except LiteratureError:
        raise
    except (OSError, ValueError) as error:
        raise LiteratureError("INVALID_ARGUMENT", "curated input is missing or invalid") from error
    if project is not None:
        verify_source(data, project, db)
    content = b"".join(map(canonical_json, papers))
    manifest = {key: data[key] for key in SOURCE_FIELDS}
    manifest.update(generator_version=version("qatlib-tools"), record_count=len(papers),
                    sha256=digest(content))
    manifest["snapshot_id"] = digest(canonical_json(manifest))
    manifest_bytes = canonical_json(manifest)
    if output.exists():
        if (output.is_dir() and (output / MANIFEST_NAME).is_file() and (output / INDEX_NAME).is_file()
                and (output / MANIFEST_NAME).read_bytes() == manifest_bytes
                and (output / INDEX_NAME).read_bytes() == content):
            return manifest
        raise LiteratureError("INVALID_ARGUMENT", "output already exists with different content")
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(mkdtemp(dir=output.parent, prefix=".literature-"))
    (staged / INDEX_NAME).write_bytes(content)
    (staged / MANIFEST_NAME).write_bytes(manifest_bytes)
    staged.rename(output)
    return manifest


@dataclass(frozen=True)
class Snapshot:
    manifest: dict
    papers: list[dict]
    identities: dict[str, dict]


def load_index(directory: Path) -> Snapshot:
    try:
        manifest = read_json((directory / MANIFEST_NAME).read_bytes())
        content = (directory / INDEX_NAME).read_bytes()
        if (not isinstance(manifest, dict) or manifest.keys() != SOURCE_FIELDS | MANIFEST_FIELDS
                or manifest["schema_version"] != SCHEMA_VERSION
                or type(manifest["record_count"]) is not int
                or not isinstance(manifest["generator_version"], str)
                or manifest["sha256"] != digest(content)):
            raise ValueError("invalid manifest or content hash")
        identity = {key: value for key, value in manifest.items() if key != "snapshot_id"}
        if manifest["snapshot_id"] != digest(canonical_json(identity)):
            raise ValueError("invalid manifest identity")
        papers = [read_json(line) for line in content.splitlines()]
        data = {key: manifest[key] for key in SOURCE_FIELDS}
        papers, identities = _normalize({**data, "papers": papers})
        if len(papers) != manifest["record_count"] or b"".join(map(canonical_json, papers)) != content:
            raise ValueError("index has incorrect record count, ordering or encoding")
        return Snapshot(manifest, papers, identities)
    except (OSError, ValueError, RecursionError) as error:
        raise LiteratureError("INDEX_STALE", "snapshot is missing, incompatible or has changed") from error
