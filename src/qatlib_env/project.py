"""Read Lake's project identity and source layout without running Lake."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class Library:
    name: str
    src_dir: Path
    roots: tuple[str, ...]
    default: bool


def lake_config(project: Path) -> dict:
    path = project / "lakefile.toml"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"{path}: cannot read Lake configuration: {error}") from error


def libraries(project: Path) -> tuple[Library, ...]:
    config = lake_config(project)
    defaults = config.get("defaultTargets", [])
    result = []
    for lib in config.get("lean_lib", []):
        name = lib.get("name")
        roots = lib.get("roots", [name])
        directory = Path(lib.get("srcDir", config.get("srcDir", ".")))
        if (not isinstance(name, str) or not name or not isinstance(roots, list)
                or not roots or not all(isinstance(root, str) and root for root in roots)
                or directory.is_absolute() or ".." in directory.parts):
            raise ValueError(f"{project / 'lakefile.toml'}: invalid lean_lib source layout")
        result.append(Library(name, directory, tuple(roots), name in defaults))
    if not result:
        raise ValueError(f"{project / 'lakefile.toml'}: no lean_lib declarations")
    return tuple(result)


def primary_library(project: Path) -> Library:
    libs = libraries(project)
    selected = [lib for lib in libs if lib.default]
    if not selected and len(libs) == 1:
        selected = list(libs)
    if len(selected) != 1:
        raise ValueError("ambiguous library: select a root explicitly or set one Lake default library")
    return selected[0]


def source_root(project: Path) -> str:
    """Select one declared source root; support isolated source-only fixtures."""
    if (project / "lakefile.toml").is_file():
        lib = primary_library(project)
        if len(lib.roots) != 1:
            raise ValueError("library has several roots; select one explicitly")
        return (lib.src_dir / lib.roots[0].replace('.', '/')).as_posix()
    roots = [path.name for path in project.iterdir() if path.is_dir()
             and not path.name.startswith('.') and any(path.rglob('*.lean'))]
    if not roots:
        roots = [path.name for path in project.iterdir() if path.is_dir()
                 and not path.name.startswith('.')]
    if len(roots) != 1:
        raise ValueError("cannot infer one source root; provide it explicitly")
    return roots[0]


def source_directories(project: Path) -> tuple[str, ...]:
    """Physical source roots, excluding dependency/build and documentation trees."""
    if (project / "lakefile.toml").is_file() and lake_config(project).get("lean_lib"):
        return tuple(dict.fromkeys((lib.src_dir / name.replace('.', '/')).as_posix()
                                   for lib in libraries(project) for name in lib.roots))
    return tuple(sorted(path.name for path in project.iterdir() if path.is_dir()
                        and not path.name.startswith('.') and any(path.rglob('*.lean'))))


def _git_config_values(project: Path) -> tuple[str, ...]:
    """Read configured remote URLs without applying transport rewrites."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    try:
        result = subprocess.run(
            ["git", "config", "--get-regexp", r"^remote\..*\.url$"],
            cwd=project, env=environment, capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode:
        return ()
    values = []
    for line in result.stdout.splitlines():
        _, separator, value = line.partition(" ")
        if separator and value.strip():
            values.append(value.strip())
    return tuple(values)


def _local_origin_path(value: str, base: Path) -> Path | None:
    """Resolve a filesystem remote while leaving scp-style URLs untouched."""
    value = value.strip()
    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.netloc not in {"", "localhost"}:
            return None
        return Path(unquote(parsed.path)).expanduser().resolve()
    if "://" in value or re.match(r"^[A-Za-z]:[\\/]", value):
        return None
    if value.startswith(("/", "./", "../", "~")) or ":" not in value:
        path = Path(value).expanduser()
        return (base / path if not path.is_absolute() else path).resolve()
    return None


def _manifest_repository_urls(project: Path, package_names: set[str]) -> tuple[str, ...]:
    """Find locked dependency URLs for a checkout or its local mirror."""
    urls = []
    try:
        resolved = project.resolve()
    except OSError:
        resolved = project.absolute()
    for ancestor in (resolved, *resolved.parents):
        manifest = ancestor / "lake-manifest.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        packages = data.get("packages", [])
        if not isinstance(packages, list):
            continue
        packages_dir = data.get("packagesDir", ".lake/packages")
        try:
            package_root = (ancestor / packages_dir).resolve()
            relative = resolved.relative_to(package_root)
        except (OSError, TypeError, ValueError):
            relative = None
        names = set(package_names)
        if relative is not None and len(relative.parts) == 1:
            names.add(relative.parts[0].strip("«»"))
        for package in packages:
            if not isinstance(package, dict):
                continue
            name = str(package.get("name", "")).strip("«»")
            url = package.get("url")
            if name in names and isinstance(url, str) and url.strip():
                urls.append(url.strip())
    return tuple(dict.fromkeys(urls))


def _repository_slug_from_origin(project: Path, value: str, seen: set[Path]) -> str:
    from qatlib_forge.repo_api import RepoRef
    try:
        ref = RepoRef.parse(value)
        return f"{ref.owner}/{ref.repo}"
    except ValueError:
        pass
    local = _local_origin_path(value, project)
    if local is None:
        return ""
    package_names = {project.name, local.name}
    package_lakefile = project / "lakefile.toml"
    try:
        package_lake = tomllib.loads(package_lakefile.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        package_lake = {}
    if isinstance(package_lake.get("name"), str):
        package_names.add(package_lake["name"].strip("«»"))
    for candidate in _manifest_repository_urls(project, package_names):
        slug = _repository_slug_from_origin(project, candidate, seen)
        if slug:
            return slug
    if local in seen:
        return ""
    seen.add(local)
    for candidate in _git_config_values(local):
        slug = _repository_slug_from_origin(local, candidate, seen)
        if slug:
            return slug
    return ""


def repository_slug(project: Path) -> str:
    """Return a canonical repository slug, including for local mirror origins."""
    for origin in dict.fromkeys(_git_config_values(project)):
        slug = _repository_slug_from_origin(project, origin, set())
        if slug:
            return slug
    return ""


def inferred_profile(project: Path) -> dict:
    """Lake owns targets; repository identity comes from the checkout's origin."""
    path = project / "lakefile.toml"
    if not path.is_file():
        return {}
    if not lake_config(project).get("lean_lib"):
        return {}
    libs = libraries(project)
    values = {"source_roots": list(dict.fromkeys(str(lib.src_dir / root.replace('.', '/'))
                                                for lib in libs for root in lib.roots))}
    primary = [lib for lib in libs if lib.default]
    if not primary and len(libs) == 1:
        primary = list(libs)
    if len(primary) == 1:
        values["library"] = primary[0].name
        auxiliary = [lib for lib in libs if not lib.default and lib.src_dir != primary[0].src_dir]
        if len(auxiliary) == 1:
            values["tests"] = auxiliary[0].name
    slug = repository_slug(project)
    if slug:
        values["repository"] = slug
    # Workflow selection is explicit when several build workflows coexist.
    workflows = sorted({path.name for directory in (".github", ".gitea")
                        for path in (project / directory / "workflows").glob("build.y*ml")})
    if len(workflows) == 1:
        values["workflow"] = workflows[0]
    return values
