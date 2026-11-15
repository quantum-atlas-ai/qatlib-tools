from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zipfile
import time

from .config import EnvConfig, choose_mode


@dataclass(frozen=True)
class CloudRequest:
    platform: str
    repository: str
    workflow: str
    head: str
    inputs: dict[str, str]
    ref: str | None = None


class CloudLoop:
    """Small stateful orchestration layer over a Forge API client."""
    def __init__(self, api, state_path: str | Path):
        self.api, self.state_path = api, Path(state_path)

    def submit(self, request: CloudRequest) -> dict[str, object]:
        submitted_at = datetime.now(timezone.utc)
        response = self.api.dispatch(request.workflow, request.head, request.inputs)
        state = {"request": request.__dict__, "response": response, "submitted_at": submitted_at.isoformat()}
        if isinstance(response, dict):
            for key in ("id", "run_id", "workflow_run_id"):
                if response.get(key) is not None:
                    state["run_id"] = response[key]
                    break
        elif response is None and hasattr(self.api, "workflow_runs"):
            # GitHub commonly answers workflow_dispatch with 204. Correlate the
            # run by exact workflow, head SHA, and a narrow creation window;
            # never substitute an unrelated latest run.
            ref = request.ref or request.head
            is_sha = bool(re.fullmatch(r"[0-9a-fA-F]{40}", request.head))
            observed = self.api.workflow_runs(request.workflow, branch=None if is_sha else ref)
            rows = observed.get("workflow_runs", observed if isinstance(observed, list) else [])
            candidates = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_head = row.get("head_sha") or (row.get("head_commit") or {}).get("id")
                raw_created = row.get("created_at") or row.get("run_started_at") or row.get("createdAt")
                try:
                    created = datetime.fromisoformat(str(raw_created).replace("Z", "+00:00")) if raw_created else submitted_at
                    if created.tzinfo is None: created = created.replace(tzinfo=timezone.utc)
                    created = created.astimezone(timezone.utc)
                except (TypeError, ValueError):
                    continue
                exact_head = row_head == request.head if is_sha else row_head == request.head or row.get("head_branch") == request.head or row.get("head_branch") == ref
                if exact_head and created >= submitted_at - timedelta(seconds=10):
                    run_id = row.get("id") or row.get("run_id")
                    if run_id is not None:
                        candidates.append(row)
            if len(candidates) == 1:
                state["run_id"] = candidates[0].get("id") or candidates[0].get("run_id")
            elif len(candidates) > 1:
                raise RuntimeError("ambiguous workflow run correlation after 204 dispatch")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, sort_keys=True, indent=2), encoding="utf-8")
        return state

    def status(self) -> object:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if "run_id" not in state: return state
        return self.api.run(str(state["run_id"]))

    def wait(self, timeout: float = 3600.0, interval: float = 5.0) -> object:
        deadline = time.monotonic() + timeout
        while True:
            current = self.status()
            if isinstance(current, dict) and str(current.get("status", "")).lower() in {"completed", "success", "failure", "failed", "cancelled"}:
                return current
            if time.monotonic() >= deadline: raise TimeoutError("cloud run did not finish")
            time.sleep(interval)

    def fetch(self, destination: str | Path, *, artifact_name: str | None = None) -> dict[str, object]:
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        run_id = state.get("run_id")
        if run_id is None: raise ValueError("cloud fetch requires a correlated run_id")
        request = CloudRequest(**state["request"])
        payload = self.api.artifacts(run_id)
        rows = payload.get("artifacts", payload if isinstance(payload, list) else [])
        choices = [row for row in rows if isinstance(row, dict) and (artifact_name is None or row.get("name") == artifact_name)]
        if not choices: raise ValueError("no verification artifact found for run")
        artifact = choices[0]
        archive = self.api.download_artifact(artifact.get("id"))
        expected = artifact.get("sha256") or artifact.get("digest")
        if isinstance(archive, tuple):
            _status, headers, data = archive
            expected = expected or next((value for key, value in headers.items()
                                         if key.lower() in {"x-artifact-sha256", "x-checksum-sha256"}), None)
        else:
            data = archive
        if isinstance(expected, str) and expected.lower().startswith("sha256:"):
            expected = expected.split(":", 1)[1]
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qatlib-fetch-", dir=destination.parent) as staging:
            staged_zip = Path(staging) / "artifact.zip"
            staged_zip.write_bytes(data)
            result = unpack_bundle(staged_zip, Path(staging) / "tree", expected_sha256=expected)
            manifest_path = Path(staging) / "tree" / "manifest.json"
            if not manifest_path.is_file(): raise ValueError("verification bundle lacks manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema_version") != 1 or manifest.get("head_sha") != request.head:
                raise ValueError("verification manifest identity mismatch")
            entries = manifest.get("entries", {})
            if not isinstance(entries, dict): raise ValueError("verification manifest entries must be an object")
            for name, details in entries.items():
                normalized = str(name).replace("\\", "/")
                parts = normalized.rstrip("/").split("/")
                if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or any(part in {"", ".", ".."} for part in parts):
                    raise ValueError("verification manifest path traversal")
                target = (Path(staging) / "tree" / name).resolve()
                if not target.is_file() or not isinstance(details, dict): raise ValueError("invalid manifest entry")
                if target.stat().st_size != details.get("size") or hashlib.sha256(target.read_bytes()).hexdigest() != details.get("sha256"):
                    raise ValueError("verification manifest digest mismatch")
            if destination.exists():
                raise ValueError("fetch destination already exists; choose an empty path")
            os.replace(Path(staging) / "tree", destination)
        return {"artifact_id": artifact.get("id"), "destination": str(destination), **result}


