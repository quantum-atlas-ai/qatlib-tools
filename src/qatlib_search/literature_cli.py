"""Command-line entry points for publishing and consuming literature snapshots."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from .literature import (ARXIV, LiteratureError, export_index, load_index, parse_identifier,
                         read_json, validate, validator)
from .literature_query import DESCRIPTIONS, LIMIT, QUERIES, execute

EXPORT_INDEX = "export-literature-index"
EXPORT_HTML = "export-literature-html"
VALIDATE = "validate-literature-finding"
SERVE = "literature-mcp"
COMMANDS = {EXPORT_INDEX, EXPORT_HTML, VALIDATE, SERVE, *QUERIES}


def add_parsers(subparsers) -> None:
    for name, description in DESCRIPTIONS.items():
        parser = subparsers.add_parser(name, help=description)
        parser.add_argument("--snapshot", required=True, type=Path, help="directory containing index and manifest")
        if name == "search-interface":
            parser.add_argument("text", nargs="?", help="text or fully qualified Lean name")
            parser.add_argument("--concept", help="exact concept tag")
            parser.add_argument("--oracle", help="exact oracle tag")
            parser.add_argument("--resource-field", help="exact resource field tag")
        else:
            parser.add_argument("identifier", help="paper ID, alias, arXiv ID or DOI")
            parser.add_argument("--version", help="arXiv revision, such as v4")
        if name == "check-errata":
            parser.add_argument("--locator", help="equation, theorem or lemma label in the specified version")
        if name != "lookup-paper":
            parser.add_argument("--limit", type=int, default=LIMIT["default"], help="limit of 1 to 100 (default 10)")
        parser.add_argument("--json", action="store_true", help="emit JSON (also the default)")
    build = subparsers.add_parser(EXPORT_INDEX, help="export a deterministic public literature snapshot")
    build.add_argument("source", type=Path, help="explicitly selected public curation JSON")
    build.add_argument("--output", required=True, type=Path, help="new snapshot directory")
    build.add_argument("--project", default=argparse.SUPPRESS, help="verify the pinned Lean checkout")
    build.add_argument("--db", default=argparse.SUPPRESS, help="existing retrieval cache for --project")
    html = subparsers.add_parser(EXPORT_HTML, help="render an offline single-page reverse index")
    html.add_argument("--snapshot", required=True, type=Path)
    html.add_argument("--output", required=True, type=Path, help="destination HTML file")
    validate_parser = subparsers.add_parser(VALIDATE, help="validate independently appendable candidate records")
    validate_parser.add_argument("records", nargs="+", type=Path)
    serve = subparsers.add_parser(SERVE, help="serve the three read-only literature tools over MCP stdio")
    serve.add_argument("--snapshot", required=True, type=Path)


def validate_findings(paths: list[Path]) -> dict:
    identifiers = set()
    schema = validator("literature_finding.schema.json")
    for number, path in enumerate(paths, 1):
        try:
            record = read_json(path.read_bytes())
            validate(record, schema)
            if record["id"] in identifiers:
                raise ValueError("duplicate candidate record ID")
            source = record["source"]
            key, embedded, kind = parse_identifier(source["identifier"])
            version = source["version"]
            if version.strip().casefold() in {"unknown", "latest", "unspecified", "unversioned", "n/a"}:
                raise ValueError("source version must be pinned")
            if kind == "arxiv":
                arxiv_id = key.removeprefix("arxiv:")
                match = ARXIV.fullmatch(arxiv_id + version)
                if (not match or not match["version"] or embedded not in (None, version)
                        or source["url"] != f"https://arxiv.org/pdf/{arxiv_id}{version}"):
                    raise ValueError("arXiv identifier, source URL and version must identify the same text")
            elif kind != "doi":
                raise ValueError("source identifier must be an arXiv ID or DOI")
            identifiers.add(record["id"])
        except OSError as error:
            raise LiteratureError("INVALID_ARGUMENT", f"candidate {number}: cannot read record") from error
        except ValueError as error:
            raise LiteratureError("INVALID_ARGUMENT", f"candidate {number}: {error}") from error
    return {"status": "VALID_CANDIDATE", "ids": sorted(identifiers),
            "limitations": ["Schema and source identity checks only. Review and compilation are pending."]}


def run(args) -> int:
    command = args.command
    try:
        project, db = getattr(args, "project", None), getattr(args, "db", None)
        if (project is not None or db is not None) and command != EXPORT_INDEX:
            raise LiteratureError("INVALID_ARGUMENT", "this command uses --snapshot instead of a Lean checkout or cache")
        if command in QUERIES:
            query = {key: getattr(args, key) for key in QUERIES[command]["properties"]
                     if getattr(args, key) is not None}
            result = execute(load_index(args.snapshot), command, query)
        elif command == EXPORT_INDEX:
            if db is not None and project is None:
                raise LiteratureError("INVALID_ARGUMENT", "--db requires --project for source verification")
            result = export_index(args.source, args.output, project=project, db=db)
        elif command == VALIDATE:
            result = validate_findings(args.records)
        elif command == EXPORT_HTML:
            from .literature_html import render_html

            snapshot = load_index(args.snapshot)
            content = render_html(snapshot)
            if args.output.exists():
                if not args.output.is_file() or args.output.read_bytes() != content:
                    raise LiteratureError("INVALID_ARGUMENT", "HTML destination already exists with different content")
            else:
                with args.output.open("xb") as output:
                    output.write(content)
            result = {"snapshot_id": snapshot.manifest["snapshot_id"]}
        else:
            from .literature_mcp import serve

            asyncio.run(serve(load_index(args.snapshot)))
            return 0
    except ModuleNotFoundError as error:
        if error.name not in {"jsonschema", "mcp"}:
            raise
        print("qatlib-retrieve: install qatlib-tools[literature] to use this command", file=sys.stderr)
        return 2
    except LiteratureError as error:
        print(f"qatlib-retrieve: {error.code}: {error}", file=sys.stderr)
        return 2
    except OSError:
        print("qatlib-retrieve: could not read or write the requested files", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0
