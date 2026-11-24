"""Install and verify the exact Lean release selected by lean-toolchain."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import uuid
import zipfile

from qatlib_env import supervisor


def locked_toolchain(project: Path) -> str:
    toolchain = (project / "lean-toolchain").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"leanprover/lean4:v4\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", toolchain):
        raise ValueError("lean-toolchain must select an explicit Lean 4 release")
    return toolchain


def _run(argv, project, env, stage):
    result = supervisor.run(argv, cwd=project, env=env, capture_output=True,
                            text=True, errors="replace", timeout=1800)
    if result.returncode:
        status = re.search(r"\b(?:HTTP\s+|status code:\s*|returned error:\s*)([45]\d\d)\b",
                           result.stderr)
        detail = f"HTTP {status[1]}" if status else f"exit {result.returncode}"
        raise ValueError(f"Lean toolchain {stage} failed ({detail})")
    return result.stdout.strip()


def _verify(root: Path, toolchain: str, project: Path, env: dict) -> dict[str, str]:
    suffix = ".exe" if os.name == "nt" else ""
    lean, lake = (root / "bin" / (name + suffix) for name in ("lean", "lake"))
    version = toolchain.split(":v", 1)[1]
    if not lean.is_file() or not lake.is_file():
        raise ValueError("Lean archive or installation is missing the platform's Lean/Lake binaries")
    lean_version = _run([str(lean), "--version"], project, env, "Lean version check")
    lake_version = _run([str(lake), "--version"], project, env, "Lake version check")
    if (not re.search(rf"\bversion {re.escape(version)}(?:[,\s)]|$)", lean_version)
            or not re.search(rf"\bLean version {re.escape(version)}(?:[,\s)]|$)", lake_version)):
        raise ValueError(f"Lean or Lake version differs from the locked release v{version}")
    sysroot = Path(_run([str(lean), "--print-prefix"], project, env, "sysroot check"))
    if sysroot.resolve() != root.resolve():
        raise ValueError("Lean reported a sysroot outside the selected toolchain")
    return {"lean": lean_version, "lake": lake_version, "sysroot": str(sysroot)}


def _extract(stream, archive: Path, target: Path, env: dict) -> None:
    if archive.suffix.casefold() == ".zip":
        with zipfile.ZipFile(stream) as source:
            for member in source.infolist():
                name = member.filename
                if (name.startswith("/") or re.search(r"[\\:]", name)
                        or ".." in name.split("/") or (member.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError("Lean zip archive contains an unsafe path")
            source.extractall(target)
        return
    if not archive.name.endswith(".tar.zst"):
        raise ValueError("Lean archive must be an official .zip or .tar.zst")
    decoder = shutil.which("zstd", path=env.get("PATH", ""))
    if not decoder:
        raise ValueError("Lean .tar.zst import requires zstd on PATH")
    with subprocess.Popen([decoder, "-dc"], stdin=stream, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, env=env) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as source:
                source.extractall(target, filter="data")
            # Drain the tar padding before waiting for the decompressor.
            while process.stdout.read(65536):
                pass
        except BaseException:
            process.kill()
            raise
        if process.wait():
            raise ValueError("Lean archive decompression failed")


def install(project: Path, env: dict, *, archive: Path | None, digest: str | None) -> dict[str, str]:
    from qatlib_env.workspace import cache_lock, remove_derived

    toolchain = locked_toolchain(project)
    if (archive is None) != (digest is None) or (digest is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
        raise ValueError("--archive requires --sha256 with the official release asset digest")
    if env.get("ELAN_TOOLCHAIN", toolchain) != toolchain:
        raise ValueError("ELAN_TOOLCHAIN differs from lean-toolchain")
    home = Path(env.get("ELAN_HOME") or Path(env.get("USERPROFILE" if os.name == "nt" else "HOME",
                                                   str(Path.home()))) / ".elan").resolve()
    child = {key: value for key, value in env.items() if key not in {"ELAN_DIST_SERVER", "ELAN_UPDATE_ROOT"}}
    child.update(ELAN_HOME=str(home), ELAN_TOOLCHAIN=toolchain)
    elan = shutil.which("elan", path=child.get("PATH", ""))
    if not elan:
        raise ValueError("install Elan from the official releases and put elan on PATH")
    slot = home / "toolchains" / toolchain.replace("/", "--").replace(":", "---")
    if archive is None:
        _run([elan, "toolchain", "install", toolchain], project, child,
             "installation from the official Elan source")
        lean_path = Path(_run([elan, "which", "lean"], project, child, "Elan selection"))
        result = _verify(lean_path.parent.parent, toolchain, project, child)
        return {"toolchain": toolchain, "source": "elan", **result}

    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest.lower():
            raise ValueError("Lean archive SHA-256 mismatch; obtain the official archive and asset digest")
        stream.seek(0)
        with cache_lock(slot.parent):
            source = "existing"
            if not slot.exists():
                stage = slot.parent / f".qatlib-import-{uuid.uuid4().hex}"
                stage.mkdir(mode=0o700)
                try:
                    _extract(stream, archive, stage, child)
                    roots = list(stage.iterdir())
                    if len(roots) != 1 or not roots[0].is_dir() or roots[0].is_symlink():
                        raise ValueError("Lean archive must contain one platform toolchain directory")
                    _verify(roots[0], toolchain, project, child)
                    roots[0].rename(slot)
                    source = "verified-archive"
                except (tarfile.TarError, zipfile.BadZipFile):
                    raise ValueError("Lean archive is invalid or truncated") from None
                finally:
                    remove_derived(stage, env)
            selected = Path(_run([elan, "which", "lean"], project, child, "Elan selection"))
            if selected.parent.parent.resolve() != slot.resolve():
                raise ValueError("Elan did not select the exact locked toolchain directory")
            result = _verify(slot, toolchain, project, child)
    return {"toolchain": toolchain, "source": source,
            "archive_sha256": actual, **result}
