#!/usr/bin/env python3
"""Minimal JSON-Schema validator for the retrieval report (stdlib only).

Implements exactly the keyword subset used by the package-bundled
`qatlib_search/retrieval-report.schema.json` (draft 2020-12 style):
`type` (string or list), `properties`, `required`, `additionalProperties`
(bool or schema), `items` (single schema), `enum`, `const`, `pattern`,
`minLength`, `minimum`, `minItems`, `maxItems`, `allOf`, `if`/`then`/`else`,
`contains`/`minContains`. String/number/array keywords only apply to instances
of the corresponding JSON type, as in real JSON Schema.

Used by verification/unittests/test_query_library.py so validation works on hosts/containers
without the jsonschema package; when that package is importable the tests
additionally cross-check with the real validator.

CLI: `python3 -m qatlib_search.validate_report REPORT.json [SCHEMA.json]` — exit 0
when REPORT validates, exit 1 with one error per line otherwise.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_SCHEMA = Path(__file__).resolve().parent / "retrieval-report.schema.json"

TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _json_equal(a, b) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _type_ok(value, expected) -> bool:
    if isinstance(expected, str):
        return TYPE_CHECKS[expected](value)
    return any(TYPE_CHECKS[name](value) for name in expected)


def validate(instance, schema: dict, path: str = "$") -> list[str]:
    """Return a list of human-readable violations (empty when valid)."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return [f"{path}: schema fragment is not an object"]

    if "type" in schema and not _type_ok(instance, schema["type"]):
        return [f"{path}: expected type {schema['type']!r}, got {type(instance).__name__}"]

    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append(f"{path}: expected const {schema['const']!r}, got {instance!r}")
    if "enum" in schema and not any(_json_equal(instance, option) for option in schema["enum"]):
        errors.append(f"{path}: {instance!r} not in enum {schema['enum']!r}")

    if isinstance(instance, str):
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{path}: {instance!r} does not match pattern {schema['pattern']!r}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than minLength {schema['minLength']}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: {instance} below minimum {schema['minimum']}")

    if isinstance(instance, list):
        if "items" in schema:
            for index, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{path}[{index}]"))
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than minItems {schema['minItems']}")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than maxItems {schema['maxItems']}")
        if "contains" in schema:
            needed = schema.get("minContains", 1)
            matching = sum(1 for item in instance if not validate(item, schema["contains"], path))
            if matching < needed:
                errors.append(f"{path}: only {matching} items match 'contains', need {needed}")

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required property {key!r}")
        properties = schema.get("properties", {})
        for key, value in instance.items():
            child = f"{path}.{key}"
            if key in properties:
                errors.extend(validate(value, properties[key], child))
            elif "additionalProperties" in schema:
                extra = schema["additionalProperties"]
                if extra is False:
                    errors.append(f"{child}: additional property not allowed")
                elif isinstance(extra, dict):
                    errors.extend(validate(value, extra, child))

    if "allOf" in schema:
        for branch in schema["allOf"]:
            errors.extend(validate(instance, branch, path))
    if "if" in schema:
        condition_holds = not validate(instance, schema["if"], path)
        branch = schema.get("then") if condition_holds else schema.get("else")
        if branch is not None:
            errors.extend(validate(instance, branch, path))
    return errors


def main(argv: list[str]) -> int:
    if len(argv) < 2 or len(argv) > 3:
        print(__doc__, file=sys.stderr)
        return 2
    report_path, schema_path = Path(argv[1]), Path(argv[2]) if len(argv) == 3 else DEFAULT_SCHEMA
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"cannot load report or schema: {error}", file=sys.stderr)
        return 2
    errors = validate(report, schema)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"{report_path} does not validate against {schema_path}", file=sys.stderr)
        return 1
    print(f"{report_path} validates against {schema_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
