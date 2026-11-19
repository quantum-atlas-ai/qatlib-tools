# 共享成果如何进入公开库

## 手册与各仓晋升说明的分工

这里说明一项成果从孵化仓进入预发验收仓、再随正式版本进入公开库的通用技术步骤。整文件依赖、普通 Lean 模块的消费者探针、增量书目归并和跨库冲突检查都适用于不同项目。具体目录、元数据、评审角色与准入标准由各仓自己的 `promote` skill 维护，并从那里链接本手册。

## 第一阶段：迁移整文件并在预发仓验证

从拟公开的模块出发，沿项目内的 `import` 递归收集依赖，区分已由接收仓或其外部依赖提供的模块。按完整的 `.lean` 文件迁移，将实例、文档注释与私有辅助引理一起保留。遇到缺少依赖或目标路径已有不同实现，先解决接口冲突，再继续迁移。对留在原仓的依赖和未迁移模块，记录原因供接收方复核。

有项目适配器时，可调用 Python 接口 `qatlib_search.promotion_closure.candidate_closure`。调用方先验证依赖锁，再传入候选模块、源码库根和适用的导入规则。接口只读源码，返回依赖在前的整文件清单和各文件的导入关系。查到错误时只返回诊断和已读到的导入关系，迁移清单为空。

在预发仓根目录运行 `qatlib-env promotion merge-bibliography --increment /path/to/increment/references.bib` 预检。加 `--write` 后，工具保留目标 `docs/references.bib` 的原有字节，只追加未收录的条目原文。同键异文会阻止写入。增量仓仍用锁定的公开书目和自己的增量一起验证引用。 [增量书目](lean-docs.md#bibliography)

预发验收同时运行项目静态检查和构建，并从普通 `module` 的视角检查外部可用的接口。例如在 `verification/Consumer.lean` 里导入并使用拟发布的模块。

```lean
module
import MyLib.Facts

#check MyLib.Facts.some_fact
```

```bash
qatlib-env promotion smoke --probe verification/Consumer.lean
```

探针应以尽可能少的导入测试真实用法。用 `example` 复述关键定理的签名，用普通 `def` 调用承诺可计算的函数，用 `rfl` 或受限 `simp` 验证承诺可展开的定义。普通 `import` 让探针遵守下游实际的可见性规则。 `smoke` 使用已有依赖工件检查 Lean 文件，记录 HEAD、工具链和诊断，区分 `PASS`、`REGRESSION` 与 `NOT-RUN`。有意改名时可用 `--migration-map migration.toml` 记录 `[[rename]]` 的 `old`、`new`、`reason`。 [模块可见性](build.md#modules)

<a id="check-export"></a>

### 检查仅供开发期使用的旧接口

改名时，可按项目约定把旧别名或测试转发文件保留一个发布周期，给尚在进行的工作留出切换时间。用独立行注释 `-- promotion: dev-only` 标记这些临时项，并在本批迁移清单里记录旧名、新名和路径。导出到接收仓时，去掉标记的临时别名和转发文件，并让消费者改用新接口。公开库按自身兼容政策使用的 `@[deprecated]` 声明可以照常保留。

```bash
qatlib-env promotion check-export MyLib/Facts.lean verification/Consumer.lean
```

向命令显式传入本批导出的全部目标 Lean 文件，包括尚未纳入 Git 的新文件。工具只读扫描真实的 Lean 行注释，忽略块注释和字符串里的示例。找到残留标记时输出文件、行号和错误，退出码非零。检查完成后再按迁移清单反查旧名和路径，确认没有漏标的过渡项，并运行普通模块的消费者探针。

## 提前演练下游切换

上游接收包进入预发分支后，下游可先建 Draft PR，在草稿里用候选依赖演练导入路径、限定名、探针和查重。按项目规则标明草稿所用的候选提交与预发验证结果。这样可以在正式发布前发现消费者的兼容问题。公开版本和对应预编译工件全部可用后，才可合并下游切换及删源。在此期间，下游主线继续保留现有实现和依赖锁。

## 第二阶段：按正式版本更新依赖锁并清退旧副本

先确认正式版本、完整提交 SHA 和对应预编译工件都可获取。将草稿切到正式依赖，检查定向更新的版本和锁文件差异。

```bash
qatlib-env promotion bump-lock --package MyLib
qatlib-env promotion verify-lock --package MyLib
qatlib-env prepare
```

`bump-lock` 更新既有直接 Git 依赖并核对更新后的检出。 `verify-lock` 要求能读取锁定的本地源码，因此在没有依赖检出的干净环境里，先运行 `prepare`，再核对锁。把锁定 SHA、缓存准备和构建结果对应到同一版本。

在同一张下游 PR 里，将 `import`、`open`、完整限定名、测试和项目元数据切换到公开接口，清退旧源码、临时别名与只剩空壳的模块。核对被删书目键已由上游完整收录，再检查未改动文件中的反向引用。最后对新锁运行以下检查。

```bash
qatlib-env promotion check-retired --package MyLib
qatlib-env conflicts --package MyLib
```

| 检查 | 核验内容 |
| --- | --- |
| `verify-lock` | 清单锁定的完整 Git SHA 与该依赖实际检出一致 |
| `prepare` | 按锁准备依赖源码和所配置的缓存 |
| `check-retired` | 按库根匹配残留的同路径模块和重复的显式公开声明 |
| `conflicts` | 扫描跨库模块、显式公开声明和全局语法的冲突。合法的重载需要复核 |
| 项目静态检查、构建和消费者探针 | 根据各仓标准核对目录、引用、元数据及接口可用性。可用 `qatlib-env ci lint` 和 `qatlib-env ci build` 接入项目流程 |

依赖锁、接口调用、旧副本和书目通过同一张验证通过的 PR 一起切换，主线才能持续提供可用接口。以上命令是各自独立的检查与准备步骤，遇到失败应修复后再合入。
