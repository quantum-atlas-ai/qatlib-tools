# 编译环境、缓存与资源控制

<a id="lakefile"></a>

## `lakefile.toml` 怎么读

顶层字段描述包。每个 `[[lean_lib]]` 定义一个库目标，每个 `[[require]]` 声明一个依赖。包名、库目标名和 Lean 模块名可以不同。下面的配置把源文件放在 `src/`，其中 `MyLib/Facts.lean` 对应模块 `MyLib.Facts`。

```toml
name = "ExampleProject"
version = "0.1.0"
defaultTargets = ["MyLib"]
srcDir = "src"
enableArtifactCache = true
restoreAllArtifacts = false

[[lean_lib]]
name = "MyLib"
roots = ["MyLib"]
globs = ["MyLib.*"]
```

| 字段 | 默认值与用途 |
| --- | --- |
| 包级 `name`、`version` | TOML 需要给出包名。版本省略时为 `0.0.0`。Lean 工具链版本另由 `lean-toolchain` 指定。 |
| 包级 `defaultTargets` | 不带目标运行 `lake build` 时选择的目标列表。TOML 省略时为 `[]`。目标名应指向实际库或其他可构建目标。 |
| 包级与库级 `srcDir` | 默认都是 `"."`。包的目录相对包根，库的目录相对包级源目录。Lake 以最终源目录定位模块。 |
| 包级与库级 `moreLeanArgs` | 默认 `[]`。包与库的列表叠加，库在包之后。这类参数计入构建追踪，改变后可能使已有工件失效。 |
| 包级与库级 `weakLeanArgs` | 默认 `[]`。同样按包再到库叠加，并排在普通 Lean 参数之前。它们不计入构建追踪，只放不应改变产物或验收结果的设置。 |
| 库级 `name`、`roots` | `name` 是目标名。根模块默认为该名字；`roots` 指定库的根模块，根及其子模块可归入库。 |
| 库级 `globs` | 指定构建库时枚举的模块。缺省逐个选择 `roots` 本身，再递归构建实际导入。写 `"MyLib.*"` 可直接枚举根和子模块，`"MyLib.+"` 只枚举子模块。若根没有聚合导入，默认也不会扫全目录。 |
| `[[require]]` 和 `lake-manifest.json` | 依赖声明写名字及 Git、路径或注册表来源，Git 可用 `rev` 指定分支、标签或提交。Lake 解析后在清单记录各依赖的来源与实际 revision，含传递依赖。提交清单以复现检出，定向升锁用 `lake update <包名>`，再审查清单差异。 |

