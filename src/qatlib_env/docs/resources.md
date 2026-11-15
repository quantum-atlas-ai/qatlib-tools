# Resource contract

Resource resolution combines CLI flags, environment variables, workspace TOML, user configuration, runner configuration and adaptive host limits. Linux cgroup v1/v2 memory, CPU quota and affinity are inspected without changing the host.

The local pool ledger accounts for memory and CPU leases. The compiler wrapper always applies an explicit Lean job count and a 2048 MiB minimum runtime memory limit.
