# Pull Requests on GitHub and Gitea

This page is for a reviewer. The question is which head the checks you already see actually measured, whether that evidence applies to the pull request in front of you, and whether it is safe to merge or delete the branch now. Opening a first draft is in [Pull requests](pull-requests.md). When each of this repository's two workflows runs is in [GitHub workflows](github-ci.md).

<a id="ci"></a>

## Which head this check measured

Write down the head SHA shown on the page. Read it again before and after the query. If the two values differ, stop and query the new SHA.

```bash
qatlib-forge pr checks <number> --expected-head <40-character lowercase SHA>
```

An exit code of 0 means the results were retrieved. Whether a job passed is the returned state: `success`, `failure`, `skipped`, `cancelled`, `pending`, or `absent`. Skipped, still queued, missing, and infrastructure failures are not passes. Filters and state names are in `qatlib-forge pr checks --help`.

GitHub serves check-runs, commit statuses, and Actions jobs from separate endpoints. Follow the target URL on a status record to the repository, the workflow run, and the job, and paginate by the full SHA so the query does not stop on an older page. [GitHub Checks API](https://docs.github.com/en/rest/checks/runs) · [GitHub Commit Statuses API](https://docs.github.com/en/rest/commits/statuses)

<a id="gitea"></a>

## How Gitea states and drafts differ

A Gitea draft follows the instance's title prefix, such as `WIP:` or `[WIP]`. The API `draft` field reflects that prefix. Whether removing the prefix starts a build depends on the workflow events and job conditions. If editing the title starts a build, look up existing records by number, head, and workflow first. An applicable job that is already queued, running, or finished should be followed in that state, not started again. [Gitea draft prefixes](https://docs.gitea.com/administration/config-cheat-sheet/#repository---pull-request-repositorypull-request)

On one SHA, a job can already be `skipped` while the outer summary is still `pending`. Treating the outer summary as the result reads a skip as a job that has not run. Open the target URL and read the job before deciding.

`tea` and `gt` are different Gitea clients. `gt` is the newer official CLI. Check which subcommands the client supports and which instance it is connected to. [tea v0.16.0](https://gitea.com/gitea/tea/releases/tag/v0.16.0) · [gt documentation](https://docs.gitea.com/cli/)

<a id="tools"></a>

## Local branches and creating a draft

Inspect and push the local branch with `git`. Create and update the pull request with GitHub's `gh`, with one of the Gitea clients above, or with the platform API. A GitHub draft is the separate `isDraft` flag: `gh pr create --draft` opens it, and `gh pr ready` marks it ready. [gh pr ready](https://cli.github.com/manual/gh_pr_ready)

<a id="reuse"></a>

## Whether an existing conclusion applies to this pull request

Whether a workflow schedules a build depends on that repository's path filters. A workflow filtered out entirely leaves no run. Inside a workflow that did start, a job skipped by a condition is recorded as `skipped`. A documentation change that misses the build paths still needs the static checks that repository requires. [Path filters](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)

After the base branch gains more commits, an older success applies only when all of these hold:

1. The successful jobs correspond to the current head, not to an earlier push.
2. The ancestry between the tested base and the current base, and the workflow rules at both ends, have been compared.
3. Commits added on the base in that interval touch only documentation that misses the build rules, and they share no file with the pull request.

For example, the current head already built, and the base later changed a different document the build does not read. When the three conditions hold, each in-flight pull request does not have to merge the base again and rebuild. A new push on the pull request, a base change to a build input, or an edit to the same file on both sides means verifying the current pair of commits.

In a local repository that contains both commits:

```bash
qatlib-forge pr paths \
  --project . \
  --base <full SHA of the current base> \
  --head <full SHA of the current head> \
  --workflow .github/workflows/<workflow file> \
  --tested-base <tested base SHA> \
  --tested-head <tested head SHA> \
  --ci-success
```

`--tested-base` and `--tested-head` are supplied together. The tool does not query CI, so add `--ci-success` only after you have confirmed that the applicable jobs succeeded. Record the tested commits, the job URLs, and the changed paths. If a filter cannot be explained, the base has a related change, or any piece of evidence is missing, run the current combination again.

<a id="concurrent-writes"></a>

## Whether it is safe to merge or delete the branch now

Between the end of review and the merge, the remote branch can still receive a commit. Send the full reviewed head to the server so the merge compares it.

On GitHub:

```bash
gh pr merge <number> --match-head-commit <40-character SHA>
```

On either platform:

```bash
qatlib-forge pr merge <number> --expected-head <40-character lowercase SHA>
```

Read the applicable CI and your own permissions again before merging. [GitHub merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

Before deleting, check the push target, the default branch, and the protection rules, then confirm the remote SHA:

```bash
qatlib-forge branch delete <branch> --expected-sha <40-character lowercase SHA>
```

Underneath, that is `git push --force-with-lease=refs/heads/<branch>:<SHA> <remote> :refs/heads/<branch>`. Git refuses the delete when the remote ref is not the expected SHA. Stop and look again when the protection rules cannot be read in full, or when the remote already has a new commit. [git push](https://git-scm.com/docs/git-push)
