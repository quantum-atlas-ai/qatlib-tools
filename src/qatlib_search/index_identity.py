"""Committed index inputs from Git object IDs, without hashing source contents."""
from pathlib import Path
import os
import subprocess
import tomllib
from qatlib_env.git_identity import assert_clean_git_sources

DEFAULT_MODULE_ROOTS = ()
DEFAULT_AREAS = ('Foundations', 'Concepts', 'Papers', 'Research')


def module_roots(project: Path | None = None) -> tuple[str, ...]:
    """Read module roots from explicit config, with a public generic fallback."""
    raw = os.environ.get('QATLIB_SEARCH_MODULE_ROOTS', '')
    if raw:
        values = tuple(part.strip() for part in raw.split(',') if part.strip())
        if values:
            return values
    if project is not None:
        for name in ('qatlib-env.toml', 'pyproject.toml', '.qatlib-env.local.toml'):
            path = Path(project) / name
            try:
                data = tomllib.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            section = data.get('tool', {}).get('qatlib-env', {}) if name == 'pyproject.toml' else data.get('tool', {}).get('qatlib-env', data)
            values = section.get('search_module_roots') if isinstance(section, dict) else None
            if isinstance(values, list) and values:
                return tuple(str(value) for value in values)
        from qatlib_env.project import source_directories
        return source_directories(Path(project))
    return DEFAULT_MODULE_ROOTS


MODULE_ROOTS = module_roots()
AREAS = tuple(f'{root}/{area}' for root in MODULE_ROOTS for area in DEFAULT_AREAS)


def git_output(root: Path, arguments: list[str]) -> bytes:
    """Read this checkout's own objects, independent of the caller's Git environment."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0')
    return subprocess.check_output(['git', *arguments], cwd=root, env=environment, timeout=10, stderr=subprocess.PIPE)


def snapshot(root: Path, paths: list[str]) -> dict[str, str]:
    root = Path(root).resolve()
    top = git_output(root, ['rev-parse', '--show-toplevel']).decode('utf-8').strip()
    if Path(top).resolve() != root:
        raise ValueError('index requires its own committed Git checkout')
    head = assert_clean_git_sources(root, paths=paths, reject_untracked=True)
    raw = git_output(root, ['ls-tree', '-rz', head, '--', *paths])
    result = {}
    for row in raw.decode('utf-8').split('\0'):
        if not row:
            continue
        metadata, path = row.split('\t', 1)
        mode, kind, oid = metadata.split()
        if kind != 'blob' or mode not in {'100644', '100755'}:
            raise ValueError('index input is not a regular tracked file: ' + path)
        result[path] = oid
    if git_output(root, ['rev-parse', '--verify', 'HEAD^{commit}']).decode().strip() != head:
        raise ValueError('source HEAD changed during index identity check')
    return result


def blob(root: Path, relative: str) -> str | None:
    return snapshot(root, [relative]).get(relative)
