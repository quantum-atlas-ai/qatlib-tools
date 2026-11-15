"""Read-only promotion inventories, exact source locks and duplicate checks.

This stdlib module is independent of Dev metadata and can be mirrored into research.
Call verify_locked_source (or verify_source_revision for an explicit rehearsal)
before check_duplicates. Neither inspection nor validation fetches dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .lean_declarations import IDENT_RE, declaration_kinds
from .lean_source import LEAN_ID_REST, mask_comments

PUBLIC_LAYERS = ('Foundations', 'Concepts', 'Papers')
_ROOT = re.compile(r'[A-Za-z][A-Za-z0-9_]*\Z')
_REV = re.compile(r'[0-9a-f]{40}\Z')


def _git(repo: Path, *args: str) -> bytes:
    env = dict(os.environ, GIT_NO_LAZY_FETCH='1', GIT_OPTIONAL_LOCKS='0', GIT_NO_REPLACE_OBJECTS='1')
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR'):
        env.pop(key, None)
    result = subprocess.run(['git', '-C', str(repo), *args], env=env,
                            capture_output=True)
    if result.returncode:
        raise ValueError(f'{repo}: git {args[0]} failed: '
                         + result.stderr.decode(errors='replace').strip())
    return result.stdout


def _tree(repo: Path, root: str) -> tuple[Path, Path]:
    if not isinstance(root, str) or not _ROOT.fullmatch(root):
        raise ValueError(f'invalid source root: {root!r}')
    repo = repo.resolve()
    tree = repo / root
    if not tree.is_dir() or tree.resolve() != tree:
        raise ValueError(f'missing source tree or redirected root: {tree}')
    return repo, tree


def _source(repo: Path, relative: str, tree: Path) -> Path:
    path = repo / relative
    if (not path.is_relative_to(tree) or path.resolve() != path
            or not path.is_file() or path.suffix != '.lean'):
        raise ValueError(f'missing or redirected Lean source: {path}')
    return path


def source_files(repo: Path, root: str) -> list[Path]:
    """List tracked Lean sources, or filesystem fixtures without a Git root.

    Persistent checkouts may contain untracked stale files: they are never
    providers. Git errors and missing tracked files fail instead of falling back.
    """
    repo, tree = _tree(repo, root)
    if (repo / '.git').exists():
        top = Path(os.fsdecode(_git(repo, 'rev-parse', '--show-toplevel')).strip()).resolve()
        if top != repo:
            raise ValueError(f'{repo}: source repository is not its Git top level')
        relatives = [os.fsdecode(item) for item in
                     _git(repo, 'ls-files', '-z', '--', root + '/').split(b'\0') if item]
    else:
        relatives = [path.relative_to(repo).as_posix() for path in tree.rglob('*.lean')]
    files = []
    for relative in sorted(set(relatives)):
        parts = Path(relative).relative_to(root).parts
        if not relative.endswith('.lean') or any(p.startswith('.') or p == 'archive' for p in parts):
            continue
        files.append(_source(repo, relative, tree))
    return files


def locked_revision(consumer_repo: Path, package: str = 'Qatlib', *, allow_inherited: bool = False) -> str:
    """Require one full direct Git pin, unless inherited pins are opted into."""
    manifest = consumer_repo / 'lake-manifest.json'
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
        packages = data.get('packages') if isinstance(data, dict) else None
        if not isinstance(packages, list):
            raise ValueError('packages must be an array')
        entries = [entry for entry in packages if isinstance(entry, dict)
                   and entry.get('name') == package]
        if len(entries) != 1:
            raise ValueError(f'expected one {package} dependency, found {len(entries)}')
        entry = entries[0]
        if entry.get('inherited') and not allow_inherited:
            raise ValueError(f'{package} is inherited; a direct dependency lock is required')
        rev = entry.get('rev')
        if entry.get('type', 'git') != 'git' or not isinstance(rev, str) or not _REV.fullmatch(rev):
            raise ValueError(f'{package} must have a full 40-character Git rev')
        return rev
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f'{manifest}: invalid dependency lock: {error}') from error


def verify_source_revision(source_repo: Path, expected_rev: str, root: str = 'Qatlib') -> str:
    """Require an independent checkout and tracked source bytes at this SHA.

    This also rejects bare/object-only mirrors, dirty source files, symlinks,
    missing objects and staged source additions. Untracked stale files are not
    scanned or accepted as source providers. Nothing is fetched or checked out.
    """
    if not isinstance(expected_rev, str) or not _REV.fullmatch(expected_rev):
        raise ValueError(f'expected revision must be a full 40-character Git SHA: {expected_rev!r}')
    repo, tree = _tree(source_repo, root)
    top = Path(os.fsdecode(_git(repo, 'rev-parse', '--show-toplevel')).strip()).resolve()
    if top != repo:
        raise ValueError(f'{repo}: selected sources are not an independent Git checkout')
    actual = _git(repo, 'rev-parse', 'HEAD').decode().strip()
    if actual != expected_rev:
        raise ValueError(f'{repo}: expected {expected_rev}, actual HEAD {actual}')
    entries = _git(repo, 'ls-tree', '-r', '-z', expected_rev, '--', root + '/')
    expected: dict[Path, str] = {}
    for entry in entries.split(b'\0'):
        if not entry:
            continue
        metadata, raw_path = entry.split(b'\t', 1)
        relative = os.fsdecode(raw_path)
        if not relative.endswith('.lean'):
            continue
        mode, kind, oid = metadata.split()
        if mode not in (b'100644', b'100755') or kind != b'blob':
            raise ValueError(f'{repo / relative}: expected a regular Lean source blob')
        path = _source(repo, relative, tree)
        data = path.read_bytes()
        actual_oid = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if actual_oid != oid.decode():
            raise ValueError(f'{path}: source bytes differ from locked revision {expected_rev}')
        expected[path] = oid.decode()
    if not expected:
        raise ValueError(f'{repo}: revision {expected_rev} has no {root} Lean sources')
    active = {path for path in expected if not any(
        part.startswith('.') or part == 'archive' for part in path.relative_to(tree).parts)}
    if set(source_files(repo, root)) != active:
        raise ValueError(f'{repo}: tracked source inventory differs from revision {expected_rev}')
    return expected_rev


def verify_locked_source(consumer_repo: Path, source_repo: Path, *,
                         package: str = 'Qatlib', root: str = 'Qatlib',
                         allow_inherited: bool = False) -> str:
    """Check a direct lock by default; legacy inherited pins require opt-in."""
    return verify_source_revision(source_repo, locked_revision(
        consumer_repo, package, allow_inherited=allow_inherited), root)


def theorem_candidates(repo: Path, *, source_root: str = 'Project',
                       allowed_layers: tuple[str, ...] = PUBLIC_LAYERS) -> tuple[str, ...]:
    """Select every theorem/lemma file, including private declarations; no rank filter."""
    repo = repo.resolve()
    candidates = []
    for path in source_files(repo, source_root):
        relative = path.relative_to(repo)
        if len(relative.parts) < 3 or relative.parts[1] not in allowed_layers:
            continue
        try:
            kinds = declaration_kinds(path.read_text(encoding='utf-8'), include_private=True)
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(f'{path}: cannot select promotion candidates: {error}') from error
        if any(kind in {'theorem', 'lemma'} for kind in kinds.values()):
            candidates.append('.'.join(part if IDENT_RE.fullmatch(part) else '«' + part + '»'
                                       for part in relative.with_suffix('').parts))
    return tuple(candidates)


@dataclass
class Inventory:
    declarations: dict[tuple[str, ...], list[str]]
    files: dict[str, str]
    errors: list[str]


def _normalized_text(text: str, root: str, normalized_root: str) -> str:
    """Replace identifier roots, never string/character values or prose."""
    masked = mask_comments(text, preserve_escaped_identifiers=True)
    pattern = re.compile(rf'(?<!{LEAN_ID_REST}){re.escape(root)}(?!{LEAN_ID_REST})')
    pieces, previous = [], 0
    for match in pattern.finditer(masked):
        pieces.extend((text[previous:match.start()], normalized_root))
        previous = match.end()
    pieces.append(text[previous:])
    return ''.join(pieces)


def inventory(repo: Path, root: str, *, allowed_layers: tuple[str, ...] | None = None,
              skip_prefixes: tuple[str, ...] = (), normalized_root: str = 'Qatlib') -> Inventory:
    """Inventory explicit public declarations and whole-file normalized text."""
    repo = repo.resolve()
    tree = repo / root
    declarations: dict[tuple[str, ...], list[str]] = {}
    files: dict[str, str] = {}
    errors = []
    for path in source_files(repo, root):
        relative = path.relative_to(tree).as_posix()
        if allowed_layers is not None and relative.split('/')[0] not in allowed_layers:
            continue
        if any(relative.startswith(prefix + '/') or relative == prefix + '.lean'
               for prefix in skip_prefixes):
            continue
        try:
            text = path.read_text(encoding='utf-8')
            kinds = declaration_kinds(text)
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f'{path}: cannot index declarations: {error}')
            continue
        files[relative] = _normalized_text(text, root, normalized_root)
        for name in kinds:
            # Only declarations rooted at the selected source package are
            # promotion candidates; short or unrelated names are not providers.
            if name[0] == root:
                declarations.setdefault(name[1:], []).append(str(path))
    return Inventory(declarations, files, errors)


def check_duplicates(source_repo: Path, public_repo: Path, *, source_root: str,
                     public_root: str = 'Qatlib', allowed_layers: tuple[str, ...] | None = None,
                     skip_prefixes: tuple[str, ...] = ()) -> tuple[list[str], dict[str, int]]:
    """Compare supplied inventories; callers must verify the Public lock first.

    downstream adapters pass their configured source root explicitly; the
    shared package keeps no repository-specific default.
    """
    source = inventory(source_repo, source_root, allowed_layers=allowed_layers,
                       skip_prefixes=skip_prefixes, normalized_root=public_root)
    public = inventory(public_repo, public_root, allowed_layers=allowed_layers,
                       skip_prefixes=skip_prefixes, normalized_root=public_root)
    errors = source.errors + public.errors
    overlaps = sorted(source.declarations.keys() & public.declarations.keys())
    for name in overlaps:
        errors.append(f"promoted declaration {'.'.join(name)} remains in {source_root}: "
                      f"{', '.join(source.declarations[name])}; Public: "
                      + ', '.join(public.declarations[name]))
    identical = sorted(name for name in source.files.keys() & public.files.keys()
                       if source.files[name] == public.files[name])
    errors.extend(f'promoted module remains in {source_root}: {name}' for name in identical)
    return errors, {'source_files': len(source.files), 'public_files': len(public.files),
                    'source_public': len(source.declarations), 'public_declarations': len(public.declarations),
                    'duplicate_declarations': len(overlaps), 'identical_modules': len(identical)}
