# 交出第一张草稿

这一页从一条**已经推送**的分支开始。上一页的例子是 Lean 仓库里的分支 `fix/facts-note`，工作树是 `mylib-facts-check`。修改本工具仓时，把下面的仓库和验证命令换成你实际跑过的命令；本仓的完整本地门禁在[贡献指南](../CONTRIBUTING.md#self-test)。

当前目录可以是那棵工作树。先确认本地 head 和远端一致：

```bash
git status
git rev-parse HEAD
git push -u origin HEAD
```

`git rev-parse HEAD` 打印的 40 位 SHA 就是后面要对照的提交。`git push` 在已经推送过时会告诉你远端已经是这个提交。成功时，`git status` 里能看到分支跟踪 `origin` 上的同名分支。

## 开一张草稿

用 GitHub 网页上的 “Compare & pull request”，标题下选择 Draft；或在工作树里用 `gh`：

```bash
gh pr create --draft --title "Note one fact in MyLib.Facts" --body-file /tmp/mylib-facts-pr.md
```

正文按仓库里的 `.github/PULL_REQUEST_TEMPLATE.md` 分成 Changes 和 Verification。下面是一份可以照着改的记录。把尖括号里的内容换成你自己的 SHA、退出码和没跑的检查：

````markdown
## Changes

在 `src/MyLib/Facts.lean` 修改了一行文档注释。模块参数是 `MyLib.Facts`。

## Verification

- 当前目录：`mylib-facts-check`。head：`<git rev-parse HEAD 的 40 位 SHA>`。
- `qatlib-env plan MyLib.Facts` — 退出码 0。`cold_or_rebuild_modules` 为 `<数字>`，`reasons` 为 `<空数组或你看到的 code>`。
- `qatlib-env prepare` — 退出码 0。
- `qatlib-env run -- lake build MyLib.Facts` — 退出码 0。
- `qatlib-env run -- lake --no-build lean src/MyLib/Facts.lean` — 退出码 0。
- 未运行：整个仓库的 `lake build`。
````

成功时，`gh pr view --json number,isDraft,url` 里的 `isDraft` 为 `true`，`url` 能在浏览器里打开。草稿仍然会触发本工具仓的 `CI / test`；下游 Lean 仓库是否跳过昂贵构建，由那个仓库的工作流决定。两边的差别写在[GitHub 工作流](github-ci.md)。

Gitea 用标题前缀表示草稿，创建命令也不一定是 `gh`。第一次如果就在 Gitea 上，先读[审阅页里的平台差异](forge.md#gitea)。

## 按当前提交读 Checks

每次阅读之前和之后都再取一次 head。中间如果又推送了，页面上的检查就不再属于你刚才看的提交。

```bash
git rev-parse HEAD
gh pr view --json headRefOid,isDraft,statusCheckRollup
gh pr checks
```

`headRefOid` 必须等于 `git rev-parse HEAD`。然后把每一条检查分成下面四类，再决定能不能离开草稿：

| 你看到的状态 | 它说明什么 |
| --- | --- |
| 排队或正在运行 | 还没有结论 |
| `skipped` | 工作流触发了，这一项按条件跳过。跳过不是通过 |
| 成功 | 这个 head 上的该作业通过了。把作业链接和 head SHA 一起写进验证记录 |
| 没有这条工作流的运行记录 | 它可能没被触发。本工具仓的 `ci.yml` 没有路径过滤，文档改动也会运行 `CI / test`。`docs.yml` 不在拉取请求上构建 Pages |

`gh pr checks` 退出码为 0 只表示命令取回了结果。作业是否通过，看每一行的结论。需要按作业名汇总时，可以用 `qatlib-forge pr checks <编号> --expected-head <40 位 SHA>`。这条命令同样只负责取回；通过与否看返回的状态。字段和跳过、排队的差别在[审阅并行提交](forge.md#ci)。

## 准备好了再转为就绪

验证记录已经写上命令、结果和未运行的项，并且当前 head 的检查符合你要请人审的范围之后：

```bash
gh pr ready
```

成功时，同一条 `gh pr view --json isDraft,headRefOid` 里 `isDraft` 为 `false`，`headRefOid` 仍是你核对过的 SHA。

改到 `DESIGN.md` 里受保护的设计正文时，保持草稿，按 [DESIGN.md](../DESIGN.md) 的人工审阅约定送审。本地先运行 `python3 scripts/check_human_contracts.py`。摘要还没批准时，脚本会打印待审摘要并以非零退出；草稿上要看候选摘要，再加 `--draft`。那是贡献指南里的门禁，不是这一页的普通文档改动所需的步骤。

合并、复用别的 head 上的结论，以及删除分支，留给审阅者。见[审阅并行提交](forge.md)。
