## v0.3.0 (2026-11-25)

- Add `qatlib-env install-toolchain` to install the Lean release pinned by the project through official Elan or from a local archive verified against its SHA-256 digest.
- Add `--gh-proxy` and `--no-gh-proxy` for locked public Lean Git dependencies. The optional mirror fetches pinned commits anonymously and leaves private Git, API, and artifact-cache traffic on their configured routes.
- Add `qatlib-env check --server FILE` for repeatable single-file JSON diagnostics from the existing Unix Lean server. The default `check` continues to run the full project gate.
- Normalize CRLF for local checkpoint, preparation, server, and retrieval identities. Read workspace TOML as UTF-8 and improve matching of upstream declarations during source conflict checks.
- Refresh the bilingual installation, worktree, build, and verification guides, including Windows cross-volume cache behavior.
- Refresh the handbook's branding, language switcher, navigation, and Chinese search.
- Document cross-repository R2 caching and Actions fallback diagnosis in both languages, with native Lake and managed CI examples.
- Add `qatlib-env cache inspect` to compute the committed input-tree hash and optionally probe both public Lake manifest aliases.
- Add `qatlib-env doctor` to report worktree cache readiness, incomplete Lake artifacts, and hardlink or copy behavior in text or JSON.
- Report cache recovery, no-build verification, fallback, and audit timings in `qatlib-env ci build`. Use the configured upload service for authenticated publication when reads and writes use separate endpoints.
- Include Actions run attempts and per-step durations in `qatlib-forge pr checks`.
- Allow CI to select a self-hosted test runner through repository configuration. Move snapshot export out of the Tools checkout.

## v0.2.0 (2026-11-23)

- Add `qatlib-env server-check` for single-file Lean diagnostics and `qatlib-env server status` and `stop` for daemon management.
- Wait for the current Lean document version to finish processing before returning diagnostics. Recover stale sockets and shut down the Lean worker cleanly.
- Add `qatlib-lean-tools retrieve` for direct Lean source lookup by qualified name or module, with written signatures, docstrings, and JSON output.
- Report the initial toolchain and worktree cache state and per-phase timings through `qatlib-lean-tools env --json`, `env prepare --json`, and `env run --json`.
- Allow Lake to validate shared artifacts during `lake --no-build lean` checks, including in fresh linked worktrees, while retaining lock and resource checks.

v0.1.0 (2026-11-21)
-------------------

- Establish the version baseline for the shared Lean environment, forge, and source retrieval tools.