[Lake v4.35 手册](https://lean-lang.org/doc/reference/4.35.0/Build-Tools-and-Distribution/Lake/) · [TOML 解码器](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Load/Toml.lean) · [库和参数配置](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/LeanLib.lean)

<a id="modules"></a>

## 模块的公开接口

Lean v4.35 的 `module` 文件只能导入同样使用模块系统的文件。迁移时先处理依赖，再迁移消费者。模块内声明默认私有，可通过 `public section` 或声明上的 `public` 让名称对下游可见。`public import` 会把被导入模块的公开接口传给下游。下游需要展开定义时，还要核对 `@[expose]`，并用依赖展开的例子验证。普通非 `module` 文件能读取模块的私有信息，使用这类文件作为测试容易掩盖模块消费者遇到的错误。 [Lean v4.35 模块手册](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/)

<a id="incremental-build"></a>

## 增量编译与单文件检查

`lake build <Module>` 根据导入关系和输入跟踪信息构建所选模块及其依赖。已有且仍匹配输入的产物可直接复用。导入顶层聚合模块可能让单文件检查牵涉整个库。保留必要的叶模块导入，先用 `qatlib-env plan <Module>` 查看冷模块数、反向依赖和依赖锁状态，再决定检查范围。

`lake --no-build lean <File.lean>` 让 Lake 按项目配置准备导入环境，再用已有依赖产物检查文件。如果有必须构建的依赖，命令会失败。可先用 `qatlib-env run -- lake build <Module>` 补齐产物，再用 `qatlib-env run -- lake --no-build lean <File.lean>` 检查正在改的文件。启用共享缓存时，直接运行裸 `lean` 或只遍历本地 `.lake/build` 都可能漏掉 Lake 已知的工件。 [Lake v4.35 命令实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/CLI/Main.lean)

<a id="cache-flags"></a>

## 缓存读写与恢复开关

两个开关都设在包级，`[[lean_lib]]` 不另设这两个字段。对当前正在构建的**每个包**，Lake 先读该包显式设置，再读工作区默认值。工作区默认值先取环境变量，再取根包字段。两条链分别计算。

| 正在构建的包 | 查找顺序（首个有值的生效） |
| --- | --- |
| 根包 | 根包显式值 → 对应环境变量 → Lake 缺省行为 |
| 依赖包 | 依赖自身显式值 → 对应环境变量 → 根包字段 → Lake 缺省行为 |

`enableArtifactCache` 对应 `LAKE_ARTIFACT_CACHE`，`restoreAllArtifacts` 对应 `LAKE_RESTORE_ARTIFACTS`。变量可用 `true`、`false` 或 `1`、`0`，空值和不可识别值等同未设置。根包明写 `restoreAllArtifacts = false` 时，环境中的 `LAKE_RESTORE_ARTIFACTS=1` 不能让根包全量恢复，却可能影响没有显式设置的依赖。依赖自己的显式值也优先。

下面按**完成上述解析后**的值列出九种组合。表描述默认支持缓存的模块目标、工作树没有可复用的本地产物且缓存命中时的行为。

| `enableArtifactCache` | `restoreAllArtifacts` 最终未填 | 最终 `false` | 最终 `true` |
| --- | --- | --- | --- |
| 最终未填，缓存可读、不可写 | 读共享缓存，恢复全套本地产物 | 同左，只读路径仍全量恢复 | 同左，恢复全套 |
| 最终 `true`，缓存可读写 | 从共享缓存取 `.olean` 等，仅恢复必需的 `.ilean` 等 | 同左 | 恢复全套本地产物 |
| 最终 `false`，缓存不可读写 | 走工作树本地构建和追踪 | 同左 | 同左，此开关不重开共享缓存 |

可写模式遇到冷编译仍会先生成文件，再把工件登记到内容寻址缓存。全量恢复关闭针对后续命中的常规行为，不能保证所有目标都只留缓存路径。需要本地文件的目标还会按需恢复。只命中 `.ltar` 而缺少可直接解析的工件时，Lake 会先在当前树解包再处理。需要打包或发布工件的命令也可能显式恢复文件。

裸 Lake 的 `LAKE_CACHE_DIR` 决定缓存位置，非空路径优先于发现的工具链缓存或系统缓存。显式传空值关闭系统缓存选择，工作区仍可回退到 `.lake/cache`。常用的 `qatlib-env prepare` 和 `run` 会选定并导出非空缓存目录，并设置 `LAKE_ARTIFACT_CACHE=true` 作为工作区默认值，不设置恢复开关，包级显式值仍优先。多工作树日常配置建议明确写 `enableArtifactCache = true` 和 `restoreAllArtifacts = false`，同时核对依赖的显式配置和运行账户的环境。依赖仍用只读模式或开启全量恢复时，该依赖仍可能在各树分别落盘。 [包配置](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/PackageConfig.lean) · [解析顺序](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/Monad.lean) · [工作区与缓存位置](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/Workspace.lean) · [模块工件读写](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Module.lean)

<a id="cache-layout"></a>

## 三类缓存和工作树

| 来源 | 数据流与位置 |
| --- | --- |
| Lake 本地工件缓存 | Lake 用构建输入哈希查工件映射，再按内容哈希定位共享 `.olean`、IR 等。按上节的可写、关闭全量恢复模式命中工件时，不必为每棵工作树复制这些大文件。内容寻址不依赖 reflink 或跨卷硬链接。 |
| `qatlib-env prepare` 的 R2 工件 | 各工作树按清单分别检出依赖源码。对配置了 R2 的包，按精确提交、适用的同输入映射和祖先候选查找，只把映射 HTTP 404 当作缺失。校验现有和下载的工件，安装到 `LAKE_CACHE_DIR/artifacts`，用 `lake cache add` 登记。匹配的本机 checkpoint 是单独的恢复来源。 |
| Mathlib 专用缓存 | `lake exe cache get` 下载并解包专用归档。在标准依赖布局下，解包产物位于当前树的 `.lake/packages/mathlib/.lake/build/`。压缩归档和解包产物各自占用空间。 |

Git 镜像可以减少下载依赖源码的流量。每棵树仍应有独立的依赖检出和 `.lake`，可以复用共享工件目录。只回收派生数据时用 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 预览 `.lake/build` 和 `.lake/packages`，核对后换用 `--apply`。回收整个旧关联工作树用 `trash-checkouts` 预览。确认无独有材料、在用分支和写入进程后，执行回收，成功后默认清理失效 Git 登记并保留分支引用。输出报告移动与实际释放空间分开计算。

<a id="windows"></a>

## Windows 跨卷和长路径

例如，缓存使用 `C:\lake-cache`，工作树在 `D:\work\project`。Windows NTFS 的硬链接只能在同一卷内创建。跨卷链接失败在 POSIX 语义中称为 `EXDEV`，Windows 的错误码和显示文字取决于调用层。缓存和工作树分盘本身可以正常使用。

| 操作 | 分盘时的行为 |
| --- | --- |
| Lake 直接读取共享工件 | 直接读缓存卷上的文件，不创建跨卷硬链接。 |
| Lake 落地工件或保存缓存 | 恢复缺失的 `.ilean` 或全量工件时先试硬链接，失败便复制到工作树。新编译输出保存到共享缓存时，硬链接不适用也可以写入缓存字节。全量恢复将消耗工作树所在卷的空间。 |
| `prepare` 安装 R2 工件 | 映射下载的临时记录可以在工作树，但工件下载和安装暂存位于 `LAKE_CACHE_DIR` 下。校验后的暂存文件与 `artifacts/` 必须在支持硬链接的同卷，以无覆盖的方式发布。此步骤没有跨卷复制回退，链接错误会使准备失败。避免把 `artifacts/` 重定向到另一卷，选择正常的可写本地 NTFS 目录。 |

Windows 路径较长时，优先把工作区和缓存放在短路径下。Git for Windows 的 `core.longpaths=true` 只影响支持该扩展的 Git 操作。Windows 的 `LongPathsEnabled` 加上应用声明 `longPathAware` 才能为支持的 Win32 API 放开传统 `MAX_PATH`，Lake、Python、归档工具和回收站仍须分别验证。长路径设置不会让两个卷之间产生硬链接。 [Lake 保存与恢复实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Common.lean) · [Windows 硬链接](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions) · [Win32 路径上限](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation) · [Git for Windows](https://gitforwindows.org/git-cannot-create-a-file-or-directory-with-a-long-path.html)

Windows 的受管构建和需要编译的缓存准备走原生 Lake，并使用 Job Object 设置整组 CPU 与内存上限。工具的默认模块匹配列表非空，请在生效的 `env.toml` 设置 `heavy_patterns = []`，再用 `qatlib-env config explain --json` 核对实际值。Windows 不装 POSIX 编译器包装层，无法为匹配模块分配独立槽位或逐编译器注入 `-M`。因此受管 Lake 遇到非空列表会报错。Job Object 的进程启动和清理见 [Runner 进程隔离](runner.md#windows-job-object)。

<a id="note-concurrency"></a>

## 多工作树与并行预算

构建用 `jobs × lean_threads` 和总内存预算申请单机租约。两个任务各设四进程四线程时，总申请是 32 个 CPU 配额。工作树共享同一个账本，实际 CPU、内存和超时由各平台后端约束。变更资源前先用 `qatlib-env plan` 查看冷编译和反向依赖范围。Linux 的编译器包装支持重模块槽位，macOS 不具备与 Linux 或 Windows 等同的聚合内存硬上限。需要硬隔离的任务应使用能强制执行该上限的环境。 [Runner 与进程排障](runner.md)

<a id="configuration"></a>

## 配置优先级

无配置时，内存池取已扣除宿主预留的可用内存与最紧的可见 cgroup 余量中的较小值，按 MiB 向下取整。预留为 2 GiB 和物理内存 10% 中的较大值，单独作用于宿主指标。CPU 池取逻辑核、亲和性、cpuset 和配额允许的共同容量。

单次构建通常使用约一半 CPU 和内存。CPU 预算最多 8 个核，可用时至少保留 2 个核给当前构建。内存预算最多 16 GiB，池不足 4 GiB 时按实际可用量计算。默认每个 Lean 进程最多两线程，单核为一线程，并行进程还受每进程约 4 GiB 的估算约束。少于最小准入内存时，先释放资源或选择其他执行环境。`qatlib-env config explain --json` 同时报告不带覆盖的默认值、实际值、来源与冲突提示。

下列配置层按优先级**从低到高**排列，同名配置键由高层覆盖低层，随时可通过 `qatlib-env config explain` 核查实际生效值与来源路径：

| 优先级（低 → 高） | 来源位置与设计职责 |
| --- | --- |
| 1. 默认值与项目推导 | 根据宿主与 cgroup 实时探测结果给出保守默认值；主库根、测试根与 `srcDir` 从 `lakefile.toml` 动态解析，仓库标识与工作流从项目目录自动推导 |
| 2. Runner 兼容配置 | 环境变量 `QATLAS_RUNNER_CONFIG` 指向的实例配置文件 |
| 3. 个人本机设置 | 依次读旧 XDG 配置、`~/.qatlib/env.toml` 和明确指定的 `$QATLIB_HOME/env.toml`，后者覆盖前者 |
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
<a id="local-paths"></a>

## 本机数据目录

`qatlib-env config explain --json` 的 `paths` 显示实际路径，不创建目录。表中工具根指 `${QATLIB_HOME:-~/.qatlib}`。

| 数据 | 默认位置 | 覆盖和兼容 |
| --- | --- | --- |
| 个人配置 | 工具根的 `env.toml` | 保留 `${XDG_CONFIG_HOME:-~/.config}/qatlib/env.toml` 和 `~/.qatlib/env.toml` 的低优先级读取 |
| R2 密钥文件 | 工具根的 `secrets` | `QATLAS_R2_KEY_FILE` 优先。改过工具根且新文件不存在时，仍可读旧文件 |
| 构建池租约 | 工具根的 `state/pool/ledger.json` 与 `ledger.lock` | 明确设置 `XDG_STATE_HOME` 或 `XDG_CACHE_HOME` 时，仍使用该目录下的 `qatlib-env/pool`。默认旧目录 `~/.cache/qatlib-env/pool` 已有账本或锁时沿用原位置 |
| 检索索引 | 工具根的 `retrieve/<corpus-key>.sqlite3` | `--db` 优先。查询可读取旧 XDG 缓存，见[本地索引](lean-docs.md#lexical-index) |
| 云端作业回执 | 工具根的 `state/cloud/<corpus-key>.json` | `--state` 优先。已有检出根 `.qatlib-env/cloud-state.json` 时继续读写旧回执 |
| Lake 工件和本地检查点 | Lake 工件目录与其 `checkpoints/` 子目录 | `LAKE_CACHE_DIR` 优先；否则沿用 elan 的对应工具链目录或独立 Lake 的 XDG 缓存。保留原有数据 |

同一台机器的构建调用应使用同一账本和锁。迁移旧账本前先停止使用该池的构建，再统一调用者使用的工具版本和环境。旧锁文件仍存在时工具会继续选择旧池。历史工作树池 `~/.legacy-worktrees/tmp/` 由相应仓库维护，可用 `qatlib-env workspace scan --history` 查找。它与用户工具目录无关。
