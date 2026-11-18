# GitHub 和 Gitea 上的拉取请求

<a id="tools"></a>

## 命令行与草稿状态

分支查看和推送用 `git`。GitHub 的常规查询、创建和更新用 `gh`，Gitea 可用 `tea` 或实例支持的 `gt`，单步接口可直接走平台 API。`tea` 是独立的 Gitea CLI，`gt` 是 Gitea 的新版命令行，使用前核对实例支持的版本和本地登录状态。 [tea v0.16.0](https://gitea.com/gitea/tea/releases/tag/v0.16.0) · [gt 官方文档](https://docs.gitea.com/cli/)

GitHub 草稿有独立的 `isDraft` 状态，由 `gh pr create --draft` 创建，`gh pr ready` 更新平台状态。Gitea 标题前缀由实例配置，常见为 `WIP:` 或 `[WIP]`。API 返回的 `draft` 可以反映此前缀，标题与状态不可当成两个独立开关。移除前缀后是否调度构建取决于工作流的事件和作业条件。以题目编辑事件补跑首次 Ready 构建时，先按 PR 编号、head 和工作流查询历史作业。之前已有排队、成功或失败的作业都代表已调度，避免一次标题编辑重复启动全库编译。 [GitHub CLI 草稿操作](https://cli.github.com/manual/gh_pr_ready) · [Gitea 草稿前缀配置](https://docs.gitea.com/administration/config-cheat-sheet#pull-request-repositorypull-request)

<a id="ci"></a>

## 检查结果与变更范围

GitHub 的 check-runs、提交 statuses 和 Actions jobs 来自独立接口。Gitea 的 status 摘要可能停在 `pending`，而同 SHA 的具体 job 已结束为 `skipped`。按完整 SHA 查询各页，用状态的目标 URL 核对同仓、同次运行、同一作业，再解释差异。读取前后都检查 PR head。查看结果的命令可以成功返回，表内作业仍可能失败或未运行。使用 `qatlib-forge pr checks --help` 查看状态和作业选择方法。 [GitHub Checks API](https://docs.github.com/en/rest/checks/runs) · [GitHub Commit Statuses API](https://docs.github.com/en/rest/commits/statuses)

路径过滤决定是否调度 Build。文档改动避开构建输入时，仍应运行仓库适用的静态检查。平台因路径过滤跳过整个工作流与 workflow 已运行但 job 退出为 `skipped` 是不同情况。复用构建结果前核对两侧工作流规则、受测 head 和 base 的祖先关系，只在 main 增量是未命中 Build 且与 PR 未相交的文档时复用既有构建结果。保留受测提交、作业链接和变更清单。遇到不支持的规则、基线变更影响代码或工作流、证据不全时重新验证。 [GitHub 工作流路径过滤](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)

<a id="concurrent-writes"></a>

## 并行合入与分支清理

审阅与执行之间可能有人推送。按审核过的完整 head SHA 合并，让服务端在执行时比对预期提交。GitHub 可用 `gh pr merge --match-head-commit <SHA>`，跨平台入口是 `qatlib-forge pr merge --expected-head <SHA>`，授权和适用 CI 仍须单独核对。删远端分支前核对仓库、默认分支和保护规则，再用 `git push --force-with-lease=refs/heads/<分支>:<SHA>` 加上删除 refspec，请 Git 仅在远端仍指向预期 SHA 时执行。遇到远端变更或保护规则无法读出时停下来重新核查。 [GitHub 合并接口](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request) · [Git push](https://git-scm.com/docs/git-push)
