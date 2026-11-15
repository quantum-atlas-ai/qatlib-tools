"""Offline contracts for host boundaries and both qatlib_forge protocols."""
import tests._bootstrap as _bootstrap  # noqa: F401
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from qatlib_forge import repo_api as api

GH = api.RepoRef.parse('https://github.com/example/tools.git')
GT = api.RepoRef.parse('git@mirror.test:example/tools.git', platform='gitea')


class Response:
    def __init__(self, value=None, *, headers=None, raw=None):
        self.data = json.dumps(value).encode() if raw is None else raw
        self.headers = headers or {}
        self.status = 200
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


class RepoAPITests(unittest.TestCase):
    def test_url_forms_and_explicit_context(self):
        for value in ('https://github.com/example/tools',
                      'ssh://git@github.com/example/tools.git',
                      'git@github.com:example/tools.git',
                      'https://api.github.com/repos/example/tools'):
            self.assertEqual(api.RepoRef.parse(value), GH)
        self.assertEqual(api.RepoRef.parse(GH.slug, web_host='api.github.com'), GH)
        self.assertEqual(GT.platform, 'gitea')
        self.assertEqual(api.RepoRef.parse('a/b', platform='gitea').web_host, 'mirror.test')
        self.assertEqual(api.RepoRef.parse('https://mirror.test/a/b', platform='gitea').platform, 'gitea')
        for value in ('a/b', 'https://mirror.test/a/b', 'https://github.com/a/b/tree/main',
                      'https://token@github.com/a/b', 'https://github.com/a/../b'):
            with self.subTest(value=value), self.assertRaises(api.RepoError):
                api.RepoRef.parse(value)
        with self.assertRaises(api.RepoError):
            api.RepoRef.parse(GH.web_url, platform='gitea')
        with self.assertRaises(api.RepoError):
            api.RepoRef.parse(GH.web_url, api_base='https://evil.example')

    def test_platform_flag_resolves_custom_gitea_without_network(self):
        import contextlib
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(api.main(['--platform', 'gitea', 'https://mirror.test/a/b']), 0)
        result = json.loads(output.getvalue())
        self.assertEqual((result['platform'], result['web_host']), ('gitea', 'mirror.test'))

    def test_git_remote_is_resolved(self):
        with patch.object(api.subprocess, 'run', return_value=Mock(stdout=GT.web_url)) as run:
            self.assertEqual(api.RepoRef.from_remote(Path('.'), platform='gitea'), GT)
            self.assertEqual(run.call_args.args[0], ['git', 'remote', 'get-url', 'origin'])

    def test_environment_tokens_are_platform_isolated(self):
        env = {'GH_TOKEN': 'gh', 'GITHUB_TOKEN': 'github', 'GITEA_TOKEN': 'gt'}
        with patch.object(api, '_command_token') as command:
            self.assertEqual(api.auth_token(GH, env=env), 'gh')
            self.assertEqual(api.auth_token(GH, env={'GITHUB_TOKEN': 'github'}), 'github')
            self.assertEqual(api.auth_token(GT, env=env), 'gt')
            command.assert_not_called()
        other = api.RepoRef.parse('https://another.example/a/b', platform='gitea')
        with patch.object(api, '_command_token', return_value=None), patch.object(api, '_config_token', return_value=None):
            self.assertIsNone(api.auth_token(other, env=env))

    def test_github_cli_precedes_git_and_ignores_gitea(self):
        with patch.object(api, '_command_token', side_effect=['cli', 'git']) as command:
            self.assertEqual(api.auth_token(GH, env={'GITEA_TOKEN': 'secret'}), 'cli')
            command.assert_called_once_with(['gh', 'auth', 'token', '--hostname', 'github.com'])
        with patch.object(api, '_command_token', side_effect=[None, 'git']) as command:
            self.assertEqual(api.auth_token(GH, env={}), 'git')
            self.assertEqual(command.call_args.kwargs['credential_host'], 'github.com')

    def test_gitea_full_chain_and_host_scoped_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / '.config/gt'; config.mkdir(parents=True)
            (config / 'config.json').write_text(json.dumps({'logins': [
                {'url': 'https://wrong.example', 'token': 'wrong'},
                {'url': 'https://mirror.test', 'token': 'config'}]}))
            with patch.dict(api.os.environ, {'XDG_CONFIG_HOME': str(home / '.config')}), \
                 patch.object(api, '_command_token', side_effect=[None, None]) as command:
                self.assertEqual(api.auth_token(GT, env={}, home=home), 'config')
            self.assertEqual(command.call_args_list[0].args[0][0], 'gt')
            self.assertEqual(command.call_args_list[1].args[0][0], 'tea')
            (config / 'config.json').write_text('{}')
            (config / 'config.yml').write_text('logins:\n- name: wrong\n  url: https://wrong.example\n  token: wrong\n- name: correct\n  url: https://mirror.test\n  token: yaml\n')
            with patch.dict(api.os.environ, {'XDG_CONFIG_HOME': str(home / '.config')}):
                self.assertEqual(api._config_token(GT.web_host, home), 'yaml')
        with patch.object(api, '_command_token', side_effect=[None, None, 'git']), \
             patch.object(api, '_config_token', return_value=None):
            self.assertEqual(api.auth_token(GT, env={}), 'git')

    def test_helper_cannot_return_another_host(self):
        with patch.object(api.subprocess, 'run', return_value=Mock(returncode=0, stdout='host=wrong.example\npassword=bad\n')):
            self.assertIsNone(api._command_token(['git', 'credential', 'fill'], credential_host='github.com'))

    def test_create_draft_and_ready_mutations(self):
        for ref in (GH, GT):
            client = api.RepoAPI(ref, token='test')
            with patch.object(client, 'request', return_value={}) as request:
                client.create_pull_request(title='Topic', head='topic', draft=True)
                payload = request.call_args.kwargs['payload']
                self.assertEqual(payload.get('draft'), True if ref.platform == 'github' else None)
                self.assertEqual(payload['title'], 'Topic' if ref.platform == 'github' else 'WIP: Topic')
            with patch.object(client, 'pull_request', return_value={'title': 'WIP: Topic', 'node_id': 'PR_node'}), \
                 patch.object(client, 'request', return_value={}) as request:
                for draft, mutation in [(True, 'convertPullRequestToDraft'), (False, 'markPullRequestReadyForReview')]:
                    client.set_draft(7, draft)
                    payload = request.call_args.kwargs['payload']
                    if ref.platform == 'github':
                        self.assertEqual(request.call_args.args[0], 'graphql')
                        self.assertIn(mutation, payload['query'])
                        self.assertEqual(payload['variables'], {'id': 'PR_node'})
                    else:
                        self.assertEqual(payload['title'], ('WIP: ' if draft else '') + 'Topic')

    def test_branch_sha_normalization(self):
        for ref, key in [(GH, 'sha'), (GT, 'id')]:
            client = api.RepoAPI(ref, token='test')
            with patch.object(client, 'request', return_value={'commit': {key: 'abc'}}):
                self.assertEqual(client.branch('feature/topic')['sha'], 'abc')

    def test_paginate_both_platforms_with_link_and_total(self):
        for ref, key in [(GH, 'per_page'), (GT, 'limit')]:
            client = api.RepoAPI(ref, token='test')
            following = client.base + f'repos/{ref.slug}/pulls?page=2&{key}=100'
            client.opener = Mock()
            client.opener.open.side_effect = [Response([{'id': 1}], headers={'Link': f'<{following}>; rel="next"', 'X-Total-Count': '2'}),
                                                Response([{'id': 2}], headers={'X-Total-Count': '2'})]
            self.assertEqual(len(client.request(client.endpoint + '/pulls', paginate=True)), 2)
            url = client.opener.open.call_args_list[0].args[0].full_url
            self.assertEqual(parse_qs(urlsplit(url).query)[key], ['100'])

    def test_full_page_without_link_fetches_next_page(self):
        client = api.RepoAPI(GH, token='test'); client.opener = Mock()
        client.opener.open.side_effect = [Response([{}]*100), Response([{'id': 101}])]
        self.assertEqual(len(client.request(client.endpoint+'/pulls', paginate=True)), 101)
        self.assertIn('page=2', client.opener.open.call_args.args[0].full_url)

    def test_pagination_rejects_incomplete_cyclic_and_foreign_links(self):
        for headers in ({'X-Total-Count': '2'}, {'Link': '<https://evil.example/list>; rel="next"'},
                        {'Link': '<https://api.github.com/list?per_page=100&page=1>; rel="next"'}):
            client = api.RepoAPI(GH, token='test'); client.opener = Mock()
            client.opener.open.return_value = Response([{}], headers=headers)
            with self.assertRaises(api.RepoError):
                client.request('list', paginate=True)

    def test_github_discussions_have_three_independent_streams(self):
        client = api.RepoAPI(GH, token='test')
        with patch.object(client, 'request', side_effect=[[{'id': 1}], [{'id': 2}], [{'id': 3}]]) as request:
            self.assertEqual([kind for kind, row in client.discussion_records(3)], ['issue', 'review', 'inline'])
        self.assertTrue(request.call_args_list[2].args[0].endswith('/pulls/3/comments'))

    def test_gitea_review_comments_are_nested(self):
        client = api.RepoAPI(GT, token='test')
        with patch.object(client, 'request', side_effect=[[], [{'id': 7, 'comments_count': 1}], [{'id': 8}]]) as request:
            self.assertEqual([kind for kind, row in client.discussion_records(3)], ['review', 'inline'])
        self.assertTrue(request.call_args_list[2].args[0].endswith('/reviews/7/comments'))

    def test_compare_does_not_silently_drop_github_files(self):
        for size, expected in [(299, False), (300, True), (301, True)]:
            client = api.RepoAPI(GH, token='test')
            with patch.object(client, 'request', return_value={'files': [{}]*size}):
                self.assertEqual(client.compare('base', 'head')['incomplete'], expected)
        client = api.RepoAPI(GT, token='test')
        with patch.object(client, 'request', return_value={}):
            self.assertTrue(client.compare('base', 'head')['incomplete'])

    def test_gitea_compare_collects_commit_file_lists_and_falls_back_to_details(self):
        client = api.RepoAPI(GT, token='test')
        response = {'total_commits': 2, 'commits': [
            {'sha': 'first', 'files': [{'filename': 'B.lean'}, {'filename': 'A.lean'}]},
            {'sha': 'second'}]}
        with patch.object(client, 'request', side_effect=[response, {'files': [{'filename': 'A.lean'}, {'filename': 'C.lean'}]}]) as request:
            result = client.compare('base', 'head')
        self.assertEqual([item['filename'] for item in result['files']], ['A.lean', 'B.lean', 'C.lean'])
        self.assertFalse(result['incomplete'])
        self.assertEqual(result['file_scope'], 'commit-union')
        self.assertIn('/git/commits/second?files=true', request.call_args.args[0])

    def test_gitea_compare_marks_missing_commits_or_files_incomplete(self):
        client = api.RepoAPI(GT, token='test')
        for response in ({'total_commits': 2, 'commits': []},
                         {'total_commits': 1, 'commits': [{}]},
                         {'total_commits': 1, 'commits': [{'files': [{}]}]}):
            with patch.object(client, 'request', return_value=response):
                self.assertTrue(client.compare('base', 'head')['incomplete'])
        with patch.object(client, 'request', return_value={'total_commits': 0, 'commits': []}):
            self.assertFalse(client.compare('same', 'same')['incomplete'])

    def test_pull_files_share_pagination_and_report_github_ceiling(self):
        for ref in (GH, GT):
            client = api.RepoAPI(ref, token='test')
            with patch.object(client, 'request', return_value=[{}]*3000) as request:
                result = client.pull_files(7)
            self.assertTrue(request.call_args.kwargs['paginate'])
            self.assertEqual(result['incomplete'], ref.platform == 'github')

    def test_github_archive_redirect_strips_authorization(self):
        client = api.RepoAPI(GH, token='secret'); client.opener = Mock()
        target = 'https://codeload.github.com/a/b/tar.gz/abc'
        client.opener.open.side_effect = [HTTPError(client.base, 302, 'redirect', {'Location': target}, io.BytesIO()), Response(raw=b'tar')]
        self.assertEqual(client.archive('abc'), b'tar')
        requests = [call.args[0] for call in client.opener.open.call_args_list]
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer secret')
        self.assertIsNone(requests[1].get_header('Authorization'))

    def test_gitea_and_untrusted_archive_redirects_are_rejected(self):
        for ref, target in [(GT, GT.web_url), (GH, 'https://evil.example/data'), (GH, 'http://codeload.github.com/data')]:
            client = api.RepoAPI(ref, token='secret'); client.opener = Mock()
            client.opener.open.side_effect = HTTPError(client.base, 302, 'redirect', {'Location': target}, io.BytesIO())
            with self.assertRaises(api.RepoError):
                client.archive('abc')
            self.assertEqual(client.opener.open.call_count, 1)

    def test_archive_budget_and_graphql_errors(self):
        client = api.RepoAPI(GH, token='test'); client.opener = Mock()
        client.opener.open.return_value = Response(raw=b'12345')
        with self.assertRaises(api.RepoError):
            client.archive('abc', max_bytes=4)
        client.opener.open.return_value = Response({'errors': [{'message': 'failed'}]})
        with self.assertRaises(api.RepoError):
            client.request('graphql', method='POST', payload={})
