# 贡献指南

修改共享能力前，核对下游当前锁定版本与调用入口。共享接口采用可组合的小操作，跨仓业务规则由消费仓维护。提交前使用 Python 3.12 依次运行 `uv lock --check`、`uv build` 与 `uv run --with pytest --with hatchling --with jsonschema pytest -q -rs`。

<a id="human-review-protected-paths"></a>

## 人工审阅保护区域（Human-Review Protected Paths）

以下文件按全文保护。路径边界与门禁配置也受保护。

- `README.md`
- `AGENTS.md`
- `CONTRIBUTING.md`
- `.github/CODEOWNERS`
- `.github/workflows/ci.yml`
- `tests/test_human_review_paths.py`

涉及列表中任何文件的改动必须直接以 Draft PR 创建，并在正文列明涉及的文件。人类维护者必须全文审阅，批准与候选版本绑定。根 README 全文另行提交委托人审阅。全文获批后方可转为 Ready，审批后的改动须重新提交全文。合入还须通过仓库的正常检查与授权。

`.github/CODEOWNERS` 提供路径归属。维护者应在 GitHub 主线规则启用代码所有者批准，并在推送后撤销过期批准。仓内静态门禁核对清单、路径归属和文件存在性。
