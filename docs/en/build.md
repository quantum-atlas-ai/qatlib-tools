# Build Environment, Caching, and Resource Control

For post-build axiom allowlists, zero-`sorry` gates, downstream interface checks, and kernel replay, see [Lean proof verification and axiom auditing](verification.md).

This page serves as a topic-by-topic reference manual. For a hands-on walkthrough covering build planning, dependency preparation, module compilation, single-file checking, and cleanup previews in a second worktree, start with [Open a second worktree in a Lean repository](worktrees.md). Expected timing for an initial Lean toolchain download or dependency build is covered at the end of the [five-minute check](quickstart.md), separately from installing the CLI itself.

For shared build artifacts and documentation snapshots, see [Cross-repository R2 caching](r2-cache.md). To identify where time goes during a restore or rebuild, see [Actions stage timing and cache fallback](actions-timing.md).

<a id="lakefile"></a>

## How to Read `lakefile.toml`

In the Lake build system, top-level fields in `lakefile.toml` declare package-wide metadata and default behaviors, while `[[lean_lib]]` entries define individual library build targets and `[[require]]` entries declare external dependencies. Note that Lake keeps the package name, library target name, and Lean module namespace independent of one another—they do not need to match.

In the typical configuration below, all Lean source files live under `src/`, so the source path `src/MyLib/Facts.lean` maps directly to the Lean module path `MyLib.Facts`:

```toml
name = "ExampleProject"
version = "0.1.0"
defaultTargets = ["MyLib"]
srcDir = "src"
enableArtifactCache = true
restoreAllArtifacts = false

[[lean_lib]]
name = "MyLib"
roots = ["MyLib"]
globs = ["MyLib.*"]
```

| Field | Default and Core Purpose |
| --- | --- |
| Package `name`, `version` | `name` must be declared explicitly in TOML configurations; `version` defaults to `0.0.0` when omitted. Note that the Lean compiler toolchain version is pinned independently by `lean-toolchain` in the project root and is unaffected by this field. |
| Package `defaultTargets` | The default target list Lake builds when running `lake build` without explicit target arguments; defaults to `[]` when omitted in TOML. Every entry must match an actual declared library or other buildable target. |
| Package and library `srcDir` | Both default to `"."`. The package-level `srcDir` is resolved relative to the package root, whereas the library-level `srcDir` is resolved relative to the package-level `srcDir`. Lake combines the two to locate module source files. |
| Package and library `moreLeanArgs` | Both default to `[]`. During a build, package-level arguments are passed to the Lean compiler first, followed by library-level arguments. Because these flags are hashed in full into the build trace, changing them can invalidate existing build artifacts. |
| Package and library `weakLeanArgs` | Both default to `[]`. They follow the same package-then-library ordering, but are placed before regular Lean arguments when invoking the compiler. Unlike regular arguments, weak arguments are excluded from the build trace hash, making them suitable only for auxiliary options that never alter artifact semantics or verification outcomes. |
| Library `name`, `roots` | `name` uniquely identifies the library target; when `roots` is omitted, `name` also serves as the default root module. `roots` explicitly declares the list of root modules governed by the library—all declared root modules and their submodules belong to this library. |
| Library `globs` | Glob rules specifying which modules to enumerate actively when building the library. When omitted, Lake loads only the `roots` themselves and recursively builds the modules they actually import. Setting `"MyLib.*"` enumerates both the root module and all of its submodules, whereas `"MyLib.+"` enumerates only its submodules. Without an umbrella root module that aggregates imports across the library, Lake does not scan the full directory tree by default. |
| `[[require]]` and `lake-manifest.json` | Declares external dependency names and their sources (Git repositories, local file paths, or package registries); Git dependencies accept `rev` to target a specific branch, tag, or commit hash. Once dependency resolution completes, Lake locks the resolved sources and exact checked-out revisions of all direct and transitive dependencies in `lake-manifest.json`. Committing this manifest to version control is essential for consistent team builds; to upgrade a specific dependency, run `lake update <package>` and carefully review the resulting manifest diff. |

