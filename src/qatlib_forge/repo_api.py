"""Host-scoped GitHub/Gitea REST and GraphQL, using only the standard library.

Repository identities always include a platform and host. Passing a bare slug
requires explicit platform/host context; a token never follows a cross-origin archive redirect.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RepoError(ValueError):
    """An incomplete API observation must not be reported as success."""


def _origin(url):
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def _safe_url(url):
    p = urlsplit(url)
    if (not p.hostname or p.username or p.password or p.fragment
            or p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"localhost", "127.0.0.1", "::1"})
            or any(part in {".", ".."} for part in unquote(p.path).split("/"))):
        raise RepoError("API URL requires HTTPS, a plain host and a non-traversing path")
    return p


@dataclass(frozen=True)
class RepoRef:
    platform: str
    web_host: str
    api_base: str
    owner: str
    repo: str

    def __post_init__(self):
        if self.platform not in {"github", "gitea"}:
            raise RepoError("platform must be github or gitea")
        for part in (self.owner, self.repo):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in {".", ".."}:
                raise RepoError("invalid repository name")
        web = _safe_url("https://" + self.web_host)
        api = _safe_url(self.api_base)
        if web.path or web.query or api.query:
            raise RepoError("invalid repository host/API base")
        allowed = {web.hostname}
        if self.platform == "github" and web.hostname == "github.com":
            allowed.add("api.github.com")
        if api.hostname not in allowed:
            raise RepoError("API base does not belong to repository host")

    @property
    def slug(self):
        return f"{self.owner}/{self.repo}"

    @property
    def web_url(self):
        return f"https://{self.web_host}/{self.slug}"

    @property
    def key(self):
        return self.platform, self.web_host.lower(), self.owner.lower(), self.repo.lower()

    @classmethod
    def parse(cls, value, *, platform=None, web_host=None, api_base=None):
        value = str(value).strip()
        if web_host == "api.github.com":
            web_host = "github.com"
        match = re.fullmatch(r"(?:[^@/]+@)?([^/:]+):([^:]+)", value) if "://" not in value else None
        if match:
            host, path = match.groups()
        elif "://" in value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"https", "ssh"} or not parsed.hostname or parsed.query or parsed.fragment:
                raise RepoError("repository must be a complete HTTPS/SSH URL")
            if parsed.password or parsed.scheme == "https" and parsed.username:
                raise RepoError("credentials must not be embedded in repository URLs")
            host = parsed.hostname
            if parsed.port:
                host += ":" + str(parsed.port)
            path = parsed.path.strip("/")
            if host == "api.github.com" and path.startswith("repos/"):
                path = path[6:]
        else:
            if not platform and not web_host:
                raise RepoError("bare owner/repo requires an explicit platform or host")
            if web_host:
                host = web_host
            elif platform == "github":
                host = "github.com"
            else:
                host = "mirror.test"
            path = value
        host = host.lower()
        if host == "api.github.com":
            host = "github.com"
        if web_host and host != web_host.lower():
            raise RepoError("repository URL differs from explicit host")
        detected = "github" if host == "github.com" else ("gitea" if "://" not in value else None)
        if platform and detected and platform != detected:
            raise RepoError("platform contradicts repository URL")
        platform = platform or detected
        if platform is None:
            raise RepoError("unknown host; specify platform explicitly")
        parts = path.strip("/").removesuffix(".git").split("/")
        if len(parts) != 2:
            raise RepoError("repository URL must identify owner/repo exactly")
        base = api_base or ("https://api.github.com" if platform == "github" and host == "github.com"
                            else "https://" + host + ("/api/v1" if platform == "gitea" else "/api/v3"))
        return cls(platform, host, base.rstrip("/"), *parts)

    @classmethod
    def from_remote(cls, project, remote="origin", **kwargs):
        result = subprocess.run(["git", "remote", "get-url", remote], cwd=project,
                                capture_output=True, text=True, check=True, timeout=15)
        return cls.parse(result.stdout.strip(), **kwargs)


def _command_token(args, *, credential_host=None):
    try:
        value = subprocess.run(args, input=(f"protocol=https\nhost={credential_host}\n\n" if credential_host else None),
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if value.returncode:
        return None
    if credential_host:
        fields = dict(line.split("=", 1) for line in value.stdout.splitlines() if "=" in line)
        # A helper must not substitute credentials belonging to another host.
        if fields.get("host", credential_host) != credential_host:
            return None
        return fields.get("password") or None
    return value.stdout.strip() or None


def _config_token(host, home, env=None):
    """Read host-bound JSON/TOML and simple CLI YAML login records, without PyYAML."""
    def find(value, bound=None):
        if isinstance(value, dict):
            raw = value.get("url") or value.get("host") or value.get("hostname") or value.get("server") or bound
            actual = urlsplit(raw if "://" in str(raw) else "https://" + str(raw)).netloc if raw else None
            if actual == host:
                for key in ("token", "access_token", "access-token"):
                    if isinstance(value.get(key), str) and value[key].strip():
                        return value[key].strip()
            for key, child in value.items():
                result = find(child, key if key == host else actual)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = find(child, bound)
                if result:
                    return result
        return None
    env = os.environ if env is None else env
    config_root = Path(env.get("XDG_CONFIG_HOME", str(home / ".config")))
    for directory in ("gitea-cli", "gt", "tea"):
        folder = config_root / directory
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            try:
                if not path.is_file() or path.stat().st_size > 1024 * 1024:
                    continue
                text = path.read_text(encoding="utf-8")
                if path.suffix == ".json":
                    data = json.loads(text)
                elif path.suffix == ".toml":
                    data = tomllib.loads(text)
                else:
                    # CLI YAML is a list of login mappings. Never associate a
                    # token with a host from a different mapping/list item.
                    records, current = [], {}
                    for line in text.splitlines():
                        match = re.fullmatch(r"\s*(-\s*)?([\w.-]+):\s*(.*?)\s*", line)
                        if not match:
                            continue
                        if match[1] and current:
                            records.append(current); current = {}
                        key, raw = match[2], match[3].strip("\"'")
                        if raw:
                            current[key] = raw
                    records.append(current)
                    data = records
                token = find(data)
                if token:
                    return token
            except (OSError, ValueError, UnicodeError, TypeError):
                continue
    return None


def auth_token(ref: RepoRef, *, env=None, home=None):
    """Resolve credentials only for the explicitly selected repository host."""
    env = os.environ if env is None else env
    host = ref.web_host
    if ref.platform == "github":
        if host == env.get("GH_HOST", "github.com"):
            for key in ("GH_TOKEN", "GITHUB_TOKEN"):
                if env.get(key, "").strip():
                    return env[key].strip()
        token = _command_token(["gh", "auth", "token", "--hostname", host])
    else:
        if host == env.get("GITEA_HOST", "mirror.test") and env.get("GITEA_TOKEN", "").strip():
            return env["GITEA_TOKEN"].strip()
        token = _command_token(["gt", "auth", "token", "--host", host, "--no-input"])
        if not token:
            token = _command_token(["tea", "login", "helper", "get"], credential_host=host)
        if not token:
            token = _config_token(host, Path.home() if home is None else Path(home), env=env)
    return token or _command_token(["git", "credential", "fill"], credential_host=host)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RepoAPI:
    def __init__(self, ref: RepoRef, *, token=None):
        self.ref = ref
        self.base = ref.api_base.rstrip("/") + "/"
        self.token = auth_token(ref) if token is None else token
        self.opener = build_opener(NoRedirect())

    def for_repo(self, ref: RepoRef):
        # Deliberately resolve target credentials again, even for forks.
        return RepoAPI(ref)

    def _url(self, endpoint):
        url = urljoin(self.base, endpoint)
        parsed = _safe_url(url)
        prefix = urlsplit(self.base).path
        if _origin(url) != _origin(self.base) or not parsed.path.startswith(prefix):
            raise RepoError("API link left configured origin/path")
        return url

    def _headers(self):
        headers = {"Accept": "application/json", "User-Agent": "qatlib-forge"}
        if self.token:
            headers["Authorization"] = ("Bearer " if self.ref.platform == "github" else "token ") + self.token
        return headers

    @staticmethod
    def _next(header):
        following = []
        for part in re.split(r",\s*(?=<)", header) if header else []:
            match = re.fullmatch(r'\s*<([^<>]+)>\s*((?:;[^<>]*)?)', part)
            if not match:
                raise RepoError("malformed pagination Link header")
            relations = re.findall(r';\s*rel="([^"]+)"', match[2])
            if len(relations) != 1:
                raise RepoError("malformed pagination relation")
            if "next" in relations[0].split():
                following.append(match[1])
        if len(following) > 1:
            raise RepoError("ambiguous pagination next link")
        return following[0] if following else None

    def request(self, endpoint, *, method="GET", payload=None, paginate=False):
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"} or method == "GET" and payload is not None or paginate and method != "GET":
            raise RepoError("unsupported API request")
        if method != "GET" and not self.token:
            raise RepoError("API writes require host-scoped authentication")
        url = self._url(endpoint)
        size_key = "per_page" if self.ref.platform == "github" else "limit"
        if paginate:
            parsed = urlsplit(url); query = dict(parse_qsl(parsed.query))
            query.pop("limit" if size_key == "per_page" else "per_page", None)
            query.setdefault(size_key, "100"); query.setdefault("page", "1")
            url = urlunsplit(parsed._replace(query=urlencode(query)))
        headers = self._headers()
        data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        items, seen, total = [], set(), None
        deadline = time.monotonic() + 120
        while True:
            if url in seen or len(seen) >= 10000 or time.monotonic() >= deadline:
                raise RepoError("cyclic, excessive or timed out pagination")
            seen.add(url)
            try:
                with self.opener.open(Request(url, data=data, headers=headers, method=method), timeout=max(.1, deadline-time.monotonic())) as response:
                    raw = response.read(); status = response.status
                    value = json.loads(raw) if raw else None
                    link = response.headers.get("Link", "")
                    count = response.headers.get("X-Total-Count")
            except HTTPError as error:
                raise RepoError(f"API HTTP {error.code}; check authentication, rate limit and endpoint") from None
            except (OSError, URLError, ValueError) as error:
                raise RepoError(f"API transport/JSON failure ({type(error).__name__})") from None
            if not 200 <= status < 300:
                raise RepoError(f"API HTTP {status}")
            if not paginate:
                if value is None and status == 204:
                    return None
                if not isinstance(value, dict):
                    raise RepoError("API object response is not an object")
                if value.get("errors") and endpoint == "graphql":
                    raise RepoError("GraphQL mutation failed")
                return value
            if not isinstance(value, list):
                raise RepoError("pagination response is not an array")
            if count is not None:
                if not re.fullmatch(r"[0-9]+", count) or total is not None and int(count) != total:
                    raise RepoError("invalid or changing pagination total")
                total = int(count)
            items.extend(value)
            following = self._next(link)
            if following:
                url = self._url(following)
            elif link or len(value) < int(dict(parse_qsl(urlsplit(url).query))[size_key]):
                if total is not None and total != len(items):
                    raise RepoError("incomplete pagination")
                return items
            else:
                parsed = urlsplit(url); query = dict(parse_qsl(parsed.query))
                query["page"] = str(int(query["page"]) + 1)
                url = urlunsplit(parsed._replace(query=urlencode(query)))

    @property
    def endpoint(self):
        return "repos/" + self.ref.slug

    def pull_request(self, number):
        return self.request(f"{self.endpoint}/pulls/{int(number)}")

    def list_pull_requests(self, *, state="open", head=None, base=None, labels=None):
        """List pull requests with the same filters on GitHub and Gitea."""
        query = {"state": state}
        if head:
            query["head"] = head
        if base:
            query["base"] = base
        if labels:
            query["labels"] = ",".join(labels)
        endpoint = self.endpoint + "/pulls?" + urlencode(query)
        rows = self.request(endpoint, paginate=True)
        return [row for row in rows if isinstance(row, dict)]

    def create_pull_request(self, *, title, head, base="main", body="", draft=False):
        payload = dict(title=title, head=head, base=base, body=body)
        if self.ref.platform == "github":
            payload["draft"] = draft
        elif draft and not title.startswith("WIP: "):
            payload["title"] = "WIP: " + title
        return self.request(self.endpoint + "/pulls", method="POST", payload=payload)

    def update_pull_request(self, number, *, title=None, body=None, base=None, state=None,
                            labels=None):
        """Update only the supplied PR fields; label replacement is explicit."""
        payload = {}
        for name, value in (("title", title), ("body", body), ("base", base), ("state", state)):
            if value is not None:
                payload[name] = value
        if labels is not None:
            payload["labels"] = list(labels)
        if not payload:
            raise RepoError("PR update needs at least one field")
        return self.request(f"{self.endpoint}/pulls/{int(number)}", method="PATCH", payload=payload)

    def set_draft(self, number, draft):
        pr = self.pull_request(number)
        if self.ref.platform == "gitea":
            title = pr["title"].removeprefix("WIP: ")
            return self.request(f"{self.endpoint}/pulls/{number}", method="PATCH", payload={"title": ("WIP: " if draft else "") + title})
        node = pr.get("node_id")
        if not node:
            raise RepoError("PR response lacks GraphQL node_id")
        mutation = "convertPullRequestToDraft" if draft else "markPullRequestReadyForReview"
        return self.request("graphql", method="POST", payload={
            "query": f"mutation($id:ID!){{{mutation}(input:{{pullRequestId:$id}}){{pullRequest{{id isDraft}}}}}}",
            "variables": {"id": node}})

    def comment(self, number, body):
        return self.request(f"{self.endpoint}/issues/{number}/comments", method="POST", payload={"body": body})

    def issue_comments(self, number):
        return self.request(f"{self.endpoint}/issues/{int(number)}/comments", paginate=True)

    # Explicit aliases keep consumer scripts readable without duplicating the
    # platform normalization in their own adapters.
    list_issue_comments = issue_comments

    def review_comments(self, number):
        """List inline review comments using each platform's native endpoint."""
        if self.ref.platform == "github":
            return self.request(f"{self.endpoint}/pulls/{int(number)}/comments", paginate=True)
        result = []
        for _, row in self.discussion_records(number):
            if _ == "inline":
                result.append(row)
        return result

    def add_review_comment(self, number, body, *, commit_id=None, path=None, line=None,
                           side="RIGHT", position=None):
        if self.ref.platform == "github":
            payload = {"body": body}
            if commit_id is not None: payload["commit_id"] = commit_id
            if path is not None: payload["path"] = path
            if line is not None: payload.update(line=line, side=side)
            if position is not None: payload["position"] = position
            return self.request(f"{self.endpoint}/pulls/{int(number)}/comments", method="POST", payload=payload)
        payload = {"body": body}
        if path is not None: payload["path"] = path
        if line is not None: payload["line"] = line
        if side is not None: payload["side"] = side
        return self.request(f"{self.endpoint}/pulls/{int(number)}/reviews", method="POST",
                            payload={"body": body, "comments": [payload] if path else []})

    def issue(self, number):
        return self.request(f"{self.endpoint}/issues/{int(number)}")

    def list_issues(self, *, state="open", labels=None, assignee=None, include_pull_requests=False):
        """List issues and remove GitHub's pull-request-shaped issue entries."""
        query = {"state": state}
        if labels:
            query["labels"] = ",".join(labels)
        if assignee:
            query["assignee"] = assignee
        rows = self.request(self.endpoint + "/issues?" + urlencode(query), paginate=True)
        if self.ref.platform == "github" and not include_pull_requests:
            rows = [row for row in rows if isinstance(row, dict) and not row.get("pull_request")]
        if labels:
            wanted = set(labels)
            rows = [row for row in rows if wanted.issubset({x.get("name") for x in row.get("labels", []) if isinstance(x, dict)})]
        return rows

    def create_issue(self, *, title, body="", labels=None, assignees=None, milestone=None):
        payload = {"title": title, "body": body}
        if labels is not None: payload["labels"] = list(labels)
        if assignees is not None: payload["assignees"] = list(assignees)
        if milestone is not None: payload["milestone"] = milestone
        return self.request(self.endpoint + "/issues", method="POST", payload=payload)

    def update_issue(self, number, *, title=None, body=None, state=None, labels=None,
                     assignees=None, milestone=None):
        payload = {}
        for name, value in (("title", title), ("body", body), ("state", state),
                            ("milestone", milestone)):
            if value is not None: payload[name] = value
        if labels is not None: payload["labels"] = list(labels)
        if assignees is not None: payload["assignees"] = list(assignees)
        if not payload:
            raise RepoError("issue update needs at least one field")
        return self.request(f"{self.endpoint}/issues/{int(number)}", method="PATCH", payload=payload)

    def close_issue(self, number):
        return self.update_issue(number, state="closed")

    def reopen_issue(self, number):
        return self.update_issue(number, state="open")

    def issue_comment(self, number, body):
        return self.comment(number, body)

    create_issue_comment = issue_comment
    list_review_comments = review_comments

    def discussion_records(self, number):
        for record in self.request(f"{self.endpoint}/issues/{number}/comments", paginate=True):
            yield "issue", record
        reviews = self.request(f"{self.endpoint}/pulls/{number}/reviews", paginate=True)
        for review in reviews:
            yield "review", review
        if self.ref.platform == "github":
            for record in self.request(f"{self.endpoint}/pulls/{number}/comments", paginate=True):
                yield "inline", record
        else:
            seen = set()
            for review in reviews:
                identity = review.get("id")
                if type(identity) is not int or identity <= 0 or identity in seen:
                    raise RepoError("malformed or duplicate review identity")
                seen.add(identity)
                records = self.request(f"{self.endpoint}/pulls/{number}/reviews/{identity}/comments", paginate=True)
                if type(review.get("comments_count")) is int and len(records) != review["comments_count"]:
                    raise RepoError("review comment count changed")
                for record in records:
                    yield "inline", record

    def branch(self, name):
        result = self.request(self.endpoint + "/branches/" + quote(name, safe=""))
        commit = result.get("commit", {})
        result["sha"] = commit.get("sha") or commit.get("id")
        if not result["sha"]:
            raise RepoError("branch response lacks commit SHA")
        return result

    def create_branch(self, name, sha):
        if self.ref.platform == "github":
            return self.request(self.endpoint + "/git/refs", method="POST", payload={"ref": "refs/heads/"+name, "sha": sha})
        return self.request(self.endpoint + "/branches", method="POST", payload={"new_branch_name": name, "old_ref_name": sha})

    def delete_branch(self, name):
        path = "/git/refs/heads/" if self.ref.platform == "github" else "/branches/"
        return self.request(self.endpoint + path + quote(name, safe=""), method="DELETE")

    def compare(self, base, head):
        result = self.request(self.endpoint + "/compare/" + quote(base, safe="") + "..." + quote(head, safe=""))
        files = result.get("files")
        incomplete = bool(result.get("incomplete"))
        if self.ref.platform == "gitea" and not isinstance(files, list):
            # Gitea Compare exposes files under each commit, rather than a
            # top-level net diff. Return a conservative union and label it;
            # reverted changes may remain, but changed files cannot disappear.
            commits = result.get("commits")
            total = result.get("total_commits")
            incomplete |= not isinstance(commits, list) or type(total) is not int
            commits = commits if isinstance(commits, list) else []
            incomplete |= total != len(commits) or len(commits) > 1000
            by_name = {}
            for commit in commits[:1000]:
                if not isinstance(commit, dict):
                    incomplete = True
                    continue
                affected = commit.get("files")
                if not isinstance(affected, list):
                    sha = commit.get("sha") or commit.get("id")
                    if not isinstance(sha, str) or not sha:
                        incomplete = True
                        continue
                    detail = self.request(self.endpoint + "/git/commits/" + quote(sha, safe="") + "?files=true&stat=false&verification=false")
                    affected = detail.get("files")
                if not isinstance(affected, list):
                    incomplete = True
                    continue
                for item in affected:
                    if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                        incomplete = True
                        continue
                    by_name[item["filename"]] = item
            files = [by_name[name] for name in sorted(by_name)]
            result["file_scope"] = "commit-union"
        # GitHub returns at most 300 changed files, on page one only. Even a
        # full page of exactly 300 cannot certify completeness.
        incomplete |= not isinstance(files, list)
        if self.ref.platform == "github" and isinstance(files, list) and len(files) >= 300:
            incomplete = True
        return {**result, "files": files if isinstance(files, list) else [], "incomplete": incomplete}

    def pull_files(self, number):
        files = self.request(f"{self.endpoint}/pulls/{number}/files", paginate=True)
        # GitHub's PR-files endpoint has its own 3000-file ceiling.
        return {"files": files, "incomplete": self.ref.platform == "github" and len(files) >= 3000}

    def archive(self, revision, *, max_bytes=128*1024*1024):
        path = "/tarball/" + quote(revision, safe="") if self.ref.platform == "github" else "/archive/" + quote(revision, safe="") + ".tar.gz"
        url, headers = self._url(self.endpoint + path), self._headers()
        headers["Accept"] = "application/octet-stream"
        for _ in range(6):
            try:
                with self.opener.open(Request(url, headers=headers), timeout=120) as response:
                    payload = response.read(max_bytes + 1)
                    if len(payload) > max_bytes:
                        raise RepoError("archive exceeds download budget")
                    return payload
            except HTTPError as error:
                location = error.headers.get("Location")
                if self.ref.platform != "github" or error.code not in {301, 302, 303, 307, 308} or not location:
                    raise RepoError(f"archive HTTP {error.code}") from None
                following = urljoin(url, location)
                parsed = _safe_url(following)
                if parsed.scheme != "https" or parsed.hostname not in {urlsplit(self.base).hostname, "github.com", "codeload.github.com"}:
                    raise RepoError("archive redirect left allowed GitHub download hosts")
                if _origin(following) != _origin(url):
                    headers.pop("Authorization", None)
                url = following
            except (URLError, OSError) as error:
                raise RepoError(f"archive transport failed ({type(error).__name__})") from None
        raise RepoError("too many archive redirects")

    # Actions-compatible transport. The response shape is intentionally kept
    # platform-neutral; callers bind the exact run/job identity before logs or
    # artifacts are read.
    def workflow_runs(self, workflow=None, *, branch=None):
        endpoint = self.endpoint + "/actions/runs"
        if workflow:
            endpoint = self.endpoint + "/actions/workflows/" + quote(str(workflow), safe="") + "/runs"
        query = {}
        if branch: query["branch"] = branch
        if query: endpoint += "?" + urlencode(query)
        try:
            return self.request(endpoint)
        except RepoError:
            rows = self.request(endpoint, paginate=True)
            return {"workflow_runs": rows, "total_count": len(rows)}

    def run(self, run_id):
        return self.request(self.endpoint + "/actions/runs/" + quote(str(run_id), safe=""))

    def jobs(self, run_id):
        endpoint = self.endpoint + "/actions/runs/" + quote(str(run_id), safe="") + "/jobs"
        try:
            return self.request(endpoint)
        except RepoError:
            rows = self.request(endpoint, paginate=True)
            return {"jobs": rows, "total_count": len(rows)}

    def dispatch(self, workflow, ref, inputs=None):
        payload = {"ref": ref, "inputs": inputs or {}}
        endpoint = self.endpoint + "/actions/workflows/" + quote(str(workflow), safe="") + "/dispatches"
        return self.request(endpoint, method="POST", payload=payload)

    def artifacts(self, run_id=None):
        endpoint = self.endpoint + "/actions/artifacts"
        if run_id is not None:
            endpoint = self.endpoint + "/actions/runs/" + quote(str(run_id), safe="") + "/artifacts"
        try:
            return self.request(endpoint)
        except RepoError:
            rows = self.request(endpoint, paginate=True)
            return {"artifacts": rows, "total_count": len(rows)}

    def _action_redirect(self, endpoint, *, accept):
        url = self._url(endpoint); headers = self._headers(); headers["Accept"] = accept
        try:
            with self.opener.open(Request(url, headers=headers), timeout=60) as response:
                return response.status, dict(response.headers.items()), response.read()
        except HTTPError as error:
            return error.code, dict(error.headers.items()), error.read()

    def download_job_log(self, job_id):
        return self._action_redirect(self.endpoint + "/actions/jobs/" + quote(str(job_id), safe="") + "/logs", accept="text/plain")

    def job_log_url(self, run_id, job_id):
        if self.ref.platform == "gitea":
            return self._url(self.endpoint + "/actions/runs/" + quote(str(run_id), safe="") + "/jobs/" + quote(str(job_id), safe="") + "/logs")
        return self._url(self.endpoint + "/actions/jobs/" + quote(str(job_id), safe="") + "/logs")

    def read_job_log_range(self, run_id, job_id, *, start=0, end=None):
        from .ci_logs import RangeLogReader
        status, headers, body = self._action_redirect(self.job_log_url(run_id, job_id), accept="text/plain")
        if status in {301, 302, 303, 307, 308} and headers.get("Location"):
            data, range_headers = RangeLogReader(headers["Location"]).read(start, end)
            return data, range_headers
        if status not in {200, 206}:
            raise RepoError(f"job log HTTP {status}")
        return body, {key.lower(): value for key, value in headers.items()} | {":status": str(status)}

    def download_artifact(self, artifact_id):
        status, headers, body = self._action_redirect(self.endpoint + "/actions/artifacts/" + quote(str(artifact_id), safe="") + "/zip", accept="application/zip")
        if status in {301, 302, 303, 307, 308} and headers.get("Location"):
            request = Request(headers["Location"], headers={"Accept": "application/zip"})
            with self.opener.open(request, timeout=120) as response:
                return response.status, dict(response.headers.items()), response.read(2_000_000_001)
        if status not in {200, 206}:
            raise RepoError(f"artifact HTTP {status}")
        return status, headers, body

    def download_run_logs(self, run_id):
        return self._action_redirect(self.endpoint + "/actions/runs/" + quote(str(run_id), safe="") + "/logs", accept="application/zip")


def main(argv=None):
    """Resolve a repository identity without contacting an API or printing secrets."""
    import argparse
    from dataclasses import asdict
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", nargs="?")
    parser.add_argument("--platform", choices=("github", "gitea"))
    parser.add_argument("--web-host")
    parser.add_argument("--api-base")
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    options = dict(platform=args.platform, web_host=args.web_host, api_base=args.api_base)
    ref = (RepoRef.parse(args.repository, **options) if args.repository
           else RepoRef.from_remote(args.project, args.remote, **options))
    print(json.dumps(asdict(ref), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
