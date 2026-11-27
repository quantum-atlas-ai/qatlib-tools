"""Shared query contract for the literature CLI and MCP tools."""
from __future__ import annotations

import re

from .literature import (LiteratureError, Snapshot, paper_identifiers, parse_identifier,
                         source_link, validate)
from .retrieve import tokens

STATUS = ("MATCH", "NO_MATCH", "RELATED_ONLY", "SOURCE_VERSION_UNKNOWN")
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 10}
IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 256}
VERSION = {"type": "string", "pattern": "^[vV]?[1-9][0-9]{0,8}$"}
PHRASE = {"type": "string", "minLength": 1, "maxLength": 2048, "pattern": "\\S"}


def object_schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required),
            "additionalProperties": False}


QUERIES = {
    "lookup-paper": object_schema({"identifier": IDENTIFIER, "version": VERSION}, ("identifier",)),
    "check-errata": object_schema({"identifier": IDENTIFIER, "version": VERSION,
                                 "locator": PHRASE, "limit": LIMIT}, ("identifier",)),
    "search-interface": {
        **object_schema({"text": PHRASE, "concept": PHRASE, "oracle": PHRASE,
                         "resource_field": PHRASE, "limit": LIMIT}),
        "anyOf": [{"required": [key]} for key in ("text", "concept", "oracle", "resource_field")],
    },
}
RESULT = object_schema({
    "schema_version": {"type": "integer"},
    "snapshot_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
    "source_commit": {"type": "string", "pattern": "^[a-f0-9]{40}$"},
    "query": {"type": "object"},
    "status": {"enum": list(STATUS)},
    "results": {"type": "array", "items": {"type": "object"}},
    "known_versions": {"type": "array", "items": {"type": "string"}},
    "limitations": {"type": "array", "items": {"type": "string"}},
    "total": {"type": "integer", "minimum": 0},
    "truncated": {"type": "boolean"},
}, ("schema_version", "snapshot_id", "source_commit", "query", "status", "results", "known_versions",
    "limitations", "total", "truncated"))
DESCRIPTIONS = {
    "lookup-paper": "Resolve an arXiv ID, DOI or paper alias and inspect indexed source versions.",
    "search-interface": "Find lexical Lean interface candidates with source evidence and coverage.",
    "check-errata": "Check recorded findings for a specific paper version and optional locator.",
}


def _response(snapshot: Snapshot, query: dict, status: str, rows: list, *,
              versions=(), notes=(), limit=10) -> dict:
    manifest = snapshot.manifest
    return dict(schema_version=manifest["schema_version"], snapshot_id=manifest["snapshot_id"],
                source_commit=manifest["source_commit"], query=query, status=status,
                known_versions=list(versions), results=rows[:limit], total=len(rows),
                truncated=len(rows) > limit, limitations=[manifest["scope"], *notes])


def _locator(value: str) -> str:
    normalized = re.sub(r"[().\s]+", "", value.casefold())
    return re.sub(r"^(?:equations?|eqs?)(?=\d)", "eq",
                  re.sub(r"^thm(?=\d)", "theorem", normalized))


def _errata(snapshot: Snapshot, paper: dict, version: str, locator: str | None) -> list[dict]:
    rows = []
    interfaces = {item["fqn"]: item for item in paper["interfaces"]}
    source = next((item for item in paper["sources"] if item["version"] == version), None)
    for finding in paper["errata"]:
        for field, disposition in (("affected", "AFFECTED"), ("fixed_in", "FIXED_IN")):
            for location in finding[field]:
                if location["version"] != version:
                    continue
                if locator is not None and _locator(locator) not in map(_locator, location["locators"]):
                    continue
                correction = interfaces.get(finding["correction_fqn"])
                rows.append({**finding, "paper_id": paper["paper_id"], "disposition": disposition,
                             "matched_location": location, "source": source,
                             "correction_url": source_link(snapshot.manifest, correction) if correction else None})
    return rows


