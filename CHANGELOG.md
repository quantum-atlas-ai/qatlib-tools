## v0.2.0 (2026-11-23)

- Add `qatlib-env server-check` for single-file Lean diagnostics and `qatlib-env server status` and `stop` for daemon management.
- Wait for the current Lean document version to finish processing before returning diagnostics. Recover stale sockets and shut down the Lean worker cleanly.
- Add `qatlib-lean-tools retrieve` for direct Lean source lookup by qualified name or module, with written signatures, docstrings, and JSON output.
- Report the initial toolchain and worktree cache state and per-phase timings through `qatlib-lean-tools env --json`, `env prepare --json`, and `env run --json`.
- Allow Lake to validate shared artifacts during `lake --no-build lean` checks, including in fresh linked worktrees, while retaining lock and resource checks.

v0.1.0 (2026-11-21)
-------------------

- Establish the version baseline for the shared Lean environment, forge, and source retrieval tools.
