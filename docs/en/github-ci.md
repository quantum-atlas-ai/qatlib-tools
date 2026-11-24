# GitHub workflows in this repository

This page is for maintainers of this repository and for anyone wiring downstream CI. If you are a contributor checking the status of your current commit, see [Pull requests](pull-requests.md). If you are a reviewer deciding whether an earlier conclusion still applies and whether a merge is safe now, see [Reviewing concurrent commits](forge.md). For the commands that register a self-hosted runner, see [Self-hosted runners](runner.md).

This repository defines two workflows, and they answer different questions.

## Events, jobs, permissions, and what a green check shows

| | `CI` (`.github/workflows/ci.yml`) | `Documentation` (`.github/workflows/docs.yml`) |
| --- | --- | --- |
| When it runs | `pull_request` events (`opened`, `synchronize`, `reopened`, `ready_for_review`, and `converted_to_draft`), as well as pushes to `main` | Pushes to `main` that modify `*.md`, `docs/**`, `site/**`, or this workflow file, as well as manual `workflow_dispatch` runs |
| Path filter | None. Even a documentation-only pull request runs this workflow | Yes. A push to `main` outside those paths does not build the site |
| Runner | `ubuntu-latest` | `ubuntu-latest` |
| Permissions | Top-level `contents: read` | Top-level `contents: read`, `pages: write`, and `id-token: write` |
| Jobs | `test`: human contracts, `uv lock --check`, `uv build`, pytest, and a wheel smoke test in a clean virtual environment | `build` builds the VitePress site. `deploy` runs only when the repository is public and `github.ref` is `refs/heads/main`, using the `github-pages` environment |
| What green means | The Python 3.12 checks for this tools repository passed on that commit. A draft pull request passes `--draft` to the contract script; a ready pull request and a push to `main` do not | A successful `build` confirms that the site builds. A successful `deploy` confirms that the public repository's `main` branch published the site to Pages |

A green `CI` check on a pull request does not mean Pages built or deployed, because `docs.yml` has no `pull_request` trigger.

Both workflows set `persist-credentials: false` on `actions/checkout`, so a later git push in the job does not reuse credentials stored by that checkout.

## Which workflow a pull request runs

Check `on.pull_request.types` in `.github/workflows/ci.yml`: opening a pull request, pushing new commits, reopening it, marking it ready for review, and converting it to a draft all trigger `CI / test`, regardless of which files changed.

Check `on` in `docs.yml`: pull requests are not included. The site builds when a push to `main` on a public repository touches one of the listed paths. The `deploy` job also requires a public repository and `refs/heads/main`, so on a private repository `deploy` is skipped.

## Who configures a credential, which entry uses it, and when

| Credential | Who configures it | Which entry uses it | When it is needed |
| --- | --- | --- | --- |
| A person's `gh` login | The person running the command | `gh pr create`, `gh pr ready`, `gh pr checks` | Opening a draft pull request, marking it ready, and viewing checks for the current commit |
| `GITHUB_TOKEN` | GitHub Actions, from the workflow `permissions` | The two workflows above | `CI` reads repository contents. The Pages write permission and `id-token: write` appear only in `docs.yml` |
| Runner registration token | Someone authorized to create a registration token for that repository, or an already authenticated `gh` session | `setup_runner.sh register` | Once, while registering a self-hosted runner. Both workflows here use `ubuntu-latest` and do not consume this token |
| `GITEA_MIRROR_SSH_KEY` | The downstream repository's secret administrator | `qatlib-env ci mirror`, and the same function at the end of `lint` | See *Optional Gitea mirror* below. This is separate from GitHub's `GITHUB_TOKEN` |
| R2 write key | Someone authorized to publish the shared cache | The upload inside `qatlib-env ci build` when `may_publish` is true | On a downstream `main` push or `workflow_dispatch`, and only when that package's write key is present. It is not given to every collaborator |
| `QATLIB_PKG_MIRROR` | The host or lane environment | The dependency source mirror | When fetching locked Git dependencies from the local mirror. It does not rewrite the GitHub website URL, and it does not push `main` to Gitea |

You can also configure an optional GitHub fetch rewrite on the host with `git config url.<mirror>.insteadOf`. `setup_runner.sh init` writes an `insteadOf` rule for `https://github.com/` on the lane account only when `QATLIB_GITHUB_MIRROR` is set. That rewrite applies while fetching; the Gitea push described below is a separate action.

## Wiring `qatlib-env ci` into a downstream repository

`ci.yml` in this repository does not call `qatlib-env ci`. To use the four subcommands in a downstream Lean repository, install the tool version locked by that repository, then call:

| Command | Role |
| --- | --- |
| `qatlib-env ci lint` | Runs static checks for the repository's profile. If the mirror conditions also hold, it attempts the mirror push at the end |
| `qatlib-env ci build` | Prepares dependencies and compiles the profile's targets. It uploads cache only when the publish conditions hold |
| `qatlib-env ci build-gate` | Reads `GITHUB_EVENT_PATH` or `GITEA_EVENT_PATH`. For a draft, it writes `build=false` to `GITHUB_OUTPUT` or `GITEA_OUTPUT`; for any other event, it writes `build=true` |
| `qatlib-env ci mirror` | Runs the mirror push alone, under the conditions described below |

`python -m qatlib_env.ci --help` and `qatlib-env ci --help` use the same parser. Required check names come from the downstream repository's own workflow, not from `CI / test` in this repository.

Both `lint` and `build` check admission against the local resource budget and expect a runner whose resource limits are already in place. For runner registration, see [Self-hosted runners](runner.md). For directory rules covering the dependency mirror and artifact caches, see [Three caches and worktrees](build.md#cache-layout).

## Optional Gitea mirror

`qatlib-env ci mirror`, and the call to the same function at the end of `lint`, return without pushing unless all of these conditions hold:

- `GITEA_MIRROR_SSH_KEY` is set
- the event name is `push`
- the ref is `refs/heads/main`

The mirror URL comes from `GITEA_MIRROR_URL`, falling back to `gitea_mirror_url` from configuration. The host key comes from `GITEA_KNOWN_HOST` or `SSH_KNOWN_HOSTS`; when both are empty, the command prints why it skipped and returns.

When these conditions hold, the command uses that key to run `git push --force`, updating the mirror's `refs/heads/main` from the current `HEAD` and `refs/tags/*` from the local tags. `--force` makes the mirror's `main` branch and tags match this push. If the push fails, the function prints `::warning::` and returns; that failure does not cause `lint` to exit with a nonzero status.

This key authorizes that Gitea mirror alone. It does not grant merge permission on GitHub, and it is neither a runner registration token nor an R2 write key.

## Changes that ask a maintainer to review

`.github/CODEOWNERS` defines maintainer coverage for `DESIGN.md`, `docs/design/human-contracts.sha256`, and `.github/`. When repository rules enable Code Owners, GitHub enforces a review from those owners for changes inside that coverage, including edits to workflows and the contract digest.

For the draft process covering protected design text, see [DESIGN.en.md](../../DESIGN.en.md). This page does not change that text.