def _paper_query(snapshot: Snapshot, command: str, query: dict) -> dict:
    try:
        key, embedded, identifier_kind = parse_identifier(query["identifier"])
    except ValueError as error:
        raise LiteratureError("INVALID_ARGUMENT", str(error)) from error
    explicit = query.get("version")
    explicit = "v" + explicit.casefold().removeprefix("v") if explicit else None
    if embedded and explicit and embedded != explicit:
        raise LiteratureError("INVALID_ARGUMENT", "identifier and version select different revisions")
    version = embedded or explicit
    paper = snapshot.identities.get(key)
    if paper is None:
        return _response(snapshot, query, "NO_MATCH", [], notes=("No paper identity in this snapshot.",))
    versions = {source["version"]: source for source in paper["sources"]}
    lookup = command == "lookup-paper"
    if (version is not None or not lookup) and (version not in versions or identifier_kind == "doi"):
        return _response(snapshot, query, "SOURCE_VERSION_UNKNOWN", [], versions=versions, notes=(
            "Select an indexed arXiv version via its arXiv ID or paper alias. A publisher DOI identifies the work only.",
        ))
    if lookup:
        row = {field: paper[field] for field in ("paper_id", "title", "arxiv_id", "dois", "aliases")}
        row["source_versions"] = paper["sources"]
        row["selected_source"] = versions.get(version)
        row["claims"] = [claim for claim in paper["claims"] if claim["version"] == version]
        fqns = {fqn for claim in row["claims"] for fqn in claim["interfaces"]}
        row["interfaces"] = [_interface(snapshot, paper, item) for item in paper["interfaces"]
                             if item["fqn"] in fqns]
        row["errata"] = _errata(snapshot, paper, version, None)
        notes = ("MATCH identifies the paper or the explicitly selected source version.",
                 "Omitting the version returns a bibliography match with no version-specific findings.")
        return _response(snapshot, query, "MATCH", [row], versions=versions, notes=notes)
    rows = _errata(snapshot, paper, version, query.get("locator"))
    return _response(snapshot, query, "MATCH" if rows else "NO_MATCH", rows,
                     versions=versions, limit=query["limit"], notes=(
                         "Findings apply only to the identified text and locator.",
                         "Coverage is partial. Absence of a finding gives no assurance about the source.",
                     ))


def _interface(snapshot: Snapshot, paper: dict, item: dict) -> dict:
    claims = [claim for claim in paper["claims"] if item["fqn"] in claim["interfaces"]]
    return {**item, "paper_id": paper["paper_id"], "identifiers": paper_identifiers(paper),
            "import": "import " + item["module"], "source_commit": snapshot.manifest["source_commit"],
            "source_url": source_link(snapshot.manifest, item), "mapped_claims": claims}


def _search(snapshot: Snapshot, query: dict) -> dict:
    text = query.get("text", "")
    words = set(tokens(text))
    if len(words) > 32:
        raise LiteratureError("INVALID_ARGUMENT", "query supports at most 32 distinct tokens")
    matches = []
    for paper in snapshot.papers:
        for item in paper["interfaces"]:
            filters = (("concept", "concepts"), ("oracle", "oracle"), ("resource_field", "resource_fields"))
            if any(query[key].strip().casefold() not in {tag.casefold() for tag in item[field]}
                   for key, field in filters if key in query):
                continue
            exact = text.strip().casefold() == item["fqn"].casefold()
            searchable = " ".join((item["fqn"], item["source_signature"], item["machine_scope"],
                                   *item["concepts"], *item["oracle"], *item["resource_fields"]))
            overlap = len(words.intersection(tokens(searchable)))
            if not text or exact or overlap:
                matches.append((not exact, -overlap, item["fqn"], paper["arxiv_id"],
                                _interface(snapshot, paper, item)))
    matches.sort(key=lambda match: match[:4])
    rows = [match[-1] for match in matches]
    return _response(snapshot, query, "RELATED_ONLY" if rows else "NO_MATCH", rows,
                     limit=query["limit"], notes=(
                         "Interface hits are lexical candidates. Check the Lean type, premises and consumer.",
                         "Source signatures are written headers with an optional enclosing variable context.",
                     ))


def execute(snapshot: Snapshot, command: str, arguments: dict) -> dict:
    from jsonschema import Draft202012Validator

    try:
        validate(arguments, Draft202012Validator(QUERIES[command]))
    except ValueError as error:
        raise LiteratureError("INVALID_ARGUMENT", str(error)) from error
    query = dict(arguments)
    if command != "lookup-paper":
        query.setdefault("limit", LIMIT["default"])
    if command == "search-interface":
        return _search(snapshot, query)
    return _paper_query(snapshot, command, query)
