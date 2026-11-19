# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
· [简体中文](README.md)

`qatlib-tools` provides a shared engineering handbook and command-line tools for Lean mathematical libraries and their downstream research repositories; specific mathematical interface design, bibliographic content, and merge criteria remain maintained by each project.

In day-to-day collaboration, developers can use `git`, GitHub's `gh`, Gitea's `tea` or `gt`, or call code-hosting platform APIs directly. Cross-cutting concerns shared across repositories—abstracting away cross-platform differences, aggregating CI check results, and verifying expected commits before write operations—are consolidated into these shared tools.

## Commands

| Command | Purpose |
| --- | --- |
| `qatlib-env` | Prepare Lean dependencies and shared caches, run controlled builds under resource quotas, and manage local worktrees |
| `qatlib-retrieve` | Search and retrieve Lean declarations, source signatures, and bibliographic references |
| `qatlib-forge` | Aggregate and verify PR check results on GitHub and Gitea, and validate expected commits before write operations |

Once installed, run `qatlib-env docs` at any time to read this handbook offline, or run `qatlib-env docs --list` to list all built-in topics and `qatlib-env docs <topic>` to read the corresponding chapter. For the exact options of each command, refer to the `--help` output of the currently installed version.

## Engineering Handbook Guide

| Topic | Offline Command (Chinese) | Contents |
| --- | --- | --- |
| [Build Environment, Caching, and Resource Control](docs/en/build.md) | `qatlib-env docs build` | Lake configuration, module visibility, shared caches, Windows cross-drive handling, and local resource budgets |
| [CI Runners and Process Troubleshooting](docs/en/runner.md) | `qatlib-env docs runner` | cgroup memory accounting, process reaping on job cancellation, and deployment verification |
| [Lean Documentation, Bibliography, and Source Retrieval](docs/en/lean-docs.md) | `qatlib-env docs lean-docs` | doc-gen4 cross-page linking, incremental bibliography validation, and static lexical indexing |
| [Pull Requests on GitHub and Gitea](docs/en/forge.md) | `qatlib-env docs forge` | Two draft mechanisms, CI status verification, concurrency-safe merging, and branch cleanup |
| [How Shared Work Enters the Public Library](docs/en/promotion.md) | `qatlib-env docs promotion` | Whole-file dependencies, consumer verification, draft rehearsals, and post-release dependency switching |

<a id="quickstart"></a>

## Installation

A single installation provides all three command-line entry points: `qatlib-env`, `qatlib-retrieve`, and `qatlib-forge`. Choose the installation method that fits your workflow and always operate inside an isolated virtual environment—never run a bare `pip install` directly against the system Python interpreter.

### Project-Pinned Version

For day-to-day development or running project checks inside a repository, use the tool version pinned by that project to stay consistent with CI. If a project declares its pinned dependencies in `python/pyproject.toml`, create a virtual environment at the repository root and install `./python` directly; repositories with a different directory layout should follow their own installation instructions.

- **Using `uv`**

  ```bash
  uv venv .venv
  uv pip install --python .venv/bin/python ./python
  source .venv/bin/activate
  qatlib-env --help
  qatlib-retrieve --help
  ```

- **Using `venv` + `pip`**

  ```bash
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install ./python
  source .venv/bin/activate
  qatlib-env --help
  qatlib-retrieve --help
  ```

In Windows PowerShell, activate the virtual environment with `.venv\Scripts\Activate.ps1` instead, and replace the interpreter path in the installation commands with `.venv\Scripts\python.exe` (for example, `uv pip install --python .venv\Scripts\python.exe ./python` or `.venv\Scripts\python.exe -m pip install ./python`). To test a specific version of the tools independently, install the wheel built from that commit in a separate isolated environment and verify its corresponding source commit against the build record.

### Standalone CLI Installation

To invoke `qatlib-env`, `qatlib-retrieve`, and `qatlib-forge` from any directory outside a project-specific virtual environment, replace `/path/to/qatlib-tools` in the commands below with a local checkout path, a downloaded `.whl` file, or a pinned commit from an accessible repository (`'git+https://github.com/quantum-atlas-ai/qatlib-tools.git@<commit-sha>'`):

- **Using `uv`**: Run `uv tool install /path/to/qatlib-tools` so `uv` manages a dedicated tool environment automatically; if your terminal cannot find the commands, check the binary directory with `uv tool dir --bin` and run `uv tool update-shell` to add it to `PATH`.
- **Using `pipx`**: Run `pipx install /path/to/qatlib-tools`; if the binary directory is not yet on `PATH`, run `pipx ensurepath` to add it.
- **Using `venv` + `pip`**: Manually create a dedicated virtual environment and add its `bin` directory (`Scripts` on Windows) to your shell's user `PATH`:

  ```bash
  python3 -m venv ~/.local/share/qatlib-tools-venv
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install --upgrade pip
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install /path/to/qatlib-tools
  export PATH="$HOME/.local/share/qatlib-tools-venv/bin:$PATH"
  ```

