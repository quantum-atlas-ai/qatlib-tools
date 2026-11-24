# GitHub 和 Gitea 上的拉取请求

这一页给审阅者。要回答的问题只有一个：已经看到的检查，测的是哪个 head；它能不能用于眼前这张拉取请求；现在能不能合并、能不能删分支。第一次开草稿的步骤在[拉取请求](pull-requests.md)。本仓两份工作流各自在什么时候运行，见[GitHub 工作流](github-ci.md)。

<a id="ci"></a>

## 这份检查测的是哪个 head

先记下网页上的 head SHA。阅读前后各取一次。两次不一致，就停下来用新的 SHA 重新查。

```bash
qatlib-forge pr checks <编号> --expected-head <40 位小写 SHA>
```

命令退出码为 0 只表示取回了结果。作业是否通过，看返回的状态：`success`、`failure`、`skipped`、`cancelled`、`pending`、`absent`。`skipped`、还在排队、没有记录、基础设施故障，都不算通过。筛选方式和状态含义见 `qatlib-forge pr checks --help`。

GitHub 把 check-runs、提交 statuses 和 Actions jobs 放在不同接口里。核对时用状态记录中的目标 URL 对上仓库、工作流运行和具体作业，并按完整 SHA 翻页，避免停在旧的一页。[GitHub Checks API](https://docs.github.com/en/rest/checks/runs) · [GitHub Commit Statuses API](https://docs.github.com/en/rest/commits/statuses)

<a id="gitea"></a>

## Gitea 上状态和草稿的差异

Gitea 的草稿跟随实例配置的标题前缀，例如 `WIP:` 或 `[WIP]`，API 的 `draft` 字段反映这个前缀。去掉前缀会不会触发构建，取决于工作流事件和作业条件。如果改标题就会触发构建，先按编号、head 和工作流看已有记录：已经在排队、运行或完成的适用作业，按其状态跟进，不要再触发一次。[Gitea 草稿前缀](https://docs.gitea.com/administration/config-cheat-sheet/#repository---pull-request-repositorypull-request)

同一 SHA 上，具体 job 可以已经是 `skipped`，外层摘要却仍是 `pending`。以外层摘要为准会把跳过看成还没跑。打开目标 URL，读到具体作业后再下结论。

`tea` 和 `gt` 是两个不同的 Gitea 客户端，`gt` 是新的官方 CLI。使用前核对客户端支持的子命令，以及它连的是哪一个实例。[tea v0.16.0](https://gitea.com/gitea/tea/releases/tag/v0.16.0) · [gt 文档](https://docs.gitea.com/cli/)

<a id="tools"></a>

## 本地分支和创建草稿

查看和推送本地分支用 `git`。创建和更新拉取请求可以用 GitHub 的 `gh`，也可以用上面的 Gitea 客户端或平台 API。GitHub 的草稿是独立的 `isDraft`：`gh pr create --draft` 创建，`gh pr ready` 转为就绪。[gh pr ready](https://cli.github.com/manual/gh_pr_ready)

<a id="reuse"></a>

## 已有结论能不能用于当前拉取请求

工作流是否安排构建，取决于仓库自己的路径过滤。整份工作流被过滤掉时，不会有运行记录。已经触发的工作流里，按条件跳过的作业记为 `skipped`。纯文档改动避开构建路径时，仍要完成该仓库要求的静态检查。[路径过滤](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)

主线又向前走了几笔提交时，旧的成功结论要同时满足下面几条才能沿用：

1. 成功的作业对应当前 head，而不是更早的一次推送。
2. 受测基线和当前基线的祖先关系，以及两端的工作流规则，都核对过。
3. 主线在这期间新增的改动只落在未命中构建规则的文档上，并且和当前拉取请求的改动没有共同文件。

例如当前 head 已经构建通过，其后主线只改了另一篇构建不会读的文档。上面三条都成立时，不需要每张在途拉取请求都再合并一次主线并各自重编。只要拉取请求又推了新提交、主线改了构建输入，或两边改了同一个文件，就对当前这对提交重新验证。

在同时含有这两个提交的本地仓库里：

```bash
qatlib-forge pr paths \
  --project . \
  --base <当前基线的完整 SHA> \
  --head <当前 head 的完整 SHA> \
  --workflow .github/workflows/<工作流文件> \
  --tested-base <受测基线 SHA> \
  --tested-head <受测 head SHA> \
  --ci-success
```

`--tested-base` 和 `--tested-head` 成对出现。工具不代查 CI，所以只在你已经确认适用作业成功之后才加上 `--ci-success`。把受测提交、作业链接和变更路径写进审阅记录。过滤规则解释不了、主线上有相关改动，或证据缺一项时，重新跑当前组合。

<a id="concurrent-writes"></a>

## 现在能不能合并，能不能删分支

审阅完成到真正合并之间，远端仍可能收到新提交。把审阅过的完整 head 交给服务端，在合并时比对。

GitHub：

```bash
gh pr merge <编号> --match-head-commit <40 位 SHA>
```

两个平台都可以：

```bash
qatlib-forge pr merge <编号> --expected-head <40 位小写 SHA>
```

合并前再看适用 CI 和你自己的权限。[GitHub 合并接口](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

删除前核对推送目标、默认分支和保护规则，再确认远端 SHA：

```bash
qatlib-forge branch delete <分支> --expected-sha <40 位小写 SHA>
```

底层是 `git push --force-with-lease=refs/heads/<分支>:<SHA> <远端> :refs/heads/<分支>`。远端引用和预期 SHA 不一致时，Git 拒绝删除。保护规则读不全，或远端已经有新提交时，停下来再核对。[git push](https://git-scm.com/docs/git-push)
