"""Retrieval contracts: real source evidence, incremental identity, no build path."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
from unittest.mock import patch

import pytest

from qatlib_search import retrieve as r
from qatlib_search.retrieve_corpus import discover
from tests._git_fixture import snapshot


SOURCE = '''module
/-! # Module premise
Postselection normalization error under a nonzero phase gap. [Paper2026]
-/
public section
namespace Outer
namespace Inner
section Bounds
variable (n : Nat)
/-! Positive mass measurement normalization requires positive mass. -/
/-- Approximate reflection phase gap bound [label][Local2026]. -/
noncomputable def phaseGap : Nat := n
/-- Reverse Markov mean coverage. -/
theorem bound : n = n := rfl
end Bounds
/-- Choi matrix channel identity. -/
lemma channel : True := True.intro
abbrev aliasValue : Nat := 1
structure State where
  value : Nat
class Law where
  valid : True
inductive Choice where
  | one
private def secret : Nat := 0
end Inner
end Outer
end
def hidden : Nat := 0
'''


@pytest.fixture
def corpus(tmp_path):
    project = tmp_path / 'project'
    (project / 'src/Lib').mkdir(parents=True)
    (project / 'docs').mkdir()
    (project / 'lakefile.toml').write_text('name="Local"\ndefaultTargets=["Lib"]\n'
                                           '[[lean_lib]]\nname="Lib"\nsrcDir="src"\n')
    (project / 'lean-toolchain').write_text('leanprover/lean4:v4.35.0\n')
    (project / 'docs/references.bib').write_text('@article{Paper2026, title={P}}\n@book{Local2026, title={L}}\n')
    source = project / 'src/Lib/Main.lean'
    source.write_text(SOURCE)
    return project, source, tmp_path / 'cache/retrieve.sqlite3'


def build(corpus, **kwargs):
    project, _, db = corpus
    return r.build_index(project, db, **kwargs)


def query(corpus, text='', **kwargs):
    project, _, db = corpus
    return r.query(text, project, db, **kwargs)


def test_lexical_fields_visibility_and_background_relations(corpus):
    result = build(corpus)
    assert result['declarations'] == 7
    assert result['backgrounds'] == 2
    row, = query(corpus, 'Outer.Inner.phaseGap', limit=1)['results']
    assert row['source_signature'] == ': Nat'
    assert row['variable_context'] == ['variable (n : Nat)']
    assert row['adjacent_doc'] == 'Approximate reflection phase gap bound [label][Local2026].'
    assert row['module'] == 'Lib.Main'
    assert row['cite_keys'] == ['Local2026', 'Paper2026']
    assert len(row['backgrounds']) == 2
    assert row['line'] == SOURCE[:SOURCE.index('noncomputable def')].count('\n') + 1
    assert 'applicability not checked' in row['verification']
    assert query(corpus, 'secret')['results'] == []
    assert query(corpus, 'hidden')['results'] == []
    channel = query(corpus, 'Outer.Inner.channel', limit=1)['results'][0]
    assert channel['variable_context'] == []
    assert len(channel['backgrounds']) == 1  # section context did not escape
    with sqlite3.connect(corpus[2]) as c:
        assert c.execute("SELECT doc FROM records WHERE fqn='Outer.Inner.phaseGap'").fetchone()[0].find('postselection') == -1


def test_literal_tokens_camelcase_fts_and_filters(corpus):
    build(corpus)
    assert r.tokens('ChoiMatrix_phaseGap') == ['choi', 'matrix', 'phase', 'gap']
    assert r.fts_query(['a" OR 1', 'NEAR']) == '"a"" OR 1" AND "NEAR"'
    assert query(corpus, 'phaseGap')['results'][0]['source_fqn'] == 'Outer.Inner.phaseGap'
    assert query(corpus, 'Choi matrix channel', kind='lemma', repo='Local')['results'][0]['source_fqn'] == 'Outer.Inner.channel'
    assert query(corpus, 'Choi matrix', repo='Other')['results'] == []
    assert query(corpus, 'phase gap', cite='Local2026')['results'][0]['source_fqn'] == 'Outer.Inner.phaseGap'
    assert query(corpus, '', cite='NotInBib')['results'] == []
    assert query(corpus, 'zzzxq nonexistentword', cite='Paper2026')['results'] == []
    for text in ['" OR * NEAR(', 'name:phase -signature:gap', "'; DROP TABLE records; --", '{}[]():^*', '“phase” gap']:
        query(corpus, text)
    assert query(corpus, 'Outer.Inner.bound')['results'][0]['source_fqn'] == 'Outer.Inner.bound'
    for kwargs in [dict(limit=0), dict(limit=101)]:
        with pytest.raises(ValueError, match='limit'):
            query(corpus, 'x', **kwargs)
    with pytest.raises(ValueError, match='32'):
        query(corpus, ' '.join('word' + str(i) for i in range(33)))


def test_incremental_add_change_delete_and_stale_hash(corpus, tmp_path):
    build(corpus)
    assert build(corpus, changed=True)['updated_files'] == 0
    project, source, db = corpus
    old_stat = source.stat()
    source.write_text(SOURCE.replace('Choi matrix', 'Choi tensor'))
    # Size/mtime are never accepted as content identity.
    import os
    os.utime(source, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    with pytest.raises(ValueError, match='stale source'):
        query(corpus, 'Outer.Inner.channel')
    assert build(corpus, changed=True)['updated_files'] == 1
    new = project / 'src/Lib/New.lean'
    new.write_text('theorem fresh : True := True.intro\n')
    assert build(corpus, changed=True)['files'] == 2
    assert query(corpus, 'fresh')['results'][0]['source_fqn'] == 'fresh'
    new.rename(tmp_path / 'removed.lean')
    build(corpus, changed=True)
    assert not query(corpus, 'fresh')['results']
    with sqlite3.connect(db) as c:
        assert c.execute('SELECT count(*) FROM files').fetchone()[0] == 1
        assert not c.execute("SELECT id FROM records WHERE fqn='fresh'").fetchall()


def test_query_missing_stale_config_and_scanner_no_writes(corpus):
    project, source, db = corpus
    with pytest.raises(ValueError, match='index missing'):
        query(corpus, 'bound')
    assert not db.parent.exists()
    build(corpus)
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
              for root in (project, db.parent) for p in root.rglob('*') if p.is_file()}
    original_run = r.git.__globals__['subprocess'].run

    def only_git(args, **kwargs):
        assert args[0] == 'git'
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == '0'
        assert kwargs['env']['GIT_NO_LAZY_FETCH'] == '1'
        assert kwargs['env']['GIT_ALLOW_PROTOCOL'] == ''
        assert 'status' not in args
        return original_run(args, **kwargs)

    with patch('qatlib_search.retrieve_corpus.subprocess.run', side_effect=only_git), \
            patch('socket.socket', side_effect=AssertionError('network forbidden')):
        query(corpus, 'phase gap')
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
             for root in (project, db.parent) for p in root.rglob('*') if p.is_file()}
    assert before == after
    with patch.object(r, 'scanner_identity', return_value='changed'):
        with pytest.raises(ValueError, match='version'):
            query(corpus, 'bound')
    (project / 'lean-toolchain').write_text('different\n')
    with pytest.raises(ValueError, match='stale corpus'):
        query(corpus, 'bound')


def test_fts5_fallback_is_explicit_and_useful(corpus):
    result = build(corpus, use_fts=False)
    assert result['backend'] == 'tokens-fallback'
    result = query(corpus, 'Choi matrix channel')
    assert result['backend'] == 'tokens-fallback'
    assert result['warnings']
    assert result['results'][0]['source_fqn'] == 'Outer.Inner.channel'


def test_fts5_unavailable_at_build_and_read(corpus):
    class NoFTS:
        def __init__(self, connection):
            self.connection = connection
        def executescript(self, sql):
            return self.connection.executescript(sql)
        def execute(self, sql, *args):
            if 'CREATE VIRTUAL TABLE' in sql:
                raise sqlite3.OperationalError('no such module: fts5')
            return self.connection.execute(sql, *args)
    with sqlite3.connect(':memory:') as c:
        assert r.create_schema(NoFTS(c), use_fts=True) == 'tokens-fallback'
    build(corpus)
    connect = sqlite3.connect
    class Reader(sqlite3.Connection):
        def execute(self, sql, *args):
            if 'search MATCH' in sql:
                raise sqlite3.OperationalError('no such module: fts5')
            return super().execute(sql, *args)
    with patch.object(r.sqlite3, 'connect', side_effect=lambda *a, **kw: connect(*a, **kw, factory=Reader)):
        result = query(corpus, 'Choi matrix channel')
    assert result['backend'] == 'tokens-fallback'
    assert result['results'][0]['source_fqn'] == 'Outer.Inner.channel'


def test_citation_examples_are_not_bibliographic_evidence():
    text = '[Paper2026] `[Code2026]`\n```lean\n[Example2026]\n```\n'
    assert r.cite_keys(text, {'Paper2026', 'Code2026', 'Example2026'}) == ['Paper2026']


def test_english_inflections_and_backgrounds_do_not_displace_declarations(corpus):
    project, source, _ = corpus
    source.write_text(SOURCE.replace('normalization', 'normalizing'))
    build(corpus)
    result = query(corpus, 'measurement normalization')
    assert any(row['source_fqn'] == 'Outer.Inner.phaseGap' for row in result['results'])
    assert result['results'][0]['kind'] != 'module'
    source.write_text(SOURCE + '\npublic def ChoiMatrix : Nat := 0\n')
    build(corpus, changed=True)
    result = query(corpus, 'Choi matrix channel')
    assert result['results'][0]['source_fqn'] == 'ChoiMatrix'
    assert 'name-phrase' in result['results'][0]['matched_fields']


def test_class_inductive_uses_the_real_name_and_original_offsets(corpus):
    project, source, _ = corpus
    source.write_text('module\npublic section\nnamespace N\n'
                      '/-- An inductive class. -/\nclass inductive Thing where\n'
                      '  | intro\nend N\n')
    build(corpus)
    row = query(corpus, 'N.Thing')['results'][0]
    assert row['source_fqn'] == 'N.Thing'
    assert row['kind'] == 'class'
    assert row['line'] == 5
    assert row['adjacent_doc'] == 'An inductive class.'
    assert all(row['source_fqn'] != 'N.inductive' for row in query(corpus, 'N.inductive')['results'])


def test_git_filters_are_never_executed(corpus, tmp_path):
    project, source, _ = corpus
    (project / '.gitattributes').write_text('src/Lib/*.lean filter=hostile\n')
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    snapshot(project)
    marker = tmp_path / 'filter-ran'
    command = "sh -c 'echo filter-ran > " + str(marker) + "; cat'"
    subprocess.run(['git', '-C', str(project), 'config', 'filter.hostile.clean', command], check=True)
    source.write_text(SOURCE + '\n-- change forces a fresh content comparison\n')
    build(corpus)
    query(corpus, 'bound')
    assert not marker.exists()


def test_query_keeps_one_generation_while_a_writer_commits(corpus):
    import threading
    import time
    build(corpus)
    changed = threading.Event()
    committed = threading.Event()
    errors = []
    threads = []
    original = r.metadata

    def observe(c):
        old = original(c)
        def writer():
            try:
                with sqlite3.connect(corpus[2], timeout=3) as other:
                    other.execute("UPDATE records SET adjacent_doc='new generation' WHERE fqn='Outer.Inner.bound'")
                    changed.set()
                committed.set()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=writer)
        threads.append(thread)
        thread.start()
        assert changed.wait(2)
        time.sleep(.05)  # give an unprotected writer the opportunity to commit
        return old

    with patch.object(r, 'metadata', side_effect=observe):
        row = query(corpus, 'Outer.Inner.bound', limit=1)['results'][0]
    for thread in threads:
        thread.join(3)
    assert not errors
    assert committed.is_set()
    assert row['adjacent_doc'] == 'Reverse Markov mean coverage.'


@pytest.mark.parametrize('backend', [True, False])
def test_repo_and_kind_filters_apply_before_candidate_limit(corpus, backend):
    project, _, _ = corpus
    other = project / 'src/Lib/Other.lean'
    other.write_text('namespace Other\n' + ''.join(
        f'/-- Matrix channel. -/\ndef matrix{i} : Nat := 0\n' for i in range(100)) +
        '/-- Matrix channel theorem. -/\ntheorem target : True := True.intro\nend Other\n')
    build(corpus, use_fts=backend)
    with patch.object(r, 'MAX_CANDIDATES', 10):
        rows = query(corpus, 'matrix channel', repo='Local', kind='theorem')['results']
    assert [row['source_fqn'] for row in rows] == ['Other.target']


@pytest.mark.parametrize('backend', [True, False])
def test_cite_filter_keeps_premise_only_matches_and_filters_before_expansion(corpus, backend):
    project, source, _ = corpus
    source.write_text('/-! specialphase background. -/\n' + ''.join(
        f'/-- An unrelated result. -/\ntheorem unrelated{i} : True := True.intro\n' for i in range(20)) +
        '/-- A cited result [Paper2026]. -/\ntheorem cited : True := True.intro\n')
    build(corpus, use_fts=backend)
    rows = query(corpus, 'specialphase', cite='Paper2026')['results']
    assert [row['source_fqn'] for row in rows] == ['cited']
    assert rows[0]['cite_keys'] == ['Paper2026']


def test_deleted_source_dirty_identity_and_corrupt_index(corpus, tmp_path):
    project, source, db = corpus
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    snapshot(project)
    build(corpus)
    assert query(corpus, 'bound')['results'][0]['dirty'] is False
    source.rename(tmp_path / 'deleted.lean')
    result = build(corpus, changed=True)
    assert result['files'] == 0
    with sqlite3.connect(db) as c:
        meta = json.loads(c.execute("SELECT value FROM meta WHERE key='snapshot'").fetchone()[0])
    assert meta['dirty'][str(project)] is True
    corrupt = tmp_path / 'corrupt.sqlite3'
    corrupt.write_bytes(b'not a database')
    with pytest.raises(ValueError, match='incompatible index'):
        r.query('bound', project, corrupt)


def test_optional_map_version_existence_and_provenance(corpus, tmp_path):
    project, source, _ = corpus
    subprocess.run(['git', 'init', '-q', str(project)], check=True)
    revision = snapshot(project)
    build(corpus)
    mapping = tmp_path / 'map.json'
    data = dict(format='qatlib-retrieve-map/v1', source_revision=revision, entries=[
        dict(fqn='Outer.Inner.bound', title='Unusual vocabulary', terms=['zebrafish'], cite_keys=['Added2026']),
        dict(fqn='Absent.result', terms=['zebrafish'])])
    mapping.write_text(json.dumps(data))
    result = query(corpus, 'zebrafish', map_path=mapping)
    assert [row['source_fqn'] for row in result['results']] == ['Outer.Inner.bound']
    assert result['results'][0]['mapping']['title'] == 'Unusual vocabulary'
    assert result['results'][0]['cite_keys'] == ['Paper2026']
    assert result['warnings'] == ['mapping FQN absent from source: Absent.result']
    assert query(corpus, '', cite='Added2026', map_path=mapping)['results']
    data['source_revision'] = 'f' * 40
    mapping.write_text(json.dumps(data))
    assert 'unused' in query(corpus, 'zebrafish', map_path=mapping)['warnings'][0]
    mapping.write_text('{broken')
    assert 'unused' in query(corpus, 'bound', map_path=mapping)['warnings'][0]
    data['source_revision'] = revision
    mapping.write_text(json.dumps(data))
    source.write_text(SOURCE + '\n-- dirty\n')
    build(corpus, changed=True)
    assert 'dirty' in query(corpus, 'zebrafish', map_path=mapping)['warnings'][0]


def test_locked_dependency_discovery_bibliography_and_pin(corpus):
    project, _, _ = corpus
    upstream = project / '.lake/packages/Upstream'
    (upstream / 'Upstream').mkdir(parents=True)
    (upstream / 'docs').mkdir()
    (upstream / 'lakefile.toml').write_text('name="Upstream"\n[[lean_lib]]\nname="Upstream"\n')
    (upstream / 'Upstream/Basic.lean').write_text('/-- Upstream bound [Up2026]. -/\ntheorem up : True := True.intro\n')
    (upstream / 'docs/references.bib').write_text('@article{Up2026, title={Up}}\n')
    subprocess.run(['git', 'init', '-q', str(upstream)], check=True)
    revision = snapshot(upstream)
    (project / 'lake-manifest.json').write_text(json.dumps(dict(packages=[
        dict(name='Upstream', type='git', rev=revision),
        dict(name='Absent', type='git', rev='f' * 40),
        dict(name='Ignored', inherited=True)])))
    assert discover(project).bibliography == {'Paper2026', 'Local2026', 'Up2026'}
    result = build(corpus)
    assert result['files'] == 2
    assert result['warnings'] == ['Absent: local TOML library unavailable; omitted (no fetch)']
    row = query(corpus, 'up', repo='Upstream')['results'][0]
    assert row['revision'] == revision
    assert row['cite_keys'] == ['Up2026']
    (upstream / 'Upstream/Basic.lean').write_text('theorem forged : True := True.intro\n')
    with pytest.raises(ValueError, match='Git blob'):
        build(corpus, changed=True)
    # A failed generation must roll back all rows and metadata.
    with sqlite3.connect(corpus[2]) as c:
        assert c.execute("SELECT fqn FROM records WHERE fqn='up'").fetchone()
    snapshot(upstream)
    with pytest.raises(ValueError, match='locked revision'):
        query(corpus, 'up')


def test_cli_parameter_positions_aliases_help_and_errors(corpus, capsys):
    project, _, db = corpus
    for argv in [['--project', str(project), '--help'], ['query', '--project', str(project), '--help'],
                 ['--project', str(project), 'find', '--help']]:
        with pytest.raises(SystemExit) as exc:
            r.main(argv)
        assert exc.value.code == 0
    assert r.main(['--project', str(project), 'index', '--db', str(db)]) == 0
    capsys.readouterr()
    for argv in [['--project', str(project), '--db', str(db), 'Outer.Inner.bound', '--json'],
                 ['query', 'bound', '--project', str(project), '--db', str(db), '--json'],
                 ['find', 'bound', '--project', str(project), '--db', str(db), '--json']]:
        assert r.main(argv) == 0
        assert json.loads(capsys.readouterr().out)['results'][0]['source_fqn'] == 'Outer.Inner.bound'
    assert r.main(['query', 'bound', '--project', str(project), '--db', str(db), '--limit', '0']) == 2
    assert r.main(['--project', str(project), '--db', str(db), '--limit', '1', 'bound']) == 0
    with patch.object(r, 'build_index', side_effect=AssertionError('literal index must not write')):
        assert r.main(['--project', str(project), '--db', str(db), '--json', '--', 'index']) == 0


def test_background_only_generated_gap_and_path_safety(corpus, tmp_path):
    project, source, _ = corpus
    source.write_text('/-! Spectral parameter background. -/\nrun_cmd pure ()\n')
    result = build(corpus)
    assert result['parse_warnings']
    result = query(corpus, 'spectral parameter')
    assert result['results'][0]['kind'] == 'module'
    assert result['results'][0]['parse_warning']
    with pytest.raises(ValueError, match='outside'):
        r.build_index(project, project / 'cache.sqlite3')
    source.rename(tmp_path / 'source.lean')
    source.symlink_to(tmp_path / 'source.lean')
    with pytest.raises(ValueError, match='symlink'):
        build(corpus)
