# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

`qatlib-tools` 是支撑多仓协同开发、门禁验证与公开发布同一套 Lean 数学库的共享工具链，同时也是一份沉淀一线排障经验的工程学习手册。安装后在任意终端运行 `qatlib-env docs`，即可离线阅读全文。

```text
成果晋升流向   下游研究仓 ──▶ example-library（薄中转） ──▶ qatlib-staging（预发整包验收） ──▶ qatlib（正式发布 + 预编译缓存）
正式消费关系   下游研究仓、example-library 均通过精确锁单向消费 qatlib，正式发布后清退已晋升旧源
工具版本绑定   各仓按需在配置与工作流中固定 qatlib-tools 提交版本
```

下游研究仓负责探索具体问题；`example-library` 汇集可复用数学成果与结构化元数据，定位为持续向公开库晋升并清退旧源的薄中转仓；`qatlib-staging` 接收整文件依赖闭包并执行预发全套门禁；`qatlib` 对外发布稳定接口、文档站点与预编译缓存。各仓的接收范围、数学契约与元数据均由对应仓库自行维护。

### 单入口分工与最小操作动线

| 命令入口 | 核心职责 |
| --- | --- |
| `qatlib-env` | 依赖与缓存准备、受控构建与资源池准入、跨库冲突与晋升检查、工作区维护及离线手册（`qatlib-env docs`） |
| `qatlib-retrieve` | 纯只读本地源码与书目检索：定位 Lean 声明、书写签名、上下文 `variable` 前提与文献引用（不启动编译器） |
| `qatlib-forge` | 同源安全的 GitHub / Gitea 协作托管接口（PR、Issue、CI 运行与日志读写），免除外部托管 CLI 版本依赖 |

```bash
# 1. 按锁准备依赖与预编译缓存
qatlib-env prepare
# 2. 建立本地索引，再只读检索已有定理与签名
qatlib-retrieve index
qatlib-retrieve query 'postselection'
# 3. 评估改动波及范围（只读）
qatlib-env plan ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
# 4. 在依赖工件就绪后，受控检查单个文件
qatlib-env run -- lake --no-build lean ExampleLibrary/Foundations/ScaledSqrt/SqrtBounds/RootCountOrder.lean
```

（底层 Python 包 `qatlib_search` 亦可供各仓门禁脚本直接调用词法解析、文档引用校验与晋升闭包分析函数。）

