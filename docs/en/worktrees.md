# Open a second worktree in a Lean repository

This walkthrough assumes you are working in a **Lean project** that already pins a `qatlib-tools` version, with `qatlib-env` active from that environment (for the tools repository's own smoke check, see the [five-minute check](quickstart.md)). We will carry one example through a complete edit: when `lakefile.toml` sets `srcDir` to `"src"`, the source file `src/MyLib/Facts.lean` maps to the module argument `MyLib.Facts`. Because package names, library target names, and module namespaces are independent in Lake, substitute your actual file path if it differs and keep the module argument paired with it; see [How to Read `lakefile.toml`](build.md#lakefile).

Start in your existing primary checkout. The commands below create a sibling worktree at `../mylib-facts-check` on a new branch `fix/facts-note` (choose a branch name that does not exist yet).

Even when you place additional worktrees on a separate data volume (such as `D:` on Windows or a dedicated data disk on Linux and macOS), all worktrees still share the global Lean toolchain and content-addressed caches under your home directory while keeping each worktree's `.lake` directory isolated. Before provisioning multiple worktrees across drives, review [Resource usage and cross-volume layout](install.md#resource-footprint) for disk and memory footprints and [Windows cross-volume setups and long paths](build.md#windows) for cross-volume copy fallback behavior.

## 1. Create the worktree without restoring caches

```bash
qatlib-env worktree ../mylib-facts-check fix/facts-note --no-bootstrap
```

Run this from your original checkout, where the command executes `git worktree add -b fix/facts-note ../mylib-facts-check`. Passing `--no-bootstrap` stops right after creating the worktree without invoking `prepare`, so no toolchain or cache downloads happen yet.

On success, `../mylib-facts-check` exists, `git status` inside it shows a clean working tree on branch `fix/facts-note`, and standard error prints one `AGENTS.local.md` line indicating whether the worktree sees the repository default instructions or a local override. You should not see bulk download output at this stage.

## 2. Enter the new tree and read the plan first

```bash
cd ../mylib-facts-check
qatlib-env doctor
qatlib-env plan MyLib.Facts
```

`doctor` checks the locked Elan toolchain files, compares Git dependencies in `lake-manifest.json` with their checkout HEADs, and looks for usable local mirrors of missing or mismatched pins. It also reports whether artifacts have native output mappings in the effective `LAKE_CACHE_DIR`. Run `qatlib-env doctor --project /path/to/worktree --json` for a versioned report with finding codes, counts, and example paths. The check is read-only and runs without Lean, Lake, or network access. Exit 0 means inspection completed with status `ok` or `warning`. Exit 1 means the report contains an inspection or configuration error.

If Lake reports **“No server olean generated. Ensure the module system is enabled.”**, look for `missing_server_olean_descriptor` or `missing_local_server_olean`. A module can have a base `.olean` but lack its server companion. `missing_cache_server_olean` means the mapping names the companion but the cache file is unavailable. Legacy sources without a `module` header can legitimately have only the base file. The scan checks ordinary and server olean descriptors and visible files, along with archive presence. It does not unpack archives, compute Lake input hashes, or verify native cache hits for a target.

For each build directory, `hardlink_eligible` means it shares a volume with the cache artifacts and Lake can try a hardlink. Lake falls back to copying if the link fails. `copy_fallback` indicates different volumes on POSIX or Windows, including paths that resolve onto another volume through a junction. If artifacts are present with no published output mappings, check cache registration before downloading them again.

`plan` inspects the source import graph, lockfile, and current Git diff in read-only mode, printing one JSON object to standard output without running `lake build`. Check these key fields:

| Field | Meaning |
| --- | --- |
| `target_modules` | Number of modules the target argument resolved to |
| `cold_or_rebuild_modules` | Estimated in-package modules that would be compiled or rebuilt |
| `reverse_invalidated_modules` | Estimated reverse-dependent modules affected by the change |
| `dependency_lock_mismatches` | Dependencies whose local checkout disagrees with the lockfile |
| `unresolved_imports` | Imports that cannot be resolved on disk yet |
| `reasons` | Budget limits triggered by the estimator; an empty array means the target fits within local budget limits |
| `estimate_basis.confidence` | Confidence note indicating a static heuristic estimate rather than an actual compiler run |

Because external dependencies have not been checked out yet, seeing them under `unresolved_imports` is expected—`prepare` populates them in the next step. If `reasons` contains `cold_limit` or `reverse_limit`, narrow your target or review the project's budget policy before deciding whether a subsequent `run` genuinely warrants `--allow-local-heavy`. This walkthrough stays on a single leaf module.

## 3. Prepare the locked dependencies

From `mylib-facts-check`, run:

```bash
qatlib-env prepare
```

`prepare` checks out locked dependency sources from `lake-manifest.json` and restores configured caches, populating both this worktree's private `.lake` dependency tree and the global content-addressed shared cache (each worktree keeps an isolated `.lake` directory—do not copy `.lake` wholesale across checkouts).

A cold run may take some time while downloading the Lean toolchain and unpacking or building missing artifacts; when the shared cache is already warm, `prepare` mostly verifies integrity and fills in any gaps. On success, it prints a summary JSON object and exits 0.

Run `plan` again once preparation completes:

```bash
qatlib-env plan MyLib.Facts
```

Compare `cold_or_rebuild_modules` against the earlier output: this figure represents the estimated build scope you are about to hand to `lake build` (its assumptions are recorded under `estimate_basis` and do not replace an actual compiler run).

## 4. Build this one module

Still inside the new worktree, run:

```bash
qatlib-env run -- lake build MyLib.Facts
```

`run` executes the command after `--` within managed resource limits. Because `--no-build` is omitted, `run` ensures dependencies are prepared if needed and then invokes Lake to build `MyLib.Facts` and its upstream imports. Outputs are registered under this worktree's `.lake`; on a writable shared-cache hit, large `.olean` files stay in the shared cache directory rather than being duplicated into every worktree (see [Cache Read/Write and Restoration Flags](build.md#cache-flags)).

On success, Lake reports that the target built cleanly and the command exits 0.

## 5. Edit one place, then check that file with the artifacts you have

Open `src/MyLib/Facts.lean`, edit a single documentation comment so the file remains well-typed, and leave its `import` header unchanged.

Now type-check just that file without allowing Lake to compile missing dependencies implicitly:

```bash
qatlib-env run -- lake --no-build lean src/MyLib/Facts.lean
```

With `--no-build`, `run` skips automatic dependency preparation and Lake checks the target file strictly against dependency artifacts already available. On success, the command exits 0 and reports diagnostics for this file alone.

If Lake reports a missing dependency artifact, go back to Step 4, run `qatlib-env run -- lake build MyLib.Facts`, and repeat the `--no-build` check. Never invoke the bare `lean` binary directly or judge artifact availability solely by inspecting `.lake/build`—artifacts served from the global shared cache are not all materialized in the local build directory; see [Incremental Builds and Single-File Checking](build.md#incremental-build).

## 6. Preview cleanup only

Still inside `mylib-facts-check`, preview derived-cache cleanup:

```bash
qatlib-env workspace clean-caches --checkout . --derived-only --dry-run
```

`--derived-only` targets existing `.lake/build` and `.lake/packages` directories, while `--dry-run` prints candidate paths without moving any files (preview mode is already the default, and passing `--dry-run` makes that intent explicit).

Stop after reviewing the output—do not pass `--apply`, since the next guide reuses this worktree to open a draft pull request.

To preview retiring the entire linked worktree rather than just its derived directories, run the command below (again omitting `--apply`) to inspect its safety warnings:

```bash
qatlib-env workspace trash-checkouts --checkout . --dry-run
```

The preview surfaces uncommitted edits, unmerged branches, and active processes still using the directory. Stop at the preview here.

## What you have when this page is done

You now have an isolated worktree on branch `fix/facts-note` with one module built and one single-file check verified. Next, push the branch and open a draft pull request following [Pull requests](pull-requests.md). For full reference tables on the nine cache-flag combinations, Windows cross-volume behavior, and the seven-layer configuration precedence, see [Build Environment, Caching, and Resource Control](build.md).
