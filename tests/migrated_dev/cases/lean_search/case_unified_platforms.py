"""GitHub and Gitea sources retain distinct identities through retrieval."""
import tests._bootstrap as _bootstrap  # noqa: F401
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from qatlib_forge.repo_api import RepoRef, RepoAPI
from qatlib_search import unified_retrieval as retrieval, proof_index


def archive():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as output:
        for name, text in [('lakefile.toml', 'name="sample"\n[[lean_lib]]\nname="Sample"'),
                           ('Sample/Main.lean', 'namespace Sample\n/-- A sample. -/\ntheorem sample : True := trivial\nend Sample\n')]:
            data = text.encode(); member = tarfile.TarInfo('snapshot/' + name); member.size = len(data)
            output.addfile(member, io.BytesIO(data))
    return stream.getvalue()


class UnifiedPlatformsTests(unittest.TestCase):
    def test_discover_both_upstreams_and_reject_ambiguous_slug(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'lakefile.toml').write_text('name="sample"')
            (root/'lake-manifest.json').write_text(json.dumps({'packages': [
                {'name': 'Qatlib', 'type': 'git', 'url': 'https://github.com/quantum-atlas-ai/qatlib.git', 'rev': 'a'*40},
                {'name': 'example_library', 'type': 'git', 'url': 'git@mirror.invalid:qatlas-lean-org/qatlib-tools.git', 'rev': 'b'*40}]}))
            with patch.object(retrieval, '_repository_fields', return_value={'repository': None}):
                sources = retrieval.discover(root)
            upstream = [s for s in sources if s['kind']=='upstream']
            self.assertEqual({s['platform'] for s in upstream}, {'github', 'gitea'})
            catalog = root / 'retrieval-catalog.json'
            catalog.write_text(json.dumps({'schema': retrieval.CATALOG_SCHEMA, 'sources': [{'id':'candidate', 'repository':'a/b'}]}))
            with patch.object(retrieval, '_repository_fields', return_value={'repository': None}), self.assertRaisesRegex(ValueError, 'explicit platform or host'):
                retrieval.discover(root)
            catalog.write_text(json.dumps({'schema': retrieval.CATALOG_SCHEMA, 'sources': [{'id':'candidate', 'repository':'https://github.com/a/b', 'pr': 7}]}))
            with patch.object(retrieval, '_repository_fields', return_value={'repository': None}):
                source = retrieval.discover(root)[-1]
            self.assertEqual((source['platform'], source['web_host'], source['repository']), ('github','github.com','a/b'))

    def test_remote_archives_on_both_platforms(self):
        for host in ('github.com', 'mirror.invalid'):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as temporary:
                ref = RepoRef.parse('https://' + host + '/a/b')
                source = {'id':'upstream', 'kind':'upstream', **retrieval._repo_fields(ref)}
                client = RepoAPI(ref, token='test')
                with patch.object(client, 'request', side_effect=[{'default_branch':'main'}, {'sha':'a'*40}]), \
                     patch.object(client, 'archive', return_value=archive()):
                    result = retrieval._remote(Path(temporary), source, refresh=True, api=client)
                self.assertEqual(result['platform'], ref.platform)
                self.assertEqual(result['web_host'], host)
                self.assertIn('Sample/Main.lean', result['files'])

    def test_pr_fork_gets_new_client_and_fork_archive(self):
        ref = RepoRef.parse('https://github.com/base/project')
        fork = RepoRef.parse('https://github.com/fork/project')
        source = {'id':'pr', 'kind':'pull-request', 'pr':4, **retrieval._repo_fields(ref)}
        client = RepoAPI(ref, token='base-secret'); target = RepoAPI(fork, token='fork-secret')
        pr = {'merged':False, 'head':{'sha':'b'*40,'repo':{'clone_url':fork.web_url + '.git'}}}
        with tempfile.TemporaryDirectory() as temporary, patch.object(client, 'request', return_value=pr), \
             patch.object(client, 'for_repo', return_value=target) as resolve, \
             patch.object(target, 'archive', return_value=archive()) as download:
            result = retrieval._remote(Path(temporary), source, refresh=True, api=client)
        resolve.assert_called_once_with(fork)
        download.assert_called_once_with('b'*40, max_bytes=retrieval.MAX_ARCHIVE)
        self.assertEqual(result['repository'], fork.slug)
        self.assertFalse(result['merged'])

    def test_platform_and_host_participate_in_deduplication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = [
                {'id':'local', 'kind':'dependency', 'root':str(root), 'repository':'a/b', 'platform':'github', 'web_host':'github.com'},
                {'id':'gitea', 'kind':'upstream', 'repository':'a/b', 'platform':'gitea', 'web_host':'mirror.invalid'},
                {'id':'github', 'kind':'upstream', 'repository':'a/b', 'platform':'github', 'web_host':'github.com'},
            ]
            index = {'version':'a'*40, 'repository':'a/b', 'merged':True, 'files':{}}
            with patch.object(retrieval, 'discover', return_value=sources), patch.object(retrieval, '_local', return_value=index), \
                 patch.object(retrieval, '_remote', return_value=index):
                corpora, _ = retrieval.collect(root)
            self.assertEqual(corpora[1]['availability'], 'dependency-update-required')
            self.assertEqual(corpora[2]['availability'], 'local-checkable')
            self.assertNotEqual(retrieval._cache(root, sources[1]), retrieval._cache(root, sources[2]))

    def test_regex_worker_uses_both_source_packages(self):
        records = [{'text':'theorem sample : True', 'kind':'source', 'path':'Sample.lean', 'line':1}]
        matches = proof_index.search({'records': records}, 'sample', mode='regex', limit=5)['results']
        self.assertTrue(matches)