[五分钟上手](#quickstart) · [跨仓晋升](#promotion) · [工程实战手记](#field-notes) · [CLI 速查](#cli-reference) · [配置优先级](#configuration) · [Runner 部署](#runner)

---

<a name="quickstart"></a>

## 五分钟上手

以下操作以具备访问权限的 `example-library` 检出目录为例。开始前请备好 Git、Python ≥ 3.11、`uv` 与 `elan`，确认项目 `lean-toolchain` 可正常调用，且 Git 已配好访问私有依赖所需的 SSH 密钥或 HTTPS 凭据助手。示例基于 Lean `v4.35.0` 的 `RootCountOrder` 模块（首次拉取大型上游缓存的耗时取决于本机已有工件与网络带宽）。

### 1. 安装项目锁定的工具链

在 `example-library` 根目录创建虚拟环境并安装 `./python` 包，其 `python/pyproject.toml` 会自动按锁定提交拉取配套版本的 `qatlib-tools`：

```bash
uv venv .venv
uv pip install --python .venv/bin/python ./python
source .venv/bin/activate
qatlib-env --help
qatlib-retrieve --help
```

Windows PowerShell 下使用 `.venv\Scripts\Activate.ps1` 激活环境，并将安装目标改为 `.venv\Scripts\python.exe`。若需在独立环境中单独测试或装入指定提交构建的 `qatlib-tools` 分发包，可执行 `uv pip install --python <环境Python路径> /path/to/qatlib_tools-*.whl` 并核对分发包对应的提交哈希。

### 2. 配置个人构建资源预算

编辑个人配置文件 `~/.qatlib/env.toml`（建议目录权限 `0700`、文件权限 `0600`，敏感凭据单独存放在仓外密钥目录）。请结合宿主机与当前容器/cgroup 的真实可用余量调整数值：

```toml
local_pool_mib = 8192
local_pool_cpu = 4

[resources]
jobs = 1
lean_threads = 2
memory_budget = "4GiB"
```

```bash
qatlib-env config explain
```

重点核对输出中的三处信息：`values` 与 `sources`（确认并行度、线程数和单任务内存确实命中你的配置文件）、`configuration`（共享池总额度），以及 `resources`（工具实时探测到的 CPU 与内存安全余量）。在 Windows 上运行受管构建时，须在 `[resources]` 下显式加上 `heavy_patterns = []`，具体机制见[手记五](#note-concurrency)。

### 3. 准备依赖并受控检查模块

```bash
qatlib-env prepare
qatlib-env promotion verify-lock --package Qatlib
qatlib-env plan ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
qatlib-env run -- lake build ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
```

- `prepare` 会严格按 `lake-manifest.json` 物化依赖检出并恢复匹配的预编译工件；无网环境下可使用 `prepare --offline`（前提是源码、工具链与缓存已就位）。
- 执行构建前先跑 `plan`，重点看 `cold_or_rebuild_modules`、`reverse_invalidated_modules` 与 `reasons`：若建议 `mode` 显示为 `cloud`，应先排查锁版本失配、缺失缓存或本机预算限制，再决定是否交由远端 CI 执行整包编译。
- 当依赖模块的编译产物均已就绪、只需快速迭代验证手头正在修改的单个 `.lean` 文件时，优先使用单文件无构建检查（若上游产物缺失，Lake 会直接报错提示先补齐依赖）：

```bash
qatlib-env run -- lake --no-build lean ExampleLibrary/Foundations/ScaledSqrt/SqrtBounds/RootCountOrder.lean
```

### 4. 检索已有定理并回源核对前提

动笔写新引理前，先用 `qatlib-retrieve` 查清公开库与当前仓是否已有现成接口：

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization error' --kind theorem --limit 10
qatlib-retrieve query 'postselection' --cite GilyenSuLowWiebe2019 --limit 10
qatlib-retrieve find Qatlib.Concepts.BlockEncoding.Postselection.normalize_error_of_gap
```

完成建索引后，第一条查询按数学关键词检索后选择归一化误差界，第二条按书目键 `GilyenSuLowWiebe2019` 收窄候选，第三条精确读取目标声明。顺着返回的文件路径与行号打开 `.lake/packages/Qatlib/Qatlib/Concepts/BlockEncoding/Postselection.lean`（参见[公开库固定版本源码示例](https://github.com/quantum-atlas-ai/qatlib/blob/4e8ad0ff0c86d3f869146d488f93ab1a3eec961f/Qatlib/Concepts/BlockEncoding/Postselection.lean#L52)），不仅能看到显式假设 `‖y - x‖ ≤ η`、`η < ‖x‖` 与结论 `y ≠ 0` 及归一化距离上界，还能一并核对所在 `section` 隐式注入的赋范群与实赋范空间类型类实例。确认适用后，在普通 `module` 文件中窄导入该叶模块交给 Lean 编译器做最终类型核验（模块级书目键仅标识学术背景，具体定理与原论文命题的对应精度仍以源码注释和原文为准）。

**检索器工作边界**：

- `index` 扫描当前仓 `lakefile.toml` 声明的正式库根、`lake-manifest.json` 中已物化的上游包源码及各自的 `docs/references.bib`，索引默认写入用户缓存目录 `qatlib/retrieve/`（可通过 `--db <路径>` 指定仓外路径，跨目录调用传 `--project <目录>`，改完代码后运行 `qatlib-retrieve index --changed` 增量刷新）。
- 语料遍历在进入目录前排除各源码仓内部的 `.lake`、`.git`、`.venv` 与 `.agent-tasks`，正式库根下嵌套的派生目录也不会扫描。锁定依赖虽物化在消费仓的 `.lake/packages/` 下，仍按各自源码仓的正式 Lake 库根发现语料，再排除其内部缓存；这不等于允许递归扫描消费仓整棵 `.lake`。
- `query` / `find` 全程纯只读：不启动 Lean 或 Lake、不联网拉依赖、不占用构建池租约，也不会静默改写索引；索引缺失或命中文件发生修改时会明确提示刷新。
- 检索基于静态源码词法分析：由宏动态展开、结构体投影或编译器自动派生的声明可能不在词法索引内；脚本调用可加 `--json` 获取结构化输出，配合 `--repo`、`--kind`、`--cite` 过滤，或通过 `--map <文件>` 挂载消费仓导出的 `qatlib-retrieve-map/v1` 映射文件补充领域别名与书目关联（工具本身不耦合任何项目的私有 `metadata/` 目录）。

---

<a name="promotion"></a>

## 跨仓晋升与门禁对齐

### 1. 提取整文件依赖闭包

晋升以可复用的叶模块为起点，沿仓内导入链向下闭包收集全部依赖。**最小迁移单元是完整的 `.lean` 源文件**（连同文件内的定义、类型类实例、英文文档注释与私有辅助引理一并迁移，不做半文件拆解）。若遇到依赖缺失、循环依赖或目标路径已存在不同实现，须先由上下游维护者对齐接口设计，并如实记录留在原仓未晋升的模块与原因；开发仓自身的结构化元数据与生成页按其仓内规范同步刷新（参见 [Dev 晋升接收规范](https://github.com/quantum-atlas-ai/example-library/blob/ad7eccff36857b11d6cdf1bcf337bcb77f9e2ef0/.agents/skills/qatlas-promote-to-staging/SKILL.md)）。

在 Lean 4.35 `module` 体系下，`module` 文件只能导入同样启用了 `module` 的文件，因此模块迁移必须自底向上推进：文件内声明默认模块私有，公开 API 须置于 `public section` 或标明 `public`，跨模块重导出使用 `public import`；若下游还需要跨模块展开定义体做计算归约，则须额外核对 `@[expose]` 标记。切忌只用旧式非 `module` 脚本去测试新模块——旧格式导入方默认能穿透读取私有环境，容易掩盖真实消费者会撞上的可见性回归（参见 [Lean 4.35 模块系统参考](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/) · [模块迁移指南](https://leanprover-community.github.io/extras/module-system-quickstart.html)）。

### 2. 在预发仓验证真实消费者契约

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

### 3. 公开仓正式发版后再升锁清退

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

---

<a name="field-notes"></a>

## 工程实战手记（Engineering Field Notes）

下面五篇手记全部整理自多仓协作与自托管构建运维中真实踩过的坑。遇到相似故障症状时，建议先看清底层机制，再按给出的边界选对解法。

### 一、为什么只检查一个单文件，却估算出上万模块冷编译？

- **现象**：只改了某份文件寥寥数行，或随手写了一个几行的测试探针，`qatlib-env plan` 却报出成千上万个模块待重编，甚至直接触发 `cold_limit` 或 `reverse_limit` 预算拦截。
- **底层原理**：静态预算器在回答一个保守问题——“在当前不确定的缓存状态下，这次改动最坏会波及多大范围？”这里有两个放大器：① **正向全库展开**：只要受检文件导入了顶层根聚合入口（如 `import Qatlib` 或 `import ExampleLibrary`），正向依赖闭包就会瞬间铺满全库；② **反向消费者传染**：预算器会从闭包中缺少本地工件或已发生修改的节点出发，沿着反向导入边搜出仓内所有下游消费者——一旦某个改动令根聚合入口失效，所有导入了根聚合的消费者及其关联依赖都会被一并卷入估算集合。注意静态波及估算不等同于 Lake 实际编译数，Lake 最终是否重编某个目标还取决于细粒度的导出接口与 `depHash`。
- **标准解法与适用边界**：
  1. 编写验证探针或普通模块时，**只窄导入实际用到的叶模块**（例如 `import Qatlib.Concepts.BlockEncoding.Postselection`），严禁图省事导入全库根入口。
  2. 日常修改前先看 `qatlib-env plan` 输出的闭包列表、反向波及路径、锁状态与未解析导入；确认上游依赖工件均已由 `prepare` 恢复就绪后，用 `qatlib-env run -- lake --no-build lean <文件>` 直接对单文件做类型与证明检查。
  3. 若修改确实位于底层公共模块、静态反向波及面很大，应直接交由远端 CI 执行完整闭包验证；仅在具备硬资源隔离与充足配额的场景下才按流程对单次命令显式传入 `--allow-local-heavy`（该参数只放开冷编模块数门槛，绝不会突破共享池或内核内存硬上限）。

<a name="note-memory"></a>

### 二、为什么整机还有几百 GiB 空闲内存，构建却迟迟等不到内存准入？

- **现象**：在宿主机上看物理内存充裕，也没有任何高 RSS 的编译进程在跑，但新提交的构建任务却一直卡在准入等待，最终报资源准入超时。
- **底层原理**：Linux cgroup v2 的 `memory.current` 对当前控制组及其全部后代统一记账，**既包含进程匿名内存（`anon`），也包含内核文件页缓存（`file`）**。在密集解包 `.ltar` 缓存归档、读写成百上千份 `.olean` 工件后，哪怕编译进程早已退出，数十 GiB 的文件页缓存仍可能常驻在该 service 所属的 cgroup 账上。假如某层 cgroup 设了 `memory.high = 40 GiB`，而页缓存让 `memory.current` 停在 `36 GiB`，那么该层账面剩余额度就只有 `4 GiB`——哪怕宿主机还有上百 GiB 空闲、或者外层 `memory.max` 设得更高，准入器逐层向上核查所有可见祖先时，依然会被最紧的这 `4 GiB` 卡住（参见[内核 cgroup v2 内存文档](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory)）。
- **为什么不能直接拿 `current - inactive_file` 放宽硬准入？** 排查时沿着目标 service 或进程的 cgroup 逐层读取 `memory.current`、`memory.high`、`memory.max`、`memory.stat`、`memory.events` 与 `memory.pressure`，对照 `anon`、`file`、`shmem`、`inactive_file`、`file_dirty` 和 `file_writeback`，确实能一眼分清“真正在跑的进程内存”和“历史留下的页缓存”。但 `inactive_file` 只是内核 LRU 链表的冷热分类，不是“立即可无损释放”的承诺（其中可能有正在回写或仍被映射的文件页，且多个统计文件之间并非原子快照）；`shmem` 虽计入 `file`，却位于 anon LRU 链表，不能把类型计数与 LRU 计数混为一谈。若前置估算擅自扣除 `inactive_file`，不仅会把未必能收回的内存当成确定配额放行，还会与执行后端对原始 `max`/`current` 的硬检查产生契约冲突。因此 `qatlib-env` 严格按各可见祖先 `high`/`max - current` 的最小非负值结合宿主 `MemAvailable`、预留额与共享池上限判定准入；一旦读到有限配额却缺失合法 `current`（或配额文件本身不可读/格式非法），探针一律按**零余量**保守拒绝，并在 `config explain` 的 `resources.limits` 中明确标出 `memory.error`（容器内不可见的宿主机外层祖先需在宿主侧核查）。
- **标准解法与适用边界（空闲维护窗口）**：
  1. **针对明确不再使用的冷归档/大产物文件（用户态精确提示）**：当归档已完整解包、所有写入句柄已关闭且近期不再读取时，可重新打开文件描述符调用 `posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)`（偏移与长度均传 `0` 代表整文件，局部释放须按页对齐；若有持久化需求须先由写者完成同步写回，未落盘脏页不会借此释放），随后关闭描述符并重新采样观察 `file` 回落情况。注意该调用会影响同一物理文件的其他并发读者，仅对确认冷置的文件使用（参见 [`posix_fadvise(2)`](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html)）。
  2. **针对特定空闲 cgroup 的定量回收（管理员维护操作）**：先暂停该 Runner 接收新任务并确认组内无活跃作业，再由具备权限的管理员向目标 cgroup 目录的 `memory.reclaim` 写入温和额度（如 `printf '%s\n' 512M > memory.reclaim`；内核实际回收量可能略多或略少，不足请求量时可能返回 `EAGAIN`），随后复查 `memory.current`、`memory.stat`、`memory.events` 与 `memory.pressure` 确认余量恢复后再重新放开调度。切勿把自动回收塞进逐次任务的热路径，更不要在共享宿主机上贸然执行全局 `drop_caches` 清空其他服务赖以维持性能的热缓存（参见 [`memory.reclaim`](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files) · [`drop_caches`](https://docs.kernel.org/admin-guide/sysctl/vm.html#drop-caches)）。

<a name="note-cancellation"></a>

### 三、为什么取消或超时后，机器仍在 90% 以上内存占用时反复抖动？

- **现象**：GitHub / Gitea 页面早已显示某次构建任务已取消或超时失败，Runner 服务状态也显示在线正常，但机器内存长期卡在 90% 以上剧烈回收抖动，排查进程表常能抓到挂在 `PPID=1` 下的孤儿 `lake` 或 `lean` 进程。
- **底层原理**：systemd 服务单元配置的 `KillMode=control-group` **只在整个 service 停机或重启时才会清扫控制组内的所有残留进程**；而自托管 Runner 是一个长期常驻的守护进程，网页端取消单次 CI job 只会让 Runner 终止顶层作业包装进程，并不会触发 systemd 停掉整个 Runner 服务！一旦深层派生的 `lake` / `lean` 子孙进程脱离父进程变成 `PPID=1` 的孤儿，它们就会留在原 service 的 cgroup 里继续狂吃内存：账面用量冲破 `MemoryHigh` 后内核启动高频直接内存回收与分配节流，但只要还没撞上 `MemoryMax` 硬上限触发组内 OOM，进程就会在“一边被强行节流回收、一边继续申请内存”的拉锯战中把整组拖入长时抖动（参见 [`systemd.kill`](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html) · [`systemd` 资源控制](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)）。
- **标准解法与适用边界**：
  1. **逐作业独立进程域与父死收割**：Linux 公共监督器为每个命令启动独立 guardian 进程，由 guardian 设置 `PR_SET_PDEATHSIG`，在父监督器被硬杀后接收信号并继续清理。只给 workload 本身设置父死信号，无法覆盖 double-fork 或 `setsid` 脱离会话的子孙；guardian 同时通过 `PR_SET_CHILD_SUBREAPER` 收养这些后代，按 PID 与启动时间跟踪进程身份，对已拥有的进程组和子孙执行 TERM、宽限期、KILL 与反复收割。正常结束、用户取消、单模块超时与整阶段（stage）超时共用这套清理流程，保留命令的 stdin、捕获输出与信号退出码契约。guardian 独立启动，避免在线程化父进程里使用可能死锁的 `preexec_fn`；共享机器上严禁仅凭 `lean`/`lake` 进程名通杀。
  2. **内核整组硬上限作为第二道防线**：受限命令进入逐作业瞬态 systemd scope，以 `MemoryMax`、`MemorySwapMax=0`、`CPUQuota`、`RuntimeMaxSec` 和 `KillMode=control-group` 对整组进程施加内存、CPU、运行期限和终止边界。即使 guardian 也被同时硬杀，内核与 systemd 仍可按有限资源和期限收束该 scope，不能只依赖常驻 Runner service 的停机清理。此保护要求 Linux 上实际可用且支持这些属性的 systemd 用户 CPU/内存控制器；属性或权限不支持时命令失败，不会静默退回无界执行。它不能保护宿主消失后的任务，也不代替外部容器 daemon 的生命周期管理。
  3. **临时实验、枚举与搜索也要接入边界**：普通 Python 和递归搜索同样可能派生残留进程或扫遍多工作树缓存。无需 Lean 项目即可使用 `qatlib-env exec -- <命令>`，默认期限 **300 秒、内存 4 GiB、1 CPU**；可用 `--timeout`、`--memory-mib`（至少 2048 MiB）与 `--cpu` 调整。它复用现有本机共享资源池的 CPU/内存租约，等待准入也有期限；请求超出池硬上限会直接拒绝。监督器超时返回 `124`，内核期限先触发时保留执行链的信号终止状态。现有 Lean 构建的预算与准入规则继续适用。这个入口需要调用方主动接入，无法自动截获外部 agent harness 的裸 shell；共享准入池也不是驻留调度平台，不新增配额数据库或 I/O 调速器。

     ```bash
     qatlib-env exec --timeout 300 --memory-mib 4096 --cpu 1 -- python experiment.py
     # 只搜索明确的源码目录，并显式裁剪派生树
     qatlib-env exec --timeout 60 -- rg --glob '*.lean' --glob '!.lake/**' --glob '!**/.lake/**' --glob '!.venv/**' --glob '!**/.venv/**' --glob '!.agent-tasks/**' --glob '!**/.agent-tasks/**' 'pattern' Source verification
     ```

     本工具仓根 `.ignore` 为遵循它的工具（如默认 `rg`）排除 `.lake`、`.git`、`.venv`、`.agent-tasks`、`dist` 与 `build`，但不会自动保护其他工作树，也不会替消费仓写配置。普通 `grep` / `find` 不读取 `.ignore`，`rg --no-ignore` 也会绕开它，调用方须显式排除 `.lake`、`.venv` 与 `.agent-tasks`；自写枚举脚本应在进入这些目录前剪枝，而不是扫描后再过滤结果。先避免无意义扫盘，再用有限 runtime 控制执行期限。
  4. **跨平台句柄绑定与残留取证**：Windows 受管构建模式通过启动即挂载 `Job Object` 实现整组 CPU/内存硬约束与生命周期绑定，关闭最后一个 Job 句柄即可由内核原子终止组内全部残留子进程（参见 [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)）；上述通用 `exec` 入口则仅支持 Linux，其他平台会明确拒绝。线上排查疑似孤儿进程时，应联合核对 PID、启动时间、完整命令行、工作目录与所属 cgroup，并对照 `memory.events` 中 `high` / `max` / `oom` 计数器的增长速率（单看某刻 `PPID=1` 只是线索；若涉及容器任务，还需结合外部容器 daemon 的真实命名空间核实）。

### 四、为什么网页能点开的文档引用，在 VS Code 悬浮窗里只显示纯文本？

- **现象**：Lean docstring 里写的声明引用在生成的 API 网站上能正常点击跳转，但在 VS Code 里鼠标悬停看同一段文档却只是普通代码文本；或者门户页面上的参考文献链接点开后能进 `references.html`，页面却没有滚动定位到那篇论文。
- **底层原理**：这两处看到的是两条完全独立的渲染管线：① `doc-gen4` 在离线构建整站 HTML 时持有全库模块符号表与完整书目索引，能把匹配的标识符和书目键批量改写为静态网页超链接——且其参考文献页为每条书目生成的真实 HTML 锚点统一带有 `ref_` 前缀（形如 `#ref_<CiteKey>`；若外层页面手拼成 `references.html#SuksmonoMinato2019`，浏览器能打开页面，但因找不到精确匹配 `ref_SuksmonoMinato2019` 的元素 `id` 而停在页首）；② VS Code 悬浮窗则是向 Lean LSP 发送 `textDocument/hover` 请求，由编译器工作进程根据当前局部环境实时拼出一段 Markdown 返回，LSP 进程手里既没有整站静态路由表，也没有外部 BibTeX 数据库（参见 [`doc-gen4`](https://github.com/leanprover/doc-gen4) · [Lean 4.35 hover 实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/Lean/Server/FileWorker/RequestHandling.lean) · [门户书目锚点修复实例](https://github.com/quantum-atlas-ai/qatlib-staging/pull/22)）。
- **标准解法与适用边界**：
  1. **日常双端兼顾写法**：普通 Markdown 注释中跨模块引用统一用反引号包裹**完整限定名（FQN）**；论文定理定位统一写 `[CiteKey], Theorem X`（`X` 填原论文真实编号，`CiteKey` 在根 `docs/references.bib` 登记）；若希望读者在编辑器悬浮窗里也能一键点开原论文，在模块头 `/-!` 中补上标准 Markdown 的 DOI 或 arXiv 链接即可。站点侧修改链接生成逻辑后，除跑静态词法引用门禁（拦截坏键与删源断链）外，还须实测最终 HTML 的 `href` 与目标 `id` 配对。
  2. **编译器级名称校验试点（`doc.verso`）**：Lean 4.35 支持在单模块内通过 `set_option doc.verso true` 启用 Verso 文档角色（如在 `RootCountOrder.lean` 中使用 `` {name}`ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.rootCount` ``），拼错常量名会在编译期直接报错 `Unknown constant`。扩大使用范围前，应严格沿用“编译期错拼负例 + LSP hover 显示 + 锁定版 `doc-gen4` 真实 HTML 锚点命中且摘要零原始 `{name}` 语法泄漏”三端联验标准（参见 [Lean 文档编写规范](https://github.com/leanprover/lean4/blob/v4.35.0/doc/style.md) · [单模块 Verso 试点实例](https://github.com/quantum-atlas-ai/example-library/pull/550)）。

<a name="note-concurrency"></a>

### 五、多工作树并行与“进程数 × 线程数”是怎么悄悄把机器打满的？

- **现象**：开两个分支同时干活时，两边 `.lake` 编译产物互相覆盖报错；或者看每个任务只开了寥寥几个构建进程，整机 CPU 却严重超卖、所有任务一起陷入龟速上下文切换。
- **底层原理**：这里叠加了文件状态与线程算力两层耦合：① **`.lake` 状态强绑定工作树**：`.lake` 目录下的依赖源码检出、`build` 产物、编译 trace 和本地锁与当前工作树的源码及 `lake-manifest.json` 严格一一对应；如果图省空间把两个工作树的 `.lake` 软链到同一处，任意一边的依赖切换或编译写盘都会直接破坏另一边的状态；② **两级并发度是乘法而非加法**：`jobs` 控制并行启动多少个进程，而 `lean_threads`（导出为 `LEAN_NUM_THREADS`）控制每个 Lean 进程内部开多少个工作线程——两个并发任务各自设 `jobs = 4`、`lean_threads = 4`，瞬时请求的逻辑核数就是 `2 × (4 × 4) = 32` 核，而不是直觉以为的 `8` 核！
- **标准解法与适用边界**：
  1. **工作树隔离 `.lake` + 内容寻址共享缓存**：用 `qatlib-env worktree` 创建隔离工作树，每棵树保有各自独立的 `.lake` 目录，底层通过工具统一管理的内容寻址工件缓存与本地 Git 对象镜像复用数据（注意：本地镜像库里有 Git 对象只省网络下载，工作树内仍须经 `prepare` 按锁实际检出依赖文件）。
  2. **统一租约账本与各平台后端真实能力边界**：让同一台机器上的所有工作树指向同一份共享池账本，按 `jobs × lean_threads` 核算 CPU 租约并登记每次任务的内存预算（外层由系统父 slice / service / 容器兜底硬边界，内层由工具共享池协调跨树排队）：
     - **Linux / POSIX**：除共享池外，编译器包装层还支持细粒度的单进程内存参数注入与重型模块并发槽位限制（`heavy_patterns` / `heavy_jobs`）。
     - **Windows**：依托内核 `Job Object` 对整次构建实施聚合 CPU 与内存硬上限，因不使用 POSIX 符号链接编译器包装，不支持单编译器重模块槽位拆分——须在配置中显式设 `heavy_patterns = []` 选择该受管模式（若配置了非空重模块规则，工具会明确报错拒绝而非静默降级）。
     - **macOS**：后端仅能调整进程调度优先级，操作系统缺乏同等的进程组聚合内存硬上限接口，因此不可用于承载要求严格内存硬隔离的本机重型构建任务。

---

<a name="cli-reference"></a>

## CLI 速查

| 常见场景 | 命令入口 | 读写与执行边界 |
| --- | --- | --- |
| 核对生效配置与探测到的资源余量 | `qatlib-env config explain` | 纯只读，输出各项最终生效值与配置来源层级 |
| 诊断缓存命中与共享池当前租约 | `qatlib-env cache explain`、`qatlib-env pool status` | 纯只读，排查缓存路径与并发排队情况 |
| 离线查阅本手册 | `qatlib-env docs` 或 `qatlib-env docs --path` | 纯只读，直接打印手册全文或返回打包文件路径 |
| 创建带缓存准备的隔离工作树 | `qatlib-env worktree <路径> <分支>` | 新建独立工作树并默认引导缓存（可加 `--no-bootstrap`） |
| 预估波及范围与受控执行 | `qatlib-env plan <模块>`、`qatlib-env run -- <命令>`、`qatlib-env check <模块>` | `plan` 默认只读；`run` / `check` 受共享池与资源硬上限准入保护 |
| 临时实验、枚举与递归搜索 | `qatlib-env exec -- <命令>` | 通用命令受本机共享池准入与 Linux 内核 scope 的时间、内存、CPU 硬上限约束；无可用控制器时拒绝执行 |
| 恢复依赖缓存或显式发布缓存 | `qatlib-env prepare`、`qatlib-env cache upload --help` | `prepare` 按锁物化并拉取只读缓存；`upload` 仅在获授权的发布流程中使用 |
| 超出本机预算转交远端执行 | `qatlib-env cloud submit --head <分支>` | 显式派发远端运行，可配合 `status`、`logs`、`wait`、`fetch` 跟踪 |
| 跨仓定向升锁、查重与冲突检查 | `qatlib-env promotion --help`、`qatlib-env conflicts --package <包名>` | `bump-lock` 更新指定包锁版本；其余锁/清退/冲突检查只读，`smoke` 实际调用 Lean |
| 建立或增量刷新本地检索索引 | `qatlib-retrieve index [--changed]` | 仅写入用户缓存目录下的独立检索库，不碰项目工作区 |
| 检索源码声明、签名与书目关联 | `qatlib-retrieve query <文本>`、`qatlib-retrieve find <文本>` | 纯只读（`query` 子命令词可省略，亦支持 `python -m qatlib_search`） |
| 访问 GitHub / Gitea 协作接口 | `qatlib-forge pr list --platform github --repo quantum-atlas-ai/qatlib` | 读取需对应仓库权限；创建/评论 PR 或 Issue 等写操作须显式调用对应子命令 |
| 扫描本地多个工作区状态 | `qatlib-env workspace scan <目录> --json` | 默认纯本地扫描，仅在显式加 `--check-remote` 时查询远端分支状态 |
| 安全预览并回收缓存或旧工作树 | `qatlib-env workspace clean-caches --checkout <路径> --dry-run` | 先 `--dry-run` 审阅清单（整树回收用 `trash-checkouts`），确认后再加 `--apply` 移入系统回收站 |

> **检索命令迁移提示**：旧版 `qatlib-search build/query/retrieve` 索引格式已退役，请直接使用 `qatlib-retrieve index` 重新初始化并用 `query` / `find` 查询（无需迁移旧索引文件）。若上游依赖尚未物化，请先运行 `qatlib-env prepare`；检索结果仅供定位源码，不能替代实际编译与晋升门禁。

---

<a name="configuration"></a>

## 配置优先级

下列配置层按优先级**从低到高**排列，同名配置键由高层覆盖低层，随时可通过 `qatlib-env config explain` 核查实际生效值与来源路径：

| 优先级（低 → 高） | 来源位置与设计职责 |
| --- | --- |
| 1. 默认值与项目推导 | 根据宿主与 cgroup 实时探测结果给出保守默认值；主库根、测试根与 `srcDir` 从 `lakefile.toml` 动态解析，仓库标识与工作流从项目目录自动推导 |
| 2. Runner 兼容配置 | 环境变量 `QATLAS_RUNNER_CONFIG` 指向的实例配置文件 |
| 3. 个人本机设置 | 先读兼容路径 `${XDG_CONFIG_HOME:-~/.config}/qatlib/env.toml`，再读标准个人配置 `~/.qatlib/env.toml`（存放与机器规模绑定的并发池与单任务资源） |
| 4. 项目共享策略 | 先读仓库根 `pyproject.toml` 的 `[tool.qatlib-env]`，再读根 `qatlib-env.toml`；二者均不存在时回退读取 `python/pyproject.toml` |
| 5. 工作树本地覆盖 | 工作树根目录 `.qatlib-env.local.toml`（次级 `worktree` 未单独放置时，可只读继承主检出的同名文件） |
| 6. 环境变量覆盖 | 如 `QATLAS_POOL_BUDGET_MIB`、`QATLAS_POOL_CPU`、`QATLAS_JOBS`；线程数变量优先级为 `QATLAS_LEAN_THREADS` 高于 `LEAN_NUM_THREADS` |
| 7. 单次命令行参数 | 如 `qatlib-env run --jobs 1 --lean-threads 2 -- lake build <模块>` |

当项目需要统一限制本地冷编译与反向波及模块数（而不绑定具体机器核数或内存大小）时，可在仓库中仅提交如下预算策略（若写入 `pyproject.toml`，表头对应改为 `[tool.qatlib-env.budget]`）：

```toml
[budget]
max_cold_modules = 15
max_reverse_modules = 15
require_explicit_local_heavy = true
```

- 将阈值设为 `0` 表示禁止对应类别的本地冷编或反向波及；临时调试可通过环境变量 `QATLAS_MAX_COLD_MODULES`、`QATLAS_MAX_REVERSE_MODULES` 覆盖。配置文件若出现未知键名或类型不匹配，工具会直接报错指出具体文件与字段路径。
- `--allow-local-heavy` 仅对当次命令行调用生效，且依然受共享池容量、系统 cgroup 控制器与超时上限约束；`QATLAS_POOL_WAIT_TIMEOUT` 仅调整排队等待已有租约释放的超时时间，一旦单次请求本身超出池硬上限仍会立即拒绝。
- 上游依赖版本由 `lake-manifest.json` 唯一锁定，远端缓存端点由 `lake-cache.toml` 配置，本地工件存储目录由 `LAKE_CACHE_DIR` 指定；并发租约账本按 `XDG_STATE_HOME` / `XDG_CACHE_HOME` 定位（需要共享机器配额的多任务须指向同一账本路径）；`qatlib-retrieve` 独立按 Lake 配置发现语料并通过 `--project` 与 `--db` 管理索引位置。

---

<a name="runner"></a>

## 自托管 Runner 部署核对单

1. **核实系统控制器与信任边界**：在 Linux / WSL2 上确认服务由 `systemd` 托管，且 `stat -fc %T /sys/fs/cgroup` 返回 `cgroup2fs`；厘清容器、WSL 虚拟机与物理宿主机三层各自的内存上限。为每个 Runner 实例分配独立工作目录、独立注册凭据与隔离运行账号，严禁将未受审的外部不受信任代码接入私有高权限 Runner。
2. **审阅安装脚本与双阶段职责**：在锁定的 `qatlib-tools` 源码检出中运行 `bash src/qatlib_env/setup_runner.sh --help`（若为 wheel 安装环境，可运行 `bash "$(python -c 'from pathlib import Path; import qatlib_env; print(Path(qatlib_env.__file__).with_name("setup_runner.sh"))')" --help`）。该脚本面向 Ubuntu / Debian + `systemd` 环境：`init` 负责配置宿主依赖、专用运行账户与共享父 `slice`；`register` 负责注册单个 Runner 实例并写入服务级资源限制。执行前务必人工核对目标路径、凭据范围与配额数值。
3. **协同预算父 `slice` 与单实例配额**：在服务 drop-in 配置的 `[Service]` 段同时声明 `Slice`、`CPUQuota`、`MemoryHigh`、`MemoryMax`、`TasksMax` 与 `KillMode=control-group`，确保各实例并发峰值之和与父 `slice` 上限相匹配，并为 Runner 自身进程、文件页缓存及同机其他服务留足安全余量。落盘重载后，执行 `systemctl show <服务> -p ControlGroup -p Slice -p EffectiveMemoryHigh -p EffectiveMemoryMax -p KillMode` 读回核实，并直接检查对应 `/sys/fs/cgroup/...` 节点的实时计费。
4. **切换至实际运行账户做准入验收**：在 Runner 实际服务用户下运行 `qatlib-env config explain` 与 `qatlib-env pool status`。Linux 重型任务优先走受限的 `systemd` 用户态瞬态 scope；若受环境所限只能沿用外层既有 cgroup，执行后端会强制校验有限的 `memory.max`、CPU 配额及所有可见祖先层的真实余量——仅配置软水线 `memory.high` 或 CPU 亲和性（affinity）不满足硬隔离准入条件。
5. **在测试泳道完成超时、取消与子孙收割演练**：正式放量前，先用会派生多层子孙进程的测试任务逐一演练正常退出、手动取消、单模块超时与整阶段超时四条路径；每次结束后复查进程表、实际 cgroup 残留、共享池租约归零情况以及 `memory.events` 计数变化。一旦观测到异常高水位，对照[手记二](#note-memory)与[手记三](#note-cancellation)区分是冷页缓存驻留还是孤儿进程泄漏，全部验收干净后再按设计并发度逐步上线。

---

**工具仓自测与离线手册一致性校验**：修改本仓库后可执行 `uv lock --check`、`uv build` 与 `uv run --with pytest --with hatchling --with jsonschema pytest -q -rs`。构建 wheel 时，根 `README.md` 会按原始字节打包为 `qatlib_env/_manual.md`；在干净虚拟环境装入 wheel 后，可直接比对 `qatlib-env docs` 输出与根 `README.md` 原文是否逐字节一致。

本项目采用 [Apache License, Version 2.0](./LICENSE) 开源许可，与公开库 [qatlib](https://github.com/quantum-atlas-ai/qatlib) 保持一致。