[Lake v4.35 Reference](https://lean-lang.org/doc/reference/4.35.0/Build-Tools-and-Distribution/Lake/) · [TOML Decoder](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Load/Toml.lean) · [Library and Argument Configuration](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/LeanLib.lean)

<a id="modules"></a>

## Module Public Interfaces

In the Lean v4.35 module system, source files declared with `module` may only import files that also adopt the `module` convention. Refactoring or migrating a project should therefore proceed bottom-up: modularize low-level foundational dependencies first, then advance upward through higher-level consumer modules.

Inside a `module`, all symbol and theorem declarations default to `private`. To expose names to downstream modules, mark individual declarations with `public` or place a group of declarations inside a `public section` block. When downstream modules need to inherit an imported module's public interface transitively, re-export it with `public import`. Whenever downstream code needs to unfold the definition of a specific constant or theorem, verify that the defining module marks it with `@[expose]`, and validate this thoroughly with concrete examples that rely on unfolding. Pay special attention to unit tests: ordinary non-`module` files are permitted to read private declarations from `module` files directly, so writing unit tests in non-`module` files can easily mask visibility failures that genuine downstream `module` consumers would hit at compile time.

[Lean v4.35 Modules Reference](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/)

<a id="incremental-build"></a>

## Incremental Builds and Single-File Checking

When you run `lake build <Module>`, Lake builds the target module and its entire dependency chain bottom-up, strictly guided by the source import graph and input trace hashes. Any build artifacts already present locally or in the cache that strictly match the current inputs are reused directly, enabling fast incremental compilation. In day-to-day engineering, however, casually importing top-level umbrella modules often forces a single-file syntax or type check to pull in and build nearly the entire library. Keep imports limited to the leaf modules actually needed; before starting a build, run `qatlib-env plan <Module>` to inspect the number of cold modules, affected reverse-dependency chains, and dependency lock status so you can select an appropriate build scope.

When you only need to quickly check a single file under active editing, prefer `lake --no-build lean <File.lean>`. This command instructs Lake to assemble the runtime import context according to the project's dependency graph and environment configuration, and then type-check the target file directly against existing dependency artifacts. If any artifact in the dependency chain has not yet been built, the command exits immediately with an error rather than implicitly triggering an expensive cascading build. When that happens, selectively build the missing prerequisites via `qatlib-env run -- lake build <Module>`, and then run `qatlib-env run -- lake --no-build lean <File.lean>` to check the file quickly. Furthermore, when shared caching is enabled, never invoke the bare `lean` binary directly or rely solely on walking the local `.lake/build` directory to check for outputs—both practices miss artifacts managed by Lake in the shared global cache.

[Lake v4.35 CLI Implementation](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/CLI/Main.lean)

### Reuse a Lean server while editing

On Unix, prepare the worktree and build the file's prerequisites first. Run
`qatlib-env check --server File.lean` after each saved edit. It returns JSON
diagnostics and uses the same worktree-local server as `qatlib-env server-check`.
The daemon holds a shared-pool lease while idle. Use `qatlib-env server status`
to inspect it and `qatlib-env server stop` to release its lease immediately.
Use the usual `qatlib-env check` for the import, lint, build, and kernel audit
gate. On Windows, use the single-file Lake command above.

<a id="cache-flags"></a>

## Cache Read/Write and Restoration Flags

The two core flags governing artifact cache behavior—`enableArtifactCache` (corresponding to `LAKE_ARTIFACT_CACHE`) and `restoreAllArtifacts` (corresponding to `LAKE_RESTORE_ARTIFACTS`)—are defined exclusively at the package level; individual `[[lean_lib]]` targets do not repeat these fields. During a build, Lake computes the effective value of each flag independently for **every package** participating in the build: it reads the package's own explicit configuration first, falling back to the workspace default if unset; the workspace default checks the corresponding environment variable first and finally falls back to the root package's field. The two resolution chains are independent and evaluated in strict order.

| Package Being Built | Lookup Precedence (First Valid Setting Wins) |
| --- | --- |
| Root Package | Root package explicit setting → Corresponding environment variable → Lake default behavior |
| Dependency Package | Dependency explicit setting → Corresponding environment variable → Root package setting → Lake default behavior |

When parsing environment variables, Lake recognizes `true`, `false`, `1`, and `0` as boolean values; empty values or unrecognized strings are treated as unset. Note an important subtlety: if the root package explicitly declares `restoreAllArtifacts = false`, exporting `LAKE_RESTORE_ARTIFACTS=1` in the environment will not override the root package's own setting, yet that environment variable still serves as the workspace default—leaking into and affecting any dependency packages that do not explicitly configure the flag. Conversely, any explicit setting declared by a dependency package itself always takes precedence over both environment variables and the root package configuration.

Once resolved through this two-level hierarchy, the two flags yield nine behavioral combinations. Assuming a module target that supports caching by default, no reusable artifacts in the local worktree, and a hit in the shared cache, Lake operates as follows:

| Resolved `enableArtifactCache` | `restoreAllArtifacts` Unset | `restoreAllArtifacts = false` | `restoreAllArtifacts = true` |
| --- | --- | --- | --- |
| **Unset** (cache readable, not writable) | Reads from the shared cache and restores the full set of local artifacts | Same as left (read-only path still restores all artifacts locally) | Same as left; restores the full set of local artifacts |
| **`true`** (cache readable and writable) | Uses large files such as `.olean` directly from the shared cache; restores only lightweight artifacts required on disk such as `.ilean` | Same as left (recommended mode; saves substantial disk space) | Restores the full set of artifacts to the local worktree regardless of file size |
| **`false`** (cache neither readable nor writable) | Falls back completely to worktree-local compilation and hash tracking | Same as left (local build and trace tracking) | Same as left (`restoreAllArtifacts` does not re-enable a disabled shared cache) |

In writable mode (`enableArtifactCache = true`), a cold build (cache miss) still compiles the target locally first and then automatically writes and registers the generated artifacts into the content-addressed shared cache pool. Disabling full restoration (`restoreAllArtifacts = false`) primarily governs routine reuse on subsequent cache hits; it does not mean every build output can stay off the local disk—targets that editor integration relies on locally (such as `.ilean`) are still fetched into the worktree on demand. In addition, if a remote hit provides only a compressed `.ltar` archive without unpacked, directly consumable artifacts, Lake unpacks it in the current worktree before continuing; likewise, specific commands that repackage or publish artifacts may explicitly trigger local restoration.

For cache storage paths, bare Lake invocations rely on `LAKE_CACHE_DIR` to determine the storage directory: whenever set to a non-empty path, it takes precedence over auto-detected toolchain caches or system default caches; passing an explicit empty string disables system-level cache discovery and falls back to the workspace-local `.lake/cache` directory. In day-to-day workflows, `qatlib-env prepare` and `qatlib-env run` automatically compute and export a canonical non-empty cache directory and inject `LAKE_ARTIFACT_CACHE=true` as the workspace default, while deliberately leaving `LAKE_RESTORE_ARTIFACTS` unset so that explicit package-level configurations retain final authority. On hosts where multiple worktrees are developed in parallel, we strongly recommend setting `enableArtifactCache = true` and `restoreAllArtifacts = false` explicitly in the root package configuration, while also auditing the explicit configurations of all dependencies and the active user environment—if even one dependency remains in read-only cache mode or enables full restoration, its artifacts will still be materialized in full across every worktree, causing unnecessary disk overhead.

[Package Configuration](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/PackageConfig.lean) · [Resolution Order](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/Monad.lean) · [Workspace and Cache Location](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/Workspace.lean) · [Module Artifact Read/Write](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Module.lean)

<a id="cache-layout"></a>
## Three Cache Types and Worktrees

| Cache Source | Core Data Flow and Storage Topology |
| --- | --- |
| Local Lake artifact cache | Lake first indexes its artifact mapping table by build input hash, then locates bulky binary outputs such as `.olean` and IR files inside the shared global cache directory by content hash. Under the "read/write enabled, full restore disabled" mode described earlier, cache hits let Lake consume these large files directly without copying them into every worktree. This content-addressed design is highly portable and relies neither on filesystem reflinks nor on cross-volume hard links. |
| R2 cloud artifacts via `qatlib-env prepare` | Each worktree checks out its own dependency sources strictly according to its manifest file. For packages configured with remote R2 cache storage, `prepare` probes candidates in order—**exact commit hash → compatible same-input-hash mapping → topological ancestor candidates**—and treats **only** an HTTP `404` response from the server as a confirmed artifact miss. After verifying the integrity of both existing and newly downloaded artifacts, `prepare` unpacks and installs them into `LAKE_CACHE_DIR/artifacts` and runs `lake cache add` to register them in the Lake cache index. In addition, a matching local host checkpoint serves as an independent fast-path local recovery source. |
| Mathlib ecosystem cache | Downloads and unpacks precompiled Mathlib archives maintained by the Mathlib project via `lake exe cache get`. Under the standard Lake dependency layout, unpacked build outputs are placed in the current worktree's `.lake/packages/mathlib/.lake/build/` directory. Note that the compressed archive files and the unpacked outputs occupy separate disk space. |

When working across multiple worktrees, configuring local Git mirrors substantially reduces network overhead from repeatedly fetching dependency sources. Although each worktree must maintain its own independent dependency checkouts and `.lake` directory to isolate development state, all worktrees can safely share a single underlying global artifact cache.

When reclaiming disk space, proceed from lightweight derived artifacts to full worktrees:
- **Reclaim derived build data only**: Run `qatlib-env workspace clean-caches --checkout <path> --derived-only --dry-run` to preview the `.lake/build` and `.lake/packages` entries slated for removal; once verified, replace `--dry-run` with `--apply` to execute the cleanup.
- **Reclaim an entire linked worktree**: Before retiring an obsolete linked worktree, run `trash-checkouts` to preview the operation safely. Proceed with removal only after confirming that the worktree holds no uncommitted unique files, is not checked out by another active workflow or branch, and has no background processes writing to it. Upon completion, the tool automatically prunes stale Git worktree registrations by default while preserving existing branch references intact. In the resulting report, file-move operations and actual reclaimed disk space are accounted for separately.

<a id="github-acceleration"></a>

## Public GitHub Acceleration

Use the [optional exact public Lean Git mirror](install.md#optional-public-lean-git-mirror) when the original GitHub address is unavailable.

<a id="windows"></a>

## Windows Cross-Volume Setups and Long Paths

In typical Windows deployments, developers often place the cache directory on the system drive (for example, `C:\lake-cache`) while keeping active worktrees on a separate data drive (for example, `D:\work\project`). Because NTFS restricts hard links to a single volume, attempting to create a cross-volume hard link fails with an error analogous to POSIX `EXDEV` (the exact Windows error code and message vary by calling layer). Placing the cache and worktrees on different drives is nevertheless a valid, fully supported configuration—the key is understanding how each build stage behaves across volume boundaries:

| Stage | Cross-Volume Behavior and Handling |
| --- | --- |
| Lake reading shared artifacts directly | Lake reads target artifact files directly from the cache volume without creating cross-volume hard links, so splitting the cache and worktrees across drives has no adverse impact. |
| Lake materializing artifacts or writing back to cache | When restoring missing `.ilean` files or performing a full restore, Lake tries to create a hard link first and transparently falls back to copying the full file into the worktree if cross-volume linking fails. Likewise, when registering newly compiled outputs into the shared cache, Lake writes the artifact byte stream directly into the cache whenever a hard link cannot be established. Note that if full restore mode is enabled, this copy fallback consumes real disk space on the worktree volume. |
| Installing R2 cloud artifacts via `prepare` | Although temporary state for downloading artifact hash mappings may live inside the worktree, binary artifact downloads and staging directories always reside inside `LAKE_CACHE_DIR`. Verified staged files must be published into `artifacts/` via atomic, non-overwriting hard links. **This publishing step has no cross-volume copy fallback**—a cross-volume hard-link error aborts the entire `prepare` run immediately. Never symlink or redirect `artifacts/` onto another volume; keep it on the same writable local NTFS volume that supports hard links. |

To avoid path-length issues caused by deeply nested directories on Windows, place workspaces and cache directories near drive roots under short base paths whenever possible. Keep the boundary between the two Windows long-path mechanisms distinct: setting `core.longpaths=true` in Git for Windows lifts path-length limits only for Git's own operations, whereas removing the legacy 260-character `MAX_PATH` limit across Win32 APIs requires both enabling `LongPathsEnabled` in the system registry and having each executable explicitly declare `longPathAware` in its application manifest. Long-path compatibility must therefore be verified separately across Lake, Python, external archive extraction utilities, and the system Recycle Bin. Note also that enabling long-path support does not lift NTFS's physical restriction against cross-volume hard links.

[Lake save/restore implementation](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Common.lean) · [Windows hard links](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions) · [Win32 path length limitation](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation) · [Git for Windows](https://gitforwindows.org/git-cannot-create-a-file-or-directory-with-a-long-path.html)

When cloning a repository with deeply nested paths for the first time, run `git clone --config core.longpaths=true <URL>` so the setting takes effect from the initial checkout and is inherited automatically by linked worktrees of that repository. Python environment creation has its own cross-volume path: if the `uv` cache lives on `C:` while a virtual environment lives on `D:`, `uv` falls back to copying files when cross-volume hard links fail. It may still print a hard-link fallback warning after a successful install, and those copied files consume disk space on the virtual environment's volume.

Keep the following line-ending, cache-reuse, and multi-worktree behaviors in mind on Windows:

- **Lake and Mathlib `CRLF → LF` line-ending normalization**: When diagnosing unexpected cache misses, run `git ls-files --eol` to inspect index line endings, worktree line endings, and `.gitattributes` rules. Even when `core.autocrlf=true` checks out LF index blobs as CRLF files in the worktree, Lean 4.35's Lake calls `crlfToLf` along `Module.recFetchInput → Hash.ofText`. The normalized source hash is one input to `depHash`. Mathlib's `Cache/Hashing.lean` likewise applies `crlfToLf` before computing its download cache keys. Consequently, whenever the Git revision and objects, target platform, dependency lock, and compiler flags match, LF and CRLF checkouts share the exact same R2 manifest and Lake module cache keys. Cache lookups rely on Git object hashes and native Lake keys rather than adding a separate raw-byte lock over checked-out working tree files. Note that the root repository's `.gitattributes` applies only to its own files—each Lake Git dependency checks out files under its own attributes. On Windows, when `qatlib-env` materializes locked Git dependencies through Lake, it sets `core.autocrlf=input` for that child process so newly checked-out dependencies preserve upstream stored line endings (while honoring explicit upstream attributes), and existing CRLF checkouts continue to compare normalized content when checking for local modifications.
- **Native cache reuse and cross-volume copy fallback**. In a native Lean `4.35.0` test on Windows, LF and CRLF checkouts of the same module produced identical `depHash` digests, and Lake reported `Reused` in the CRLF worktree. When restoring the `.ilean` from a `C:` shared cache into a `D:` worktree, Lake logged error `4037` for the hard-link attempt. It copied the file and exited successfully. For measured disk and memory figures, see [Resource usage and cross-volume layout](install.md#resource-footprint).
- **Concurrent `prepare` locking and avoiding manual `.lake` copies**: When multiple worktrees run `prepare` against the same shared cache on Windows, preparation is serialized through `.fetch.lock` in the cache directory, while ordinary incremental builds across separate worktrees continue to run concurrently. Avoid copying an entire `.lake` directory from an older worktree into a fresh checkout: file-copy utilities can preserve older output timestamps (for example, `Chebyshev.olean` and `.trace` in a lock-matched Mathlib checkout retaining modification times older than the newly checked-out `.lean` sources), causing `qatlib-env plan`'s timestamp heuristic to count those modules as cold even though native Lake validates inputs and artifacts by `depHash`. Instead, run `prepare` independently in each worktree to restore artifacts cleanly before scheduling builds from that worktree's admission plan.

[Lake 4.35 module inputs](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Module.lean) · [Lake 4.35 text hash](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Trace.lean) · [Mathlib 4.35 cache hashing](https://github.com/leanprover-community/mathlib4/blob/56a005855ea17e0b0958f73ec801ae140b0a1679/Cache/Hashing.lean)

On Windows, managed builds—as well as cache-preparation runs that invoke compilation—execute through native Lake while using a Windows Job Object to enforce hard CPU and memory limits across the entire process group. Because `qatlib-env` ships with a non-empty default list of heavy-module matching patterns and Windows lacks the dedicated POSIX compiler wrapper required to allocate isolated execution slots for matched heavy modules or inject per-invocation `-M` memory caps, managed Lake fails fast if it detects non-empty heavy-module rules on Windows. On Windows hosts, explicitly set `heavy_patterns = []` in an active configuration file and verify that the setting has taken effect via `qatlib-env config explain --json`. For details on Job Object process-tree initialization and lifecycle cleanup, see [Runner Process Isolation](runner.md#windows-job-object).

<a id="note-concurrency"></a>

## Multiple Worktrees and Concurrency Budgets

During concurrent builds, `qatlib-env` requests scheduling leases from the host's global resource pool based on the total core count (`jobs × lean_threads`) and the declared host memory budget. For example, two simultaneous build jobs each configured with 4 processes and 4 threads account for 32 CPU quota units in the system ledger. All concurrent worktrees on the same physical machine are governed by a single global ledger, whereas OS-level CPU quotas, hard memory limits, and timeout termination are enforced by each platform's native backend.

Before adjusting resource budgets for a job, run `qatlib-env plan` to evaluate the cold-build blast radius and the scale of affected reverse dependencies. Platform enforcement capabilities also differ: Linux pairs cgroups with a dedicated compiler wrapper for fine-grained heavy-module slot reservation and memory clamping, whereas macOS lacks an OS-level aggregate process-group hard memory limit equivalent to Linux cgroups or Windows Job Objects. Workloads requiring strict hard memory isolation should therefore be deployed on operating systems capable of enforcing rigid resource limits.

[Runner and Process Troubleshooting](runner.md)

<a id="configuration"></a>

## Configuration Precedence

With no explicit configuration present, `qatlib-env` computes the baseline host resource pool automatically:
- **Memory Pool**: Takes the smaller of "available host memory after subtracting the host system reserve" and "the strictest visible cgroup memory quota," rounded down to whole MiB. The host reserve applies specifically to the system-level physical memory metric and equals the larger of `2 GiB` or `10%` of total physical RAM.
- **CPU Pool**: Reads physical/logical core count, CPU affinity, system `cpuset` assignment, and cgroup CPU quotas, taking the intersection of cores permitted across all four constraints.

Under the default scheduling policy, a single build invocation typically receives roughly half of the system's available CPU and memory pool:
- **CPU budget allocation**: Capped at 8 cores per build; when capacity permits, at least 2 cores are always reserved for foreground host responsiveness.
- **Memory budget allocation**: Capped at 16 GiB per build; if the global memory pool has less than 4 GiB available, the budget is bounded strictly by actual detected capacity.
- **Thread and process sizing**: Each Lean compiler process is assigned at most 2 concurrent threads by default (automatically falling back to 1 thread on single-core constrained hosts), while parallel job count is further bounded by a capacity model estimating roughly 4 GiB of memory per compiler process. If available system resources fall below admission thresholds, `qatlib-env` blocks and refuses to schedule the run—free up host resources first or move the task to a machine with sufficient capacity. At any time, run `qatlib-env config explain --json` to emit a machine-readable configuration report showing unoverridden defaults, effective values, per-field provenance, and potential configuration conflict warnings.

The configuration layers below are ordered strictly **from lowest to highest precedence**; when the same key appears in multiple layers, higher-precedence layers unconditionally override lower ones. Use `qatlib-env config explain` at any point to verify the effective configuration and its source paths:

| Precedence (Low to High) | Configuration Layer | Scope and Resolution Rules |
| :---: | --- | --- |
| 1 (Lowest) | Defaults and project inference | Conservative defaults dynamically probed from host hardware and cgroup limits; dynamically parses primary library roots, test roots, and `srcDir` from the root `lakefile.toml`, and infers repository identity and workflow from workspace directory topology. Adheres to repository architecture constraints and maintenance boundaries; see `DESIGN.en.md` for core engineering design principles. |
| 2 | Runner compatibility config | External Runner instance configuration file explicitly pointed to by `QATLIB_RUNNER_CONFIG`. |
| 3 | User-level host settings | Reads `~/.qatlib/env.toml` first. When `QATLIB_HOME` is set, keys in its `env.toml` override matching keys from the default file. |
| 4 | Shared project policy | Reads `[tool.qatlib-env]` in the root `pyproject.toml` followed by the root `qatlib-env.toml`, which overrides matching keys. Reads `python/pyproject.toml` only when neither root file exists. |
| 5 | Worktree-local overrides | `.qatlib-env.local.toml` in an individual worktree root. When a secondary worktree does not define its own file, it inherits the primary checkout's `.qatlib-env.local.toml` read-only. |
| 6 | Environment variable overrides | Ephemeral per-host or CI environment variables such as `QATLIB_POOL_BUDGET_MIB`, `QATLIB_POOL_CPU`, and `QATLIB_JOBS`. For thread control, `QATLIB_LEAN_THREADS` takes precedence over native `LEAN_NUM_THREADS`. |
| 7 (Highest) | Per-invocation CLI flags | Ephemeral flags passed directly on the command line at runtime, such as `qatlib-env run --jobs 1 --lean-threads 2 -- lake build <module>`. |

When a project team wants to enforce uniform caps on local cold-compiled modules and reverse-dependency blast radius without coupling build policy to specific host hardware specs, commit only hardware-agnostic budget constraints to the repository (if maintained inside `pyproject.toml`, replace the table header with `[tool.qatlib-env.budget]`):

```toml
[budget]
max_cold_modules = 15
max_reverse_modules = 15
require_explicit_local_heavy = true
```

- **Threshold semantics and ad-hoc overrides**: Setting a module cap directly to `0` strictly forbids triggering local cold builds or reverse-dependency rebuilds of that category. During troubleshooting or ad-hoc debugging, override these caps immediately via `QATLIB_MAX_COLD_MODULES` and `QATLIB_MAX_REVERSE_MODULES`. If a configuration file contains misspelled unknown keys or mismatched field types, `qatlib-env` fails fast at startup and pinpoints both the offending file path and field path.
- **Heavy-build exemptions and queue timeouts**: The `--allow-local-heavy` CLI flag applies only to the current invocation and remains unconditionally bound by global shared-pool physical capacity, OS-level cgroup controllers, and hard lease timeouts. Meanwhile, `QATLIB_POOL_WAIT_TIMEOUT` adjusts only how long a queued request waits for active jobs to release leases; if a single build request demands more capacity than the shared pool's physical ceiling can ever satisfy, it is rejected immediately rather than queued in vain.

<a id="local-paths"></a>

## Local Data Directories

The `paths` block in `qatlib-env config explain --json` displays the resolved absolute paths for each local storage area; this inspection command is strictly read-only and creates no directories on disk. In the table below, the tool home directory denotes `${QATLIB_HOME:-~/.qatlib}`:

| Data Category | Default Location | Path Overrides and Compatibility Rules |
| --- | --- | --- |
| User configuration file | `~/.qatlib/env.toml` | Keys in `$QATLIB_HOME/env.toml` may override matching keys from the default file. |
| R2 credentials file | `${QATLIB_HOME:-~/.qatlib}/secrets` | `QATLIB_R2_KEY_FILE` takes highest precedence. If the tool home directory is relocated and no credentials file exists at the new path yet, `qatlib-env` automatically falls back to reading the legacy location for compatibility. |
| Build pool state and lease ledger | `${QATLIB_HOME:-~/.qatlib}/state/pool/ledger.json` and `ledger.lock` | When `XDG_STATE_HOME` or `XDG_CACHE_HOME` is explicitly set in the environment, `qatlib-env` continues using `qatlib-env/pool` under the respective XDG directory; if a ledger or lock file already exists in the historical default `~/.cache/qatlib-env/pool`, it continues reading and writing in place. |
| Retrieval index database | `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3` | The `--db` CLI flag takes highest precedence. During search queries, existing XDG cache files remain readable for backward compatibility; see [Local Index](lean-docs.md#lexical-index). |
| Cloud job receipts | `${QATLIB_HOME:-~/.qatlib}/state/cloud/<corpus-key>.json` | The `--state` CLI flag takes highest precedence. If `.qatlib-env/cloud-state.json` already exists in the checkout root, `qatlib-env` preferentially reuses and updates that receipt file in place. |
| Lake artifacts and local checkpoints | Lake artifact store directory and its `checkpoints/` subdirectory | `LAKE_CACHE_DIR` takes highest precedence; when unset, `qatlib-env` follows the built-in directory of the active `elan` toolchain or standalone Lake's XDG cache path while preserving historical cached data intact. |

All concurrent managed builds on the same host must share a single resource-pool ledger and lock file. Before archiving or relocating a ledger directory, drain all active builds in that pool and align tool versions and runtime environments across callers; as long as an existing ledger or lock file remains at its legacy path, `qatlib-env` continues reusing that pool in place.

To audit worktree directories and include Git-ignored local files without mutating disk state, run the read-only scan `qatlib-env workspace scan /path/to/current /path/to/archive --inspect-ignored` (pass `--exact` to restrict inspection strictly to the specified checkout roots). Each engineering repository continues to own the branch and working-tree state of its own worktrees.
