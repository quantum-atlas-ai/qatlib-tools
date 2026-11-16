"""Explicit maintainer uploads of fully verified Lake revision caches.

Credential values are never included in diagnostics.  Local failure recovery
belongs to :mod:`qatlib_env.checkpoint` and cannot be published by this command.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlparse
import urllib.error
import urllib.request

R2_ENDPOINT = ""
REGION = "auto"
SERVICE = "s3"
# Compatibility registration point for callers; shared code owns no package names.
KEY_VARIABLES: dict[str, str] = {}
_KEY_VARIABLE = re.compile(r"LAKE_(?:[A-Z0-9_]+_)?CACHE_KEY\Z")
ARTIFACT_NAME = re.compile(r"[0-9a-fA-F]{16}\.ltar\Z")


def cache_endpoint(env: dict[str, str] | None = None) -> str:
    value = (os.environ if env is None else env).get("QATLAS_R2_ENDPOINT", "").strip()
    if not value:
        raise ValueError("QATLAS_R2_ENDPOINT must be configured outside the shared package")
    return value.rstrip("/")


def credential_variables(package_name: str, env: dict[str, str] | None = None,
                         *, project: Path | None = None) -> tuple[str, ...]:
    """Select explicit aliases or a package-specific name, never a generic fallback.

    QATLIB_CACHE_KEY_VARIABLES is a JSON map from package names to variable names
    (or ordered lists of names). It contains configuration, never credential values.
    A caller must explicitly opt a particular package into LAKE_CACHE_KEY.
    """
    if not package_name:
        return ()
    values = os.environ if env is None else env
    configured = KEY_VARIABLES.get(package_name)
    if project is not None:
        from .config import load_config
        configured = load_config(project, values).metadata.get("cache_key_variable", configured)
    aliases = json.loads(values.get("QATLIB_CACHE_KEY_VARIABLES", "{}"))
    if not isinstance(aliases, dict):
        raise ValueError("QATLIB_CACHE_KEY_VARIABLES must be a JSON object")
    configured = aliases.get(package_name, configured)
    if configured is None:
        stem = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", package_name)
        stem = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").upper()
        if not stem:
            raise ValueError("package name cannot form a credential variable")
        configured = "LAKE_" + stem + "_CACHE_KEY"
    variables = (configured,) if isinstance(configured, str) else tuple(configured) if isinstance(configured, list) else ()
    if not variables or not all(isinstance(name, str) and _KEY_VARIABLE.fullmatch(name) for name in variables):
        raise ValueError("cache credential aliases must be LAKE_<scope>_CACHE_KEY variable names")
    return tuple(dict.fromkeys(variables))


def package_environment(package_name: str, env: dict[str, str], *, project: Path | None = None) -> dict[str, str]:
    """Remove inherited native credentials before running a workspace or its deps."""
    result = dict(env)
    generic = result.pop("LAKE_CACHE_KEY", "").strip()
    variables = credential_variables(package_name, env, project=project)
    if variables == ("LAKE_CACHE_KEY",):
        stem = re.sub(r"[^A-Za-z0-9]+", "_", package_name).strip("_").upper()
        variables = ("LAKE_" + stem + "_CACHE_KEY", *variables)
    if package_name and variables != credential_variables(package_name, env):
        aliases = json.loads(result.get("QATLIB_CACHE_KEY_VARIABLES", "{}"))
        aliases[package_name] = list(variables)
        result["QATLIB_CACHE_KEY_VARIABLES"] = json.dumps(aliases)
    dedicated = next((name for name in variables if name != "LAKE_CACHE_KEY"), None)
    if "LAKE_CACHE_KEY" in variables and dedicated and not result.get(dedicated, "").strip() and generic:
        result[dedicated] = generic
    return result


def native_key_environment(env: dict[str, str], key: str) -> dict[str, str]:
    """Bind an already selected bucket credential only for one native child call."""
    result = dict(env)
    result.pop("LAKE_CACHE_KEY", None)
    if key.strip():
        result["LAKE_CACHE_KEY"] = key.strip()
    return result


def default_key_file(env: dict[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    explicit = values.get("QATLAS_R2_KEY_FILE", "").strip()
    return Path(explicit).expanduser() if explicit else Path(values.get("HOME", str(Path.home()))) / ".qatlib/secrets"


def _private_path(path: Path, *, directory: bool = False) -> None:
    if path.is_symlink():
        raise PermissionError(f"refusing symlinked R2 credential {'directory' if directory else 'file'}: {path}")
    info = path.stat()
    expected = 0o700 if directory else 0o600
    if os.name != "nt" and (stat.S_IMODE(info.st_mode) != expected or info.st_uid != os.getuid()):
        raise PermissionError(f"R2 credential {'directory' if directory else 'file'} must be owned by the current user with mode {oct(expected)}: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"R2 credential parent is not a directory: {path}")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"R2 credential path is not a regular file: {path}")


def parse_key_file(path: Path, *, check_permissions: bool = True) -> dict[str, str]:
    path = Path(path).expanduser()
    if not path.exists() and not path.is_symlink():
        return {}
    if check_permissions:
        _private_path(path.parent, directory=True)
        _private_path(path)
    entries = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" in line:
            name, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not _KEY_VARIABLE.fullmatch(name.strip()):
                raise ValueError("unsupported variable in R2 credential file")
            entries[name.strip()] = value
        elif ":" in line:
            entries.setdefault("LAKE_CACHE_KEY", line)
        else:
            raise ValueError("malformed R2 credential file (expected KEY=VALUE or AK:SK)")
    return entries


def _valid_key(value: str) -> bool:
    return bool(re.fullmatch(r"[^:\s=]+:[^:\s]+", value)) and len(value) >= 20


def save_key_file(path: Path, key_value: str, var_name: str = "LAKE_CACHE_KEY") -> None:
    """Save a canonical bucket key without relaxing existing path permissions."""
    if not _valid_key(key_value.strip()):
        raise ValueError("R2 write key must have format <AccessKeyId>:<SecretAccessKey>")
    if not _KEY_VARIABLE.fullmatch(var_name):
        raise ValueError("unsupported R2 credential variable")
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private_path(path.parent, directory=True)
    existing = parse_key_file(path)
    existing[var_name] = key_value.strip()
    # O_NOFOLLOW prevents replacing a symlink in the check/open interval.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("# Maintainer R2 write credentials; private mode 0600\n")
        stream.writelines(f"{name}={value}\n" for name, value in existing.items())
    _private_path(path)


def resolve_write_key(package_name: str, *, env: dict[str, str] | None = None,
                      key_file: Path | None = None, allow_prompt: bool = False,
                      persist_prompted: bool = True) -> str:
    """Resolve the bucket's permitted env names, then the selected private file."""
    values = os.environ if env is None else env
    candidates = credential_variables(package_name, values)
    variable = candidates[0]
    for name in candidates:
        value = values.get(name, "").strip()
        if value:
            if not _valid_key(value):
                raise ValueError(f"invalid R2 write credential in {name}; expected AK:SK")
            return value
    path = Path(key_file) if key_file is not None else default_key_file(values)
    entries = parse_key_file(path)
    for name in candidates:
        value = entries.get(name, "").strip()
        if value:
            if not _valid_key(value):
                raise ValueError(f"invalid R2 write credential in {path}; expected AK:SK")
            return value
    if allow_prompt and sys.stdin.isatty():
        value = getpass.getpass("R2 write key (<AK>:<SK>): ").strip()
        if not _valid_key(value):
            raise ValueError("invalid R2 write credential; expected AK:SK")
        if persist_prompted:
            save_key_file(path, value, variable)
        return value
    return ""


