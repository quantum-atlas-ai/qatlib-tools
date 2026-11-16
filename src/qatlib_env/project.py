"""Read Lake's project identity and source layout without running Lake."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tomllib


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


def repository_slug(project: Path) -> str:
    from qatlib_forge.repo_api import RepoRef
    # Read canonical identity, before transport-only url.insteadOf rewrites.
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    result = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=project,
                            env=environment, capture_output=True, text=True)
    if result.returncode:
        return ""
    try:
        ref = RepoRef.parse(result.stdout.strip())
        return f"{ref.owner}/{ref.repo}"
    except ValueError:
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
