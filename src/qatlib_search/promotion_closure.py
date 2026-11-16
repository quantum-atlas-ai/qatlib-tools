"""Read-only whole-file promotion closure; callers verify dependency locks first.

Standard layers run Foundations → Concepts → Papers; additional source layers
follow in their configured order. A reviewed extraction may explicitly extend
``allowed_layers`` (for example Research). Public providers always retain
the three standard layers. This module neither fetches nor writes.
"""
from __future__ import annotations

from dataclasses import dataclass
import heapq
from pathlib import Path
import re
from typing import Callable, Iterable

from qatlib_search.lean_declarations import IDENT_RE, NAME, NAME_RE, name_parts
from qatlib_search.lean_source import import_header, mask_comments


IDENTIFIER = r"[A-Za-z_]\w*"
MODULE_RE = NAME_RE
IMPORT_RE = re.compile(rf"\bimport\s+(?:all\s+)?(?P<module>{NAME})")


def _parts(module: str) -> tuple[str, ...]:
    parts = name_parts(module)
    if any(part in {'', '.', '..'} or '/' in part or '\\' in part for part in parts):
        raise ValueError(f'unsafe source module name: {module!r}')
    return parts


def _module_name(parts: Iterable[str]) -> str:
    return '.'.join(part if IDENT_RE.fullmatch(part) else '«' + part + '»' for part in parts)


@dataclass(frozen=True)
class ClosureResult:
    modules: tuple[str, ...]
    imports: dict[str, tuple[str, ...]]
    errors: tuple[str, ...]


def _inventory(repo: Path, root: str) -> dict[str, Path]:
    # Keep the core import lazy: promotion's CLI may import this module before
    # invoking the closure, while its inventory API is already fully initialized.
    from qatlib_search.promotion import source_files

    inventory: dict[str, Path] = {}
    for path in source_files(repo, root):
        path = path if path.is_absolute() else repo / path
        if not path.resolve().is_relative_to(repo / root):
            raise ValueError(f"{path}: source escapes {root}")
        relative = path.relative_to(repo)
        raw_parts = relative.with_suffix("").parts
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in raw_parts):
            raise ValueError(f"{path}: unsupported source module name")
        module = _module_name(relative.with_suffix("").parts)
        if (not MODULE_RE.fullmatch(module) or relative.parts[0] != root
                or path.suffix != ".lean"):
            raise ValueError(f"{path}: unsupported source module name")
        if module in inventory:
            raise ValueError(f"{path}: duplicate source provider for {module}")
        inventory[module] = path
    return inventory


def _cycle(graph: dict[str, set[str]], nodes: set[str]) -> list[str]:
    """Find a deterministic cycle without recursion limits on long import chains."""
    finished: set[str] = set()
    for start in sorted(nodes):
        if start in finished:
            continue
        path = [start]
        active = {start: 0}
        stack = [iter(sorted(graph[start] & nodes))]
        while stack:
            dependency = next(stack[-1], None)
            if dependency is None:
                finished.add(path[-1])
                active.pop(path.pop())
                stack.pop()
            elif dependency in active:
                return path[active[dependency]:] + [dependency]
            elif dependency not in finished:
                active[dependency] = len(path)
                path.append(dependency)
                stack.append(iter(sorted(graph[dependency] & nodes)))
    return []


