"""Resolve canonical Lean paths across Dev and pinned Frozen, without building.

Planning lives in Dev. Mathematical source paths retain their module owner;
Paths retain their exact module root; no legacy alias or cross-root fallback is used.
Only source text and Git identity are inspected, never compiled cache contents.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tomllib
from qatlib_env.git_identity import assert_clean_git_sources

LEAN_ROOTS = ()


def configured_lean_roots(project: Path | None = None) -> tuple[str, ...]:
    raw = os.environ.get('QATLIB_SEARCH_SOURCE_ROOTS', '')
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
            values = section.get('search_source_roots') if isinstance(section, dict) else None
            if isinstance(values, list) and values:
                return tuple(str(value) for value in values)
        from qatlib_env.project import source_directories
        return source_directories(Path(project))
    return LEAN_ROOTS


def lean_path(value: object, *, roots: tuple[str, ...] = LEAN_ROOTS) -> bool:
    if not isinstance(value, str) or not value.endswith('.lean'):
        return False
    path = PurePosixPath(value)
    return (not path.is_absolute() and any(value == n + '.lean' or value.startswith(n + '/') for n in roots)
            and not any(part in {'', '.', '..'} for part in value.split('/'))
            and not any(c in value for c in '\\:\x00'))


@dataclass(frozen=True)
class Source:
    owner: str
    root: Path
    relative: str

    @property
    def path(self) -> Path:
        return self.root / self.relative

    @property
    def module(self) -> str:
        return self.relative[:-5].replace('/', '.')


class SourceCatalog:
    """One bounded source observation; external frozen sources must match the pin."""
    def __init__(self, root: Path, *, lean_roots: tuple[str, ...] | None = None):
        self.root = Path(root).resolve()
        self.lean_roots = lean_roots or configured_lean_roots(self.root)
        self.roots = {'local': self.root}
        self._checked: set[Path] = set()
        self._tracked: dict[Path, set[str]] = {}
        self.frozen_pin = None
        self.upstream_roots = ()
        manifest = self.root / 'lake-manifest.json'
        if manifest.is_file():
            data = json.loads(manifest.read_text())
            selected = os.environ.get('QATLIB_SOURCE_PACKAGE')
            candidates = [p for p in data.get('packages', []) if p.get('name') == selected
                          or selected is None and not p.get('inherited', False)]
            dependency = candidates[0] if len(candidates) == 1 else None
            if dependency:
                self.frozen_pin = dependency['rev']
                self.roots['frozen'] = self.root / data.get('packagesDir', '.lake/packages') / dependency['name']
        if os.environ.get('QATLAS_FROZEN_ROOT'):
            self.roots['frozen'] = Path(os.environ['QATLAS_FROZEN_ROOT']).expanduser().resolve()
        if 'frozen' in self.roots and self.roots['frozen'].is_dir():
            self.upstream_roots = configured_lean_roots(self.roots['frozen'])
            self.lean_roots = tuple(dict.fromkeys((*self.lean_roots, *self.upstream_roots)))

    def validate_root(self, owner: str, root: Path) -> None:
        if root in self._checked:
            return
        if owner == 'frozen' and self.frozen_pin:
            try:
                root_name = next((name for name in self.lean_roots if (root / name).is_dir()), None)
                if root_name is None:
                    raise ValueError('configured frozen source root is missing')
                frozen_paths = (root_name, root_name + '.lean')
                head = assert_clean_git_sources(root, frozen_paths,
                                                expected_head=self.frozen_pin, reject_untracked=True)
                dirty = ''
            except (OSError, subprocess.CalledProcessError) as error:
                raise ValueError('cannot inspect the pinned frozen source checkout') from error
            except ValueError as error:
                raise ValueError('frozen sources differ from the exact dependency pin or are modified: ' + str(error)) from error
            if head != self.frozen_pin or dirty:
                raise ValueError('frozen sources differ from the exact dependency pin or are modified')
        self._checked.add(root)

    def candidates(self, value: str) -> list[Source]:
        if not lean_path(value, roots=self.lean_roots):
            raise ValueError(f'invalid Lean source path: {value!r}')
        is_frozen = any(value == root + '.lean' or value.startswith(root + '/') for root in self.upstream_roots)
        if is_frozen and 'frozen' in self.roots:
            return [Source('frozen', self.roots['frozen'], value)]
        return [Source('local', self.root, value)]

    def resolve(self, value: str) -> Source:
        for source in self.candidates(value):
            if not source.path.is_file():
                continue
            if not source.path.resolve().is_relative_to(source.root.resolve()):
                raise ValueError('source path leaves its checkout through a symlink')
            self.validate_root(source.owner, source.root)
            return source
        raise ValueError(f'source unavailable: {value}; prepare the owning checkout or locked Frozen sources')

    def tracked(self, source: Source) -> bool:
        if source.root not in self._tracked:
            result = subprocess.run(['git', 'ls-files', '-z'], cwd=source.root,
                                    text=True, capture_output=True)
            self._tracked[source.root] = set(result.stdout.split('\0')) if result.returncode == 0 else set()
        paths = self._tracked[source.root]
        # Isolated, non-Git planning proposals preserve the original checker contract.
        return (not paths and source.owner == 'local') or source.relative in paths
