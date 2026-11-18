# 共享工具仓维护约定

从 [README.md](README.md) 选择专题，从 [CONTRIBUTING.md](CONTRIBUTING.md) 查阅验证命令。修改公共接口前核对下游锁定版本和实际调用。

<!-- human-contract:start -->
## 人工审阅契约

根 `README.md` 面向使用者，用清楚的标题和自然语言说明工具用途、安装方法和工作方式。面向智能体的协作及审批规则集中放在本文件。

根 `README.md` 和 `AGENTS.md` 各有一个受保护区块，起止标记分别为独立整行的 `<!-- human-contract:start -->` 和 `<!-- human-contract:end -->`。两处标记、区块正文和[摘要清单](docs/design/human-contracts.sha256)受本契约保护。区块之外的技术文档按常规流程维护。

改动受保护区块、标记或摘要清单时，先以 GitHub Draft PR 或带 Gitea `WIP:` 标题前缀的草稿 PR 发起，把当前候选区块全文交给人类维护者审阅。获批后才更新摘要、将 PR 转为就绪，并按仓库的检查和授权流程合入。审阅期间若修改区块，重新提交候选全文供复核。
<!-- human-contract:end -->

`python3 scripts/check_human_contracts.py --draft` 校验标记和清单格式并报告待审摘要。去掉 `--draft` 会要求摘要与当前区块一致。摘要只用于校验批准版本，维护者依据审阅记录确认批准人和候选版本。
