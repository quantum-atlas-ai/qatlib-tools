# GitHub 和 Gitea 上的拉取请求

多人和多个智能体并行维护形式化仓库时，常会同时出现一批草稿 PR。大型库的完整编译可能要几十分钟，纯文档或索引的静态检查有时一分钟就能完成。准确区分草稿状态、实际作业和变更范围，可以让机器把编译时间留给真正影响构建的改动。确认合并时的远端提交也很重要，否则审阅后新推送的代码可能未经验证就进入主线。

<a id="tools"></a>

## 命令行与草稿状态

查看分支和推送用 `git`。平台查询、创建 PR 和更新状态，可用 GitHub 的 `gh`，或 Gitea 的 `tea`、`gt`，也可直接调用平台 API。`tea` 和 `gt` 是不同的 Gitea 客户端，`gt` 是新版官方 CLI。使用时核对客户端支持的命令和所连接的实例。 [tea v0.16.0](https://gitea.com/gitea/tea/releases/tag/v0.16.0) · [gt 官方文档](https://docs.gitea.com/cli/)

GitHub 有独立的 `isDraft` 状态，用 `gh pr create --draft` 创建草稿，用 `gh pr ready` 转为就绪。Gitea 用实例配置的标题前缀标记草稿，例如 `WIP:` 或 `[WIP]`，API 的 `draft` 字段也反映这个状态。移除前缀后，是否构建由工作流的触发事件和作业条件决定。若配置了标题编辑触发构建，先按 PR 编号、head 和工作流查看已有作业。已有排队、运行或已完成的适用作业时，按其状态处理，避免重复触发。 [GitHub CLI 草稿操作](https://cli.github.com/manual/gh_pr_ready) · [Gitea 草稿前缀配置](https://docs.gitea.com/administration/config-cheat-sheet/#repository---pull-request-repositorypull-request)

<a id="ci"></a>

## 检查结果与变更范围

GitHub 的 check-runs、提交 statuses 和 Actions jobs 分属不同接口。Gitea 的状态摘要可能还在 `pending`，同一 SHA 的具体 job 却已标为 `skipped`。排查时按完整 SHA 分页查询，结合状态的目标 URL 核对仓库、运行和作业。读取前后核对 PR 的 head，防止查到旧提交。查询退出成功只表示查到了结果，作业是否通过要看返回的状态。筛选和状态含义见 `qatlib-forge pr checks --help`。 [GitHub Checks API](https://docs.github.com/en/rest/checks/runs) · [GitHub Commit Statuses API](https://docs.github.com/en/rest/commits/statuses)

路径过滤决定工作流是否为变更安排构建。纯文档改动避开构建路径时，仍按仓库规则做静态检查。整个工作流被过滤掉时没有对应的运行，已触发的工作流则可能记录具体作业为 `skipped`。要复用既有 CI 结论，先确认成功作业对应当前 head，再核对受测基线、当前基线的祖先关系及两端的工作流规则。主线新增改动须为未命中构建规则的文档，且与 PR 改动互不相交。

例如，PR 已在当前 head 构建通过，主线随后只修改了另一篇未被构建读取的文档。上述条件都成立时，主线前进本身无需让每张 PR 都合一次主线、再各编一遍。反过来，PR 新推了提交、主线更改了构建输入，或者两边改到了同一文件，就要针对当前组合重新核验。

`qatlib-forge pr paths` 根据本地提交树判定这些条件。用 `--tested-base` 和 `--tested-head` 指明受测提交，核验适用作业成功后再传 `--ci-success`，工具不会代查 CI。记录受测提交、作业链接和变更路径。碰到无法解释的过滤规则、有关联的主线变更或不完整的证据时，重新验证。 [GitHub 工作流路径过滤](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)

<a id="concurrent-writes"></a>

## 并行合入与分支清理

审阅后，远端分支可能收到新提交。合并时把已审阅的完整 head SHA 传给平台，让服务端在合并时核对。GitHub 可用 `gh pr merge <PR 编号> --match-head-commit <SHA>`，两种平台也可用 `qatlib-forge pr merge <PR 编号> --expected-head <SHA>`。执行前核对适用的 CI 和权限。

删除分支前，先核对推送目标、默认分支和保护规则，再确认远端 SHA。`qatlib-forge branch delete <分支> --expected-sha <SHA>` 带这些校验，实际删除使用 `git push --force-with-lease=refs/heads/<分支>:<SHA> <远端> :refs/heads/<分支>`。远端引用与预期 SHA 不一致时，Git 会拒绝删除。保护规则读不全或远端有新提交时，先停下来核对。 [GitHub 合并接口](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request) · [Git push](https://git-scm.com/docs/git-push)
