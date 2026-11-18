from datetime import datetime, timezone
import json
from unittest.mock import Mock

import pytest

from qatlib_forge import pr_evidence, pr_writes
from qatlib_forge.pr_paths import classify
from qatlib_forge.repo_api import RepoAPI, RepoError, RepoRef
from tests._git_fixture import snapshot


SHA = "a" * 40


def api_for(platform):
    host = "github.com" if platform == "github" else "git.example.com"
    return RepoAPI(RepoRef.parse(f"https://{host}/team/repo", platform=platform), token="")


def test_wrapped_pages_and_exact_gitea_sha():
    api = api_for("gitea")

    def page(number, rows):
        response = Mock(status=200, headers={
            "Link": f'<{api.base}repos/team/repo/actions/runs?head_sha={SHA}&limit=1&page=2>; rel="next"'
        } if number == 1 else {})
        response.read.return_value = json.dumps({"workflow_runs": rows, "total_count": 2}).encode()
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        return context

    api.opener = Mock()
    api.opener.open.side_effect = [page(1, [{"id": 2, "head_sha": "b" * 40}]),
                                  page(2, [{"id": 1, "head_sha": SHA}])]
    assert api.action_runs(head_sha=SHA) == [{"id": 1, "head_sha": SHA}]
    assert api.opener.open.call_count == 2


def test_gitea_exact_job_corrects_only_its_own_summary():
    api = api_for("gitea")
    api.pull_request = Mock(return_value={"head": {"sha": SHA}})
    api.action_runs = Mock(return_value=[{"id": 5, "conclusion": "skipped"}])
    api.action_jobs = Mock(return_value=[{"id": 8, "head_sha": SHA, "name": "build",
                                         "status": "completed", "conclusion": "skipped"}])
    path = "/team/repo/actions/runs/5/jobs/8"
    api.request = Mock(return_value=[
        {"id": 4, "context": "Build", "status": "pending", "target_url": path},
        {"id": 3, "context": "External", "status": "pending", "target_url": "https://other.test" + path},
    ])
    report = pr_evidence.checks(api, 1, expected_head=SHA)
    assert [(row["state"], row["reported_state"]) for row in report["statuses"]] == [
        ("skipped", "pending"), ("pending", "pending")]
    assert report["jobs"][0]["state"] == "skipped"
    with pytest.raises(RepoError, match="differs"):
        pr_evidence.checks(api, 1, expected_head="c" * 40)


def test_activity_ignores_bot_comments_and_pr_updated_at():
    api = api_for("github")
    api.pull_request = Mock(return_value={
        "head": {"sha": SHA}, "user": {"login": "author"},
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"})
    api.discussion_records = Mock(return_value=[
        ("issue", {"user": {"login": "notice[bot]"}, "created_at": "2026-05-01T00:00:00Z"}),
        ("review", {"user": {"login": "reviewer"}, "submitted_at": "2026-01-05T00:00:00Z"}),
    ])
    api.request = Mock(return_value=[])
    report = pr_evidence.activity(api, 1, now=datetime(2026, 5, 1, tzinfo=timezone.utc))
    assert report["cold"] is True
    assert report["last_activity"]["login"] == "reviewer"


@pytest.mark.parametrize(("platform", "field", "verb"), [
    ("github", "sha", "PUT"), ("gitea", "head_commit_id", "POST")])
def test_merge_supplies_server_side_precondition(platform, field, verb):
    api = api_for(platform)
    ready = {"head": {"sha": SHA}, "title": "Ready", "state": "open"}
    api.pull_request = Mock(side_effect=[ready, {**ready, "merged": True}])
    api.request = Mock(return_value={"merged": True})
    assert pr_writes.merge(api, 1, expected_head=SHA)["merged"]
    assert api.request.call_args.kwargs["payload"][field] == SHA
    assert api.request.call_args.kwargs["method"] == verb


def test_branch_delete_honors_protection_then_uses_explicit_lease(monkeypatch):
    api = api_for("gitea")
    api.request = Mock(return_value={"default_branch": "main"})
    api.branch = Mock(return_value={"sha": SHA, "protected": True})
    git = Mock(return_value="https://git.example.com/team/repo.git\n")
    monkeypatch.setattr(pr_writes, "_git", git)
    with pytest.raises(RepoError, match="protected"):
        pr_writes.delete_branch(api, "topic", expected_sha=SHA, project=".")
    assert not any(call.args[1] == "push" for call in git.call_args_list)
    api.branch.return_value["protected"] = False
    pr_writes.delete_branch(api, "topic", expected_sha=SHA, project=".")
    assert git.call_args.args == (".", "push", "--porcelain",
                                  f"--force-with-lease=refs/heads/topic:{SHA}",
                                  "--", "origin", ":refs/heads/topic")


def test_path_classification_and_base_advance(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = ".github/workflows/build.yml"
    workflow = tmp_path / path
    workflow.parent.mkdir(parents=True)
    workflow.write_text("on:\n  pull_request:\n    paths: ['src/**', '!src/*.md']\n")
    (tmp_path / "Guide.md").write_text("base\n")
    (tmp_path / "Other.md").write_text("base\n")
    start = snapshot(tmp_path)

    (tmp_path / "Guide.md").write_text("branch\n")
    head = snapshot(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "switch", "--detach", start], check=True)
    (tmp_path / "Other.md").write_text("base advanced\n")
    base = snapshot(tmp_path)

    options = dict(head=head, workflow=path, tested_head=head, tested_base=start, ci_success=True)
    report = classify(tmp_path, base=base, **options)
    assert report["classification"] == "static-only"
    assert report["reuse"] is True
    assert report["base_advance_paths"] == ["Other.md"]
    (tmp_path / "Guide.md").write_text("main advance\n")
    overlap = classify(tmp_path, base=snapshot(tmp_path), **options)
    assert overlap["overlap"] == ["Guide.md"]
    assert overlap["reuse"] is False
