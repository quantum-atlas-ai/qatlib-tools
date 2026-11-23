# Open the first draft

This page starts from a branch that is **already pushed**. The previous page's example is the branch `fix/facts-note` in a Lean repository, in the worktree `mylib-facts-check`. When the change is to this tools repository, replace the repository and the verification commands below with the commands you actually ran. The full local gate for this repository is in the [contributing guide](../../CONTRIBUTING.en.md#self-test).

The working directory may be that worktree. Confirm that the local head and the remote match:

```bash
git status
git rev-parse HEAD
git push -u origin HEAD
```

The 40-character SHA from `git rev-parse HEAD` is the commit the rest of this page compares. If the branch was already pushed, `git push` reports that the remote already has this commit. On success, `git status` shows the branch tracking the same name on `origin`.

## Open a draft

Use “Compare & pull request” on GitHub and choose Draft under the title, or use `gh` from the worktree:

```bash
gh pr create --draft --title "Note one fact in MyLib.Facts" --body-file /tmp/mylib-facts-pr.md
```

Split the body into Changes and Verification, matching `.github/PULL_REQUEST_TEMPLATE.md` in the repository. The record below is a pattern. Replace the bracketed text with your SHA, exit codes, and the checks you did not run:

````markdown
## Changes

One documentation comment in `src/MyLib/Facts.lean` changed. The module argument is `MyLib.Facts`.

## Verification

- Working directory: `mylib-facts-check`. Head: `<40-character SHA from git rev-parse HEAD>`.
- `qatlib-env plan MyLib.Facts` — exit 0. `cold_or_rebuild_modules` was `<number>`. `reasons` was `<an empty array or the codes you saw>`.
- `qatlib-env prepare` — exit 0.
- `qatlib-env run -- lake build MyLib.Facts` — exit 0.
- `qatlib-env run -- lake --no-build lean src/MyLib/Facts.lean` — exit 0.
- Not run: a repository-wide `lake build`.
````

On success, `gh pr view --json number,isDraft,url` shows `isDraft` as `true`, and `url` opens in a browser. A draft still triggers `CI / test` in this tools repository. Whether a downstream Lean repository skips an expensive build is decided by that repository's workflow. The two cases are separated in [GitHub workflows](github-ci.md).

Gitea marks a draft with a title prefix, and the create command may not be `gh`. If the first pull request is on Gitea, read the [platform differences on the review page](forge.md#gitea) first.

## Read Checks for the current commit

Take the head again before and after every reading. A push in between means the checks on the page no longer belong to the commit you just inspected.

```bash
git rev-parse HEAD
gh pr view --json headRefOid,isDraft,statusCheckRollup
gh pr checks
```

`headRefOid` must equal `git rev-parse HEAD`. Sort each check into one of these four rows before leaving the draft:

| Status you see | What it means |
| --- | --- |
| Queued or in progress | There is no conclusion yet |
| `skipped` | The workflow started, and this job was skipped by a condition. Skipped is not passed |
| Success | That job passed on this head. Copy the job URL and the head SHA into the verification record |
| No run for that workflow | It may not have been triggered. `ci.yml` in this tools repository has no path filter, so a documentation change still runs `CI / test`. `docs.yml` does not build Pages on a pull request |

An exit code of 0 from `gh pr checks` means the command retrieved the results. Whether a job passed is the conclusion on each line. To summarize by job name, use `qatlib-forge pr checks <number> --expected-head <40-character SHA>`. That command also only retrieves; the returned states say whether the jobs passed. The fields, and the difference between skipped and queued, are in [Reviewing concurrent commits](forge.md#ci).

## Mark it ready when the record is complete

After the verification record names the commands, the results, and the checks you did not run, and the checks on the current head cover the review you are requesting:

```bash
gh pr ready
```

On success, `gh pr view --json isDraft,headRefOid` shows `isDraft` as `false`, and `headRefOid` is still the SHA you compared.

A change to the protected design text in `DESIGN.md` stays a draft and follows the human-review agreement in [DESIGN.en.md](../../DESIGN.en.md). Locally, run `python3 scripts/check_human_contracts.py`. While the digest is unapproved, the script prints the pending digest and exits nonzero. To see the candidate digest on a draft, add `--draft`. That gate is in the contributing guide. An ordinary documentation edit on this page does not need it.

Merging, reusing a conclusion from another head, and deleting a branch belong to the reviewer. See [Reviewing concurrent commits](forge.md).