def candidate_closure(
    repo: Path, public_repo: Path, candidates: Iterable[str], *,
    source_root: str | None = None, public_root: str | None = None,
    allowed_layers: Iterable[str] = ("Foundations", "Concepts", "Papers"),
    external_roots: Iterable[str] = ("Init", "Lean", "Std", "Mathlib"),
    non_promotable_roots: Iterable[str] = (),
    test_roots: Iterable[str] = ("tests",),
    external_provider: Callable[[str], bool] | None = None,
) -> ClosureResult:
    """Close explicit modules over local imports, including definition-only files.

    ``modules`` is a stable dependency-first order of local files to promote;
    ``imports`` preserves their actual header imports. Reachable public provider
    chains are also checked, but remain public inputs rather than copied files.
    Errors always clear ``modules`` so a partial closure cannot be used as a plan.
    A caller must validate its exact dependency locks before calling this helper.
    An optional ``external_provider`` checks actual foreign module providers;
    it never relaxes the explicit external-root policy.
    """
    from qatlib_search.promotion import PUBLIC_LAYERS

    errors: list[str] = []
    local_imports: dict[str, tuple[str, ...]] = {}

    def result(modules: Iterable[str] = ()) -> ClosureResult:
        return ClosureResult(tuple(modules) if not errors else (),
                             dict(sorted(local_imports.items())), tuple(dict.fromkeys(errors)))

    try:
        from qatlib_env.project import source_root as discover_root
        source_root = source_root or discover_root(Path(repo))
        public_root = public_root or discover_root(Path(public_repo))
        if any(isinstance(value, (str, bytes)) for value in (allowed_layers, external_roots, non_promotable_roots, test_roots)):
            raise ValueError("closure roots and layers must be iterables of identifiers, not strings")
        layers = tuple(allowed_layers)
        external = tuple(external_roots)
        non_promotable = tuple(non_promotable_roots)
        tests = tuple(test_roots)
        if source_root in external or public_root in external \
                or set(non_promotable).intersection(external) or any(
                    root == test or root.endswith(test) for root in external for test in tests):
            raise ValueError('a project root cannot be declared an external library')
        if (not layers or len(set(layers)) != len(layers)
                or any(not isinstance(name, str) or not re.fullmatch(IDENTIFIER, name)
                       for name in (source_root, public_root, *layers, *external, *non_promotable, *tests))
                or source_root == public_root):
            raise ValueError("roots must be identifiers; layers must be nonempty and unique; project roots must differ")
        if isinstance(candidates, (str, bytes)):
            raise ValueError("candidates must be an iterable of module names, not a single string")
        requested = tuple(candidates)
        if not requested:
            raise ValueError("at least one explicit candidate module is required")
        repo, public_repo = Path(repo).resolve(), Path(public_repo).resolve()
        local = _inventory(repo, source_root)
        public = _inventory(public_repo, public_root)
    except (OSError, ValueError, TypeError) as error:
        errors.append(str(error))
        return result()

    ranks = {layer: rank for rank, layer in enumerate(dict.fromkeys((*PUBLIC_LAYERS, *layers)))}
    providers = {**local, **public}
    graph: dict[str, set[str]] = {}
    locations: dict[str, dict[str, int]] = {}
    pending: list[str] = []

    def project_layer(module: str, location: str) -> str | None:
        if not isinstance(module, str) or not MODULE_RE.fullmatch(module):
            errors.append(f"{location}: invalid module name {module!r}")
            return None
        try:
            parts = _parts(module)
        except ValueError as error:
            errors.append(f"{location}: {error}")
            return None
        if len(parts) < 3:
            errors.append(f"{location}: forbidden project aggregate {module!r}")
            return None
        if parts[0] in non_promotable or parts[1] in tests:
            errors.append(f"{location}: forbidden non-promotable source root/layer in {module}")
            return None
        allowed = PUBLIC_LAYERS if parts[0] == public_root else layers
        if parts[1] not in allowed:
            errors.append(f"{location}: forbidden layer {parts[1]!r} in {module}")
            return None
        return parts[1]

    for module in requested:
        location = f"candidate {module!r}"
        layer = project_layer(module, location)
        if layer is None:
            continue
        module = _module_name(_parts(module))
        if _parts(module)[0] != source_root:
            errors.append(f"{location}: candidate must belong to {source_root}")
        elif module not in local:
            errors.append(f"{location}: missing local source provider")
        else:
            heapq.heappush(pending, module)

    while pending:
        module = heapq.heappop(pending)
        if module in graph:
            continue
        path = providers[module]
        graph[module] = set()
        try:
            text = path.read_text(encoding="utf-8")
            dependencies, end = import_header(text)
            header = mask_comments(text, preserve_escaped_identifiers=True)[:end]
        except (OSError, UnicodeError, ValueError) as error:
            errors.append(f"{path}: cannot read import header: {error}")
            continue
        positions: dict[str, int] = {}
        for match in IMPORT_RE.finditer(header):
            positions.setdefault(match.group("module"), text.count("\n", 0, match.start("module")) + 1)
        locations[module] = positions
        if module in local:
            local_imports[module] = tuple(dependencies)
        layer = _parts(module)[1]
        for dependency in dependencies:
            location = f"{path}:{positions.get(dependency, 1)}"
            try:
                dependency = _module_name(_parts(dependency))
                root = _parts(dependency)[0]
            except ValueError as error:
                errors.append(f"{location}: {error}")
                continue
            if root not in {source_root, public_root}:
                if dependency == "Mathlib":
                    errors.append(f"{location}: forbidden root aggregate Mathlib")
                elif root not in external:
                    errors.append(f"{location}: forbidden foreign project import {dependency}")
                elif external_provider is not None and not external_provider(dependency):
                    errors.append(f"{location}: missing external source provider {dependency}")
                continue
            dependency_layer = project_layer(dependency, location)
            if dependency_layer is None:
                continue
            if module in public and root == source_root:
                errors.append(f"{location}: public provider {module} imports local project {dependency}")
            if ranks[dependency_layer] > ranks[layer]:
                errors.append(f"{location}: reverse layer dependency {module} -> {dependency}")
            if dependency not in providers:
                kind = "public" if root == public_root else "local"
                errors.append(f"{location}: missing {kind} source provider {dependency}")
                continue
            graph[module].add(dependency)
            heapq.heappush(pending, dependency)

    # Kahn's algorithm gives a lexical, dependency-first order independent of
    # candidate ordering, import ordering, and set/dict iteration details.
    remaining = {module: len(dependencies) for module, dependencies in graph.items()}
    users = {module: set() for module in graph}
    for module, dependencies in graph.items():
        for dependency in dependencies:
            users[dependency].add(module)
    ready = [module for module, count in remaining.items() if not count]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        module = heapq.heappop(ready)
        ordered.append(module)
        for user in sorted(users[module]):
            remaining[user] -= 1
            if remaining[user] == 0:
                heapq.heappush(ready, user)
    if len(ordered) != len(graph):
        cycle = _cycle(graph, {module for module, count in remaining.items() if count})
        edges = [f"{providers[left]}:{locations.get(left, {}).get(right, 1)}"
                 for left, right in zip(cycle, cycle[1:])]
        errors.append("import cycle: " + " -> ".join(cycle) + " (" + ", ".join(edges) + ")")
    return result(module for module in ordered if module in local)
