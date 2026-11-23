# 共享成果如何进入公开库

## 手册与各仓晋升说明的分工

本文介绍一项共享成果从孵化仓迁入预发验收仓、再随正式版本发布进入公开库的通用技术流程。其中，整文件依赖收集、面向普通 Lean 模块的消费者探针、增量书目归并以及跨库冲突检查等机制在不同项目间通用，统一由本手册说明；具体的目录规划、元数据要求、评审角色与准入标准则由各仓库自身的 `promote` skill 维护，并从那里链接到本手册。

## 第一阶段：迁移整文件并在预发仓验证

迁移从拟公开的模块出发，沿着项目内的 `import` 链递归收集依赖，同时区分哪些模块已由接收仓或其外部依赖提供。源码应按完整的 `.lean` 文件迁移，将文件内的实例、文档注释与私有辅助引理一并保留。若遇到缺失依赖，或接收仓的目标路径下已存在不同实现，须先解决接口冲突再继续迁移；对于留在原仓的依赖和未迁移模块，应记录原因供接收方复核。

具备项目适配器时，可调用 Python 接口 `qatlib_search.promotion_closure.candidate_closure` 辅助整理迁移闭包：调用方先验证依赖锁，再传入候选模块、源码库根与适用的导入规则。该接口仅以只读方式解析源码，正常情况下返回依赖在前的整文件清单及各文件的导入关系；一旦查出错误，则仅返回诊断信息与已读到的导入关系，迁移清单保持为空。

同步文献引用时，先在预发仓根目录运行 `qatlib-env promotion merge-bibliography --increment /path/to/increment/references.bib` 进行归并预检。传入 `--write` 后，工具会原样保留目标 `docs/references.bib` 的原有字节，仅追加尚未收录的条目原文；若遇到同一引用键对应不同内容的同键异文冲突，则会阻止写入。与此同时，增量仓仍结合锁定的公开书目与自身增量文件共同验证引用，具体机制见[增量书目](lean-docs.md#bibliography)。

预发验收不仅要运行项目的静态检查与构建，还需站在外部普通 `module` 的视角核验实际对外暴露的接口。例如，可在 `verification/Consumer.lean` 中导入并使用拟发布的模块：

```lean
module
import MyLib.Facts

#check MyLib.Facts.some_fact
```

```bash
qatlib-env promotion smoke --probe verification/Consumer.lean
```

消费者探针应以尽可能精简的导入覆盖真实的调用方式：用 `example` 复述关键定理的签名，用普通 `def` 调用承诺可计算的函数，并用 `rfl` 或受限 `simp` 验证承诺可展开的定义。采用普通 `import` 能让探针严格遵循下游实际面对的可见性边界（参见[模块可见性](build.md#modules)）。运行时，`smoke` 会利用已有的依赖工件检查目标 Lean 文件，记录当前 `HEAD`、工具链与诊断信息，并将结果区分为 `PASS`、`REGRESSION` 与 `NOT-RUN`。若涉及有意的接口改名，可通过 `--migration-map migration.toml` 传入迁移映射，在 `[[rename]]` 表项中记录 `old`、`new` 和 `reason`。

<a id="check-export"></a>

### 检查仅供开发期使用的旧接口

重构改名时，原仓可按项目约定将旧别名或测试转发文件保留一个发布周期，为尚在进行的工作留出切换时间。这类过渡项需用独立行注释 `-- promotion: dev-only` 显式标记，并在本批迁移清单中记录旧名、新名与路径。导出到接收仓时，应去掉所有带标记的临时别名与转发文件，并让消费者改用新接口；公开库依据自身兼容政策维护的 `@[deprecated]` 声明则可照常保留。

```bash
qatlib-env promotion check-export MyLib/Facts.lean verification/Consumer.lean
```

执行检查时，须向命令显式传入本批导出的全部目标 Lean 文件，包括尚未纳入 Git 的新文件。该工具仅以只读方式扫描真实的 Lean 行注释，会自动忽略块注释与字符串中的示例文本；一旦检出残留标记，即输出对应的文件、行号与错误信息，并返回非零退出码。扫描通过后，还应对照迁移清单反查旧名与路径，确认没有漏标的过渡项，最后再运行面向普通模块的消费者探针。

## 提前演练下游切换

公开库将改动合入预发分支后，依赖它的项目可以用草稿 PR 试用候选版本。检查导入路径、限定名、接口探针与重复声明，并在 PR 中记录候选提交和验证结果。等正式版本和对应的预编译工件都可用后，再合并接口切换与源码清理。

## 第二阶段：按正式版本更新依赖锁并清退旧副本

正式切换前，先确认上游正式版本、完整提交 SHA 及对应的预编译工件均已可正常获取。下面两段都发生在**依赖这个公开库的下游检出**里。预发仓的验收仍留在上一阶段的那一棵树。

### 定向升级已经检出的依赖

当前目录是下游项目里、这份直接 Git 依赖已经检出的工作树。

```bash
qatlib-env promotion bump-lock --package MyLib
qatlib-env promotion verify-lock --package MyLib
```

`bump-lock` 更新既有的直接 Git 依赖，并核对更新后的本地检出。紧接着的 `verify-lock` 读取这份已经锁定的本地源码，确认清单里的完整 Git SHA 与实际检出一致。查看 `lake-manifest.json` 的差异，确认改动落在预期的包上。

### 在另一棵干净工作树里验锁

当前目录是一份尚未准备依赖的干净检出。`verify-lock` 要读取已锁定的本地依赖源码，所以先按锁准备，再验锁。

```bash
qatlib-env prepare
qatlib-env promotion verify-lock --package MyLib
```

`prepare` 按锁准备依赖源码及所配置的缓存。验锁通过后，锁定的提交 SHA、缓存准备与后续构建才对应同一个版本。

在同一张下游 PR 中，一并将 `import`、`open`、完整限定名、测试及项目元数据切换到上游公开接口，同时清退本地旧源码、临时别名与只剩空壳的模块；确认拟删除的本地书目键均已由上游完整收录后，再检查未改动文件中的反向引用。最后，针对更新后的依赖锁运行以下检查：

```bash
qatlib-env promotion check-retired --package MyLib
qatlib-env conflicts --package MyLib
```

| 检查 | 核验内容 |
| --- | --- |
| `verify-lock` | 清单锁定的完整 Git SHA 与该依赖的实际检出一致 |
| `prepare` | 按锁准备依赖源码及所配置的缓存 |
| `check-retired` | 按库根匹配本地残留的同路径模块与重复的显式公开声明 |
| `conflicts` | 扫描跨库模块、显式公开声明与全局语法的冲突，合法的重载需单独复核 |
| 项目静态检查、构建和消费者探针 | 按各仓标准核对目录、引用、元数据及接口可用性；可通过 `qatlib-env ci lint` 和 `qatlib-env ci build` 接入项目流程 |

只有将依赖锁、接口调用、旧副本清退与书目变更放在同一张验证通过的 PR 中同步切换，下游主线才能持续提供完整可用的接口。以上命令均为各自独立的准备与检查步骤，遇到失败应修复后再行合入。
