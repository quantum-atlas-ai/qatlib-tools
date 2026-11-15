# Runner contract

`setup_runner.sh` provisions a runner from explicit environment and repository configuration. Lane names, service URLs, cache locations, known hosts, accounts and resource budgets are inputs; no repository identity is embedded in the shared package.

Runner maintenance uses the installed `qatlib-env` command and preserves process-tree cleanup, cache locks and workspace-local configuration.
