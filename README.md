# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**`qatlib-tools`** is a pure Python 3.11+ command-line toolkit and library for managing large-scale [Lean 4](https://lean-lang.org/) formalization workspaces, collaborative GitHub and Gitea workflows, and declaration/proof search indexes. Built entirely on the Python standard library with **zero third-party runtime dependencies**, it installs cleanly into isolated `uv` environments on Linux, macOS, and Windows.

---

## Overview

`qatlib-tools` provides three focused packages and matching CLI entry points:

| CLI | Python Package | Scope |
| :--- | :--- | :--- |
| `qatlib-env` | `qatlib_env` | Lean 4 multi-worktree isolation, shared dependency mirrors, S3/R2 artifact caches, OS-bounded local build pools, and cloud CI offloading |
| `qatlib-forge` | `qatlib_forge` | Unified GitHub and Gitea repository, pull request, issue, incremental CI log, and workflow artifact client |
| `qatlib-search` | `qatlib_search` | Lean 4 source and docstring parser, dependency-closure analyzer, and declaration/proof search indexing and retrieval |

---

## Installation

Requires **Python 3.11 or newer** and [`uv`](https://docs.astral.sh/uv/).

Install the CLI tools in an isolated tool environment:

```bash
uv tool install git+https://github.com/quantum-atlas-ai/qatlib-tools.git
```

Or add `qatlib-tools` to a project's `pyproject.toml`:

```bash
uv add git+https://github.com/quantum-atlas-ai/qatlib-tools.git
```

An optional `validation` extra (`qatlib-tools[validation]`) installs `jsonschema>=4` for full JSON Schema validation of retrieval evaluation reports, falling back to built-in structural validation when omitted.

---

## Components

### 1. `qatlib-env` (`qatlib_env`) — Workspaces, Caches, Resource Pools & Cloud CI

`qatlib-env` coordinates local Lean 4 development across concurrent Git worktrees while protecting host memory and CPU budgets:

- **Multi-worktree isolation & Lake caches**: Provisions isolated Git worktrees (`worktree`), maintains shared local Lake package mirrors (`maintain-mirror`), resolves S3/R2 SigV4 precompiled `.ltar` and revision manifests (`prepare`, `bootstrap`, `cache explain`, `cache upload`), and performs lock-safe artifact garbage collection (`clean-setup`, `gc`).
- **Cross-platform local build pool**: Admits builds through a lease-based machine ledger (`pool status`, `pool prune`) with background lease heartbeats and stale-process reaping. Resource limits are enforced via Linux `systemd --user` cgroup v2 scopes (`MemoryHigh`, `MemoryMax`, `CPUQuota`), Windows Job Objects, and macOS `taskpolicy` QoS, paired with a Lake-transparent compiler wrapper (`LEAN_SYSROOT` overlay + `LAKE_OVERRIDE_LEAN`) that injects explicit thread (`-j`) and memory (`-M`, minimum `2048 MiB`) caps while preserving Lake dependency hashes.
- **Cloud CI offloading**: Routes warm incremental checks locally and dispatches cold or full-gate builds to remote CI workflows (`plan`, `ci`, `cloud submit`, `cloud status`, `cloud wait`), streams incremental failure diagnostics over HTTP byte-range requests (`cloud logs`), and imports SHA-256–verified build bundles back into the local workspace (`cloud fetch`).

```bash
# Inspect resolved host budgets, toolchain identity, and pool leases
qatlib-env config explain
qatlib-env pool status

# Create an isolated worktree and materialize cached Lake artifacts
qatlib-env worktree ../feature-branch feat/new-lemma
qatlib-env --project ../feature-branch check MyLib.TargetModule

# Offload a cold or full-repository gate to CI and inspect diagnostics
qatlib-env cloud submit --head HEAD
qatlib-env cloud logs --job-id 123456789
```

### 2. `qatlib-forge` (`qatlib_forge`) — Unified GitHub & Gitea Automation

`qatlib-forge` exposes a consistent JSON-first CLI and Python client (`RepoAPI`, `RepoRef`) across **GitHub** and **Gitea** instances with host-scoped token resolution:

- **Repository & diff inspection**: Resolve `https://` and `ssh://` remotes (`repo`) and query changed-file comparisons (`compare`).
- **Pull requests & issues**: List, read, open, update, comment, add line-anchored review comments, and toggle draft/ready states (`pr`, `issue`), plus read/update structured task status blocks (`status`).
- **CI runs, byte-range logs & artifacts**: Query workflow runs (`runs list`, `runs read`), stream bounded log slices via HTTP `206 Partial Content` with structured Lean `file:line:col` diagnostic parsing (`logs --diagnostics`), and inspect or download SHA-256–checked workflow artifacts (`artifacts`).

```bash
# Inspect open pull requests and workflow runs on GitHub or Gitea
qatlib-forge pr list --repo quantum-atlas-ai/qatlib --state open
qatlib-forge runs list --repo quantum-atlas-ai/qatlib --branch main

# Read a bounded CI log window and extract structured Lean compiler diagnostics
qatlib-forge logs 123456789 --repo quantum-atlas-ai/qatlib --start 0 --end 131071 --diagnostics
```

### 3. `qatlib-search` (`qatlib_search`) — Lean 4 Source Analysis & Proof Search

`qatlib-search` provides lexical analysis, dependency-graph utilities, and search indexing over Lean 4 repositories:

- **Source & declaration parsing**: Strips nested `/- ... -/` block comments and string literals while preserving line numbers, parses standard and guillemet-quoted (`«...»`) module imports and headers (`lean_source`), and extracts public/private declarations and docstrings (`lean_declarations`, `lean_doc_source`).
- **Closure & duplicate analysis**: Computes transitive intra-package file closures (`promotion_closure`) and detects cross-package module or declaration collisions (`promotion`).
- **Search indexing & retrieval**: Builds, enriches, validates, and queries declaration and proof indexes from source trees and Lean environments (`build`, `query`, `ExportSearchIndex.lean`, `proof_index`, `unified_retrieval`, `query_library`, `find_symbol`, `check_goal`).

```bash
# Build a declaration search index from Lean source roots and query symbols
qatlib-search build Qatlib -o .cache/search-index.json
qatlib-search query .cache/search-index.json "gram" --kind theorem --limit 10
```

---

## Project Configuration

Repositories configure workspace roots, resource budgets, and CI workflows declaratively in `qatlib-env.toml` (or `[tool.qatlib-env]` in `pyproject.toml`), with optional machine-local overrides in `.qatlib-env.local.toml`:

```toml
source_roots = ["Qatlib"]
cache_dir = ".lake"
local_pool_mib = 24576
local_pool_cpu = 4
default_jobs = 2
default_memory_mib = 4096
cloud_platform = "github"
repository = "quantum-atlas-ai/qatlib"
workflow = "build.yml"
```

---

## Development & Testing

Set up an isolated virtual environment with `uv`, run the unit test suite, and build source/wheel distributions:

```bash
uv venv .venv
uv pip install -e .
uv run python -m unittest discover -s tests -v
uv build
```

---

## License

Licensed under the [Apache License, Version 2.0](./LICENSE) (`Apache-2.0`), aligned with [`quantum-atlas-ai/qatlib`](https://github.com/quantum-atlas-ai/qatlib).
