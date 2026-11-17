# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

`qatlib-tools` 帮助 Lean 项目共同开发、验证和消费同一套数学接口。这个仓库也是一份工程手册，记录实际遇到的构建、缓存和文档问题，方便在下次遇到相同症状时找到原因和处理步骤。安装后运行 `qatlib-env docs`，就能离线阅读本页。

```text
成果晋升   下游研究仓 → example-library → qatlib-staging → qatlib
正式消费   下游研究仓、example-library 分别通过精确锁消费 qatlib
共用工具   各仓按需固定 qatlib-tools 版本
```

下游研究仓探索问题，`example-library` 整理可复用成果、维护元数据，是持续晋升并清退旧源的薄中转仓。`qatlib-staging` 接收整文件闭包并验证，`qatlib` 发布正式接口和对应缓存。箭头表示成果的交接方向，各仓仍按自己的接收合同决定迁移范围。

| 入口 | 用途 |
| --- | --- |
| `qatlib-env` | 环境准备、受控构建、缓存、冲突和晋升检查，以及离线手册 |
| `qatlib-retrieve` | 本地只读查看 Lean 声明、书写签名、前提说明和文献引用 |
| `qatlib-forge` | 既有 GitHub / Gitea 协作脚本使用的 PR、Issue、运行与日志入口 |

共享 Python 包 `qatlib_search` 继续为各仓提供词法、文档引用与晋升门禁的基础函数。项目接收政策、数学合同和元数据由项目自身维护。

