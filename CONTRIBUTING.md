# 贡献指南

调整本仓的跨仓共享能力前，请先核对各下游仓库当前锁定的版本与实际调用入口。共享工具接口保持职责单一、可组合，具体仓库自身的业务规则与接收契约由各消费仓自行维护。使用 Python 3.12 运行与 CI 一致的检查。没有 `uv` 时可用隔离环境构建、运行其余测试并校验离线手册，具体命令见[工具仓自测与离线手册校验](README.md#self-test)。

<a id="human-review-protected-paths"></a>

## 人工审阅保护区域（Human-Review Protected Paths）

下列核心文档按全文保护，定义路径边界与门禁规则的配置文件一并纳入保护范围：

- `README.md`
- `AGENTS.md`
- `CONTRIBUTING.md`
- `.github/CODEOWNERS`
- `.github/workflows/ci.yml`
- `tests/test_human_review_paths.py`

凡涉及上述清单中任一文件的改动，必须直接创建为 Draft PR，并在 PR 正文中列明所触及的受保护文件。人类维护者须对最新候选版本执行全文审阅，审阅批准与该候选版本严格绑定；其中根 `README.md` 的候选全文须另行提交委托人审阅。全文获批后 PR 方可转为 Ready，获批后若继续追加改动须重新提交最新全文审阅；最终合入仍须通过仓库常规检查并取得合并授权。

受保护文件的路径归属由 `.github/CODEOWNERS` 声明。仓内静态门禁确认清单包含要求保护的路径，逐一核对文件存在、`.github/CODEOWNERS` 路径与清单一致，并检查每条规则至少有一个符合格式的 GitHub 用户名。代码所有者批准和推送后撤销过期批准由维护者在 GitHub 主线分支规则中启用。