def detect_repo_profile(repo: Path) -> dict[str, object]:
    config = tomllib.loads((repo / "lakefile.toml").read_text(encoding="utf-8"))
    name = config["name"]
    tc = (repo / "lean-toolchain").read_text(encoding="utf-8").strip()
    from .config import load_config
    profile = load_config(repo).metadata
    library = str(profile.get("library", name))
    tests = str(profile["tests"]) if profile.get("tests") else None
    bucket = str(profile.get("cache_bucket", ""))
    slug = str(profile.get("cache_repository", profile.get("repository", "")))
    if "://" in slug:
        from urllib.parse import urlsplit
        slug = urlsplit(slug).path.strip("/").removesuffix(".git")
    if not bucket or not slug:
        bucket, slug = "", ""
    from .ci import source_inputs
    return {"package": name, "bucket": bucket, "repo_slug": slug,
            "build_args": ["-KCI", library] if tests is None else [library, tests],
            "tree_inputs": source_inputs(repo, library),
            "tc": tc, "tc_dir": tc.replace("/", "--").replace(":", "---")}


def resolve_artifacts_dirs(repo: Path, tc_raw: str, env: dict[str, str] | None = None) -> list[Path]:
    values = os.environ if env is None else env
    paths = []
    if values.get("LAKE_CACHE_DIR", "").strip():
        paths.append(Path(values["LAKE_CACHE_DIR"]).expanduser() / "artifacts")
    home = Path(values.get("HOME", str(Path.home())))
    elan = Path(values.get("ELAN_HOME", str(home / ".elan")))
    paths += [elan / "toolchains" / tc_raw.replace("/", "--").replace(":", "---") / "lake/cache/artifacts",
              repo / ".lake/cache/artifacts"]
    return list(dict.fromkeys(path.resolve() for path in paths if path.is_dir()))


