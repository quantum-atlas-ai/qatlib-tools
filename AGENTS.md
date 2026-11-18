# 共享工具仓维护约定

先读 [README.md](README.md) 和 [CONTRIBUTING.md](CONTRIBUTING.md)。本仓维护共享文档和可跨仓复用的工程能力。基础操作优先使用官方 CLI 或单次 API。跨仓重复出现的平台差异、证据汇总和需要原子前置条件的操作可由共享工具处理。各仓自身的业务规则留在业务仓。

触及[人工审阅保护区域](CONTRIBUTING.md#human-review-protected-paths)时先开 Draft PR。人类维护者必须批准当前候选全文，方可转为 Ready 并继续走合入流程。
