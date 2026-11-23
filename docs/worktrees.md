# 在 Lean 仓库里打开第二棵工作树

本页以一个 **Lean 项目**仓库为前提：它已经锁定了 `qatlib-tools` 的版本，且 `qatlib-env` 来自该环境。如果你需要的是工具仓本身的五分钟验证，请先看[上一篇](quickstart.md)。下文会用同一个例子走完一次完整修改：当 `lakefile.toml` 将 `srcDir` 设为 `"src"` 时，文件 `src/MyLib/Facts.lean` 对应的模块参数就是 `MyLib.Facts`。注意，包名、库名和模块命名空间可以各不相同；如果你手头要改的文件不在这个路径，请换成真实文件，并确保模块参数与文件路径始终保持成对。相关字段的含义见[怎么读 lakefile.toml](build.md#lakefile)。

开始操作前，请确保当前目录位于原有检出中。下面的示例使用新分支 `fix/facts-note`（实际操作时请选一个尚未存在的分支名），并在原有检出旁创建新工作树 `../mylib-facts-check`。

## 1. 创建工作树，先不恢复缓存

```bash
qatlib-env worktree ../mylib-facts-check fix/facts-note --no-bootstrap
```

此时当前目录仍为原有检出。命令会在这里执行 `git worktree add -b fix/facts-note ../mylib-facts-check`。加上 `--no-bootstrap` 后，这一步只创建工作树，不调用 `prepare`，因此不会下载工具链或缓存。

执行成功时：`../mylib-facts-check` 目录已经存在；在该目录中运行 `git status`，会看到当前位于分支 `fix/facts-note` 且工作区干净；标准错误中会输出一行 `AGENTS.local.md` 提示，说明这棵工作树看到的是仓库默认说明还是一份本地说明。这一步不应出现大量下载输出。

## 2. 进入新树，先看计划

```bash
cd ../mylib-facts-check
qatlib-env plan MyLib.Facts
```

`plan` 会读取源码图、依赖锁和当前 Git 变更，并向标准输出打印一个 JSON 对象；它不会运行 `lake build`。先看这几个字段：

| 字段 | 含义 |
| --- | --- |
| `target_modules` | 目标解析到了几个模块 |
| `cold_or_rebuild_modules` | 估计需要重新编译的包内模块数 |
| `reverse_invalidated_modules` | 估计会波及的反向依赖数 |
| `dependency_lock_mismatches` | 锁记录与当前检出不一致的依赖 |
| `unresolved_imports` | 尚未解析到的导入 |
| `reasons` | 估算器提出的限制；空数组表示没有本地预算异议 |
| `estimate_basis.confidence` | 表明这是启发式估计，而非一次真实编译 |

由于此时依赖尚未检出，`unresolved_imports` 里出现对应的依赖模块属于正常情况，这些缺失项会在下一步的 `prepare` 中补齐。如果 `reasons` 中出现 `cold_limit` 或 `reverse_limit`，请先缩小目标范围，或者阅读项目自身的预算说明，再决定要不要在后续的 `run` 命令中加上 `--allow-local-heavy`。本页练习只处理一个叶子模块。

## 3. 按锁准备依赖

确认当前目录位于 `mylib-facts-check`，然后执行：

```bash
qatlib-env prepare
```

这一步会按 `lake-manifest.json` 准备依赖源码，并安装已配置的缓存。写入位置包括这棵工作树自己的 `.lake` 依赖检出，以及内容寻址的共享缓存；各棵工作树的 `.lake` 目录仍然彼此独立。

这一步耗时可能较长。首次运行时，它会下载 Lean 工具链，并解包或编译缺失的工件；如果共享缓存中已有这些输入，则主要做核对并补齐缺口。执行成功时，命令退出码为 0，标准输出为一段 JSON。

准备完成后，再查看一次计划：

```bash
qatlib-env plan MyLib.Facts
```

对比前后的 `cold_or_rebuild_modules`：这个数字就是接下来将交给 `lake build` 的预估范围。估计依据记录在 `estimate_basis` 中，它不能代替实际编译结果。

## 4. 构建这一个模块

保持当前目录仍在新工作树中，执行：

```bash
qatlib-env run -- lake build MyLib.Facts
```

`run` 会在资源预算内执行 `--` 后面的命令。由于命令里没有 `--no-build`，它会在需要时先补做一次依赖准备，再调用 Lake。Lake 会按导入关系构建 `MyLib.Facts` 及其上游。产物写入这棵工作树的 `.lake` 目录；命中共享缓存时，较大的 `.olean` 文件可以留在缓存目录里。开关的细节见[缓存读写与恢复开关](build.md#cache-flags)。

构建成功时，Lake 会报告该目标已经构建，整条命令退出码为 0。

## 5. 改一处，再用现成产物检查这一个文件

先在 `src/MyLib/Facts.lean` 中修改一行文档注释，保持文件仍能通过类型检查，同时不要改动导入。

然后只检查这一个文件，不让 Lake 为缺失的依赖补编译：

```bash
qatlib-env run -- lake --no-build lean src/MyLib/Facts.lean
```

带上 `--no-build` 后，这次 `run` 不会再自动执行 `prepare`，Lake 只使用已经存在的依赖产物来检查该文件。执行成功时退出码为 0，输出中是这个文件的检查结果。

如果 Lake 提示依赖产物还不存在，请回到上一步，先执行 `qatlib-env run -- lake build MyLib.Facts`，再重新运行这条 `--no-build` 命令。不要改用裸的 `lean` 二进制文件，也不要只在 `.lake/build` 里找文件：共享缓存里的工件不会全部出现在该目录中。详见[增量编译与单文件检查](build.md#incremental-build)。

## 6. 只预览清理

保持当前目录仍在新工作树 `mylib-facts-check` 中，先预览缓存清理范围：

```bash
qatlib-env workspace clean-caches --checkout . --derived-only --dry-run
```

`--derived-only` 只选择已有的 `.lake/build` 和 `.lake/packages`；`--dry-run` 只打印将要清理的目标，不移动文件。这条命令默认本来就是预览，显式加上该参数是为了让命令本身写明意图。

读完输出就停，不要加 `--apply`——这棵工作树还要用来开拉取请求。

如果要预览移走整棵工作树（而不只是派生目录）时的影响，可以运行下面这条命令查看其中的警告，同样不要加 `--apply`：

```bash
qatlib-env workspace trash-checkouts --checkout . --dry-run
```

预览结果会列出未提交的改动、在途分支和仍在运行的进程。本页操作到预览为止。

## 做完时你手里有什么

此时你已经有了一棵位于分支 `fix/facts-note` 上的新工作树，并在其中完成了一次模块构建和一次单文件检查。下一步可以把这个分支推送上去，并按[拉取请求](pull-requests.md)开一张草稿。缓存矩阵、Windows 跨卷和配置优先级留在[编译环境](build.md)中，需要查询某一项时再打开即可。