def _run(repo: Path, env: dict[str, str], args: list[str], *, capture: bool = False):
    from . import workspace
    return workspace.run(args, cwd=repo, env=env, capture=capture)


def compute_input_tree(repo: Path, tree_inputs: list[str], env: dict[str, str] | None = None) -> str:
    output = _run(repo, dict(os.environ if env is None else env),
                  ["git", "rev-parse", *["HEAD:" + item for item in tree_inputs]], capture=True)
    return hashlib.sha256(output.encode()).hexdigest()


def _sign(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, body: bytes, content_type: str | None,
                  access_key: str, secret_key: str) -> dict[str, str]:
    parsed = urlparse(url)
    now = dt.datetime.now(dt.timezone.utc)
    date, timestamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
    payload = hashlib.sha256(body).hexdigest()
    values = {"host": parsed.netloc, "x-amz-content-sha256": payload, "x-amz-date": timestamp}
    if content_type:
        values["content-type"] = content_type
    names = ";".join(sorted(values))
    canonical = "\n".join([method, parsed.path, "", "".join(f"{key}:{values[key]}\n" for key in sorted(values)), names, payload])
    scope = f"{date}/{REGION}/{SERVICE}/aws4_request"
    signing = ("AWS4" + secret_key).encode()
    for value in (date, REGION, SERVICE, "aws4_request"):
        signing = _sign(signing, value)
    signature = _sign(signing, "\n".join(["AWS4-HMAC-SHA256", timestamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])).hex()
    headers = {"x-amz-content-sha256": payload, "x-amz-date": timestamp,
               "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={names}, Signature={signature}"}
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def s3_put_with_retry(url: str, body: bytes, content_type: str, access_key: str,
                      secret_key: str, max_attempts: int = 5) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            request = urllib.request.Request(url, data=body, method="PUT",
                headers=sigv4_headers("PUT", url, body, content_type, access_key, secret_key))
            with urllib.request.urlopen(request, timeout=60) as response:
                if 200 <= response.status < 300:
                    return
        except (OSError, urllib.error.URLError):
            pass
        if attempt != max_attempts:
            time.sleep(min(attempt * 0.5, 3))
    # Exception objects can include request headers: expose no raw exception.
    raise RuntimeError(f"R2 PUT failed after {max_attempts} attempts: {url}")


def s3_get_optional(url: str, access_key: str, secret_key: str) -> bytes | None:
    request = urllib.request.Request(url, method="GET",
        headers=sigv4_headers("GET", url, b"", None, access_key, secret_key))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read() or None
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise RuntimeError(f"R2 GET rejected with HTTP {error.code}: {url}") from None
    except (OSError, urllib.error.URLError):
        raise RuntimeError(f"R2 GET transport failed: {url}") from None


def _manifest(raw: bytes) -> tuple[str, dict[str, str]]:
    lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
    if not lines:
        raise ValueError("empty Lake output manifest")
    header = json.loads(lines[0])
    if not isinstance(header, str):
        raise ValueError("invalid Lake manifest header")
    rows = {}
    for line in lines[1:]:
        row = json.loads(line)
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str) or not row[0] \
                or not isinstance(row[1], str) or not ARTIFACT_NAME.fullmatch(row[1]):
            raise ValueError("invalid Lake artifact mapping")
        if row[0] in rows and rows[row[0]] != row[1]:
            raise ValueError("conflicting Lake artifact mappings")
        rows[row[0]] = row[1]
    return lines[0], rows


