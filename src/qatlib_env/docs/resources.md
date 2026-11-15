# Resource contract

Resource resolution uses the same validated pipeline as routing and budget:
CLI > environment > worktree-local override (primary-checkout fallback) > shared
project TOML > existing user/runner compatibility layers > adaptive host limits.
The nested Python distribution layout is supported when root configs are absent.
Linux cgroup v1/v2 memory, CPU quota and affinity are inspected without changing
the host. No user configuration file is created.

The local pool ledger accounts for memory and CPU leases. The compiler wrapper always applies an explicit Lean job count and a 2048 MiB minimum runtime memory limit.

Explicit pool limits are hard: a task cannot enlarge them. CPU requests account
for jobs times per-process threads. Full gates, source identity uncertainty and
plans above the default 15-module thresholds are denied locally unless the
current invocation supplies `--allow-local-heavy`. That flag cannot bypass pool
limits, controllers or timeouts. Heavy work requires a hard CPU/memory controller;
unavailable controls fail closed. Cloud queue/failure never triggers a local run.
