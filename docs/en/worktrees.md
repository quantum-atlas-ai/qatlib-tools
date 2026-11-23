# Open a second worktree in a Lean repository

This page assumes you are working in a **Lean project** that already pins a `qatlib-tools` version, with `qatlib-env` provided by that environment. For the five-minute check of the tools repository itself, see the [previous page](quickstart.md). This walkthrough carries the same example through a single edit. When `lakefile.toml` sets `srcDir` to `"src"`, the file `src/MyLib/Facts.lean` maps to the module argument `MyLib.Facts`. Your project's package name, library name, and module namespace may differ; if your file lives at another path, substitute that path and keep the module argument paired with it. Those fields are explained in [How to read lakefile.toml](build.md#lakefile).

Start in your original checkout. The example creates a new worktree beside it at `../mylib-facts-check` on the branch `fix/facts-note`. Choose a branch name that does not exist yet.

## 1. Create the worktree without restoring caches

```bash
qatlib-env worktree ../mylib-facts-check fix/facts-note --no-bootstrap
```

Your working directory stays in the original checkout, where the command runs `git worktree add -b fix/facts-note ../mylib-facts-check`. Passing `--no-bootstrap` stops right after creating the tree without calling `prepare`, so this step does not download a toolchain or a cache.

On success, `../mylib-facts-check` exists, `git status` inside it shows the branch `fix/facts-note`, and the worktree is clean. Standard error prints one `AGENTS.local.md` line stating whether the tree sees the repository default or a local note. You should not see long download output here.

## 2. Enter the new tree and read the plan first

```bash
cd ../mylib-facts-check
qatlib-env plan MyLib.Facts
```

`plan` reads the source graph, the lock, and the current Git diff, then prints one JSON object to standard output without running `lake build`. Focus on these fields:

| Field | Meaning |
| --- | --- |
| `target_modules` | Number of modules the target resolved to |
| `cold_or_rebuild_modules` | Estimated package modules that would be rebuilt |
| `reverse_invalidated_modules` | Estimated reverse dependencies that would be invalidated |
| `dependency_lock_mismatches` | Dependencies whose checkout disagrees with the lock |
| `unresolved_imports` | Imports that could not be resolved yet |
| `reasons` | Limits raised by the estimator; an empty array means no local-budget objection was raised |
| `estimate_basis.confidence` | Confidence of the estimate; this is a heuristic, not a real compilation |

Before dependencies are checked out, `unresolved_imports` may list those dependency modules; `prepare` fetches them in the next step. If `reasons` contains `cold_limit` or `reverse_limit`, narrow the target—or check the project's own budget notes—before deciding whether a later `run` needs `--allow-local-heavy`. This exercise stays on one leaf module.

## 3. Prepare the locked dependencies

Run this from `mylib-facts-check`:

```bash
qatlib-env prepare
```

`prepare` sets up dependency sources from `lake-manifest.json` and installs configured caches, writing both this worktree's `.lake` dependency checkout and the content-addressed shared cache. Each worktree keeps a separate `.lake`.

This step can take a while. The first run downloads the Lean toolchain and unpacks or compiles any missing artifacts. When the shared cache already contains those inputs, the command mostly checks what is present and fills in gaps. On success, it prints JSON and exits 0.

Run `plan` again after preparation finishes:

```bash
qatlib-env plan MyLib.Facts
```

Compare `cold_or_rebuild_modules` with the previous output. That number is the scope you are about to pass to `lake build`. Its basis appears in `estimate_basis`, though it does not replace an actual build result.

## 4. Build this one module

Stay in the new worktree.

```bash
qatlib-env run -- lake build MyLib.Facts
```

`run` executes the command after `--` within the resource budget. Because this command omits `--no-build`, `run` prepares dependencies again when required, then invokes Lake. Lake builds `MyLib.Facts` and its imports, writing artifacts into this tree's `.lake`. On a shared-cache hit, large `.olean` files can remain in the cache directory. See [Cache flags](build.md#cache-flags) for the available switches.

On success, Lake reports that the target is built and the command exits 0.

## 5. Edit one place, then check that file with the artifacts you have

Open `src/MyLib/Facts.lean`, edit one documentation comment, and leave the file type-correct. Do not change imports in the same edit.

Then check only this file, without letting Lake build missing dependencies:

```bash
qatlib-env run -- lake --no-build lean src/MyLib/Facts.lean
```

Passing `--no-build` tells `run` to skip the automatic `prepare`, so Lake checks the file using only dependency artifacts that already exist. On success, the exit code is 0 and the output reflects only the check of this file.

If Lake reports that a dependency artifact is missing, return to the previous step, run `qatlib-env run -- lake build MyLib.Facts`, and repeat the `--no-build` command. Do not switch to a bare `lean` binary, and do not inspect only `.lake/build`—artifacts stored in the shared cache are not all copied into that directory. See [Incremental builds and single-file checks](build.md#incremental-build).

## 6. Preview cleanup only

Stay in the new worktree (`mylib-facts-check`).

```bash
qatlib-env workspace clean-caches --checkout . --derived-only --dry-run
```

`--derived-only` selects existing `.lake/build` and `.lake/packages` directories, while `--dry-run` prints the targets without moving any files. Preview mode is already the default; passing `--dry-run` makes that explicit in the command.

Review the output and stop here. Do not add `--apply`—you will use this worktree next on the pull-request page.

To preview retiring the entire worktree instead of just its derived directories, run the next command (again omitting `--apply`) and read its warnings:

```bash
qatlib-env workspace trash-checkouts --checkout . --dry-run
```

The preview lists uncommitted changes, in-flight branches, and processes that are still running. This page stops at the preview.

## What you have when this page is done

You now have a new worktree on `fix/facts-note`, with one module built and one single-file check completed. Next, push that branch and open a draft pull request following [Pull requests](pull-requests.md). For reference on the cache matrix, Windows cross-volume behavior, and configuration precedence, see the [build guide](build.md).
