"""Conditional writes. The forge/Git server enforces the expected SHA."""
import re
import subprocess
from urllib.parse import quote

from .pr_evidence import exact_sha
from .repo_api import RepoError, RepoRef


def merge(api, number, *, expected_head, method="merge"):
    """Send the reviewed head as a merge condition after checking PR readiness."""
    expected_head = exact_sha(expected_head)
    if method not in {"merge", "squash", "rebase"}:
        raise RepoError("unsupported merge method")
    pr = api.pull_request(number)
    if pr["head"]["sha"] != expected_head:
        raise RepoError("PR head differs from expected SHA")
    if pr.get("draft") or pr["title"].startswith("WIP: ") or pr["state"] != "open":
        raise RepoError("PR must be open and ready")
    endpoint = f"{api.endpoint}/pulls/{int(number)}/merge"
    if api.ref.platform == "github":
        result = api.request(endpoint, method="PUT", payload={"sha": expected_head, "merge_method": method})
        if not result or result.get("merged") is not True:
            raise RepoError("GitHub did not report a successful merge")
        return result
    api.request(endpoint, method="POST", payload={"head_commit_id": expected_head, "do": method})
    result = api.pull_request(number)
    if result.get("merged") is not True:
        raise RepoError("merge response was not confirmed by Gitea")
    return {"merged": True, "head_sha": expected_head,
            "merge_commit_sha": result.get("merge_commit_sha"), "url": result.get("html_url")}


def _git(project, *args):
    result = subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RepoError(f"git {args[0]} failed; verify remote, branch protection and expected SHA")
    return result.stdout


def delete_branch(api, name, *, expected_sha, project, remote="origin"):
    """Delete a protected-checked remote ref only while it points to expected_sha."""
    sha = exact_sha(expected_sha)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", remote):
        raise RepoError("invalid Git remote name")
    ref = "refs/heads/" + name
    _git(project, "check-ref-format", ref)
    urls = _git(project, "remote", "get-url", "--push", "--all", remote).splitlines()
    if len(urls) != 1 or RepoRef.parse(urls[0], platform=api.ref.platform,
                                      web_host=api.ref.web_host).key != api.ref.key:
        raise RepoError("Git push remote must identify exactly the API repository")
    metadata = api.request(api.endpoint)
    default = metadata.get("default_branch")
    if not default or default == name:
        raise RepoError("cannot delete the default branch or determine its identity")
    branch = api.branch(name)
    if branch.get("protected") is not False:
        raise RepoError("cannot delete a protected branch or verify branch protection")
    if api.ref.platform == "github":
        rules = api.request(api.endpoint + "/rules/branches/" + quote(name, safe=""), paginate=True)
        if rules:
            raise RepoError("active GitHub rules apply to the branch")
    if branch["sha"] != sha:
        raise RepoError("branch SHA differs from expected SHA")
    _git(project, "push", "--porcelain", f"--force-with-lease={ref}:{sha}", "--", remote, f":{ref}")
    return {"deleted": name, "expected_sha": sha, "repo": api.ref.web_url}