def detect_platform(repository: str | None = None, configured: str = "auto") -> str:
    if configured in {"github", "gitea"}:
        return configured
    value = (repository or "").lower()
    return "gitea" if "gitea" in value or "forge" in value else "github"


def make_request(config: EnvConfig, *, head: str, policy: str = "auto", cold: bool = False,
                 memory_mib: int = 0, full_gate: bool = False) -> CloudRequest | None:
    mode = choose_mode(policy, cold=cold, memory_mib=memory_mib, full_gate=full_gate, pool_mib=config.local_pool_mib)
    if mode != "cloud":
        return None
    if not config.repository or not config.workflow:
        raise ValueError("cloud mode requires repository and workflow in config")
    ref = str(config.metadata.get("ref")) if config.metadata.get("ref") else None
    return CloudRequest(detect_platform(config.repository, config.cloud_platform), config.repository,
                        config.workflow, head, {}, ref)


def unpack_bundle(bundle: str | Path, destination: str | Path, *, expected_sha256: str | None = None,
                  max_files: int = 100000, max_bytes: int = 2_000_000_000) -> dict[str, object]:
    bundle, destination = Path(bundle), Path(destination)
    if expected_sha256:
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        if digest.lower() != expected_sha256.lower():
            raise ValueError("bundle digest mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    total = 0; names: list[str] = []
    with zipfile.ZipFile(bundle) as archive:
        for info in archive.infolist():
            name = info.filename
            target = (destination / name).resolve()
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ValueError("bundle symlink is forbidden")
            normalized = name.replace("\\", "/")
            path_parts = normalized.rstrip("/").split("/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or any(part in {"", ".", ".."} for part in path_parts):
                raise ValueError("bundle path traversal")
            if info.is_dir() or len(names) >= max_files or total + info.file_size > max_bytes:
                if not info.is_dir():
                    raise ValueError("bundle size limit exceeded")
                continue
            if name.endswith(("/", "\\")):
                target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if target.is_symlink():
                raise ValueError("bundle symlink is forbidden")
            names.append(name); total += info.file_size
    return {"files": len(names), "bytes": total, "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest()}


def receipt(path: str | Path, request: CloudRequest, *, build_verified: bool,
            artifact_available: bool, published: bool = False) -> Path:
    path = Path(path)
    path.write_text(json.dumps({"platform": request.platform, "repository": request.repository,
                                "workflow": request.workflow, "head": request.head,
                                "build_verified": build_verified,
                                "artifact_available": artifact_available,
                                "published": published}, sort_keys=True, indent=2), encoding="utf-8")
    return path

