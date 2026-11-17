"""Bounded local Lake corpus discovery. Reads Git objects; never invokes Lake."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

from .doc_references import parse_bibliography
from .paths import project_root


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1',
               GIT_NO_LAZY_FETCH='1', GIT_ALLOW_PROTOCOL='')
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', *args], cwd=root,
                            env=env, capture_output=True, timeout=15)
    if result.returncode:
        raise ValueError(f'{root}: cannot inspect local Git identity')
    return result.stdout.decode('utf-8')


def contained(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or '..' in Path(relative).parts or not path.resolve().is_relative_to(root):
        raise ValueError(f'path leaves source repository: {relative}')
    return path


@dataclass
class Corpus:
    project: Path
    repos: list[dict]
    inputs: dict[str, str]
    bibliography: set[str]
    warnings: list[str]

    @property
    def key(self) -> str:
        return digest(str(self.project).encode())[:24]

    def files(self):
        """Yield regular files only in the selected default libraries, never .lake."""
        for repo in self.repos:
            root = Path(repo['root'])
            seen = set()
            for src_dir, names in repo['libraries']:
                source = contained(root, src_dir)
                for name in names:
                    stem = contained(source, name.replace('.', '/'))
                    paths = ([stem.with_suffix('.lean')] if stem.with_suffix('.lean').is_file() else [])
                    paths += sorted(stem.rglob('*.lean')) if stem.is_dir() else []
                    for path in paths:
                        relative = path.relative_to(root).as_posix()
                        if relative in seen:
                            continue
                        seen.add(relative)
                        # rglob does not traverse directory symlinks. The selected
                        # source root was checked above; reject leaf links below.
                        if path.is_symlink() or any(p.startswith('.') for p in path.relative_to(source).parts):
                            raise ValueError(f'hidden or symlink source is not admitted: {path}')
                        yield repo, relative, path.relative_to(source).with_suffix('').as_posix().replace('/', '.')


def discover(project: str | Path | None = None) -> Corpus:
    root = project_root(project)
    inputs: dict[str, str] = {}
    warnings: list[str] = []
    repos: list[dict] = []
    bib: set[str] = set()

    def read(path: Path, owner: Path) -> bytes:
        if path.is_symlink() or not path.resolve().is_relative_to(owner):
            raise ValueError(f'symlink input is not admitted: {path}')
        data = path.read_bytes() if path.is_file() else b''
        inputs[str(path)] = digest(data)
        return data

    def add(path: Path, pin: str | None = None) -> None:
        path = path.resolve()
        config_path = path / 'lakefile.toml'
        try:
            config = tomllib.loads(read(config_path, path).decode('utf-8'))
        except (ValueError, UnicodeError) as error:
            raise ValueError(f'{config_path}: invalid Lake TOML') from error
        libs = config.get('lean_lib', [])
        defaults = config.get('defaultTargets', [])
        selected = [lib for lib in libs if lib.get('name') in defaults]
        if not selected and len(libs) == 1:
            selected = libs
        if not selected:
            raise ValueError(f'{config_path}: set defaultTargets to an unambiguous Lean library')
        layouts = []
        for lib in selected:
            directory = lib.get('srcDir', config.get('srcDir', '.'))
            names = lib.get('roots', [lib.get('name')])
            if not isinstance(directory, str) or not isinstance(names, list) or not names or not all(
                    isinstance(n, str) and re.fullmatch(r'[\w]+(?:\.[\w]+)*', n) for n in names):
                raise ValueError(f'{config_path}: unsupported library layout')
            contained(path, directory)
            layouts.append((directory, names))
        try:
            top = Path(git(path, 'rev-parse', '--show-toplevel').strip()).resolve()
            if top != path:
                raise ValueError('not its own checkout')
            revision = git(path, 'rev-parse', '--verify', 'HEAD^{commit}').strip()
            tree = git(path, 'rev-parse', revision + '^{tree}').strip()
        except ValueError:
            if pin or (path / '.git').exists():
                raise ValueError(f'{path}: cannot read the repository Git identity offline') from None
            revision, tree = None, None
        if pin and revision != pin:
            raise ValueError(f'{path}: checkout {revision} differs from locked revision {pin}')
        read(path / 'lean-toolchain', path)
        read(path / 'lake-manifest.json', path)
        bib_text = read(path / 'docs/references.bib', path).decode('utf-8')
        if bib_text:
            keys, errors = parse_bibliography(bib_text)
            if errors:
                raise ValueError(f'{path}: invalid root bibliography: {errors[0].message}')
            bib.update(keys)
        repos.append(dict(repo=config.get('name', path.name), root=str(path), revision=revision,
                          tree=tree, locked=bool(pin), libraries=layouts))

    add(root)
    manifest_path = root / 'lake-manifest.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        packages = contained(root, manifest.get('packagesDir', '.lake/packages'))
        for package in manifest.get('packages', []):
            if package.get('inherited', False):
                continue
            name = package.get('name', '')
            if not re.fullmatch(r'[\w.-]+', name) or name in {'.', '..'}:
                raise ValueError('invalid manifest package name')
            path = packages / name
            subdir = package.get('subDir')
            if subdir:
                raise ValueError(f'{name}: Git subDir dependencies are not supported')
            if package.get('type') != 'git':
                warnings.append(f'{name}: dependency has no exact Git lock; omitted')
                continue
            pin = package.get('rev', '')
            if not re.fullmatch(r'[0-9a-f]{40}', pin):
                raise ValueError(f'{name}: dependency requires a full Git revision')
            if not path.is_dir() or not (path / 'lakefile.toml').is_file():
                warnings.append(f'{name}: local TOML library unavailable; omitted (no fetch)')
                continue
            add(path, pin)
    return Corpus(root, repos, inputs, bib, warnings)


def default_db(corpus: Corpus) -> Path:
    cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')).expanduser()
    return cache / 'qatlib/retrieve' / (corpus.key + '.sqlite3')


def check_destination(path: Path, corpus: Corpus) -> None:
    """An override may not put the cache (or its SQLite journals) into the corpus."""
    for repo in corpus.repos:
        if path.resolve().is_relative_to(Path(repo['root'])):
            raise ValueError('--db must be outside every source repository')
