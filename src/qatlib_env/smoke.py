"""Small public-interface consumers, with explicit rename records and real diagnostics."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess
import time
import tomllib

from . import workspace
from qatlib_search.lean_declarations import name_parts
from qatlib_search.lean_source import import_header, mask_comments


def migration_map(path: Path | None) -> list[dict]:
    if path is None:
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(data) != {"rename"} or not isinstance(data["rename"], list) or not data["rename"]:
        raise ValueError("migration map must contain nonempty [[rename]] entries")
    seen = set()
    for row in data["rename"]:
        if (not isinstance(row, dict) or set(row) != {"old", "new", "reason"}
                or not all(isinstance(value, str) and value.strip() for value in row.values())):
            raise ValueError("each rename requires exactly old, new and reason")
        name_parts(row["old"])
        name_parts(row["new"])
        if row["old"] == row["new"] or row["old"] in seen:
            raise ValueError("rename sources must be unique and change the name")
        seen.add(row["old"])
    return data["rename"]


def validate_probe(project: Path, path: Path) -> Path:
    path = (project / path).resolve()
    if not path.is_relative_to(project) or path.suffix != ".lean" or not path.is_file():
        raise ValueError("probe must be an existing Lean file inside the selected project")
    source = path.read_text(encoding="utf-8")
    masked = mask_comments(source, preserve_escaped_identifiers=True).lstrip("\ufeff")
    imports, _ = import_header(source)
    if not re.match(r"\s*module\b", masked) or not imports:
        raise ValueError("public-interface probe requires a module header and explicit imports")
    if re.search(r"\bimport\s+all\b", masked):
        raise ValueError("public-interface probe must use ordinary imports, without import all")
    if re.search(r"\b(?:noncomputable|unsafe|axiom|sorry|admit)\b", masked):
        raise ValueError("probe must consume the interface without noncomputable or proof bypasses")
    return path


def run(project: Path, probes: list[Path], mapping: Path | None = None, *, env: dict | None = None) -> dict:
    project = project.resolve()
    renames = migration_map(project / mapping if mapping is not None else None)
    paths = [validate_probe(project, path) for path in probes]
    if not paths:
        raise ValueError("at least one consumer probe is required")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                          capture_output=True, text=True)
    toolchain = project / "lean-toolchain"
    results = []
    for path in paths:
        started = time.monotonic()
        record = {"probe": path.relative_to(project).as_posix()}
        try:
            effective = workspace.environment(project, base_env=env) if env is not None else workspace.environment(project)
            output = workspace.run(["lake", "--no-build", "lean", str(path)], cwd=project,
                                   env=effective, capture=True, capture_stderr=True)
            record.update(status="PASS", diagnostics=output or "")
        except subprocess.CalledProcessError as error:
            record.update(status="REGRESSION", diagnostics=(error.stdout or "") + (error.stderr or ""),
                          exit_code=error.returncode)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            record.update(status="NOT-RUN", diagnostics=str(error))
        record["seconds"] = round(time.monotonic() - started, 3)
        results.append(record)
    return {"action": "smoke", "head": head.stdout.strip() if head.returncode == 0 else None,
            "toolchain": toolchain.read_text().strip() if toolchain.is_file() else None,
            "renames": renames, "probes": results,
            "errors": [row["probe"] + ": " + row["status"] for row in results if row["status"] != "PASS"]}