def find_baseline_ltars(repo: Path, profile: dict[str, object], access_key: str,
                        secret_key: str, env: dict[str, str] | None = None) -> set[str]:
    effective = dict(os.environ if env is None else env)
    base = f"{cache_endpoint(effective)}/{profile['bucket']}/revisions/{profile['repo_slug']}/tc/{profile['tc_dir']}"
    ancestors = _run(repo, effective, ["git", "rev-list", "--max-count=25", "HEAD"], capture=True).splitlines()
    for sha in ancestors:
        raw = s3_get_optional(f"{base}/{sha}.jsonl", access_key, secret_key)
        if raw:
            _, rows = _manifest(raw)
            if rows:
                return set(rows.values())
    return set()


def _source_identity(repo: Path, profile: dict[str, object], env: dict[str, str]) -> tuple[str, str]:
    # Reject staged, unstaged and untracked inputs, including ignored Lean
    # files: uncommitted sources must not be published under HEAD's identity.
    paths = list(profile["tree_inputs"])
    # The audit executable and supporting Python code must also be committed;
    # otherwise a locally weakened audit could certify HEAD's strong sources.
    validation_paths = [*paths, "scripts", "python"]
    status = _run(repo, env, ["git", "status", "--porcelain", "--untracked-files=all", "--", *validation_paths], capture=True)
    if status.strip():
        raise ValueError("cache publication requires committed, clean Lean source inputs")
    ignored = _run(repo, env, ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--", *validation_paths], capture=True)
    if any(line.endswith(".lean") for line in ignored.splitlines()):
        raise ValueError("cache publication refuses ignored, untracked Lean sources")
    head = _run(repo, env, ["git", "rev-parse", "HEAD"], capture=True).strip()
    return head, compute_input_tree(repo, paths, env)


class ArtifactHashMismatch(ValueError):
    """The native hash command succeeded but bytes differ from the descriptor."""


def _verify_artifact_hashes(repo: Path, paths: dict[str, Path], env: dict[str, str]) -> dict[str, str]:
    """Recompute Lake's native content hash, independently of stale .hash files."""
    names = sorted(paths)
    for name in names:
        if not re.fullmatch(r"[0-9a-fA-F]{16}(?:\.[A-Za-z0-9._-]*)?", name):
            raise ValueError("invalid Lake artifact descriptor")
    with tempfile.TemporaryDirectory(prefix="qatlas-artifact-audit-", dir=repo / ".lake") as temporary:
        root = Path(temporary)
        listing = root / "artifacts.txt"
        if any("\n" in str(paths[name]) for name in names):
            raise ValueError("artifact paths containing newlines are unsupported")
        # newline="\n": the Lean helper splits lines on "\n"; the default
        # Windows text-mode translation would leave "\r" on every path.
        listing.write_text("".join(str(paths[name]) + "\n" for name in names), newline="\n")
        helper = root / "hash.lean"
        helper.write_text('''import Lake
open Lake System
#eval show IO Unit from do
  let some listFile ← IO.getEnv "QATLAS_ARTIFACT_AUDIT_LIST" | throw (IO.userError "missing artifact list")
  for path in (← IO.FS.readFile listFile).splitOn "\\n" do
    unless path.isEmpty do
      let hash ← computeBinFileHash (FilePath.mk path)
      IO.println hash.hex
''')
        actual = _run(repo, {**env, "QATLAS_ARTIFACT_AUDIT_LIST": str(listing)},
                      ["lake", "--no-build", "lean", str(helper)], capture=True).splitlines()
        if actual != [name.split(".", 1)[0].lower() for name in names]:
            raise ArtifactHashMismatch("Lake artifact content hash does not match its manifest descriptor")
    # Keep a standard checksum for the interval between validation and upload.
    return {name: hashlib.sha256(paths[name].read_bytes()).hexdigest() for name in names}


def verified_upload_plan(repo: Path, env: dict[str, str], *, build: bool = False) -> dict:
    """Validate complete targets and kernel audit before constructing any upload."""
    from .ci import complete_outputs_manifest
    from .checkpoint import local_identity
    repo = Path(repo).resolve()
    profile = detect_repo_profile(repo)
    identity = _source_identity(repo, profile, env)
    files_identity = local_identity(repo)
    output = repo / ".lake/outputs.jsonl"
    if build:
        _run(repo, env, ["lake", "--fail-fast", "build", *profile["build_args"], "-o", str(output)])
    # A prior partial output file is not evidence.  Both complete build
    # freshness and the repository's kernel audit must succeed on this run.
    _run(repo, env, ["lake", "--no-build", "build", *profile["build_args"]])
    _run(repo, env, ["lake", "test"])
    if not output.is_file():
        raise ValueError("no output manifest; run cache upload --build first")
    complete_outputs_manifest(repo, output, Path(env["LAKE_CACHE_DIR"]), None, env)
    raw = output.read_bytes()
    _, rows = _manifest(raw)
    if not rows:
        raise ValueError("refusing to publish an empty revision manifest")
    artifacts = resolve_artifacts_dirs(repo, str(profile["tc"]), env)
    paths = {}
    for name in set(rows.values()):
        found = next((directory / name for directory in artifacts if (directory / name).is_file()), None)
        if found is None or found.is_symlink() or found.stat().st_size == 0:
            raise ValueError(f"missing or invalid Lake artifact: {name}")
        paths[name] = found
    checksums = _verify_artifact_hashes(repo, paths, env)
    if _source_identity(repo, profile, env) != identity or local_identity(repo) != files_identity:
        raise ValueError("source identity changed during cache verification")
    return {"profile": profile, "head": identity[0], "tree": identity[1],
            "manifest": raw, "artifacts": paths, "artifact_sha256": checksums,
            "files_identity": files_identity}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--build", action="store_true", help="Build complete targets before validating and uploading")
    parser.add_argument("--dry-run", action="store_true", help="Run all validation without credentials or R2 requests")
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--save-key", action="store_true", help="Explicitly prompt for and save a private maintainer key")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    repo = args.repo.resolve()
    profile = detect_repo_profile(repo)
    if args.save_key:
        save_key_file(args.key_file or default_key_file(), getpass.getpass("R2 write key (<AK>:<SK>): "),
                      credential_variables(str(profile["package"]), project=repo)[0])
        print("Saved private R2 credential file (mode 0600).")
        return 0
    from . import workspace
    env = package_environment(str(profile["package"]), workspace.environment(repo), project=repo)
    plan = verified_upload_plan(repo, env, build=args.build)
    print(f"Verified {profile['repo_slug']} HEAD={plan['head']} artifacts={len(plan['artifacts'])}")
    if args.dry_run:
        return 0
    key = resolve_write_key(str(profile["package"]), env=env, key_file=args.key_file)
    if not key:
        raise ValueError("R2 write credential missing; set the package environment variable or use cache upload --save-key")
    access, secret = key.split(":", 1)
    baseline = find_baseline_ltars(repo, profile, access, secret, env)
    base = f"{cache_endpoint(env)}/{profile['bucket']}"
    def upload(item):
        name, path = item
        if name not in baseline:
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != plan["artifact_sha256"][name]:
                raise ValueError(f"Lake artifact changed after validation: {name}")
            s3_put_with_retry(f"{base}/artifacts/{profile['repo_slug']}/{name.removesuffix('.ltar')}.art", body,
                              "application/vnd.reservoir.artifact", access, secret)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        list(executor.map(upload, plan["artifacts"].items()))
    from .checkpoint import local_identity
    if _source_identity(repo, profile, env) != (plan["head"], plan["tree"]) or local_identity(repo) != plan["files_identity"]:
        raise ValueError("source identity changed; artifacts uploaded but revision manifests withheld")
    revisions = f"{base}/revisions/{profile['repo_slug']}/tc/{profile['tc_dir']}"
    for target in (plan["head"] + ".jsonl", "inputs/" + plan["tree"] + ".jsonl"):
        s3_put_with_retry(f"{revisions}/{target}", plan["manifest"], "application/vnd.reservoir.outputs+json-lines", access, secret)
    print(f"Published verified revision manifests for {profile['repo_slug']} {plan['head']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


