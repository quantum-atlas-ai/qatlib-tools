# 在 Lean 仓库里打开第二棵工作树

本页以一个已锁定 `qatlib-tools` 版本的 **Lean 项目**仓库为前提，且当前终端中的 `qatlib-env` 来自该环境（若需对工具仓本身做快速自检，请先阅读[五分钟验证](quickstart.md)）。下文围绕同一个示例走完一次完整修改：当 `lakefile.toml` 将 `srcDir` 设为 `"src"` 时，源文件 `src/MyLib/Facts.lean` 对应的模块参数即为 `MyLib.Facts`。由于包名、库目标名与模块命名空间在 Lake 中彼此独立，若你实际修改的文件位于其他路径，请替换为真实路径，并确保模块参数与文件路径一一对应（相关字段规则见[`lakefile.toml` 怎么读](build.md#lakefile)）。

开始操作前，请确认当前目录位于原有主检出中。下方示例将在主检出旁新建工作树 `../mylib-facts-check`，并切出新分支 `fix/facts-note`（实际操作时请使用尚未存在的分支名）。

即使将新增工作树放在独立数据盘（例如 Windows 的 `D:` 盘或 Linux/macOS 的大容量挂载盘），各棵工作树依然共享用户主目录下的全局 Lean 工具链与内容寻址缓存，仅各自维护独立的 `.lake` 目录。在跨盘规划多棵工作树前，建议先查阅[资源占用与跨盘布局](install.md#resource-footprint)核对缓存与单树空间开销；跨盘硬链接回退与长路径注意事项见[Windows 跨卷和长路径](build.md#windows)。

## 1. 创建工作树，先不恢复缓存

```bash
qatlib-env worktree ../mylib-facts-check fix/facts-note --no-bootstrap
```

此时当前目录仍位于原有主检出，命令在此执行 `git worktree add -b fix/facts-note ../mylib-facts-check`。传入 `--no-bootstrap` 后，该步骤仅创建工作树而不调用 `prepare`，因此不会触发工具链或缓存下载。

执行成功后，`../mylib-facts-check` 目录即已就绪；进入该目录运行 `git status`，应显示当前位于分支 `fix/facts-note` 且工作区干净；标准错误会打印一行 `AGENTS.local.md` 状态提示，标明该工作树读取的是仓库默认说明还是本地补充说明。此步骤不应出现大量下载输出。

## 2. 进入新树，先看计划

```bash
cd ../mylib-facts-check
qatlib-env plan MyLib.Facts
```

`plan` 只读解析源码导入图、依赖锁与当前 Git 差异，向标准输出打印一个 JSON 对象，不会启动 `lake build`。重点核对以下字段：

| 字段 | 含义 |
| --- | --- |
| `target_modules` | 目标参数解析出的模块数量 |
| `cold_or_rebuild_modules` | 预计需要编译或重编的包内模块数 |
| `reverse_invalidated_modules` | 预计受波及的反向依赖模块数 |
| `dependency_lock_mismatches` | 本地检出与锁文件记录不一致的依赖项 |
| `unresolved_imports` | 当前尚未解析到的导入模块 |
| `reasons` | 触发本地预算拦截的原因列表；空数组表示未触发预算限制 |
| `estimate_basis.confidence` | 估算置信度说明（表明这是静态启发式估算，而非真实编译结果） |

由于此时尚未检出外部依赖，`unresolved_imports` 中列出对应依赖模块属于预期现象，下一步执行 `prepare` 后即可补齐。若 `reasons` 中出现 `cold_limit` 或 `reverse_limit`，请先收窄目标范围或查阅项目自身的预算策略，再决定后续 `run` 是否确需显式传入 `--allow-local-heavy`。本页示例仅处理单个叶子模块。

## 3. 按锁准备依赖

确认当前目录位于 `mylib-facts-check`，执行：

```bash
qatlib-env prepare
```

该命令依据 `lake-manifest.json` 检出锁定的依赖源码并安装已配置的缓存，同时写入当前工作树私有的 `.lake` 依赖检出与全局内容寻址共享缓存（各棵工作树的 `.lake` 目录始终彼此隔离，切勿跨树整目录拷贝）。

首次运行时可能需要下载 Lean 工具链并解包或编译缺失工件，耗时相对较长；若共享缓存中已具备对应输入，则主要执行完整性核对与缺口补齐。成功后命令退出码为 0，并向标准输出打印一段汇总 JSON。

准备完成后，再次运行 `plan` 对照变化：

```bash
qatlib-env plan MyLib.Facts
```

对比前后的 `cold_or_rebuild_modules`：此时给出的数字即接下来交给 `lake build` 的预估模块范围（估算依据见 `estimate_basis`，最终仍以实际编译为准）。

## 4. 构建这一个模块

保持当前目录位于新工作树中，执行：

```bash
qatlib-env run -- lake build MyLib.Facts
```

`run` 会在受管资源预算内执行 `--` 之后的命令。由于未传入 `--no-build`，它会在必要时先补做一次依赖准备，随后调用 Lake 按导入链构建 `MyLib.Facts` 及其前置模块。构建产物登记在当前工作树的 `.lake` 下；命中可写共享缓存时，体积较大的 `.olean` 文件可直接留在共享缓存目录中读取，无需逐树重复落盘（开关组合详见[缓存读写与恢复开关](build.md#cache-flags)）。

构建成功时，Lake 会报告目标已完成构建，整条命令退出码为 0。

## 5. 改一处，再用现成产物检查这一个文件

打开 `src/MyLib/Facts.lean`，修改一行文档注释，确保文件依然能通过类型检查，且不要在本次修改中增删 `import`。

随后仅针对该文件做增量类型检查，禁止 Lake 为缺失依赖隐式触发补编：

```bash
qatlib-env run -- lake --no-build lean src/MyLib/Facts.lean
```

传入 `--no-build` 后，`run` 会跳过自动 `prepare`，Lake 也只利用现成的依赖产物检查目标文件。成功时命令退出码为 0，输出仅反映该单文件的检查结果。

若 Lake 报错提示前置依赖产物缺失，请退回上一步先执行 `qatlib-env run -- lake build MyLib.Facts`，再重新运行这条 `--no-build` 命令。切勿直接调用裸 `lean` 二进制文件，也不要仅凭扫描本地 `.lake/build` 目录判断产物是否存在——托管在全局共享缓存中的工件并不会全部复制到本地目录中，详见[增量编译与单文件检查](build.md#incremental-build)。

## 6. 只预览清理

保持当前目录位于新工作树 `mylib-facts-check` 中，预览派生缓存的清理范围：

```bash
qatlib-env workspace clean-caches --checkout . --derived-only --dry-run
```

`--derived-only` 仅选取已存在的 `.lake/build` 与 `.lake/packages` 目录；`--dry-run` 只列出待清理目标而不移动任何文件（预览本就是默认模式，显式写出该标志便于直观核对命令意图）。

核对完输出即可停下，切勿追加 `--apply`——下一篇还要继续使用这棵工作树发起拉取请求。

若想一并预览回收整棵关联工作树（而非仅清理派生目录）时的安全检查输出，可运行下方命令查看告警项，同样不要加 `--apply`：

```bash
qatlib-env workspace trash-checkouts --checkout . --dry-run
```

预览报告会逐项列出未提交改动、在途分支以及仍在运行的后台进程。本页操作到此预览为止。

## 做完时你手里有什么

至此，你已在分支 `fix/facts-note` 的独立工作树中完成了一次模块构建与一次单文件检查。下一步可将该分支推送到远端，并按[拉取请求](pull-requests.md)发起首张草稿 PR。关于九种缓存开关组合、Windows 跨卷细节与七层配置优先级，可按需查阅[编译环境、缓存与资源控制](build.md)。
