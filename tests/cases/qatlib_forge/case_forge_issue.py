"""Offline contracts for the generic qatlib_forge PR and Issue surface."""
import tests._bootstrap as _bootstrap  # noqa: F401
import unittest
from unittest.mock import patch

from qatlib_forge.repo_api import RepoAPI, RepoRef


GH = RepoRef.parse("https://github.com/a/b")
GT = RepoRef.parse("https://mirror.invalid/a/b", platform="gitea")


class ForgeIssueTests(unittest.TestCase):
    def test_github_issue_listing_removes_pull_requests_and_filters_labels(self):
        client = RepoAPI(GH, token="x")
        rows = [{"number": 1, "labels": [{"name": "docs"}]},
                {"number": 2, "pull_request": {"url": "..."}, "labels": [{"name": "docs"}]},
                {"number": 3, "labels": [{"name": "bug"}]}]
        with patch.object(client, "request", return_value=rows) as request:
            result = client.list_issues(labels=["docs"])
        self.assertEqual([row["number"] for row in result], [1])
        self.assertIn("labels=docs", request.call_args.args[0])

    def test_create_and_update_issue_use_common_payload_on_both_platforms(self):
        for ref in (GH, GT):
            client = RepoAPI(ref, token="x")
            with patch.object(client, "request", return_value={}) as request:
                client.create_issue(title="Title", body="Body", labels=["docs"], assignees=["alice"])
                self.assertEqual(request.call_args.kwargs["payload"], {
                    "title": "Title", "body": "Body", "labels": ["docs"], "assignees": ["alice"]})
                client.update_issue(4, title="New", state="closed", labels=["done"])
                self.assertEqual(request.call_args.kwargs["payload"], {
                    "title": "New", "state": "closed", "labels": ["done"]})

    def test_pull_request_list_update_and_review_comment(self):
        client = RepoAPI(GT, token="x")
        with patch.object(client, "request", return_value=[]) as request:
            self.assertEqual(client.list_pull_requests(base="main", labels=["docs"]), [])
            self.assertIn("base=main", request.call_args.args[0])
            client.update_pull_request(7, title="New", labels=["docs"])
            self.assertEqual(request.call_args.kwargs["payload"], {"title": "New", "labels": ["docs"]})
            client.add_review_comment(7, "Inline", path="README.md", line=3)
            self.assertEqual(request.call_args.args[0], "repos/a/b/pulls/7/reviews")


if __name__ == "__main__":
    unittest.main()
