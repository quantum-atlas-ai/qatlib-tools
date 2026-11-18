"""Exact-revision CI and discussion evidence from both forges."""
from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urljoin, urlsplit

from .repo_api import RepoError


def exact_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RepoError("expected a full lowercase SHA")
    return value


def state(row):
    status = row.get("status") or row.get("state")
    value = row.get("conclusion") or status
    if value in {"success", "skipped", "cancelled"}:
        return value
    if value in {"failure", "error", "timed_out", "action_required", "startup_failure"}:
        return "failure"
    if value in {"neutral"}:
        return "skipped"
    if value == "stale":
        return "cancelled"
    if value in {"queued", "requested", "waiting", "pending", "in_progress", "blocked", "running"}:
        return "pending"
    raise RepoError(f"unknown or missing CI state: {value!r}")


def _job_url(api, value):
    if not value:
        return None
    parsed = urlsplit(urljoin(api.ref.web_url + "/", value))
    base = urlsplit(api.ref.web_url)
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
        return None
    if not parsed.path.startswith(base.path.rstrip("/") + "/"):
        return None
    match = re.search(r"/actions/runs/(\d+)/jobs/(\d+)$", parsed.path)
    return tuple(map(int, match.groups())) if match else None


def _pr_run(api, run, number):
    if run.get("event") not in {"pull_request", "pull_request_target"}:
        return False
    if api.ref.platform == "gitea":
        return (run.get("path") or "").endswith(f"@refs/pull/{number}/head")
    return any(pr["number"] == number for pr in run.get("pull_requests") or [])


def checks(api, number, *, expected_head=None, required_jobs=()):
    """Return observations for one PR head; a successful read is not a green CI verdict."""
    pr = api.pull_request(number)
    sha = exact_sha(pr["head"]["sha"])
    if expected_head is not None and exact_sha(expected_head) != sha:
        raise RepoError("PR head differs from expected SHA")
    prefix = f"{api.endpoint}/commits/{sha}"
    runs = sorted(api.action_runs(head_sha=sha), key=lambda row: row["id"], reverse=True)
    jobs, job_states = [], {}
    for run in runs:
        for job in api.action_jobs(run["id"]):
            if job.get("head_sha", sha) != sha or job.get("run_id", run["id"]) != run["id"]:
                raise RepoError("Actions job disagrees with run identity")
            terminal = state(job)
            job_states[run["id"], job["id"]] = terminal
            jobs.append({"source": "job", "name": job["name"], "state": terminal,
                         "id": job["id"], "run_id": run["id"],
                         "workflow": run.get("path") or run.get("name"), "event": run.get("event"),
                         "url": job.get("html_url")})
    statuses = []
    for row in api.request(prefix + "/statuses", paginate=True):
        if row.get("sha") not in (None, sha):
            raise RepoError("commit status disagrees with exact SHA")
        reported = state(row)
        job_state = (job_states.get(_job_url(api, row.get("target_url")))
                     if api.ref.platform == "gitea" else None)
        statuses.append({"source": "status", "name": row["context"], "id": row["id"],
                         "state": job_state or reported, "reported_state": reported,
                         "url": row.get("target_url")})
    check_runs = []
    if api.ref.platform == "github":
        for row in api.request(prefix + "/check-runs?filter=all",
                               paginate=True, collection="check_runs"):
            if row.get("head_sha") != sha:
                raise RepoError("check run disagrees with exact SHA")
            check_runs.append({"source": "check-run", "name": row["name"], "id": row["id"],
                               "app_id": (row.get("app") or {}).get("id"),
                               "state": state(row), "url": row.get("html_url")})
    if api.pull_request(number)["head"]["sha"] != sha:
        raise RepoError("PR head changed while collecting checks")
    required = {}
    for selector in required_jobs:
        workflow, separator, name = selector.partition(":")
        if not separator or not workflow or not name:
            raise RepoError("required job must be in workflow-file:job-name form")
        relevant = next((run for run in runs
                         if (run.get("path") or "").split("@", 1)[0].rsplit("/", 1)[-1] == workflow
                         and _pr_run(api, run, number)), None)
        latest = [job for job in jobs if relevant and job["run_id"] == relevant["id"] and job["name"] == name]
        if len(latest) > 1:
            raise RepoError(f"required job is ambiguous: {selector}")
        required[selector] = latest[0] if latest else {"state": "absent"}
    return {"head_sha": sha, "runs": [
        {"id": run["id"], "workflow": run.get("path") or run.get("name"),
         "event": run.get("event"), "state": state(run), "url": run.get("html_url")}
        for run in runs], "jobs": jobs, "statuses": statuses, "check_runs": check_runs,
        "required_jobs": required,
        "state": "observed" if runs or jobs or statuses or check_runs else "absent"}


def _date(value):
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise RepoError("activity timestamp must include its time zone")
    return date


def activity(api, number, *, cold_days=30, ignore_users=(), now=None):
    """Find the newest recorded activity after excluding identifiable automation."""
    if cold_days < 1:
        raise RepoError("cold-days must be positive")
    now = now or datetime.now(timezone.utc)
    excluded = {login.casefold() for login in ignore_users}
    pr = api.pull_request(number)
    sha = exact_sha(pr["head"]["sha"])
    records = []

    def record(kind, actor, date, url, *, fallback=""):
        actor = actor or {}
        login = actor.get("login") or actor.get("username") or fallback
        normalized = login.casefold()
        if (actor.get("type", "").casefold() == "bot" or actor.get("is_bot")
                or normalized.endswith("[bot]") or normalized in excluded):
            return
        if date:
            records.append({"kind": kind, "login": login, "at": date, "url": url})

    record("opened", pr.get("user"), pr["created_at"], pr.get("html_url"))
    for kind, row in api.discussion_records(number):
        if kind != "review" or str(row.get("state", "")).casefold() != "pending":
            record(kind, row.get("user"), row.get("submitted_at") or row.get("created_at"),
                   row.get("html_url"))
    for row in api.request(f"{api.endpoint}/pulls/{number}/commits", paginate=True):
        commit = row.get("commit") or {}
        author = commit.get("author") or {}
        committer = commit.get("committer") or {}
        record("commit", row.get("author"), committer.get("date") or author.get("date"),
               row.get("html_url"), fallback=author.get("name", ""))
    if api.pull_request(number)["head"]["sha"] != sha:
        raise RepoError("PR head changed while collecting activity")
    latest = max(records, key=lambda row: _date(row["at"]), default=None)
    cutoff = now - timedelta(days=cold_days)
    return {"number": number, "head_sha": sha, "last_activity": latest,
            "cold_before": cutoff.isoformat(),
            "cold": _date(latest["at"]) < cutoff if latest else None}
