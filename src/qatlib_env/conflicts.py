"""Read-only source check for cross-library modules, exported names and global syntax.

This deliberately reads source headers, not .olean proof data. Explicit declaration
names use the shared module/public-section lexer. Macro-generated declarations,
deriving output and arbitrary environment extensions still require compiler tests.
Matching global syntax terminals are conservative ambiguity candidates, not a proof
that Lean rejects two overloads. Prefer local/scoped syntax or review the overlap.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterator
import os
from pathlib import Path
import re
import sys
import time

from qatlib_search.lean_declarations import declaration_names
from qatlib_search.lean_source import mask_comments
from qatlib_search.promotion import _git, locked_revision, verify_source_revision

from .project import libraries, primary_library
from .promotion import dependency


SYNTAX = re.compile(
    r"^[ \t]*(?P<modifiers>(?:@\[[^\]]*\]\s*|(?:public|private|local|scoped|meta)\s+)*)"
    r"(?P<kind>notation|infixl|infixr|infix|prefix|postfix|macro|syntax)\b", re.M)
LITERAL = re.compile(r'/-|-/|--|r#*"|"(?:\\.|[^"\\])*"|«[^»]*»', re.S)


def explicit_exports(text: str) -> set[tuple[str, ...]]:
    """Reuse the declaration lexer; isolate literal syntax grammar declarations.

    The syntax grammar is indexed separately. Named/generated parser constants
    remain outside the explicit declaration-header inventory, as do constructors
    and deriving output; this is not a compiler environment-extension audit.
    """
    if 'syntax' not in text:
        return declaration_names(text)
    masked = mask_comments(text)
    spans = []
    for match in SYNTAX.finditer(masked):
        if match['kind'] != 'syntax':
            continue
        end = re.search(r':\s*[A-Za-z_][A-Za-z0-9_]*[ \t]*(?=\n|$)', masked[match.end():])
        if end is None:
            raise ValueError('unsupported syntax header requires a Lean declaration index')
        spans.append((match.start(), match.end() + end.end()))
    for start, end in reversed(spans):
        text = text[:start] + re.sub(r'[^\n]', ' ', text[start:end]) + text[end:]
    return declaration_names(text)


def terminals(header: str) -> tuple[str, ...]:
    """Read literal syntax terminals, ignoring nested comments and escaped names."""
    result = []
    depth = position = 0
    while match := LITERAL.search(header, position):
        token = match.group()
        position = match.end()
        if depth:
            depth += (token == '/-') - (token == '-/')
        elif token == '/-':
            depth = 1
        elif token == '--':
            newline = header.find('\n', position)
            position = len(header) if newline < 0 else newline
        elif token.startswith('r'):
            closing = '"' + '#' * (len(token) - 2)
            end = header.find(closing, position)
            if end < 0:
                raise ValueError('unterminated raw syntax terminal')
            result.append(header[position:end].strip())
            position = end + len(closing)
        elif token.startswith('"'):
            result.append(token[1:-1].strip())
    return tuple(result)


def global_syntax(text: str) -> set[tuple[str, tuple[str, ...]]]:
    """Index syntax category and literal sequence; local/scoped rules stay isolated."""
    possible = False
    for word in ('notation', 'infix', 'prefix', 'postfix', 'macro', 'syntax'):
        position = text.find(word)
        while position >= 0:
            if SYNTAX.match(text, text.rfind('\n', 0, position) + 1):
                possible = True
                break
            position = text.find(word, position + len(word))
        if possible:
            break
    if not possible:
        return set()
    masked = mask_comments(text)
    commands = list(SYNTAX.finditer(masked))
    result = set()
    for index, match in enumerate(commands):
        if {'local', 'scoped'} & set(match['modifiers'].split()):
            continue
        end = commands[index + 1].start() if index + 1 < len(commands) else len(masked)
        stop = re.search(r'=>|\n\s*\n', masked[match.end():end])
        if stop:
            end = match.end() + stop.start()
        kind = match['kind']
        category = 'term'
        if kind in {'macro', 'syntax'}:
            categories = re.findall(r':\s*([A-Za-z_][A-Za-z0-9_]*)\s*$', masked[match.end():end])
            if not categories:
                raise ValueError('syntax category/header requires an explicit source index')
            category = categories[-1]
        values = terminals(text[match.end():end])
        if not values:
            raise ValueError('syntax without literal terminals requires an explicit source index')
        result.add((category, values))
    return result


def selected_roots(repo: Path, override: str | None = None) -> tuple[Path, tuple[str, ...]]:
    """Select a Lake library and its logical module roots, honoring srcDir."""
    if override:
        matches = [lib for lib in libraries(repo) if override in lib.roots]
        if len(matches) != 1:
            raise ValueError('explicit root must identify one Lake library: ' + override)
        library = matches[0]
        roots = (override,)
    else:
        library = primary_library(repo)
        roots = library.roots
    for name in roots:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*', name):
            raise ValueError(f'unsupported library root: {name!r}')
    base = repo / library.src_dir
    if base.resolve() != base:
        raise ValueError(f'{base}: redirected source directory is unsupported')
    return base, roots


def library_sources(repo: Path, override: str | None = None, *,
                    tracked_only: bool = False) -> Iterator[tuple[str, Path]]:
    """Resolve formal libraries from Lake, including their aggregate modules."""
    base, roots = selected_roots(repo, override)
    tracked = ({os.fsdecode(path) for path in _git(repo, 'ls-files', '-z').split(b'\0') if path}
               if tracked_only else None)
    seen = set()
    for name in roots:
        root = base / name.replace('.', '/')
        files = ([root.with_suffix('.lean')] if root.with_suffix('.lean').is_file() else [])
        if root.is_symlink():
            raise ValueError(f'{root}: redirected source directory is unsupported')
        if root.is_dir():
            for directory, subdirs, names in os.walk(root):
                subdirs.sort()
                for child in subdirs:
                    if Path(directory, child).is_symlink():
                        raise ValueError(f'{directory}/{child}: redirected source directory is unsupported')
                files.extend(Path(directory, item) for item in sorted(names) if item.endswith('.lean'))
        if not files:
            raise ValueError(f'{repo}: library root {name} has no sources')
        for path in files:
            if tracked is not None and os.path.relpath(path, repo) not in tracked:
                continue
            if path in seen:
                continue
            if path.is_symlink():
                raise ValueError(f'{path}: redirected source is unsupported')
            seen.add(path)
            yield os.path.splitext(os.path.relpath(path, base))[0].replace(os.sep, '.'), path


def check(repo: Path, upstream: Path, *, source_root: str | None = None,
          upstream_root: str | None = None) -> tuple[list[str], dict[str, int]]:
    # Only keep the small downstream inventory. Stream the upstream source tree
    # rather than retaining its many proof bodies/declaration maps in memory.
    modules: dict[str, Path] = {}
    names: dict[tuple[str, ...], Path] = {}
    syntax: dict[tuple[str, tuple[str, ...]], Path] = {}
    counts = dict(source_files=0, upstream_files=0, source_exports=0, upstream_export_candidates=0,
                  upstream_indexed_files=0,
                  source_global_syntax=0, upstream_global_syntax=0)
    errors = []
    roots: set[str] = set()
    for side, tree, override in (('source', repo, source_root), ('upstream', upstream, upstream_root)):
        for module, path in library_sources(tree, override,
                                           tracked_only=side == 'upstream' and (tree / '.git').exists()):
            counts[side + '_files'] += 1
            text = path.read_text(encoding='utf-8')
            try:
                # An explicit FQN must spell its first namespace component in
                # the source. This conservative byte filter can have false
                # positives (comments/literals), never a skipped matching name.
                indexed = side == 'source' or any(root in text for root in roots)
                exports = explicit_exports(text) if indexed else set()
                rules = global_syntax(text)
            except ValueError as error:
                raise ValueError(f'{path}: {error}') from error
            counts['source_exports' if side == 'source' else 'upstream_export_candidates'] += len(exports)
            counts[side + '_global_syntax'] += len(rules)
            if side == 'source':
                modules[module] = path
                names.update((name, path) for name in exports)
                roots.update(name[0] for name in exports)
                syntax.update((rule, path) for rule in rules)
                continue
            counts['upstream_indexed_files'] += int(indexed)
            if module in modules:
                errors.append(f'module {module}: {modules[module]} <> {path}')
            for name in sorted(exports & names.keys()):
                errors.append(f'export {".".join(name)}: {names[name]} <> {path}')
            for rule in sorted(rules & syntax.keys()):
                errors.append(f'global syntax {rule!r}: {syntax[rule]} <> {path}')
    return errors, counts


def verify_upstream(project: Path, source: Path, package: str,
                    root: str | None, expected_rev: str | None = None) -> str:
    """Require exact dependency source bytes, aggregate and Lake layout."""
    revision = expected_rev or locked_revision(project, package, allow_inherited=True)
    base, roots = selected_roots(source, root)
    config = source / 'lakefile.toml'
    if config.read_bytes() != _git(source, 'show', revision + ':lakefile.toml'):
        raise ValueError(f'{config}: Lake layout differs from revision {revision}')
    for name in roots:
        physical = base / name.replace('.', '/')
        verify_source_revision(source, revision, physical.relative_to(source).as_posix())
        aggregate = physical.with_suffix('.lean')
        if aggregate.exists():
            relative = aggregate.relative_to(source).as_posix()
            if aggregate.read_bytes() != _git(source, 'show', revision + ':' + relative):
                raise ValueError(f'{aggregate}: aggregate bytes differ from revision {revision}')
    return revision


def main(argv: list[str] | None = None, *, project: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', required=True, help='exact upstream Lake dependency name')
    parser.add_argument('--source', type=Path, help='explicit upstream dependency checkout')
    parser.add_argument('--source-root', help='consumer logical Lean library root')
    parser.add_argument('--upstream-root', help='upstream logical Lean library root')
    parser.add_argument('--expected-rev', help='explicit full SHA for a rehearsal')
    args = parser.parse_args(argv)
    start = time.perf_counter()
    try:
        repo = (project or Path.cwd()).resolve()
        upstream = dependency(repo, args.package, args.source)
        revision = verify_upstream(repo, upstream, args.package, args.upstream_root, args.expected_rev)
        errors, counts = check(repo, upstream, source_root=args.source_root,
                               upstream_root=args.upstream_root)
    except (OSError, ValueError) as error:
        print(f'LIBRARY_CONFLICTS_ERROR {error}', file=sys.stderr)
        return 2
    for error in errors[:25]:
        print('LIBRARY_CONFLICT ' + error, file=sys.stderr)
    print(f'LIBRARY_CONFLICTS {"FAIL" if errors else "OK"} package={args.package} upstream_rev={revision} '
          + ' '.join(f'{key}={value}' for key, value in counts.items())
          + f' conflicts={len(errors)} seconds={time.perf_counter() - start:.3f}')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
