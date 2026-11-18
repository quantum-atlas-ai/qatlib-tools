"""Small, script-friendly CLI for GitHub and Gitea pull requests and issues."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qatlib_forge.repo_api import RepoAPI, RepoError, RepoRef
from qatlib_forge import task_state
from qatlib_forge import pr_evidence, pr_writes


def _text(value: str | None, path: Path | None) -> str:
    if value is not None and path is not None:
        raise ValueError("use either --body or --body-file")
    if path is not None:
        return path.read_text(encoding="utf-8")
    return value or ""


def _labels(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    result = []
    for value in values:
        result.extend(part.strip() for part in value.split(",") if part.strip())
    return result or None


def _title(value: str, *, kind: str | None = None, origin_tag: str | None = None) -> str:
    result = value.strip()
    if not result:
        raise ValueError("title must be nonempty")
    if kind:
        result = f"[{kind}] {result}" if not result.startswith(f"[{kind}]") else result
    if origin_tag and origin_tag not in result:
        result = f"{origin_tag} {result}"
    return result


def _api(args) -> tuple[RepoAPI, RepoRef]:
    if not getattr(args, "repo", None):
        raise ValueError("--repo is required")
    ref = RepoRef.parse(args.repo, platform=getattr(args, "platform", None),
                        web_host=getattr(args, "web_host", None),
                        api_base=getattr(args, "api_base", None))
    return RepoAPI(ref, token=getattr(args, "token", None)), ref


def _add_repo_options(parser: argparse.ArgumentParser, *, suppress=False) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--repo", default=default, help="repository URL or owner/repo")
    parser.add_argument("--platform", default=default, choices=("github", "gitea"))
    parser.add_argument("--web-host", default=default)
    parser.add_argument("--api-base", default=default)
    parser.add_argument("--token", default=default)


def _add_body_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--body")
    parser.add_argument("--body-file", type=Path)


def _pr_parser(commands) -> None:
    p = commands.add_parser("pr", help="pull request operations")
    _add_repo_options(p)
    sub = p.add_subparsers(dest="pr_command", required=True)
    read = sub.add_parser("read"); _add_repo_options(read, suppress=True); read.add_argument("number", type=int)
    listing = sub.add_parser("list"); _add_repo_options(listing, suppress=True); listing.add_argument("--state", default="open"); listing.add_argument("--head"); listing.add_argument("--base"); listing.add_argument("--label", action="append")
    create = sub.add_parser("create"); _add_repo_options(create, suppress=True); create.add_argument("--title", required=True); create.add_argument("--head", required=True); create.add_argument("--base", default="main"); create.add_argument("--draft", action="store_true"); create.add_argument("--kind"); create.add_argument("--origin-tag"); _add_body_options(create); create.add_argument("--label", action="append")
    update = sub.add_parser("update"); _add_repo_options(update, suppress=True); update.add_argument("number", type=int); update.add_argument("--title"); update.add_argument("--base"); update.add_argument("--state", choices=("open", "closed")); update.add_argument("--kind"); update.add_argument("--origin-tag"); _add_body_options(update); update.add_argument("--label", action="append")
    comment = sub.add_parser("comment"); _add_repo_options(comment, suppress=True); comment.add_argument("number", type=int); _add_body_options(comment)
    comments = sub.add_parser("comments"); _add_repo_options(comments, suppress=True); comments.add_argument("number", type=int); comments.add_argument("--kind", choices=("all", "issue", "review"), default="all")
    review = sub.add_parser("review-comment"); _add_repo_options(review, suppress=True); review.add_argument("number", type=int); review.add_argument("--path"); review.add_argument("--line", type=int); review.add_argument("--commit-id"); review.add_argument("--position", type=int); _add_body_options(review)
    draft = sub.add_parser("draft"); _add_repo_options(draft, suppress=True); draft.add_argument("number", type=int)
    ready = sub.add_parser("ready"); _add_repo_options(ready, suppress=True); ready.add_argument("number", type=int)
    for name in ("checks", "activity", "merge"):
        command = sub.add_parser(name)
        _add_repo_options(command, suppress=True)
        if name != "activity":
            command.add_argument("number", type=int)
        if name in {"checks", "merge"}:
            command.add_argument("--expected-head", required=name == "merge")
        if name == "checks":
            command.add_argument("--require-job", action="append", default=[],
                                 metavar="WORKFLOW-FILE:JOB-NAME")
        if name == "merge":
            command.add_argument("--method", choices=("merge", "squash", "rebase"), default="merge")
        if name == "activity":
            command.add_argument("number", type=int, nargs="?", help="omit to scan open PRs")
            command.add_argument("--cold-days", type=int, default=30)
            command.add_argument("--ignore-user", action="append", default=[])
    paths = sub.add_parser("paths", help="classify exact Git trees and compare CI bases")
    paths.add_argument("--project", default=Path.cwd(), type=Path)
    paths.add_argument("--base", required=True)
    paths.add_argument("--head", required=True)
    paths.add_argument("--workflow", required=True)
    paths.add_argument("--tested-base")
    paths.add_argument("--tested-head")
    paths.add_argument("--ci-success", action="store_true", help="attest successful applicable jobs")


def _issue_parser(commands) -> None:
    p = commands.add_parser("issue", help="issue operations")
    _add_repo_options(p)
    sub = p.add_subparsers(dest="issue_command", required=True)
    listing = sub.add_parser("list"); _add_repo_options(listing, suppress=True); listing.add_argument("--state", default="open"); listing.add_argument("--label", action="append"); listing.add_argument("--assignee")
    read = sub.add_parser("read"); _add_repo_options(read, suppress=True); read.add_argument("number", type=int)
    create = sub.add_parser("create"); _add_repo_options(create, suppress=True); create.add_argument("--title", required=True); create.add_argument("--kind"); create.add_argument("--origin-tag"); _add_body_options(create); create.add_argument("--label", action="append"); create.add_argument("--assignee", action="append")
    update = sub.add_parser("update"); _add_repo_options(update, suppress=True); update.add_argument("number", type=int); update.add_argument("--title"); update.add_argument("--state", choices=("open", "closed")); update.add_argument("--kind"); update.add_argument("--origin-tag"); _add_body_options(update); update.add_argument("--label", action="append"); update.add_argument("--assignee", action="append")
    for name in ("close", "reopen"):
        command = sub.add_parser(name); _add_repo_options(command, suppress=True); command.add_argument("number", type=int)
    comment = sub.add_parser("comment"); _add_repo_options(comment, suppress=True); comment.add_argument("number", type=int); _add_body_options(comment)


def _ci_parser(commands) -> None:
    runs = commands.add_parser("runs", help="workflow run inspection")
    _add_repo_options(runs)
    run_sub = runs.add_subparsers(dest="runs_command", required=True)
    listing = run_sub.add_parser("list"); _add_repo_options(listing, suppress=True); listing.add_argument("--workflow"); listing.add_argument("--branch")
    read = run_sub.add_parser("read"); _add_repo_options(read, suppress=True); read.add_argument("run_id")
    logs = commands.add_parser("logs", help="bounded workflow log inspection")
    _add_repo_options(logs); logs.add_argument("job_id"); logs.add_argument("--start", type=int, default=0); logs.add_argument("--end", type=int); logs.add_argument("--diagnostics", action="store_true")
    artifacts = commands.add_parser("artifacts", help="workflow artifact inspection")
    _add_repo_options(artifacts)
    artifact_sub = artifacts.add_subparsers(dest="artifacts_command", required=True)
    listing = artifact_sub.add_parser("list"); _add_repo_options(listing, suppress=True); listing.add_argument("--run-id")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qatlib-forge", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    _pr_parser(commands); _issue_parser(commands)
    _ci_parser(commands)
    repo = commands.add_parser("repo", help="resolve repository identity")
    repo.add_argument("repository", nargs="?"); repo.add_argument("--platform", choices=("github", "gitea")); repo.add_argument("--web-host"); repo.add_argument("--api-base"); repo.add_argument("--project", type=Path, default=Path.cwd()); repo.add_argument("--remote", default="origin")
    compare = commands.add_parser("compare", help="read a complete changed-file comparison")
    _add_repo_options(compare); compare.add_argument("base"); compare.add_argument("head")
    branch = commands.add_parser("branch", help="conditional branch operations")
    delete = branch.add_subparsers(dest="branch_command", required=True).add_parser("delete")
    _add_repo_options(delete)
    delete.add_argument("name")
    delete.add_argument("--expected-sha", required=True)
    delete.add_argument("--project", default=Path.cwd(), type=Path)
    delete.add_argument("--remote", default="origin")
    status = commands.add_parser("status", help="read or write a task-card status marker")
    status_sub = status.add_subparsers(dest="status_command", required=True)
    read = status_sub.add_parser("read"); read.add_argument("body_file", type=Path)
    write = status_sub.add_parser("write"); write.add_argument("body_file", type=Path); write.add_argument("--head", required=True); write.add_argument("--state", required=True, choices=sorted(task_state.STATES)); write.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    args = _parser().parse_args(arguments)
    try:
        if args.command == "repo":
            ref = RepoRef.parse(args.repository, platform=args.platform, web_host=args.web_host,
                                api_base=args.api_base) if args.repository else RepoRef.from_remote(args.project, args.remote, platform=args.platform, web_host=args.web_host, api_base=args.api_base)
            print(json.dumps(asdict(ref), ensure_ascii=False, indent=2)); return 0
        if args.command == "status":
            body = args.body_file.read_text(encoding="utf-8")
            if args.status_command == "read":
                print(json.dumps({"controls": task_state.controls(body)}, ensure_ascii=False, indent=2)); return 0
            output = task_state.replace_control(body, args.head, args.state)
            (args.output or args.body_file).write_text(output, encoding="utf-8")
            return 0
        if args.command == "pr" and args.pr_command == "paths":
            from qatlib_forge.pr_paths import classify
            result = classify(args.project, base=args.base, head=args.head, workflow=args.workflow,
                              tested_base=args.tested_base, tested_head=args.tested_head,
                              ci_success=args.ci_success)
            print(json.dumps(result, indent=2))
            return 0
        api, ref = _api(args)
        if args.command == "branch":
            result = pr_writes.delete_branch(api, args.name, expected_sha=args.expected_sha,
                                             project=args.project, remote=args.remote)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "compare":
            print(json.dumps(api.compare(args.base, args.head), ensure_ascii=False, indent=2)); return 0
        if args.command == "runs":
            result = (api.workflow_runs(args.workflow, branch=args.branch)
                      if args.runs_command == "list" else api.run(args.run_id))
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str)); return 0
        if args.command == "logs":
            from qatlib_forge.ci_logs import RangeLogReader, extract_diagnostics
            status, headers, raw = api.download_job_log(args.job_id)
            if status in {301, 302, 303, 307, 308} and headers.get("Location"):
                reader = RangeLogReader(headers["Location"])
                raw, log_headers = reader.read(args.start, args.end)
                status = int(log_headers.get(":status", "200"))
            result = {"status": status, "bytes": len(raw), "text": raw.decode("utf-8", "replace")}
            if args.diagnostics:
                result["diagnostics"] = [d.__dict__ for d in extract_diagnostics(result["text"], base_offset=args.start)]
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.command == "artifacts":
            result = api.artifacts(args.run_id) if args.artifacts_command == "list" else {}
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str)); return 0
        if args.command == "pr":
            if args.pr_command == "read": result = api.pull_request(args.number)
            elif args.pr_command == "checks":
                result = pr_evidence.checks(api, args.number, expected_head=args.expected_head,
                                            required_jobs=args.require_job)
            elif args.pr_command == "merge": result = pr_writes.merge(api, args.number, expected_head=args.expected_head, method=args.method)
            elif args.pr_command == "activity":
                options = {"cold_days": args.cold_days, "ignore_users": args.ignore_user}
                if args.number is not None:
                    result = pr_evidence.activity(api, args.number, **options)
                else:
                    rows = api.list_pull_requests(state="open")
                    result = [pr_evidence.activity(api, row["number"], **options) for row in rows]
                    result.sort(key=lambda row: (row["cold"] is not True, row["number"]))
            elif args.pr_command == "list": result = api.list_pull_requests(state=args.state, head=args.head, base=args.base, labels=_labels(args.label))
            elif args.pr_command == "create": result = api.create_pull_request(title=_title(args.title, kind=args.kind, origin_tag=args.origin_tag), head=args.head, base=args.base, body=_text(args.body, args.body_file), draft=args.draft)
            elif args.pr_command == "update": result = api.update_pull_request(args.number, title=_title(args.title, kind=args.kind, origin_tag=args.origin_tag) if args.title else None, body=_text(args.body, args.body_file) if args.body is not None or args.body_file else None, base=args.base, state=args.state, labels=_labels(args.label))
            elif args.pr_command == "comment": result = api.comment(args.number, _text(args.body, args.body_file))
            elif args.pr_command == "comments": result = [{"kind": kind, **record} for kind, record in api.discussion_records(args.number) if args.kind == "all" or kind == args.kind]
            elif args.pr_command == "review-comment": result = api.add_review_comment(args.number, _text(args.body, args.body_file), commit_id=args.commit_id, path=args.path, line=args.line, position=args.position)
            else: result = api.set_draft(args.number, args.pr_command == "draft")
        else:
            if args.issue_command == "list": result = api.list_issues(state=args.state, labels=_labels(args.label), assignee=args.assignee)
            elif args.issue_command == "read": result = api.issue(args.number)
            elif args.issue_command == "create": result = api.create_issue(title=_title(args.title, kind=args.kind, origin_tag=args.origin_tag), body=_text(args.body, args.body_file), labels=_labels(args.label), assignees=args.assignee)
            elif args.issue_command == "update": result = api.update_issue(args.number, title=_title(args.title, kind=args.kind, origin_tag=args.origin_tag) if args.title else None, body=_text(args.body, args.body_file) if args.body is not None or args.body_file else None, state=args.state, labels=_labels(args.label), assignees=args.assignee)
            elif args.issue_command == "close": result = api.close_issue(args.number)
            elif args.issue_command == "reopen": result = api.reopen_issue(args.number)
            else: result = api.issue_comment(args.number, _text(args.body, args.body_file))
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str)); return 0
    except (OSError, ValueError, TypeError, KeyError, RepoError) as error:
        print(f"qatlib-forge: {error}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
