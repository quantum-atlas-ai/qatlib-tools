# 共享工具仓维护约定

从 [README.md](README.md) 选择专题，从 [CONTRIBUTING.md](CONTRIBUTING.md) 查阅验证命令。修改公共接口前核对下游锁定版本和实际调用。

<!-- human-contract:start -->
## 人工审阅契约

根 README 面向使用者编写，用清楚的标题和自然语言解释用途、安装和工作方式。面向智能体的协作与审批规则放在本文件。

根 `README.md` 和 `AGENTS.md` 各有一个由独立整行 `<!-- human-contract:start -->` 与 `<!-- human-contract:end -->` 包围的受保护区块。标记、区块内容及 [摘要清单](docs/design/human-contracts.sha256) 受本契约保护。区块之外的技术文档按常规流程维护。

涉及区块、标记或清单变更时，以 GitHub Draft 或 Gitea `WIP:` 草稿发起 PR，提交当前候选区块全文供人类维护者审阅。获批后才更新摘要清单、转为就绪，再按仓库检查与授权流程合入。区块再改时重新提交候选全文。
<!-- human-contract:end -->

`python3 scripts/check_human_contracts.py --draft` 校验标记和清单格式并报告待审摘要。去掉 `--draft` 会要求摘要与当前区块一致。摘要只用于校验批准版本，维护者依据审阅记录确认批准人和候选版本。
