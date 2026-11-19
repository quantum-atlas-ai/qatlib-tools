# GitHub 和 Gitea 上的拉取请求

当多人与多个智能体并行维护形式化仓库时，往往会同时挂起一批草稿 PR。大型库的完整编译动辄耗时几十分钟，而纯文档或索引改动的静态检查有时一分钟内就能完成。准确区分草稿状态、实际运行的作业与变更范围，能够让机器把宝贵的编译时间留给真正影响构建的改动；而在合并时严格确认远端提交也同样关键，否则审阅通过后新推送的代码就可能未经验证直接进入主线。

<a id="tools"></a>

## 命令行与草稿状态

本地分支查看与推送使用 `git`；涉及平台的查询、创建 PR 与更新状态，则可使用 GitHub 的 `gh`、Gitea 的 `tea` 或 `gt`，也可直接调用平台 API。需要注意，`tea` 与 `gt` 是两款不同的 Gitea 客户端，其中 `gt` 为新版官方 CLI，使用时应先核对客户端支持的命令以及当前连接的实例。 [tea v0.16.0](https://gitea.com/gitea/tea/releases/tag/v0.16.0) · [gt 官方文档](https://docs.gitea.com/cli/)

两个平台标记草稿的机制有所不同：GitHub 设有独立的 `isDraft` 状态，可通过 `gh pr create --draft` 创建草稿，待就绪后再用 `gh pr ready` 切换状态；Gitea 则依靠实例配置的标题前缀（例如 `WIP:` 或 `[WIP]`）标识草稿，API 返回的 `draft` 字段也会同步反映这一状态。在 Gitea 上移除草稿前缀后，是否触发构建取决于工作流的触发事件与作业条件。如果仓库配置了编辑标题触发构建，操作前应先按 PR 编号、head 和工作流排查现有记录——若已有处于排队、运行或已完成状态的适用作业，直接按其状态跟进即可，避免重复触发构建。 [GitHub CLI 草稿操作](https://cli.github.com/manual/gh_pr_ready) · [Gitea 草稿前缀配置](https://docs.gitea.com/administration/config-cheat-sheet/#repository---pull-request-repositorypull-request)

<a id="ci"></a>

## 检查结果与变更范围

查询 CI 结果时需留意不同平台的接口划分与状态汇总差异：GitHub 的 check-runs、提交 statuses 和 Actions jobs 分属不同接口；而在 Gitea 上，有时同一 SHA 下的具体 job 已被标为 `skipped`，外层的状态摘要却仍停留在 `pending`。因此，排查时应按完整 SHA 分页查询，结合状态记录中的目标 URL 核对所属仓库、工作流运行与具体作业，并在读取前后再次确认 PR 的 head，防止误查到旧提交。此外，查询命令退出成功仅代表顺利取回了结果，作业是否通过仍须依据返回的状态判定。具体的筛选方式与状态含义见 `qatlib-forge pr checks --help`。 [GitHub Checks API](https://docs.github.com/en/rest/checks/runs) · [GitHub Commit Statuses API](https://docs.github.com/en/rest/commits/statuses)

工作流是否针对变更安排构建，取决于仓库配置的路径过滤规则。当纯文档改动避开构建路径而不触发构建时，仍须按仓库规则完成静态检查。反映在平台记录上，如果整个工作流被过滤掉，就不会产生对应的运行记录；而对于已触发的工作流，按条件跳过的具体作业则会记录为 `skipped`。在主线推进后，若要直接复用既有的 CI 通过结论，必须先确认成功作业确实对应当前 head，再核对受测基线与当前基线的祖先关系以及两端的工作流规则；同时，主线在此期间新增的改动必须仅限于未命中构建规则的文档，且与当前 PR 的改动互不相交。

举例来说，假设某张 PR 已在当前 head 上构建通过，此后主线仅修改了另一篇未被构建读取的文档。当上述条件全部满足时，主线本身的前进并不要求每张在途 PR 都重新合并一次主线、再各自重编一遍。反之，只要 PR 推送了新提交、主线更改了构建输入，或者双方改到了同一个文件，就必须针对当前的提交组合重新核验。

`qatlib-forge pr paths` 可根据本地提交树判定上述复用条件：调用时通过 `--tested-base` 和 `--tested-head` 指明受测提交；由于该工具不会代查 CI，须在核实适用作业已成功通过后再传入 `--ci-success`。核验时应一并记录受测提交、作业链接与变更路径；一旦遇到无法解释的过滤规则、有关联的主线变更或证据不完整的情况，切勿勉强复用，应直接重新验证。 [GitHub 工作流路径过滤](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)

<a id="concurrent-writes"></a>

## 并行合入与分支清理

代码审阅完成后，远端分支在合并前仍可能收到新提交。为防止未经核验的改动进入主线，合并时应将已审阅的完整 head SHA 传给平台，由服务端在执行合并时比对核实。GitHub 可使用 `gh pr merge <PR 编号> --match-head-commit <SHA>`，两种平台也可统一使用 `qatlib-forge pr merge <PR 编号> --expected-head <SHA>`。执行合并前，请先核对适用的 CI 状态与操作权限。

删除分支前，应先核对推送目标、默认分支与保护规则，再确认该分支当前的远端 SHA。`qatlib-forge branch delete <分支> --expected-sha <SHA>` 已内置这些校验，实际删除操作底层通过 `git push --force-with-lease=refs/heads/<分支>:<SHA> <远端> :refs/heads/<分支>` 完成；当远端引用与预期 SHA 不一致时，Git 会拒绝删除。若保护规则读取不全或远端已出现新提交，应先停下来核对清楚。 [GitHub 合并接口](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request) · [Git push](https://git-scm.com/docs/git-push)
