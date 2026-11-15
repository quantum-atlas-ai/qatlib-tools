"""Find source candidates, preferring pinned Frozen over unstable Dev modules.

No index, compilation, download or cache hashing is performed. A source hit is
not a Lean type check; inspect the declaration and use #check in the consumer.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_search.paths import ROOT
from qatlib_search.source_roots import SourceCatalog, Source
from qatlib_search.lean_declarations import declaration_kinds, name_parts


def search(root: Path, query: str, *, owner: str = 'all', limit: int = 20) -> dict:
    if not query.strip() or limit < 1:
        raise ValueError('query must be nonempty and limit positive')
    catalog = SourceCatalog(root)
    locations = []
    frozen = catalog.roots.get('frozen')
    for prefix in catalog.lean_roots:
        if frozen and (frozen / prefix).is_dir():
            catalog.validate_root('frozen', frozen)
            locations.append(('frozen', frozen, prefix))
        elif (catalog.root / prefix).is_dir():
            locations.append(('dev', catalog.root, prefix))
    names = name_parts(query) if '.' in query or query.startswith('«') else None
    if names and names[0] == '_root_':
        names = names[1:]
    needle = names[-1] if names else query
    matches, unverified = [], []
    checked = []
    for label, checkout, prefix in locations:
        if owner != 'all' and label != owner:
            continue
        checked.append({'owner': label, 'root': str(checkout), 'module_root': prefix})
        result = subprocess.run(['rg', '-l', '--fixed-strings', '--glob', '*.lean', '--', needle,
                                 str(checkout / prefix)], capture_output=True, text=True)
        if result.returncode not in (0, 1):
            raise ValueError('source search failed; install rg and inspect source permissions')
        for value in sorted(result.stdout.splitlines()):
            path = Path(value)
            if not path.resolve().is_relative_to(checkout.resolve()):
                raise ValueError('source candidate leaves its checkout')
            try:
                declarations = declaration_kinds(path.read_text())
            except ValueError as error:
                unverified.append({'file': str(path), 'reason': str(error)})
                continue
            for name, kind in declarations.items():
                fqn = '.'.join(name)
                if (names is not None and tuple(names) != name) or (names is None and query not in fqn):
                    continue
                source = Source(label, checkout, path.relative_to(checkout).as_posix())
                matches.append({'owner': label, 'file': source.relative, 'checkout': str(checkout),
                                'module': source.module, 'import': 'import ' + source.module,
                                'fqn': fqn, 'kind': kind, 'lean_type_checked': False})
                if len(matches) > limit:
                    break
            if len(matches) > limit:
                break
        if len(matches) > limit:
            break
    return {'query': query, 'pinned_frozen_commit': catalog.frozen_pin,
            'matches': matches[:limit], 'truncated': len(matches) > limit,
            'checked_roots': checked, 'unverified_sources': unverified,
            'scope': 'Source candidates only. Use a narrow import and #check under the pinned consumer toolchain; do not silently change pins when a Dev symbol moved to Frozen.'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('query')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--owner', choices=['all', 'frozen', 'dev'], default='all')
    parser.add_argument('--limit', type=int, default=20)
    args = parser.parse_args()
    try:
        print(json.dumps(search(args.root.resolve(), args.query, owner=args.owner, limit=args.limit),
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError) as error:
        print('find_symbol: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