[五分钟上手](#quickstart) · [跨仓晋升](#promotion) · [工程实战手记](#field-notes) · [CLI 速查](#cli-reference) · [配置优先级](#configuration) · [Runner 部署](#runner)

<a name="quickstart"></a>

## 五分钟上手

下面在已有访问权限的 `example-library` 检出中操作。准备 Git、Python 3.11 及以上、`uv` 和 Elan，确认项目的 `lean-toolchain` 可用。私有依赖的 SSH 密钥或 HTTPS credential helper 要能被 Git 使用。示例对应 Lean 4.35.0 下的 `RootCountOrder` 模块，首次准备大库缓存的时间由本机已有工件和网络决定。

### 1. 装入项目锁定的工具

从 `example-library` 根目录运行，`python/pyproject.toml` 的直接依赖会拉取指定提交的 `qatlib-tools`。本教程需要该锁已采用提供 `qatlib-retrieve` 的版本。维护者审核工具升锁时，可先用待审提交的 wheel 在单独环境验证本页命令。

```bash
uv venv .venv
uv pip install --python .venv/bin/python ./python
source .venv/bin/activate
qatlib-env --help
qatlib-retrieve --help
```

Windows PowerShell 可用 `.venv\Scripts\Activate.ps1` 激活，并将安装命令中的 Python 路径改为 `.venv\Scripts\python.exe`。若只想给已准备好的 Lean 项目加上新版工具，可在独立环境中安装按完整 SHA 构建的 wheel。此时用 `uv pip install --python <环境的Python> /path/to/qatlib_tools-*.whl`，并核对 wheel 所对应的提交。

### 2. 配置个人构建预算

手动编辑 `~/.qatlib/env.toml`。下面的数值适用于一台有足够可用资源的机器，请根据宿主和当前 cgroup 的实际余量调整。建议目录权限 `0700`、文件权限 `0600`，凭据另行配置。

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

先看 `values` 和 `sources`，确认线程及任务内存来自预期配置。查看 `configuration` 中的池上限，以及 `resources` 中探测到的 CPU 和内存余量。Windows 需在 `[resources]` 中显式设置 `heavy_patterns = []` 以启用受管构建，原因见[手记五](#note-concurrency)。

### 3. 准备依赖并检查一个模块

```bash
qatlib-env prepare
qatlib-env promotion verify-lock --package Qatlib
qatlib-env plan ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
qatlib-env run -- lake build ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
```

`prepare` 按锁物化依赖并恢复可用工件。离线时可用 `prepare --offline`，所需源码、工具链和缓存须已在本机。计划里先看 `cold_or_rebuild_modules`、`reverse_invalidated_modules` 和 `reasons`。若 `mode` 为 `cloud`，先处理锁失配、缺失工件或资源限制，再决定是否交给项目 CI。缓存恢复成功后仍以实际编译退出码为准。

已有依赖工件时，可只检查正在改的源文件。缺产物会由 Lake 报错，补齐相应依赖后再检查。

```bash
qatlib-env run -- lake --no-build lean ExampleLibrary/Foundations/ScaledSqrt/SqrtBounds/RootCountOrder.lean
```

### 4. 查到定理后回源核对

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization error' --kind theorem --limit 10
qatlib-retrieve query 'postselection' --cite GilyenSuLowWiebe2019 --limit 10
qatlib-retrieve find Qatlib.Concepts.BlockEncoding.Postselection.normalize_error_of_gap
```

第一个查询试找后选择归一化的误差界，第二个按书目键筛选这组候选。精确查名可定位 `Qatlib.Concepts.BlockEncoding.Postselection.normalize_error_of_gap`。在 `example-library` 中，沿结果标注的文件和行号打开 `.lake/packages/Qatlib/Qatlib/Concepts/BlockEncoding/Postselection.lean`。这条引理要求 `‖y - x‖ ≤ η` 与 `η < ‖x‖`，结果包含 `y ≠ 0` 和归一化向量的距离上界。还要读它所在 section 的赋范群、实赋范空间实例。准备复用时，在普通 `module` 文件中导入该叶模块，用 Lean 核对完整类型和自己的目标。书目键在此标记模块的数学背景，精确的论文结果归属还需查原文。 [锁定 Public 的源码示例](https://github.com/quantum-atlas-ai/qatlib/blob/4e8ad0ff0c86d3f869146d488f93ab1a3eec961f/Qatlib/Concepts/BlockEncoding/Postselection.lean#L52)

索引读取当前仓的正式 `lakefile.toml` 库根、`lake-manifest.json` 中已经物化的上游，以及各自的 `docs/references.bib`。索引默认写到用户缓存目录的 `qatlib/retrieve/` 下，可用 `--db <路径>` 另选仓外位置。切换工作目录时用 `--project <目录>`，源码改动后显式运行 `qatlib-retrieve index --changed`。查询只读，不启动 Lean 或 Lake，不下载依赖、不领取构建租约，也不自动更新索引。缺索引或命中文件已变化时，按提示显式更新后再查。

输出的签名、`variable` 和文档是源码证据。宏、投影、自动生成声明等可能超出词法覆盖，模块说明也不一定适用于其中的每条定理。脚本可用 `--json` 读取候选，或用 `--repo`、`--kind`、`--cite` 缩小范围。需要额外术语时，用 `--map <文件>` 显式传入 `qatlib-retrieve-map/v1` 映射，以已存在的 FQN 增补标题、术语或书目键。消费项目负责导出映射，工具无需读取其 `metadata/`。

<a name="promotion"></a>

## 跨仓晋升与门禁对齐

### 提交整文件闭包

从可复用的叶模块起步，沿本仓导入追到全部依赖。迁移单元是一份完整文件，跟随其中的定义、实例、文档和私有 helper。依赖缺提供者、有循环，或同路径已有不同实现时，先由两边维护者厘清接口。记录每个留在原仓的文件及原因。Dev 自己的结构化元数据和生成文档仍按其接收合同更新。 [Dev 晋升合同](https://github.com/quantum-atlas-ai/example-library/blob/ad7eccff36857b11d6cdf1bcf337bcb77f9e2ef0/.agents/skills/qatlas-promote-to-staging/SKILL.md)

Lean 4.35 中，`module` 文件只能导入其他 `module` 文件，迁移要从依赖底层开始。文件内声明默认私有，`public` 和 `public section` 控制可见性，`public import` 转发公开接口。若接口还承诺跨模块归约定义体，需要另核实 `@[expose]` 等约定。迁移中保留旧式非 `module` 导入方可能掩盖可见性问题，因为它能加载新模块的私有信息。 [Lean 4.35 模块系统参考](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/) · [模块迁移指南](https://leanprover-community.github.io/extras/module-system-quickstart.html)

### 在 Staging 验证真实消费

Staging 对候选组合运行 Lint 和 Build。另在接收仓准备一个小文件（示例为 `verification/Consumer.lean`），用 `module` 加普通窄导入，写出消费方需要的类型和前提。下面用 Public 的现成接口示范普通导入的写法，可用于上手示例的固定 Public 版本。

```lean
module
import Qatlib.Concepts.BlockEncoding.Postselection

#check Qatlib.Concepts.BlockEncoding.Postselection.normalize_error_of_gap
```

```bash
qatlib-env promotion smoke --probe verification/Consumer.lean
```

正式 smoke 应用 `example` 独立重述所需签名。对承诺可计算的定义，用普通 `def` 调用并对小输入执行。对承诺可展开的定义，再用 `rfl` 或窄 `simp` 验证。探针用普通 import，避免 `import all` 或用 `noncomputable` 掩盖回归。回执记录 HEAD、工具链与诊断，区分 `PASS`、`REGRESSION`、`NOT-RUN`。有意改名可用 `--migration-map migration.toml` 记录 `[[rename]]` 的 `old`、`new` 和 `reason`。这份表供消费者改写，回执仍以编译结果为准。是否列为 Required Check 由项目合同决定。

### Public 发布后再切换消费

Staging 接收后可以在独立 Draft 分支预演改写，但 Dev 主线仍须有可用的提供者。等正式 Public 接口和对应缓存可消费，再更新下游 `lakefile.toml` 中直接依赖 `Qatlib` 的目标修订，定向更新并核对实际检出。

```bash
qatlib-env promotion bump-lock --package Qatlib
qatlib-env promotion verify-lock --package Qatlib
qatlib-env prepare
```

改写受影响的 `import`、`open`、FQN、测试及 Dev 的 `covered_by`。确认新提供者可用后，清退旧 `.lean` 和空导入壳。随晋升同步有关 `docs/references.bib` 条目，逐键处理冲突，消费仓只清退已由上游接管且没有本地独立用途的增量条目。检查未改动文件是否仍引用已删除的声明或书目键。最后在精确 Public 锁上重新跑适用门禁，再按项目流程合入。

```bash
qatlib-env promotion check-retired --package Qatlib
qatlib-env conflicts --package Qatlib
```

| 门禁 | 验证内容 |
| --- | --- |
| `verify-lock` | 核对 manifest 的完整 SHA 与实际依赖检出和源码 |
| `check-retired` | 检出归一化库根后的重复公开声明或整文件副本 |
| `conflicts` | 比较真实模块名、显式公开 FQN 和全局语法注册。合法重载可能需人工确认 |
| 项目静态门禁 | 核对导入层级、元数据、生成文档、增量书目和被删提供者的反向引用 |
| 受控 Build、EnvLint 与内核审计 | 编译实际消费者，验证环境和项目公理合同，覆盖需要编译器判断的构造器、投影和宏 |
| 普通 `module` smoke | 提前发现少量代表性公开接口的签名、计算性或归约回归 |

轻量门禁以已物化源码为前提。外层 CI 入口可用 `qatlib-env ci lint` 与 `qatlib-env ci build`，仓库提供驱动、公理白名单和 PR 基线。项目 CI 需要执行完整编译时，按 Runner 预算对该次调用显式传入 `--allow-local-heavy`。`python -m qatlib_env.check_scoped_env_lint --base "$BASE_SHA"` 中的 `BASE_SHA` 必须指向本地可用的真实 PR 基线。发布、缓存上传和合并各按项目授权执行。

<a name="field-notes"></a>

## 工程实战手记（Engineering Field Notes）

### 一、为什么检查一个文件，会估算出上万模块冷编译？

**现象。** 文件只改几行，计划却显示大量模块待重编，甚至因 `cold_limit` 或 `reverse_limit` 拒绝执行。

**原理。** 如果检查文件导入根聚合模块，正向依赖就可能展开到全库。预算器还从选中闭包里缺产物或已修改的节点沿反向边查项目内消费者。若某个节点使根聚合入口失效，估算会把受影响消费者的其他依赖也计入。这里的闭包是在回答「这次修改和不确定的缓存会波及多大」，Lake 原生构建是否实际执行这些任务还取决于其目标与 depHash。

**处理。** 写临时探针时仅导入所需叶模块，例如上面的 `Qatlib.Concepts.BlockEncoding.Postselection`。对真实目标先看 `plan` 的闭包、反向波及、锁和未解析导入。确认依赖工件可用后用 `run -- lake --no-build lean <文件>` 检查。若预算器仍按共享依赖判断存在大范围波及，让 CI 验证完整闭包。估算依据是源码导入、锁、工件和 trace 等保守筛查，无法承诺实际冷编时间。整库任务在有可用硬限制和适当预算时才按流程给予本次 `--allow-local-heavy`，该标志无法提高资源上限。

<a name="note-memory"></a>

### 二、为什么整机还有几百 GiB，构建仍等不到内存准入？

**现象。** 宿主看起来空闲，也没有一个 RSS 特别大的进程。构建在开始编译前报等待资源准入超时。

**原理。** Linux cgroup v2 的 `memory.current` 对当前组及后代计费，包含匿名内存、文件页缓存等。读写或解包大量 `.ltar` 和 `.olean` 会增加 `file`，进程退出后页缓存仍可能驻留。假设某层 `memory.high` 为 40 GiB、`memory.current` 为 36 GiB，当前按该层剩 4 GiB，即使另一层的上限更高。宿主余量和各层限额逐一生效，最后受最紧的边界约束。 [内核 cgroup v2 内存文档](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory)

**诊断。** 从实际 service 或进程的 cgroup 找到可见祖先，逐层采集 `memory.current`、`memory.high`、`memory.max`、`memory.stat`、`memory.events` 和 `memory.pressure`。`anon`、`file`、`shmem`、`inactive_file`、`file_dirty` 和 `file_writeback` 帮助分辨在跑的进程和驻留的缓存。`inactive_file` 是 LRU 分类，仍可能遇到正在写回、映射或再次访问的页。各统计值也来自不同采样时刻。直接用 `current - inactive_file` 做准入会把无法保证释放的页计为确定容量。

`qatlib-env` 对各可见祖先计算有限 high/max 减去该层原始 current 的非负余量，再结合宿主 `MemAvailable`、物理内存和预留以及池上限。遇到有限限额却缺少有效 current，或限额本身无法合法读取时，探针以零余量拒绝，并在 `config explain` 的 `resources.limits` 中报告 `memory.error`。资源探测是快照，运行中的约束仍由池和内核提供。容器外的不可见祖先从宿主核对。

**空闲维护。** 若确认是冷缓存，解包完成、写入者关闭且确认近期不用的完整归档或输出文件，可以重新打开描述符，请求 `posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` 后关闭。`0, 0` 表示从文件起点到末尾。对局部范围按页对齐，未写回的脏页不能据此视为释放。需要持久化时由拥有者先完成写回，并重新采样观察实际效果。这一提示可能影响同时使用同一文件的其他进程，适合冷文件。 [posix_fadvise(2)](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html)

如需按组回收，管理员在暂停接收新 job、确认该组空闲后，可在已核对的目标 cgroup 目录向 `memory.reclaim` 写入小额度，例如 `printf '%s\n' 512M > memory.reclaim`。回收量可能多于或少于请求，少于请求可能返回 `EAGAIN`。操作后重新采集 current、stat、events 和压力，确认预算变化再开放调度。权限不足或内核无该接口时保留现状并报告。逐 job 热路径应避免自动回收，全机 `drop_caches` 会影响其他服务的热缓存。 [memory.reclaim](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files) · [drop_caches](https://docs.kernel.org/admin-guide/sysctl/vm.html#drop-caches)

<a name="note-cancellation"></a>

### 三、为什么取消或超时后，机器还在 90% 以上内存占用时反复抖动？

**现象。** Forge 记录 job 已取消或超时，宿主仍持续回收内存，Runner 却显示正常运行。此时可能还能找到 `PPID=1` 的 `lake` 或 `lean`。

**原理。** 父进程退出后，未被收割的后代仍可能在原 cgroup 计费。越过 `MemoryHigh` 会触发直接回收与节流，持续分配可能让回收和运行反复争抢。`MemoryMax` 才提供最终的内存硬边界，无法回收时可能触发该组 OOM。systemd 的 `KillMode=control-group` 在停止 service 时作用于整个单元。Runner 持续运行期间取消单次 job 不会自动触发该机制。 [systemd.kill](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html) · [systemd 资源控制](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)

**处理。** 按 job 建独立进程组或 scope，由监督器追踪进程和后代。取消、模块超时、stage 超时与正常完成都应触发清理，并复扫确认；不要仅按进程名清理共享机器。Windows 受管入口在命令运行前附加 Job Object，用于聚合限制和清理，关闭最后的 Job 句柄可一并终止归属其中的进程。核对残留时读取 PID、创建时间、命令、工作目录和 cgroup，再看 `memory.events` 中 high/max/oom 的增量。单次采样或 `PPID=1` 只能作为线索。容器可能由外部 daemon 启动，或在命名空间中看不到父层，需要通过实际 runtime 复核。 [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

### 四、为什么网页能点开的文档引用，在 VS Code 悬浮窗里只显示文字？

**现象。** Lean docstring 中的声明名在 API 网页上可点击，同一段文字在编辑器里并不具有同样的链接。书目链接也可能打开正确页面却跳不到正确条目。

**原理。** `doc-gen4` 在生成期拥有模块、声明链接和书目表，可以把相应文本渲染成 HTML 链接。VS Code 向 Lean LSP 发出 `textDocument/hover`，服务器从当前命令的环境和 elaboration 信息生成 Markdown，回复中没有生成网站的书目表和静态路径信息。API 参考文献页的实际锚点形如 `#ref_<CiteKey>`。例如链接目标写成 `references.html#SuksmonoMinato2019` 时，页面能打开，但对应的实际 ID 是 `ref_SuksmonoMinato2019`。 [doc-gen4](https://github.com/leanprover/doc-gen4) · [Lean 4.35 hover 实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/Lean/Server/FileWorker/RequestHandling.lean) · [门户锚点修复](https://github.com/quantum-atlas-ai/qatlib-staging/pull/22)

**处理。** 跨模块普通 Markdown 用反引号包完整 FQN，论文结果使用 `[CiteKey], Theorem X`，其中 `X` 按原文实际编号填写，键在根 `docs/references.bib` 中登记。确需在编辑器中提供可点击的论文入口时，附上标准 DOI 或 arXiv 链接。对网页分别实查生成后的 href 和目标 ID。词法引用门禁能发现新增坏引用和删除提供者后留下的消费者，生成声明与 HTML 锚点仍由相应前端验证。

Lean 4.35 试点在 `RootCountOrder.lean` 中以 `set_option doc.verso true` 启用 Verso，用角色 `` {name}`ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.rootCount` `` 引用已有声明。故意拼错常量名会在编译时产生 `Unknown constant`。该试点还验证了 LSP 悬浮、锁定版 doc-gen4 的目标锚点和生成摘要。推广时先沿用这套三处验证，仅在受测模块和角色上启用。角色能检查名字解析，书目键有效性和论文本身的数学归属仍各自核验。 [Lean 文档编写规范](https://github.com/leanprover/lean4/blob/v4.35.0/doc/style.md) · [单模块试点](https://github.com/quantum-atlas-ai/example-library/pull/550)

<a name="note-concurrency"></a>

### 五、工作树多开和线程乘积是怎么超配的？

**现象。** 两个分支互相覆盖 `.lake` 工件，或每个 job 单看只有几个编译进程，合起来却十分慢。

**原理。** `.lake` 中的依赖检出、build 目录、trace 和本地锁与工作树的源码和依赖版本绑定。两棵树软链成同一 `.lake` 后，更新依赖和写入构建产物会互相干扰。并发还分两层，`jobs` 是并行进程数，`lean_threads` / `LEAN_NUM_THREADS` 是每个 Lean 进程的线程数。比如两个任务各设 `jobs=4`、`lean_threads=4`，合计请求 32 个 CPU 单位，而非 8 个。

**处理。** 每个 worktree 保有独立 `.lake`，通过工具管理的内容寻址缓存与 Git 对象镜像复用数据。对象镜像里有对象并不表示依赖已检出。将同一机器上的任务接到同一租约账本和一致的池上限，按 `jobs × lean_threads` 估算 CPU，另给每次任务记内存预算。内核的父 slice、service 或容器给出外层限制，工具的共享池负责跨工作树准入。POSIX 编译器包装层还分配每进程内存与重模块槽位。Windows 的 Job Object 提供聚合 CPU/内存限制，须用 `heavy_patterns = []` 选择该模式，重模块槽位不适用。macOS 后端可调整调度优先级，但缺少聚合内存硬上限，不能用它执行需要该保证的本机重任务。

<a name="cli-reference"></a>

## CLI 速查

| 任务 | 入口 | 行为 |
| --- | --- | --- |
| 查看生效配置和本机资源 | `qatlib-env config explain` | 输出值和来源 |
| 查看缓存和租约 | `qatlib-env cache explain`、`qatlib-env pool status` | 诊断缓存和并发准入 |
| 离线读本手册 | `qatlib-env docs` 或 `qatlib-env docs --path` | 打印文档或安装路径 |
| 创建独立工作树 | `qatlib-env worktree <路径> <分支>` | 默认准备缓存，可加 `--no-bootstrap` |
| 估算和执行 | `qatlib-env plan <模块>`、`qatlib-env run -- <命令>`、`qatlib-env check <模块>` | `plan` 默认只读。实际执行受资源准入限制 |
| 准备或显式上传缓存 | `qatlib-env prepare`、`qatlib-env cache upload --help` | 准备可联网。上传由项目发布流程授权 |
| 超过本机预算 | `qatlib-env cloud submit --head <分支>` | 显式提交远端。配合 `status`、`logs`、`wait`、`fetch` |
| 定向升锁和查重 | `qatlib-env promotion --help`、`qatlib-env conflicts --package <包名>` | `bump-lock` 更新目标包，其余锁和副本检查只读。smoke 运行 Lean |
| 索引源码 | `qatlib-retrieve index [--changed]` | 只写索引缓存 |
| 查源码 | `qatlib-retrieve query <文本>`、`qatlib-retrieve find <文本>` | 只读，亦可省略 `query`，或用 `python -m qatlib_search` |
| 查询 Forge | `qatlib-forge pr list --platform github --repo quantum-atlas-ai/qatlib` | 需读取权限。写操作如 `pr create`、`issue comment` 另行调用 |
| 维护工作区 | `qatlib-env workspace scan <目录> --json` | 默认本地扫描，`--check-remote` 才查远端 |
| 预览回收 | `qatlib-env workspace clean-caches --checkout <路径> --dry-run` | 清理缓存或用 `trash-checkouts` 选择整树，审阅清单后再 `--apply` 移入系统回收站 |

旧 `qatlib-search build/query/retrieve` 使用的索引格式已退役。改用 `qatlib-retrieve index` 初始化、`query` 查询，无需转换历史索引。锁定上游未准备时先用 `qatlib-env prepare` 核对，检索命中不能代替构建或晋升门禁。

<a name="configuration"></a>

## 配置优先级

以下按低到高排列，同一键由后者覆盖。运行 `config explain` 可以看到最终值与来源。

| 层级 | 来源与职责 |
| --- | --- |
| 默认与项目推导 | 宿主探测给出资源默认值。主库、测试根和 `srcDir` 来自 `lakefile.toml`，仓库身份和工作流从项目本身推导 |
| Runner 兼容层 | `QATLAS_RUNNER_CONFIG` 指向的文件 |
| 个人设置 | 先读 `${XDG_CONFIG_HOME:-~/.config}/qatlib/env.toml` 兼容配置，再读 `~/.qatlib/env.toml` |
| 项目共享 | 先读根 `pyproject.toml` 的 `[tool.qatlib-env]`，再读根 `qatlib-env.toml`。两文件均不存在时才回退到 `python/pyproject.toml` |
| 工作树本地覆盖 | 根 `.qatlib-env.local.toml`。次级工作树没有时可只读使用主检出的同名文件 |
| 环境变量 | 如 `QATLAS_POOL_BUDGET_MIB`、`QATLAS_POOL_CPU`、`QATLAS_JOBS`。线程变量的优先级为 `QATLAS_LEAN_THREADS` 高于 `LEAN_NUM_THREADS` |
| 本次命令参数 | 如 `qatlib-env run --jobs 1 --lean-threads 2 -- lake build <模块>` |

项目需要共享预算策略时可提交下面这样的 `qatlib-env.toml`。机器规模、每个任务的运行资源留在个人或 Runner 配置中。若放到 `pyproject.toml`，表名改为 `[tool.qatlib-env.budget]`。

```toml
[budget]
max_cold_modules = 15
max_reverse_modules = 15
require_explicit_local_heavy = true
```

这两个阈值填 `0` 表示对应类别不允许重编或波及。可用 `QATLAS_MAX_COLD_MODULES` 和 `QATLAS_MAX_REVERSE_MODULES` 覆盖。项目和个人配置出现未知键或错误类型时，按报错路径改正。`--allow-local-heavy` 只在单次调用中有效，执行仍受池、控制器和超时约束。`QATLAS_POOL_WAIT_TIMEOUT` 调整等待其他租约的时间，请求超过池硬上限时仍会直接拒绝。

依赖精确修订由 `lake-manifest.json` 维护。Lake 缓存服务由 `lake-cache.toml` 配置，本机工件路径由 `LAKE_CACHE_DIR` 指定。池账本由工具按 `XDG_STATE_HOME` / `XDG_CACHE_HOME` 定位；让需要共用容量的任务指向同一账本。检索从 Lake 项目和本地源码独立发现语料，用自己的 `--project` 与 `--db`。

<a name="runner"></a>

## 自托管 Runner 部署核对单

1. **确认环境与身份。** Linux / WSL2 检查 systemd 正在管理服务，`stat -fc %T /sys/fs/cgroup` 通常应报告 `cgroup2fs`。确认当前容器、WSL 虚拟机和宿主各自的内存边界。每个实例安排独立工作目录与注册凭据，按信任边界划分运行账号，检查目标代码是否获准在相应 Runner 上运行。
2. **核对脚本版本和职责。** 在锁定的 Tools 源码检出运行 `bash src/qatlib_env/setup_runner.sh --help`。已装 wheel 时可运行 `bash "$(python -c 'from pathlib import Path; import qatlib_env; print(Path(qatlib_env.__file__).with_name("setup_runner.sh"))')" --help`。脚本针对 Ubuntu/Debian 和 systemd，`init` 会配置主机依赖、账户和共享 slice，`register` 注册单个实例并写入服务资源设置。先审阅实际路径、认证和配额，再按部署变更流程执行。
3. **同时预算父 slice 与每个实例。** 在已有服务的 drop-in 中使用 `[Service]` 下的 `Slice`、`CPUQuota`、`MemoryHigh`、`MemoryMax`、`TasksMax` 和 `KillMode=control-group`。按实例数与父 slice 的有效上限计算，为 Runner、文件缓存和其他服务留余量。使用 `systemctl show <服务> -p ControlGroup -p Slice -p EffectiveMemoryHigh -p EffectiveMemoryMax -p KillMode` 读回，并到实际 cgroup 检查计费。
4. **按实际运行用户验收。** 核对个人配置、Runner 配置、共享池及项目约定，运行 `qatlib-env config explain`、`qatlib-env pool status`。Linux 重任务优先使用可用的受限 systemd 用户 scope。若只能沿用已有 cgroup，后端要求能证明有限 `memory.max`、CPU quota 和各可见祖先具备足够余量。单靠 `memory.high` 或 CPU affinity 无法满足硬限制要求。
5. **跑小任务与取消测试。** 在测试 lane 分别检查成功退出、取消、模块超时和 stage 超时。测试任务应派生子孙。结束后复核进程、实际 cgroup 和租约，记录 `memory.events` 与压力变化。遇到高占用先看[手记二](#note-memory)和[手记三](#note-cancellation)，确认是缓存驻留还是仍有任务。最后按设计并发逐步放量。

工具仓本地开发可运行 `uv lock --check`、`uv build` 和 `uv run --with pytest --with hatchling --with jsonschema pytest -q -rs`。根 README 按原始字节打入 wheel 的 `qatlib_env/_manual.md`，在干净环境装 wheel 后可将 `qatlib-env docs` 的输出与本文件比较。

本项目采用 [Apache License, Version 2.0](./LICENSE)，与 [qatlib](https://github.com/quantum-atlas-ai/qatlib) 一致。
