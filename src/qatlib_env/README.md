# qatlib_env

This package contains the shared Lean workspace control plane. It is distributed as part of `qatlib-tools` and is used through `qatlib-env` or the importable `qatlib_env` package.

## Commands

`qatlib-env` supports workspace inspection and execution (`env`, `shell`, `bootstrap`, `prepare`, `config explain`, `cache explain`, `cache upload`, `docs`, `run`, `clean-setup`, `gc`, `maintain-mirror`, `worktree`, and `check`). The same control plane exposes `pool status`, `pool prune`, `plan`, and `cloud submit/status/logs/wait/fetch`. CI orchestration is available as `qatlib-env ci lint`, `qatlib-env ci build`, `qatlib-env ci build-gate`, and `python -m qatlib_env.ci`.

All workspace roots, package targets, cache services, Forge endpoints, and runner values are read from `qatlib-env.toml`, `[tool.qatlib-env]`, `.qatlib-env.local.toml`, environment variables, or explicit CLI flags. The shared package contains no consumer-repository profile.

## Local pool and cloud loop

Lake builds acquire a machine-wide lease with a heartbeat and stale-process reaping. Linux uses a delegated user cgroup scope when available; macOS and Windows use their native process controls where available and retain ledger admission plus supervisor cleanup as the portable fallback. The compiler overlay supplies explicit Lean job and memory limits without changing Lake dependency identity.

Cloud requests bind the exact workflow and head revision. Dispatch correlation handles APIs that return no body, logs are read with bounded HTTP ranges, and verification bundles are checked before import.
