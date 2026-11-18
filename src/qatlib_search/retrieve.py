"""Explicit cache indexing and read-only, local Lean source retrieval.

No Lean, Lake, environment manager, network client or build-pool imports belong
here. The cache is disposable; source snippets and hashes are the evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from . import lean_declarations, lean_doc_source, lean_source
from .doc_references import INLINE_RE, strip_fences, strip_source_excerpts
from .retrieve_corpus import check_destination, default_db, digest, discover, git

SCHEMA = 1
STOP = {'a', 'an', 'the', 'of', 'and', 'or', 'to', 'in', 'for', 'is', 'with'}
WEIGHTS = (12.0, 3.0, 6.0, 9.0)
MAX_CANDIDATES = 2000
FTS_SQL = ("CREATE VIRTUAL TABLE search USING "
           "fts5(name,signature,doc,cites, tokenize='porter unicode61')")


def tokens(text: str) -> list[str]:
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([A-Z])([A-Z][a-z])', r'\1 \2', text)
    return [s for s in re.findall(r'[^\W_]+', text.casefold()) if s not in STOP]


def searchable(text: str) -> str:
    return ' '.join(tokens(text))


def fts_query(words: list[str], operator: str = 'AND') -> str:
    # Never pass user syntax to MATCH, even with bound SQL parameters.
    return (' ' + operator + ' ').join('"' + w.replace('"', '""') + '"' for w in words)


def scanner_identity() -> str:
    files = ('retrieve.py', 'retrieve_corpus.py', 'lean_source.py',
             'lean_declarations.py', 'lean_doc_source.py', 'doc_references.py', 'paths.py')
    return digest(b''.join(Path(__file__).with_name(name).read_bytes() for name in files))


def metadata(c: sqlite3.Connection) -> dict:
    try:
        return json.loads(c.execute("SELECT value FROM meta WHERE key='snapshot'").fetchone()[0])
    except (sqlite3.Error, TypeError, ValueError) as error:
        raise ValueError('missing/incompatible index; run qatlib-retrieve index') from error


def compatible(meta: dict) -> None:
    if meta.get('schema') != SCHEMA or meta.get('scanner') != scanner_identity():
        raise ValueError('scanner/index version changed; run qatlib-retrieve index')


def create_schema(c: sqlite3.Connection, *, use_fts: bool) -> str:
    c.executescript("""
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS files(
          id INTEGER PRIMARY KEY, repo TEXT, root TEXT, path TEXT, module TEXT,
          hash TEXT, warning TEXT, UNIQUE(root,path));
        CREATE TABLE IF NOT EXISTS records(
          id INTEGER PRIMARY KEY, file_id INTEGER, fqn TEXT, short TEXT, kind TEXT,
          line INTEGER, source_signature TEXT, adjacent_doc TEXT, variable_context TEXT,
          name TEXT, signature TEXT, doc TEXT, cites TEXT);
        CREATE INDEX IF NOT EXISTS names ON records(fqn COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS short_names ON records(short COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS file_records ON records(file_id);
        CREATE TABLE IF NOT EXISTS backgrounds(
          declaration_id INTEGER, background_id INTEGER, PRIMARY KEY(declaration_id,background_id));
        CREATE INDEX IF NOT EXISTS background_members ON backgrounds(background_id);
        CREATE TABLE IF NOT EXISTS citations(record_id INTEGER, key TEXT, PRIMARY KEY(record_id,key));
        CREATE INDEX IF NOT EXISTS citation_keys ON citations(key);
    """)
    if use_fts:
        try:
            c.execute(FTS_SQL.replace('TABLE search', 'TABLE IF NOT EXISTS search'))
            return 'fts5'
        except sqlite3.OperationalError as error:
            if 'no such module' not in str(error):
                raise
    return 'tokens-fallback'


def cite_keys(text: str, bibliography: set[str]) -> list[str]:
    # Only actual root BibTeX keys count, never arbitrary mathematical brackets.
    text = INLINE_RE.sub('', strip_fences(strip_source_excerpts(text)))
    return sorted(set(re.findall(r'\[([^\[\]\n]+)\]', text)) & bibliography)


def source_path(repo: dict, relative: str) -> bool:
    for directory, names in repo['libraries']:
        for name in names:
            prefix = (Path(directory) / name.replace('.', '/')).as_posix()
            if relative == prefix + '.lean' or relative.startswith(prefix + '/') and relative.endswith('.lean'):
                return True
    return False


def blob_hash(data: bytes, expected: str | None) -> str:
    algorithm = hashlib.sha256 if expected and len(expected) == 64 else hashlib.sha1
    return algorithm(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def extract(text: str, path: Path, root: Path):
    spans = []
    masked = lean_source.mask_comments(text, comment_spans=spans)
    backgrounds = [(text.count('\n', 0, s.start) + 1, text[s.start + 3:s.end - 2].strip())
                   for s in spans if text.startswith('/-!', s.start)]
    # The declaration-name lexer understands "class inductive"; adapt that
    # command for the documentation reader without changing source offsets.
    adapted = text
    offset = 0
    for line in masked.splitlines(keepends=True):
        command = line.lstrip(' \t')
        header = lean_doc_source.DECL_RE.match(command)
        if header and header['kind'] == 'class' and header['name'] == 'inductive':
            start = offset + len(line) - len(command) + header.start('name')
            adapted = adapted[:start] + ' ' * len('inductive') + adapted[start + len('inductive'):]
        offset += len(line)
    try:
        module = lean_doc_source.parse_module_text(adapted, path, root)
    except ValueError as error:
        return [], backgrounds, {}, str(error)
    # Scope the background references using the same command masks and patterns.
    by_line = {line: i for i, (line, _) in enumerate(backgrounds)}
    links: dict[int, list[int]] = {}
    active: list[int] = []
    stack: list[list[int]] = []
    for line, command in enumerate(masked.split('\n'), 1):
        if line in by_line:
            active = [*active, by_line[line]]
        command = command.strip()
        opening = lean_doc_source.NAMESPACE_RE.match(command) or lean_doc_source.SECTION_RE.match(command)
        if opening:
            written = (opening.group(1) if opening.re is lean_doc_source.NAMESPACE_RE
                       else opening.group('name')) or ''
            for _ in written.split('.'):
                stack.append(active[:])
        ending = lean_doc_source.END_RE.match(command)
        if ending:
            for _ in (ending.group(1) or '').split('.'):
                if stack:
                    active = stack.pop()
        links[line] = active[:]
    return module.decls, backgrounds, links, ''


def build_index(project=None, db: str | Path | None = None, *, changed=False, use_fts=True) -> dict:
    start = time.perf_counter()
    corpus = discover(project)
    path = Path(db).expanduser().resolve() if db else default_db(corpus)
    check_destination(path, corpus)
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    try:
        backend = create_schema(c, use_fts=use_fts)
        c.execute('BEGIN IMMEDIATE')
        previous = metadata(c) if changed else {}
        if changed:
            compatible(previous)
            if previous['project'] != str(corpus.project):
                raise ValueError('index belongs to a different project')
            backend = previous['backend']
        old_files = {(r['root'], r['path']): dict(r) for r in c.execute('SELECT * FROM files')} if changed else {}
        # Locked sources must really be the pinned Git blobs, including dirty bytes.
        tracked = {}
        dirty = {}
        for repo in corpus.repos:
            if repo['revision']:
                tracked[repo['root']] = {
                    row.split('\t', 1)[1]: row.split()[2]
                    for row in git(Path(repo['root']), 'ls-tree', '-rz', repo['tree']).split('\0') if row}
            dirty[repo['root']] = repo['revision'] is None
            if repo['revision']:
                for relative in ('docs/references.bib', 'lakefile.toml', 'lake-manifest.json', 'lean-toolchain'):
                    control = Path(repo['root']) / relative
                    expected = tracked[repo['root']].get(relative)
                    if (control.is_file() and blob_hash(control.read_bytes(), expected) != expected
                            or not control.is_file() and expected is not None):
                        dirty[repo['root']] = True
            if repo['locked'] and dirty[repo['root']]:
                raise ValueError(f"locked source differs from its Git blob: {repo['root']}")
        updated = 0
        source_ids = []
        seen = set()
        parse_warnings = []
        bib_changed = previous.get('inputs') != corpus.inputs
        with c:
            if not changed:
                for table in ('backgrounds', 'citations', 'records', 'files', 'meta'):
                    c.execute(f'DELETE FROM {table}')
                if backend == 'fts5':
                    c.execute('DROP TABLE search')
                    c.execute(FTS_SQL)

            def remove(file_id):
                ids = [r[0] for r in c.execute('SELECT id FROM records WHERE file_id=?', (file_id,))]
                for ident in ids:
                    c.execute('DELETE FROM backgrounds WHERE declaration_id=? OR background_id=?', (ident, ident))
                    c.execute('DELETE FROM citations WHERE record_id=?', (ident,))
                    if backend == 'fts5':
                        c.execute('DELETE FROM search WHERE rowid=?', (ident,))
                c.execute('DELETE FROM records WHERE file_id=?', (file_id,))
                c.execute('DELETE FROM files WHERE id=?', (file_id,))

            for repo, relative, module in corpus.files():
                key = (repo['root'], relative)
                seen.add(key)
                source = Path(repo['root']) / relative
                data = source.read_bytes()
                sha = digest(data)
                if repo['revision']:
                    expected = tracked[repo['root']].get(relative)
                    blob = blob_hash(data, expected)
                    if expected != blob:
                        if repo['locked']:
                            raise ValueError(f'locked source differs from its Git blob: {source}')
                        dirty[repo['root']] = True
                source_ids.append([*key, sha])
                old = old_files.get(key)
                if old and old['hash'] == sha and not bib_changed:
                    if old['warning']:
                        parse_warnings.append(old['warning'])
                    continue
                if old:
                    remove(old['id'])
                text = data.decode('utf-8')
                decls, backgrounds, links, warning = extract(text, source, Path(repo['root']))
                if warning:
                    parse_warnings.append(warning)
                file_id = c.execute('INSERT INTO files(repo,root,path,module,hash,warning) VALUES(?,?,?,?,?,?)',
                                    (repo['repo'], *key, module, sha, warning)).lastrowid

                def insert(fqn, kind, line, signature='', doc='', variables=()):
                    cites = cite_keys(doc, corpus.bibliography)
                    fields = (searchable(fqn), searchable(signature + ' ' + ' '.join(variables)),
                              searchable(doc), searchable(' '.join(cites)))
                    ident = c.execute('INSERT INTO records(file_id,fqn,short,kind,line,source_signature,'
                                      'adjacent_doc,variable_context,name,signature,doc,cites) '
                                      'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                      (file_id, fqn, fqn.rsplit('.', 1)[-1], kind, line, signature, doc,
                                       json.dumps(variables), *fields)).lastrowid
                    if backend == 'fts5' and changed:
                        c.execute('INSERT INTO search(rowid,name,signature,doc,cites) VALUES(?,?,?,?,?)', (ident, *fields))
                    c.executemany('INSERT INTO citations VALUES(?,?)', [(ident, key) for key in cites])
                    return ident

                bg_ids = [insert(module, 'module', line, doc=doc) for line, doc in backgrounds]
                for decl in decls:
                    # Reuse the production name contract; do not fabricate elaborated names.
                    lean_declarations.name_parts(decl.fqn)
                    ident = insert(decl.fqn, decl.kind, decl.line, decl.signature, decl.doc, decl.variable_context)
                    c.executemany('INSERT INTO backgrounds VALUES(?,?)',
                                  [(ident, bg_ids[i]) for i in links.get(decl.line, [])])
                updated += 1
            for key, old in old_files.items():
                if key not in seen:
                    remove(old['id'])
            for repo in corpus.repos:
                if repo['revision']:
                    expected = {p for p in tracked[repo['root']] if source_path(repo, p)}
                    actual = {p for root, p in seen if root == repo['root']}
                    if expected != actual:
                        dirty[repo['root']] = True
                    if repo['locked'] and dirty[repo['root']]:
                        raise ValueError(f"locked source inventory differs from Git: {repo['root']}")
            if backend == 'fts5' and not changed:
                c.execute('INSERT INTO search(rowid,name,signature,doc,cites) '
                          'SELECT id,name,signature,doc,cites FROM records')
            # Recheck configuration and Git identity before committing one complete generation.
            final = discover(corpus.project)
            if final.repos != corpus.repos or final.inputs != corpus.inputs:
                raise ValueError('corpus changed during indexing; retry index')
            content = digest(json.dumps(sorted(source_ids), ensure_ascii=False).encode())
            snapshot = dict(schema=SCHEMA, scanner=scanner_identity(), tool_version='0.1.0',
                            backend=backend, project=str(corpus.project), repos=corpus.repos,
                            inputs=corpus.inputs, content_identity=content, dirty=dirty, warnings=corpus.warnings,
                            parse_warnings=parse_warnings)
            c.execute("INSERT OR REPLACE INTO meta VALUES('snapshot',?)", (json.dumps(snapshot),))
        return dict(db=str(path), backend=backend, files=len(seen), updated_files=updated,
                    declarations=c.execute("SELECT count(*) FROM records WHERE kind!='module'").fetchone()[0],
                    backgrounds=c.execute("SELECT count(*) FROM records WHERE kind='module'").fetchone()[0],
                    content_identity=content, warnings=corpus.warnings, parse_warnings=parse_warnings,
                    seconds=round(time.perf_counter() - start, 3))
    finally:
        c.close()


def read_map(path, repos, c, dirty):
    if path is None:
        return {}, []
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('format') != 'qatlib-retrieve-map/v1' or not isinstance(data.get('entries'), list):
            raise ValueError('expected qatlib-retrieve-map/v1 and entries array')
        revision = data.get('source_revision')
        if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
            raise ValueError('source_revision must be a full Git SHA')
        owners = [r for r in repos if r['revision'] == revision]
        if not owners:
            raise ValueError('source_revision does not match an indexed repository')
        # A map bound only to HEAD cannot certify uncommitted source bytes.
        for repo in owners:
            if dirty[repo['root']]:
                raise ValueError('source_revision map cannot annotate dirty sources')
        additions = {}
        warnings = []
        for entry in data['entries']:
            fqn = entry['fqn']
            if not isinstance(fqn, str) or not isinstance(entry.get('title', ''), str) or not all(
                    isinstance(entry.get(field, []), list) and all(isinstance(s, str) for s in entry.get(field, []))
                    for field in ('terms', 'cite_keys')):
                raise ValueError('invalid mapping entry fields')
            ids = [r[0] for r in c.execute('SELECT r.id FROM records r JOIN files f ON f.id=r.file_id '
                                          'WHERE r.fqn=? AND r.kind!=\'module\' AND f.root IN ('
                                          + ','.join('?' for _ in owners) + ')',
                                          [fqn, *[r['root'] for r in owners]])]
            if not ids:
                warnings.append(f'mapping FQN absent from source: {fqn}')
            for ident in ids:
                additions[ident] = entry
        return additions, warnings
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        return {}, [f'mapping supplement unused: {error}']


def query(text: str, project=None, db=None, *, limit=10, cite=None, kind=None, repo=None, map_path=None) -> dict:
    if not 1 <= limit <= 100 or len(text) > 2048:
        raise ValueError('limit must be 1..100 and query at most 2048 characters')
    words = list(dict.fromkeys(tokens(text)))
    if len(words) > 32:
        raise ValueError('query supports at most 32 distinct tokens')
    corpus = discover(project)
    path = Path(db).expanduser().resolve() if db else default_db(corpus)
    check_destination(path, corpus)
    if not path.is_file():
        raise ValueError('index missing; run qatlib-retrieve index')
    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=0.1)
    c.row_factory = sqlite3.Row
    deadline = time.monotonic() + 5
    c.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    try:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        meta = metadata(c)
        compatible(meta)
        if meta['project'] != str(corpus.project) or meta['repos'] != json.loads(json.dumps(corpus.repos)) or meta['inputs'] != corpus.inputs:
            raise ValueError('stale corpus identity/configuration; run qatlib-retrieve index --changed')
        additions, warnings = read_map(map_path, corpus.repos, c, meta['dirty'])
        filters, filter_args = [], []
        if repo:
            filters.append('ff.repo=?')
            filter_args.append(repo)
        if kind:
            filters.append("(rr.kind=? OR rr.kind='module')")
            filter_args.append(kind)
        cite_cte, cite_args = '', []
        if cite:
            mapped = [i for i, entry in additions.items() if cite in entry.get('cite_keys', [])]
            eligible = ('SELECT record_id FROM citations WHERE key=? UNION '
                         'SELECT b.declaration_id FROM backgrounds b JOIN citations ci '
                         'ON ci.record_id=b.background_id WHERE ci.key=?')
            cite_args.extend((cite, cite))
            if mapped:
                eligible += ' UNION SELECT id FROM records WHERE id IN (' + ','.join('?' for _ in mapped) + ')'
                cite_args.extend(mapped)
            cite_cte = ('WITH cited(id) AS (' + eligible + '), allowed(id) AS '
                        '(SELECT id FROM cited UNION SELECT b.background_id FROM backgrounds b '
                        'JOIN cited ON cited.id=b.declaration_id) ')
            filters.append('rr.id IN (SELECT id FROM allowed)')
        filter_sql = ''.join(' AND ' + condition for condition in filters)
        candidates = {}
        reasons = {}

        def add(ident, tier, score, reason):
            old = candidates.get(ident)
            if old is None or (tier, score) < old:
                candidates[ident] = (tier, score)
            reasons.setdefault(ident, set()).add(reason)

        for row in c.execute('SELECT id FROM records WHERE kind!=\'module\' AND '
                             '(fqn=? COLLATE NOCASE OR short=? COLLATE NOCASE)', (text, text)):
            add(row[0], 0, 0, 'exact-name')
        for row in c.execute('SELECT record_id FROM citations WHERE key=?', (text or cite,)):
            add(row[0], 1, 0, 'exact-citation')
        for ident, entry in additions.items():
            terms = set(tokens(entry.get('title', '') + ' ' + ' '.join(entry.get('terms', []))))
            hits = len(terms & set(words))
            if hits:
                add(ident, 2, -hits, 'mapping-terms')
            if (text or cite) in entry.get('cite_keys', []):
                add(ident, 1, 0, 'mapping-citation')
        backend = meta['backend']
        relaxed = False
        if words and backend == 'fts5':
            try:
                found = []
                for operator in ('AND', 'OR'):
                    batch = list(c.execute(cite_cte + 'SELECT search.rowid, bm25(search,12,3,6,9) AS rank FROM search '
                                           'JOIN records rr ON rr.id=search.rowid JOIN files ff ON ff.id=rr.file_id '
                                           'WHERE search MATCH ?' + filter_sql + ' ORDER BY rank LIMIT ?',
                                           [*cite_args, fts_query(words, operator), *filter_args, MAX_CANDIDATES]))
                    found.extend(batch)
                    if operator == 'AND' and len(batch) >= max(20, limit * 2):
                        break
                    if operator == 'OR':
                        relaxed = True
                for row in found:
                    add(row[0], 2, row[1], 'bm25')
            except sqlite3.OperationalError as error:
                if 'no such module' not in str(error):
                    raise
                backend = 'tokens-fallback'
        if words and backend == 'tokens-fallback':
            warnings.append('FTS5 unavailable: bounded token fallback (up to 2000 candidates)')
            conditions = ' OR '.join('(name LIKE ? OR signature LIKE ? OR doc LIKE ? OR cites LIKE ?)' for _ in words)
            rows = c.execute(cite_cte + 'SELECT rr.* FROM records rr JOIN files ff ON ff.id=rr.file_id WHERE ('
                             + conditions + ')' + filter_sql + ' LIMIT ?',
                             [*cite_args, *['%' + w + '%' for w in words for _ in range(4)], *filter_args, MAX_CANDIDATES])
            for row in rows:
                score = sum(weight * sum(Counter(row[field].split())[w] for w in words)
                            for field, weight in zip(('name', 'signature', 'doc', 'cites'), WEIGHTS))
                if score:
                    add(row['id'], 2, -score, 'token-fallback')
            relaxed = True
        # Expand matching backgrounds via references; do not duplicate their large
        # text in every declaration's FTS row or allow an unbounded expansion.
        direct_scores = dict(candidates)
        for ident in list(candidates):
            row = c.execute('SELECT kind FROM records WHERE id=?', (ident,)).fetchone()
            if row and row[0] == 'module':
                members = list(c.execute(cite_cte + 'SELECT rr.* FROM backgrounds b JOIN records rr '
                                         'ON rr.id=b.declaration_id JOIN files ff ON ff.id=rr.file_id '
                                         'WHERE b.background_id=?' + filter_sql + ' LIMIT ?',
                                         [*cite_args, ident, *filter_args, MAX_CANDIDATES]))
                members.sort(key=lambda r: (-len(set(words) & set((r['name'] + ' ' + r['signature'] + ' ' + r['doc']).split())), r['line']))
                for member in members[:10]:
                    own_score = direct_scores.get(member['id'], (2, 0))[1]
                    add(member['id'], 2, own_score + 0.35 * direct_scores[ident][1], 'module-premise')
        results = []
        for ident in candidates:
            row = c.execute('SELECT r.*,f.repo,f.root,f.path,f.module,f.hash,f.warning FROM records r '
                            'JOIN files f ON f.id=r.file_id WHERE r.id=?', (ident,)).fetchone()
            if row is None or kind and row['kind'] != kind or repo and row['repo'] != repo:
                continue
            background_ids = [r[0] for r in c.execute('SELECT background_id FROM backgrounds WHERE declaration_id=?', (ident,))]
            source_cites = sorted({r[0] for i in [ident, *background_ids]
                                   for r in c.execute('SELECT key FROM citations WHERE record_id=?', (i,))})
            if cite and cite not in source_cites and cite not in additions.get(ident, {}).get('cite_keys', []):
                continue
            lexical = set((row['name'] + ' ' + row['signature'] + ' ' + row['doc']).split())
            coverage = len(set(words) & lexical)
            tier = 4 if row['kind'] == 'module' and kind != 'module' else candidates[ident][0]
            score = candidates[ident][1]
            short_words = set(tokens(row['short']))
            if tier == 2 and len(short_words) >= 2 and short_words <= set(words):
                score *= 2
                reasons[ident].add('name-phrase')
            results.append((tier, score, -coverage,
                            row['repo'], row['path'], row['line'], ident, row, source_cites, background_ids))
        results.sort(key=lambda r: r[:7])
        output = []
        for *_, ident, row, cites, backgrounds in results[:limit]:
            source = Path(row['root']) / row['path']
            if source.is_symlink() or not source.resolve().is_relative_to(Path(row['root'])):
                raise ValueError(f'stale source path: {source}; run index --changed')
            try:
                data = source.read_bytes()
            except OSError:
                raise ValueError(f'stale source (missing): {source}; run index --changed') from None
            if digest(data) != row['hash']:
                raise ValueError(f'stale source: {source}; run index --changed')
            owner = next(r for r in corpus.repos if r['root'] == row['root'])
            output.append(dict(repo=row['repo'], revision=owner['revision'], tree=owner['tree'],
                               dirty=meta['dirty'][row['root']],
                               content_identity=meta['content_identity'], path=str(source), line=row['line'],
                               module=row['module'], source_fqn=row['fqn'], kind=row['kind'],
                               source_signature=row['source_signature'], variable_context=json.loads(row['variable_context']),
                               adjacent_doc=row['adjacent_doc'], source_hash=row['hash'], cite_keys=cites,
                               backgrounds=[dict(c.execute('SELECT id,line,adjacent_doc FROM records WHERE id=?', (i,)).fetchone())
                                            for i in backgrounds], mapping=additions.get(ident),
                               matched_fields=sorted(reasons[ident]), parse_warning=row['warning'],
                               verification='lexical source candidate; applicability not checked'))
        return dict(backend=backend, relaxed=relaxed, content_identity=meta['content_identity'],
                    warnings=[*meta['warnings'], *warnings], parse_warning_count=len(meta['parse_warnings']),
                    results=output)
    finally:
        c.close()


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Only project/db/help are global. Everything after "--" is literal query
    # text, including words such as "index" that would otherwise select writes.
    position = 0
    while position < len(argv):
        value = argv[position]
        if value in {'--project', '--db'}:
            position += 2
        elif value.startswith(('--project=', '--db=')) or value in {'-h', '--help'}:
            position += 1
        else:
            if value not in {'index', 'query', 'find'}:
                argv.insert(position, 'query')
            break
    p = argparse.ArgumentParser(prog='qatlib-retrieve', description=__doc__)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--project', default=argparse.SUPPRESS, help='target Lake TOML project')
    common.add_argument('--db', default=argparse.SUPPRESS,
                        help='index path outside source repositories (default: ${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/<corpus-key>.sqlite3)')
    for action in common._actions:
        if action.option_strings:
            p.add_argument(*action.option_strings, default=argparse.SUPPRESS, help=action.help)
    sub = p.add_subparsers(dest='command')
    index = sub.add_parser('index', parents=[common], help='explicitly build/update the cache',
                           description='Index Lean source and BibTeX in the project and available locked dependencies. Does not build Lean or fetch dependencies.')
    index.add_argument('--changed', action='store_true', help='refresh only changed files when the index can be reused')
    q = sub.add_parser('query', aliases=['find'], parents=[common], help='read-only source candidates',
                       description='Read an existing index. A missing or stale index must be refreshed explicitly with index. Results are lexical candidates, including source signatures and enclosing variable declarations.')
    q.add_argument('text', nargs='*', help='literal words or source FQN')
    q.add_argument('--limit', type=int, default=10)
    for name, help_text in (('cite', 'BibTeX key'), ('kind', 'Lean declaration kind'),
                            ('repo', 'filter by repository'), ('map', 'load a qatlib-retrieve-map/v1 JSON file')):
        q.add_argument('--' + name, help=help_text)
    q.add_argument('--json', action='store_true', help='emit structured matches and coverage diagnostics')
    args = p.parse_args(argv)
    if not args.command:
        p.print_help()
        return 0
    try:
        if args.command == 'index':
            result = build_index(getattr(args, 'project', None), getattr(args, 'db', None), changed=args.changed)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            text = ' '.join(args.text)
            if not text and not args.cite:
                raise ValueError('provide query words or --cite KEY')
            result = query(text, getattr(args, 'project', None), getattr(args, 'db', None),
                           limit=args.limit, cite=args.cite, kind=args.kind, repo=args.repo, map_path=args.map)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"backend={result['backend']} relaxed={result['relaxed']} (lexical candidates)")
                for warning in result['warnings']:
                    print('warning: ' + warning, file=sys.stderr)
                if result['parse_warning_count']:
                    print(f"warning: {result['parse_warning_count']} files have lexical coverage gaps", file=sys.stderr)
                for row in result['results']:
                    suffix = '+dirty' if row['dirty'] else ''
                    print(f"{row['source_fqn']} [{row['kind']}] {row['repo']}@{row['revision'] or 'uncommitted'}{suffix}")
                    print(f"  {row['path']}:{row['line']} {row['source_signature']}")
                    if row['variable_context']:
                        print('  ' + '; '.join(row['variable_context']))
                    print('  ' + row['adjacent_doc'][:320].replace('\n', ' '))
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'qatlib-retrieve: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