<a id="configuration"></a>

## Build Resources

When no resource budget is explicitly configured, `qatlib-env` automatically derives the default resource pool capacity from the host's available memory, process CPU affinity, and any visible cgroup v1/v2 limits; when computing host memory headroom, it reserves the larger of 2 GiB or 10% of total physical memory as a system buffer. Under purely default settings, a single build typically requests roughly half of the pool's capacity, capped by default at 8 CPUs, 16 GiB of memory, and 4 parallel compilation processes; if the pool itself is small, a single build may use its full available capacity, and in single-core environments it falls back by default to 1 compilation process with 1 Lean thread.

To inspect how a budget is derived, run the read-only diagnostic command `qatlib-env config explain --json`. Without fetching dependencies or acquiring build-pool quotas, it displays the detected underlying capacity (`resources`), the automatically derived values prior to overrides (`automatic`), and the effective parameters and routing configuration (`values` and `configuration.routing`), making it straightforward to combine source attribution (`sources`, `configuration.sources`) and diagnostic warnings (`warnings`) to decide whether manual tuning is needed. Persistent local settings can be stored in `~/.qatlib/env.toml`, while one-off adjustments for a single build can be passed via command-line flags. The tool preserves explicitly configured user values, though actual job execution remains jointly bounded by remaining capacity in the shared build pool and underlying platform resource limits. See [Configuration Sources and Resource Detection](docs/en/build.md#configuration) for details.

## Local Directories

The default state root directory for `qatlib-tools` is `~/.qatlib`, which can be redirected to another location via the `QATLIB_HOME` environment variable. Within this directory, personal configuration resides in `env.toml`, local retrieval indexes are stored in `retrieve/`, and newly created build-pool ledgers and cloud job receipts are placed under `state/pool/` and `state/cloud/`, respectively; to verify the exact read/write locations in effect, check the `paths` field in the output of `qatlib-env config explain --json`. Across version upgrades, the tools remain compatible with existing configuration files and indexes, and running build pools continue using their existing ledger and lock files; Lake build artifacts, login configurations for upstream CLIs, and legacy worktree pools are each maintained in separate paths. See [Directory Layout and Compatibility Rules](docs/en/build.md#local-paths) for details.

<a id="artifact-cache"></a>
## Building and Shared Artifact Caching

```bash
qatlib-env prepare
qatlib-env plan MyLib.Facts
qatlib-env run -- lake build MyLib.Facts
```

During a build, Lake compiles the target module and its upstream dependencies in import order. When upstream dependency artifacts are already in place with matching hashes, you can quickly check an individual source file with `qatlib-env run -- lake --no-build lean <File.lean>`; if any dependency still needs to be built, the command fails immediately with an error.

In Lean v4.35 source files with the `module` system enabled, declarations inside a module are private by default: a declaration must sit inside a `public section` or carry an explicit `public` modifier before downstream modules can reference its name, and `public import` is required whenever an imported module's public interface should be re-exported further downstream. If downstream consumers also need to unfold an ordinary definition body in proofs or computation—beyond merely referencing its name—verify that the declaration is annotated with `@[expose]` and write test cases that genuinely rely on unfolding it. Note that ordinary files without `module` enabled can still read private module internals; testing exclusively from non-`module` files often fails to surface cases where true `module` consumers cannot resolve a declaration name or unfold its definition body.

During day-to-day development and dependency fetching, three classes of precompiled artifacts are stored and reused through distinct mechanisms:

1. **Lake local shared artifacts**: When Lake in Lean v4.35 is invoked directly without explicit cache options, it reads from the shared cache in read-only mode by default, yet still restores artifacts into the current package's build directory on a module hit. For concurrent development across multiple worktrees, we recommend setting `enableArtifactCache = true` and `restoreAllArtifacts = false`: on a hit against native artifacts in the shared directory, Lake resolves the content hash from its input-tracking metadata and reads `.olean` files directly in place under `LAKE_CACHE_DIR`, without requiring filesystem reflink support or cross-worktree hard links. Meanwhile, each worktree retains its own independent `.lake` directory, and `.ilean` index files used by the editor continue to be written locally as usual.
2. **R2 remote artifacts**: For dependency packages configured with an R2 remote cache, `qatlib-env prepare` first prepares the source code pinned by the dependency lock, downloads and verifies any precompiled artifacts missing locally, and registers their mappings in the local Lake cache. Because the hard-link operations required when publishing individual artifacts take place entirely within the cache directory, the shared cache and worktrees can reside on different drive letters on Windows.
3. **Dedicated Mathlib cache**: Under the standard dependency layout, Mathlib's `lake exe cache get` unpacks precompiled archives directly into `.lake/packages/mathlib/.lake/build/` inside the current worktree.

For cache field defaults, configuration precedence between the root package and dependencies, and detailed behavior when restoring or downloading artifacts across drives, see [Lake Cache Flags](docs/en/build.md#cache-flags) and [Windows Cross-Drive Storage and Path Length](docs/en/build.md#windows).

Use `qatlib-env workspace scan <directory> [other directories ...]` to find worktrees. Add `--inspect-ignored` to inspect Git-ignored files.

To reclaim disk space occupied by idle worktrees, run `qatlib-env workspace clean-caches --checkout <path> --derived-only --dry-run` to preview the `.lake/build` and `.lake/packages` directories. To retire entire worktrees, preview with `qatlib-env workspace trash-checkouts --checkout <path>` and add `--apply` after verification to move them to the trash. The tool also prunes stale registrations from the parent Git repository by default. See [Worktrees and Artifact Storage](docs/en/build.md#cache-layout) for details.

<a id="retrieval"></a>

## Lean Documentation and Retrieval

Running `qatlib-retrieve index` builds a local static lexical index, written by default to `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`, where `<corpus-key>` is the first 24 hexadecimal characters of the SHA-256 digest of the project root's resolved absolute path. If you select a custom database path via `--db`, the target file must reside outside all indexed source repositories. Once the index is ready, query commands locate matching declarations along with `variable` declarations from enclosing `section` blocks directly, without launching the Lean compiler or Lake:

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization' --kind theorem
qatlib-retrieve query --cite GilyenSuLowWiebe2019
```

When documenting source files, use module docstrings `/-! ... -/` to state the overall responsibility of a module, and place `/-- ... -/` docstrings immediately above declarations to record mathematical semantics, preconditions, and typical usage. Wrap inline cross-declaration references in backticks using fully qualified names, and write bibliography citations as `[CiteKey]` matching entries in `docs/references.bib`. Downstream development repositories need only maintain newly added bibliography entries locally; during bibliography validation, the tooling automatically combines them with the locked public baseline to check uniformly for missing keys, duplicate keys, and conflicting entries under the same key.

For documentation rendering, `doc-gen4` builds full-site documentation with a global symbol table, page routing, and a bibliography index, automatically turning backticked declaration names and citation keys into cross-page links. By contrast, the editor's Lean LSP hover renders only the current declaration's own docstring fragment without site-wide routing or a global bibliography index. To make reference papers directly clickable from editor hover popups as well, include standard Markdown links pointing to the DOI or arXiv URL in the comment. See [Lean Documentation, Bibliography, and Source Retrieval](docs/en/lean-docs.md) for details.

<a id="forge"></a>

## GitHub and Gitea

For everyday PR viewing, creation, and commenting, use `gh`, `tea`, or `gt` directly. When you need to aggregate check results across multiple APIs or execute guarded write operations verified against an expected commit, use the following commands:

<a id="cli-reference"></a>

| Command | Purpose |
| --- | --- |
| `qatlib-forge pr checks` | Aggregate relevant checks and actual job states for the same PR head |
| `qatlib-forge pr paths` | Verify build-path matches and conditions for reusing existing CI results |
| `qatlib-forge pr activity` | Filter out recognized bot activity to inspect long-inactive candidate PRs |
| `qatlib-forge pr merge` | Merge a PR with expected-head verification |
| `qatlib-forge branch delete` | Safely delete a branch after verifying its remote SHA and branch protection rules |

GitHub and Gitea represent draft PRs and expose status endpoints differently: GitHub provides a dedicated `isDraft` field and exposes check data separately across check-runs, commit statuses, and Actions job endpoints, whereas Gitea derives draft status from instance-configured title prefixes (such as `WIP:` or `[WIP]`). On a draft PR, the Build job may be conditionally skipped while the outer commit-status summary still reports `pending`. Inspecting PR status therefore requires drilling down into workflow runs and individual jobs for the exact same PR head SHA to distinguish conditional skips (`skipped`), jobs that have not yet run, and genuine passes.

When multiple contributors and agents submit drafts concurrently, a full-repository build often takes tens of minutes, whereas static documentation checks typically take about a minute. If a PR touches only documentation outside the configured build paths, repository rules allow running static checks alone. On top of that, as long as the tested PR head has not changed and any commits added to the mainline in the meantime are likewise documentation changes that do not match the build rules and touch paths disjoint from the current PR, passing job results already verified on that head can be reused directly without a redundant rebuild. Before reusing prior results, however, path rules, the tested baseline, and existing job evidence must still be verified item by item.

New pushes can still arrive on a remote branch between the completion of human review and the actual merge or branch deletion. To guard against this, `qatlib-forge pr merge` passes the expected head commit to the server for verification, while `qatlib-forge branch delete` first checks whether the target is the default branch or matches a protection rule and then relies on Git's `--force-with-lease` to require that the remote ref still points to the expected SHA, preventing accidental deletion of newly pushed commits. See [Pull Requests on GitHub and Gitea](docs/en/forge.md) for details.

<a id="promotion"></a>

## Promoting Shared Results to the Public Library

Formalized results shared across repositories advance through a three-tier pipeline: reusable results are first organized in an incubation repository, then staged in a staging repository to verify whole-file dependency closure and compatibility with real consumers, and finally published by the public library with stable interfaces and precompiled artifacts. Once staging validation passes, downstream repositories can open draft PRs ahead of time to rehearse the interface transition; once the public library's official release and remote artifacts are ready, that same PR updates the dependency lock while removing superseded local source files and incremental bibliography entries now absorbed into the public baseline. Repository-specific directory layouts, review workflows, and acceptance criteria are defined by each repository's own promotion guidelines. See [Promotion Steps and Temporary Alias Checks](docs/en/promotion.md) for details.

<a id="field-notes"></a><a id="runner"></a>

## Build Troubleshooting and Runners

Under Linux cgroup v2, `memory.current` counts both anonymous process memory and the filesystem page cache (`file`). After heavy reads and writes of compiled artifacts finish, unreclaimed page cache can continue occupying the cgroup's accounted memory budget even after the compiler processes themselves have exited. When diagnosing build-pool admission waits, inspect `memory.current`, the detailed breakdown in `memory.stat`, and the memory limits across each level of the cgroup hierarchy together.

When a persistent CI runner cancels a single job, it typically terminates only that job's process rather than stopping the entire runner service, so service-level `KillMode=control-group` is not triggered. On Linux, the tooling uses `PR_SET_PDEATHSIG` so the guard process promptly receives a termination signal if its parent exits, and `PR_SET_CHILD_SUBREAPER` to adopt orphaned descendant processes so they can be reaped cleanly; in environments with a working systemd backend, heavy tasks can also be placed in a dedicated transient scope so lingering processes are swept across the entire cgroup upon cancellation.

When configuring concurrency, `jobs` sets the number of parallel build processes and `lean_threads` sets the thread count within each process. The build pool budgets total requested CPU quota as the product of the two—for example, 4 processes opening 4 threads each request a CPU quota of 16. Multiple concurrent worktrees on the same host should share a single build-pool ledger for coordinated rate limiting, while keeping each worktree's `.lake` directory independent. See [CI Runners and Process Troubleshooting](docs/en/runner.md) for details.

<a id="self-test"></a>

## Tooling Self-Tests and Offline Manual Verification

When maintaining this tooling repository, run `uv lock --check`, `uv build`, and `uv run --with pytest --with hatchling --with jsonschema pytest -q -rs` sequentially with Python 3.12 to run the exact same build and full test suite locally as in GitHub CI.

If `uv` is not installed in the current environment, you can create a fresh virtual environment, build distribution packages with `build` before running the self-tests, and leave installation tests that invoke `uv` directly for verification in an environment with `uv`. Always complete the build before running the test suite so that the wheel package under `dist/` read by the tests comes from the current working tree:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e . build pytest hatchling jsonschema
.venv/bin/python -m build
.venv/bin/python -m pytest -q -rs --ignore=tests/migrated_dev/test_migrated_dev_smoke_packages.py
```

The built wheel packages the repository root `README.md` byte-for-byte as `qatlib_env/_manual.md` and bundles `docs/` into `qatlib_env/docs/`. Running `qatlib-env docs --list` lists only the Markdown topics directly under `docs/`, which are selected for reading using the filename without its `.md` suffix. After installing the current wheel via `pipx` or a clean `venv` and confirming that the invoked command resolves to the freshly installed environment, run `qatlib-env docs | cmp README.md -` and `qatlib-env docs build | cmp docs/build.md -` from the repository root to verify byte-for-byte parity between the offline manual, topic guides, and their source files.

This project is licensed under the [Apache License, Version 2.0](./LICENSE).
