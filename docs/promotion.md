# 共享成果如何进入公开库

## 收集整文件依赖

晋升以可复用的叶模块为起点，沿仓内导入链向下闭包收集全部依赖。**最小迁移单元是完整的 `.lean` 源文件**（连同文件内的定义、类型类实例、英文文档注释与私有辅助引理一并迁移，不做半文件拆解）。若遇到依赖缺失、循环依赖或目标路径已存在不同实现，须先由上下游维护者对齐接口设计，并如实记录留在原仓未晋升的模块与原因；开发仓自身的结构化元数据与生成页按其仓内规范同步刷新（参见 [Dev 晋升接收规范](https://github.com/quantum-atlas-ai/example-library/blob/ad7eccff36857b11d6cdf1bcf337bcb77f9e2ef0/.agents/skills/qatlas-promote-to-staging/SKILL.md)）。

迁移模块时从叶文件开始，并核对实际消费者的公开接口。详见[模块的公开接口](lake.md#modules)。

## 预发仓的消费者验证

晋升书目时，在接收仓根目录运行 `qatlib-env promotion merge-bibliography --increment /path/to/dev/docs/references.bib` 按键预检；确认后加 `--write`，工具会保留上游 `docs/references.bib` 原有字节并追加新增条目原文，同键异文会阻止写入。开发仓只保留尚未公开的增量书目，允许清退后为 0 条目；引用校验叠加锁定 Public 基线。

`qatlib-staging` 除了运行全套 `Lint` 与 `Build`，还应在普通 `module` 环境下以最小窄导入验证公开接口的实际可消费性（例如编写探针 `verification/Consumer.lean`）：

```lean
module
import Qatlib.Concepts.BlockEncoding.Postselection

#check Qatlib.Concepts.BlockEncoding.Postselection.normalize_error_of_gap
```

```bash
qatlib-env promotion smoke --probe verification/Consumer.lean
```

正式晋升探针建议用独立 `example` 显式复述关键定理签名：对承诺可计算的函数，用普通 `def` 调用并在小规模输入上验算；对承诺可展开的定义，用 `rfl` 或受限 `simp` 验证归约；全程使用普通 `import`，禁止用 `import all` 或套一层 `noncomputable` 掩盖接口退化。`promotion smoke` 会在回执中记录精确 HEAD、工具链与诊断详情，明确区分 `PASS`、`REGRESSION` 与 `NOT-RUN`。若晋升包含有意的规范化改名，可通过 `--migration-map migration.toml` 登记 `[[rename]]`（含 `old`、`new`、`reason`）供下游适配参考，最终门禁结论仍以真实编译结果为准。

## 发布后的升锁和清退

预发仓合入后，下游可在独立草稿分支提前演练路径切换，但在公开仓正式发版且对应预编译缓存就绪前，开发仓主线不得提前删源断供。待新版本 `Qatlib` 及其缓存正式可消费后，按序执行定向升锁与源码清退：

```bash
qatlib-env promotion bump-lock --package Qatlib
qatlib-env promotion verify-lock --package Qatlib
qatlib-env prepare
```

随后将仓内受影响的 `import`、`open`、完整限定名（FQN）、测试及元数据引用统一切至新公开接口，清退已发布的旧 `.lean` 源文件与空壳模块，并同步核对 `docs/references.bib`（仅清退上游已完整收录且内容一致的书目键，并反查未改动文件是否仍引用已移除的声明或书目键），最后在精确新锁上跑通查重与跨库冲突检查：

```bash
qatlib-env promotion check-retired --package Qatlib
qatlib-env conflicts --package Qatlib
```

| 门禁环节 | 核心核验内容 |
| --- | --- |
| `verify-lock` | 校验 `lake-manifest.json` 记录的完整 SHA 与本地实际检出的依赖源码树严格一致 |
| `check-retired` | 拦截归一化库根下尚未清退的同名重复公开声明或整文件残留副本 |
| `conflicts` | 扫描跨库模块重名、显式公开 FQN 碰撞与全局语法注册冲突（合法符号重载由人工复核） |
| 项目静态门禁 | 校验目录导入层级、结构化元数据、自动生成文档、增量书目及被删提供者的反向引用 |
| 受控 Build、`EnvLint` 与内核审计 | 编译全量消费者，在内核层验证零 `sorry` 与公理白名单契约（覆盖构造器、投影与宏生成项） |
| 普通 `module` `smoke` | 从外部普通模块视角提前拦截公开接口签名、可计算性或定义展开可见性的无意退化 |

外层 CI 工作流可直接调用 `qatlib-env ci lint` 与 `qatlib-env ci build`（完整构建按 Runner 预算对该次调用显式传入 `--allow-local-heavy`；增量环境审计 `python -m qatlib_env.check_scoped_env_lint --base "$BASE_SHA"` 须确保 `BASE_SHA` 在本地 Git 对象库中真实可读）。
