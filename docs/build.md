# 编译环境、缓存与资源控制

构建后的公理白名单、零 `sorry` 门禁、下游接口检查与内核重放见 [Lean 证明复核与公理审计](verification.md)。

这一篇按名称查阅。要在第二棵工作树里走完计划、准备、构建、单文件检查和清理预览，从[在 Lean 仓库里工作](worktrees.md)开始。第一次下载 Lean 工具链或编译依赖的耗时，写在[五分钟验证](quickstart.md)的末尾；那一次不属于工具本身的安装检查。

<a id="lakefile"></a>

## `lakefile.toml` 怎么读

在 Lake 构建体系中，`lakefile.toml` 的顶层字段用于声明整个软件包的全局元数据与默认行为；配置内部则通过 `[[lean_lib]]` 定义具体的库构建目标，并通过 `[[require]]` 声明外部依赖项。需要注意的是，Lake 允许包名（Package Name）、库目标名（Lib Target Name）以及 Lean 模块命名空间各自独立，彼此无需保持一致。

以如下典型配置为例，我们将所有 Lean 源码集中存放在 `src/` 目录下，此时源文件路径 `src/MyLib/Facts.lean` 将精确对应于 Lean 语言内部的模块路径 `MyLib.Facts`：

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

| 字段 | 默认值与核心用途 |
| --- | --- |
| 包级 `name`、`version` | TOML 配置中必须显式声明包名；版本号 `version` 省略时默认记为 `0.0.0`。需要注意，Lean 编译器工具链的具体版本由项目根目录下的 `lean-toolchain` 文件独立指定，不受该字段影响。 |
| 包级 `defaultTargets` | 当直接执行不带具体构建目标的 `lake build` 时，Lake 所选取的默认目标列表；在 TOML 中省略时默认为 `[]`。列表中的目标名必须准确指向已声明的实际库或其他可构建目标。 |
| 包级与库级 `srcDir` | 两者的缺省值均为 `"."`。其中包级源目录相对于软件包根目录，库级源目录则相对于包级源目录进行解析。Lake 会综合二者定位最终的模块源码路径。 |
| 包级与库级 `moreLeanArgs` | 默认值均为 `[]`。构建时会先取包级参数、再叠加库级参数传给 Lean 编译器。由于此类编译参数会被完整计入构建追踪哈希，一旦参数发生变更，可能会使已有的构建工件全部失效。 |
| 包级与库级 `weakLeanArgs` | 默认值均为 `[]`。同样遵循先包级后库级的叠加顺序，但在调用编译器时排在普通 Lean 参数之前。与普通参数不同，弱参数不会计入构建追踪哈希，因此仅适合传递那些绝不会改变产物语义或验收结果的辅助选项。 |
| 库级 `name`、`roots` | `name` 代表该库目标的唯一标识符；未显式指定根模块时，默认沿用该名称作为根模块。`roots` 明确声明该库所管辖的根模块列表，声明的根模块及其全部子模块均归入该库管理。 |
| 库级 `globs` | 指定构建该库时需要主动枚举的模块规则。缺省未配置时，Lake 仅会逐一加载 `roots` 本身，再递归构建其实际导入的下游模块。若显式配置为 `"MyLib.*"`，则会直接遍历枚举根模块与所有子模块；若写为 `"MyLib.+"` 则仅枚举子模块。在根模块没有组织全局聚合导入的情况下，默认不会全盘扫描目录。 |
| `[[require]]` 与 `lake-manifest.json` | 用于声明外部依赖项的名称及其获取来源（支持 Git 仓库、本地文件路径或包注册表）；对 Git 依赖可通过 `rev` 指定特定的分支、标签或提交哈希。Lake 完成依赖解析后，会在清单文件 `lake-manifest.json` 中完整锁定记录各依赖（包含传递依赖）的确定来源与具体检出 revision。将清单文件提交进版本控制是保证团队构建一致性的关键；若需定向升级某个依赖，执行 `lake update <包名>` 后应仔细审查清单文件的 diff 差异。 |

[Lake v4.35 手册](https://lean-lang.org/doc/reference/4.35.0/Build-Tools-and-Distribution/Lake/) · [TOML 解码器](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Load/Toml.lean) · [库和参数配置](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/LeanLib.lean)

<a id="modules"></a>

## 模块的公开接口

在 Lean v4.35 的模块系统中，声明了 `module` 的源文件只能导入同样采用了模块约定的文件；因此在重构或迁移项目时，应当遵循“自底向上”的演化路径，优先完成底层基础依赖的模块化改造，再逐步推进上层消费模块。

在模块内部，所有符号与定理声明默认均为私有（private）。若需使名称对下游模块可见，可在具体声明前修饰 `public`，或者将一组声明整体置于 `public section` 代码块内。如果下游模块需要级联继承被导入模块的公开接口，可使用 `public import` 进行再导出传递。当下游存在展开（unfold）特定常数或定理定义的需求时，还必须核对原模块是否正确标注了 `@[expose]`，并通过依赖展开的实际样例进行充分验证。需要特别警惕的是，普通的非 `module` 文件拥有直接读取模块私有信息的权限；若误用非模块文件编写单元测试，极易掩盖真实下游模块因访问权限受限而可能触发的编译失败。

[Lean v4.35 模块手册](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/)

<a id="incremental-build"></a>

## 增量编译与单文件检查

执行 `lake build <Module>` 时，Lake 会严格基于源码的导入拓扑与输入跟踪哈希，自底向上构建目标模块及其整条依赖链。凡是本地或缓存中已经存在且与当前输入严格匹配的编译产物，都会被直接复用，从而实现高效的增量编译。但在工程实践中需要注意：如果文件中随意导入了顶层聚合模块，往往会导致单文件的语法或类型检查被迫牵扯并构建几乎整个库。因此建议日常仅保留必要的叶子模块导入；在动手编译前，可先通过 `qatlib-env plan <Module>` 查看潜在的冷模块数量、受波及的反向依赖链路以及依赖锁状态，评估并选定最合理的构建范围。

当仅需要快速校验单个正在编辑的文件时，推荐使用 `lake --no-build lean <File.lean>`。该命令指示 Lake 按照当前项目的依赖拓扑与环境配置搭建好运行时导入上下文，随后直接利用现存的依赖产物对指定文件进行类型检查。如果当前依赖链上存在尚未构建的产物，该命令会直接报错退出，避免隐式触发耗时的级联构建。遇到这种情况时，可先通过 `qatlib-env run -- lake build <Module>` 定向补齐前置依赖产物，然后再执行 `qatlib-env run -- lake --no-build lean <File.lean>` 快速完成目标文件的检查。此外，在开启了共享缓存的体系下，切忌直接运行裸 `lean` 二进制或仅依靠遍历本地 `.lake/build` 目录排查产物，这类做法极易遗漏 Lake 已在全局共享缓存中纳管的工件。

[Lake v4.35 命令实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/CLI/Main.lean)

<a id="cache-flags"></a>

## 缓存读写与恢复开关

两个控制缓存行为的核心开关——`enableArtifactCache`（对应环境变量 `LAKE_ARTIFACT_CACHE`）与 `restoreAllArtifacts`（对应环境变量 `LAKE_RESTORE_ARTIFACTS`）均定义在包级别，具体的 `[[lean_lib]]` 目标中不重复提供这两个字段。在构建过程中，对于参与构建的**每一个包**，Lake 都会独立计算这两个开关的生效值：先读取该包自身的显式配置，若未设置则回退至工作区默认值；而工作区默认值则优先读取环境变量，最后回退至根包的相关字段。两条计算链相互独立且按序求值。

| 正在构建的包 | 配置查找顺序（首个命中有效值的规则生效） |
| --- | --- |
| 根包（Root Package） | 根包显式配置 → 对应环境变量 → Lake 缺省行为 |
| 依赖包（Dependency Package） | 依赖包自身显式配置 → 对应环境变量 → 根包字段配置 → Lake 缺省行为 |

在环境变量的解析上，布尔值支持 `true`、`false`、`1` 或 `0`，空值或无法识别的字符串一律视同未设置。需要特别注意的是：当根包中显式声明了 `restoreAllArtifacts = false` 时，即使外部环境中导出了 `LAKE_RESTORE_ARTIFACTS=1`，也不会强行改变根包自身的设置；但该环境变量却会作为工作区默认值，意外渗透并影响到那些未显式配置该开关的下游依赖。同样，任何依赖包自身若显式指定了该开关，其优先级始终高于外部环境与根包的配置。

在完成上述双重层级解析后，两个开关根据最终求值结果将衍生出九种行为组合。下表详细列出了在模块目标默认支持缓存的前提下，当本地工作树中不存在可直接复用的产物且共享缓存顺利命中时的具体运作逻辑：

| `enableArtifactCache` 最终解析值 | `restoreAllArtifacts` 最终未设置 | `restoreAllArtifacts` 最终为 `false` | `restoreAllArtifacts` 最终为 `true` |
| --- | --- | --- | --- |
| **最终未填**（缓存可读、不可写） | 从共享缓存读取，全量恢复全套本地产物 | 行为同左（只读路径下仍全量恢复产物） | 行为同左，全量恢复全套本地产物 |
| **最终为 `true`**（缓存读写均开放） | 直接从共享缓存取用 `.olean` 等大文件，仅向本地恢复必须落盘的 `.ilean` 等轻量工件 | 行为同左（推荐模式，极大节约磁盘空间） | 无论文件大小，全量恢复全套本地产物到工作树 |
| **最终为 `false`**（缓存不可读写） | 完全回退至工作树本地编译与哈希追踪 | 行为同左（本地构建与追踪） | 行为同左（此时恢复开关不会反向激活已被禁用的共享缓存） |

在可写模式下，如果遇到冷编译（即缓存未命中），Lake 依然会先在本地编译生成相应产物，随后将构建生成的工件自动回写并登记到基于内容寻址的共享缓存池中。关闭全量恢复（即 `restoreAllArtifacts = false`）主要作用于后续命中缓存时的常规复用策略，它并不意味着所有构建产物都可以脱离本地磁盘——对于编辑器交互等必须依赖本地文件的目标（如 `.ilean`），Lake 仍会按需将其拉回本地。另外，如果远程仅命中了打包压缩的 `.ltar` 归档而缺少已解压的可直接解析工件，Lake 会先在当前工作树完成解压再继续后续处理；而在执行需要重新打包或对外发布工件的特定命令时，Lake 也可能会显式触发文件的本地落地恢复。

在缓存存储路径方面，纯裸 Lake 运行依赖环境变量 `LAKE_CACHE_DIR` 决定实际存放路径，只要配置了非空路径，其优先级就高于自动探测到的工具链缓存或系统默认缓存；若显式将该变量传入空值，则会禁用系统级缓存发现机制，工作区将自动回退到 `.lake/cache` 局部目录。在日常工程实践中，`qatlib-env prepare` 与 `qatlib-env run` 命令会自动计算并导出规范的非空缓存目录，并将 `LAKE_ARTIFACT_CACHE=true` 注入为工作区默认值；同时有意保持恢复开关不作预设，以确保包级别的显式配置具有最高解释权。对于多工作树并行开发的宿主环境，强烈建议在项目根配置中明确写入 `enableArtifactCache = true` 与 `restoreAllArtifacts = false`；与此同时，务必仔细核对依赖项各自的显式配置以及当前执行账户的环境变量——因为一旦某个依赖仍保持只读模式或开启了全量恢复，该依赖的产物依然会在各个工作树中分别全量落盘，造成不必要的磁盘开销。

[包配置](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/PackageConfig.lean) · [解析顺序](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/Monad.lean) · [工作区与缓存位置](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Config/Workspace.lean) · [模块工件读写](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Module.lean)

<a id="cache-layout"></a>

## 三类缓存和工作树

| 缓存来源 | 核心数据流与存储拓扑 |
| --- | --- |
| Lake 本地工件缓存 | Lake 先利用构建输入哈希索引工件映射表，再根据内容哈希在全局共享目录中精准定位 `.olean`、IR 等体积庞大的二进制产物。在前文所述的“开放读写且关闭全量恢复”模式下命中缓存时，Lake 无需为每个工作树单独复制这些大文件。这种基于内容寻址的设计具备极佳的通用性，完全不依赖底层的 reflink 特性或跨卷硬链接能力。 |
| `qatlib-env prepare` 的 R2 云端工件 | 各工作树严格按照清单文件分别检出各自的依赖源码。对于配置了 R2 远端缓存存储的包，系统会按照“精确提交哈希 → 兼容的同输入哈希映射 → 拓扑祖先候选”的顺序依次检索，并严格仅将服务端的 HTTP 404 响应判定为工件确实缺失。在完成现有与新下载工件的完整性校验后，将其解压安装至 `LAKE_CACHE_DIR/artifacts`，并调用 `lake cache add` 将其正式登记至 Lake 缓存索引中。此外，本地匹配的本机 checkpoint 也是一个独立的本地快速恢复来源。 |
| Mathlib 专用生态缓存 | 通过 `lake exe cache get` 命令下载并解包 Mathlib 官方维护的专用编译归档。在标准的 Lake 依赖目录布局下，解包后的构建产物会被置于当前工作树的 `.lake/packages/mathlib/.lake/build/` 目录下。需要注意，压缩归档文件与解包产物会各自占用独立的磁盘空间。 |

在多工作树协作场景中，配置 Git 本地镜像能够显著降低重复拉取依赖源码的网络开销。虽然每棵工作树都必须保持独立的依赖源码检出与 `.lake` 目录以隔离开发状态，但它们完全可以安全地共享同一个底层的全局工件缓存。

当需要回收磁盘空间时，应遵循由浅入深的原则：
- **仅回收派生构建数据**：可执行 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 提前预览将要清理的 `.lake/build` 与 `.lake/packages` 内容，核对安全无误后，再将参数替换为 `--apply` 正式执行。
- **回收整棵关联工作树**：如果需要清理已经废弃的旧关联工作树，先运行 `trash-checkouts` 进行安全预览；在确认该工作树内不存在未提交的独有材料、没有其他分支正在占用且无任何后台进程正在写入后，再执行安全回收。回收完成后，工具默认会自动注销并清理失效的 Git 工作树登记，同时完整保留原有的分支引用。在工具输出的报告中，文件移动操作与磁盘实际腾退释放的空间大小将分别独立核算。

<a id="windows"></a>

<a id="github-acceleration"></a>

## GitHub 受限网络加速（免改全局配置）

先用 `uv venv .venv` 创建隔离环境。若当前 Python 项目在 `./python` 中声明了带提交锁的 GitHub 依赖，可对本次安装临时改写 Git URL。Lean 项目则可在自身根目录通过 `QATLIB_GITHUB_PREFIX` 启动准备流程：

```bash
GIT_CONFIG_COUNT=1 \
  GIT_CONFIG_KEY_0='url.https://ghfast.top/https://github.com/.insteadOf' \
  GIT_CONFIG_VALUE_0='https://github.com/' \
  uv pip install --python .venv/bin/python ./python

QATLIB_GITHUB_PREFIX='https://ghfast.top/' qatlib-env prepare
```

`QATLIB_GITHUB_PREFIX` 只接受以 `/` 结尾的 HTTPS 代理前缀，工具为 Git 子进程追加 `insteadOf`。同一条 Git 改写也可用于 Lake 的 Git 拉取。`LAKE_PKG_URL_MAP` 则用于按包名选择本地镜像等替代地址。保持依赖清单里的上游 URL 和完整提交 SHA，并在已配置其他 `GIT_CONFIG_*` 条目时追加新编号。

只把公开依赖交给第三方代理。固定提交的对象 ID 绑定其树和文件内容，Git 在接收对象时校验哈希，调用者还须核对检出的提交与可信锁一致。代理仍可拒绝或延迟交付。分支名可由代理返回任意引用，Release 二进制也不在 Git 对象树内。执行下载的程序前，使用可信渠道发布的官方 SHA-256、签名或官方来源验证其身份。

## Windows 跨卷和长路径

在典型的 Windows 部署场景中，开发者常将缓存目录设在系统盘（如 `C:\lake-cache`），而将实际的工作树置于数据盘（如 `D:\work\project`）。由于 Windows NTFS 文件系统的限制，硬链接（Hard Link）只能在同一个磁盘分区卷内创建；一旦尝试跨卷建立硬链接，底层便会触发类似于 POSIX 语义下的 `EXDEV` 错误（其具体的 Windows 错误码与错误信息因调用层而异）。不过，缓存与工作树跨盘部署本身完全属于合法且受支持的使用方式，关键在于理解不同构建阶段的处理机制：

| 具体操作阶段 | 跨卷分盘时的底层行为与应对机制 |
| --- | --- |
| Lake 直接读取共享工件 | Lake 会直接从缓存卷读取目标工件文件，整个过程根本不涉及跨卷硬链接的创建，因此跨卷分盘对此无任何负面影响。 |
| Lake 落地工件或回写缓存 | 在恢复缺失的 `.ilean` 文件或执行全量恢复时，Lake 会优先尝试创建硬链接；若因跨卷导致硬链接失败，系统会自动平滑降级为将文件完整复制至工作树中。当把新编译完成的输出登记至共享缓存时，即使无法建立硬链接，也可以直接将工件字节流写入缓存；需要注意的是，如果开启了全量恢复模式，该降级复制操作将实质性消耗工作树所在卷的磁盘配额。 |
| `prepare` 安装 R2 云端工件 | 虽然工件哈希映射下载的临时状态可以存放在工作树中，但二进制工件的下载与安装暂存目录始终位于 `LAKE_CACHE_DIR` 内部。校验通过后的暂存文件必须能以原子、无覆盖的硬链接方式正式发布至 `artifacts/` 目录中。**该发布步骤并未设计跨卷复制的降级回退机制**，一旦发生跨卷硬链接错误将直接导致整个准备流程中断。因此，切勿将 `artifacts/` 目录软链接或重定向到其他磁盘卷，必须保证其位于同一个支持硬链接的本地可写 NTFS 卷中。 |

针对 Windows 平台下深层目录嵌套导致的超长路径问题，首要建议是尽量将工作区与缓存目录置于较短的磁盘根路径下。需要明确区分两套长路径机制的边界：Git for Windows 提供的 `core.longpaths=true` 配置仅能解除 Git 自身操作的路径长度限制；而若要让操作系统的 Win32 API 彻底放开传统 260 字符的 `MAX_PATH` 上限，除了必须在系统注册表中启用 `LongPathsEnabled` 之外，可执行程序自身还必须在其清单（manifest）中显式声明 `longPathAware`。因此对于 Lake、Python、外部归档解压工具以及系统回收站，均需分别确认其长路径兼容性。另外请注意，启用长路径支持并不能打破“NTFS 无法跨卷建立硬链接”的物理限制。

[Lake 保存与恢复实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Common.lean) · [Windows 硬链接](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions) · [Win32 路径上限](https://learn.microsoft.com/en-us/windows/win32/fileio/maximum-file-path-limitation) · [Git for Windows](https://gitforwindows.org/git-cannot-create-a-file-or-directory-with-a-long-path.html)

首次克隆含长路径的仓库时，可用 `git clone --config core.longpaths=true <URL>`，让该设置从首次检出起就生效，并由同仓的 linked worktree 继承。Python 工具的安装也有独立的跨盘路径。如果 uv 缓存位于 C: 而虚拟环境位于 D:，uv 会在硬链接不可用时复制文件。安装成功后仍会出现降级提示。这些复制品计入虚拟环境所在盘符的空间占用。

排查缓存未命中时，先区分 Git 的检出字节和缓存的输入哈希。可用 `git ls-files --eol` 查看索引、工作树及属性。Git 的 `core.autocrlf=true` 可能把索引中的 LF 转为工作树的 CRLF。Lean 4.35 的 Lake 模块输入使用会规范化 CRLF 的 `Hash.ofText`，同期 Mathlib 的下载键也在 `hashFileContents` 中做同样转换。按所用版本核对这些计算路径，并单独检查平台、依赖锁和编译参数。根仓的 `.gitattributes` 只覆盖该仓的文件，每个 Lake Git 依赖按它自己的属性检出。

在 Windows 原生 Lean 4.35.0 的单模块对照中，两份源码只改变行尾，Lake 生成相同的 `depHash`。LF 工作树先编译，CRLF 工作树随后报告 `Reused`，并从 C: 的默认 Lake 缓存复制 `.ilean` 至 D:。该对照验证的是原生 Lake 对文本输入和跨盘恢复的处理。

工具通过 Lake 物化锁定的 Git 依赖时，在 Windows 上为该子进程设置 `core.autocrlf=input`。新检出保留上游存储的行尾，显式的上游属性仍然适用。既有 CRLF 检出仍以规范化后的内容判断是否有改动。

多个工作树同时调用 `prepare` 时，Windows 通过缓存目录内的 `.fetch.lock` 串行化准备任务。工作树中普通的增量编译仍可并发进行。

从较早的工作树整目录复制 `.lake` 时，复制工具可能保留早于新检出的文件时间。实测同锁 Mathlib 的 `Chebyshev.olean` 和 `.trace` 都存在，复制后的输出时间仍早于目标树的 `.lean` 源文件。此时 `qatlib-env plan` 的时间戳筛查会将该模块估为冷模块；原生 Lake 则按 `depHash` 核验输入和工件。每个工作树通过各自的准备流程恢复构件，再以该树的准入结果安排编译。

[Lake 4.35 模块输入](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Module.lean) · [Lake 4.35 文本哈希](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Trace.lean) · [Mathlib 4.35 缓存哈希](https://github.com/leanprover-community/mathlib4/blob/56a005855ea17e0b0958f73ec801ae140b0a1679/Cache/Hashing.lean)

在 Windows 环境下，受管构建以及伴随编译任务的缓存准备流程均依托原生 Lake 运行，并借助 Windows Job Object 统一对整组进程施加 CPU 与内存的刚性约束。由于 `qatlib-env` 的默认重型模块匹配规则列表非空，且 Windows 缺乏专用的 POSIX 编译器包装层（Wrapper），既无法为匹配到的高负载模块单独分配隔离执行槽位，也无法在单次编译器调用中精确注入 `-M` 内存上限参数，因此受管 Lake 在检测到非空的重模块规则时会主动抛错拦截。针对 Windows 环境，请务必在生效的配置文件中显式配置 `heavy_patterns = []`，并通过 `qatlib-env config explain --json` 验证该配置已正确生效。关于 Job Object 的进程树初始化与生命周期清理机制，请详见 [Runner 进程隔离](runner.md#windows-job-object)。

<a id="note-concurrency"></a>

## 多工作树与并行预算

在并发执行构建时，`qatlib-env` 会综合换算并以 `jobs × lean_threads` 得到的总核心数与声明的整机内存预算，向本机的全局资源池申请调度租约。举例来说，若同时有两个构建任务各自申请配置为 4 进程与 4 线程，则系统账本中记录的总申请量将达到 32 个 CPU 配额。同一台物理机上的所有并发工作树共同受控于同一个全局账本，而在调度落到操作系统底层时，实际的 CPU 配额、内存硬上限及超时强杀机制则交由各平台的原生后端分别执行。

在调整任务的资源配额前，强烈建议先通过 `qatlib-env plan` 评估冷编译的波及范围与受影响的反向依赖规模。在操作系统差异层面：Linux 平台依托专门的编译器包装层，能够精细化支持重型模块的专用槽位分配与内存钳制；而 macOS 在操作系统层面缺乏与 Linux cgroup 或 Windows Job Object 等价的进程组聚合内存硬上限机制。因此，对于具有严格内存硬隔离要求的构建任务，建议优先部署在能够切实执行资源刚性限制的操作系统环境中。

[Runner 与进程排障](runner.md)

<a id="configuration"></a>

## 配置优先级

在没有任何显式配置的前提下，`qatlib-env` 会自动计算基础资源池：
- **内存池（Memory Pool）**：取“扣除宿主系统预留后的可用内存”与“最严格的可见 cgroup 限制配额”两者的较小值，并按 MiB 单位向下取整。其中，宿主预留量独立作用于系统级物理指标，取 2 GiB 与物理总内存 10% 两者之中的较大值。
- **CPU 池（CPU Pool）**：综合读取物理逻辑核心数、CPU 亲和性（Affinity）、系统 cpuset 分配以及 cgroup 限制配额，计算出四者共同约束下的可用核数交集。

在默认调度策略下，单次构建操作通常会被分配大约一半的系统可用 CPU 与内存资源：
- **CPU 预算分配**：单次构建最多分配 8 个核心；在资源充裕时，会始终为宿主前台预留至少 2 个核心。
- **内存预算分配**：单次构建上限为 16 GiB；若全局可用内存池不足 4 GiB，则直接按实际检测到的可用容量从严计算。
- **线程与进程控制**：每个 Lean 编译器进程默认最多分配 2 个并发线程（在单核受限环境下自动回退为 1 个线程）；同时，并行任务进程数还会受到“单编译器进程预估占用约 4 GiB 内存”的容量模型制约。若系统可用资源低于准入阈值，工具将直接阻断并拒绝调度，此时需先释放宿主资源或将任务迁移至容量充足的节点执行。开发者随时可通过 `qatlib-env config explain --json` 输出机器可读的配置分析报告，其中完整呈现了未覆盖的默认值、当前生效值、各字段来源以及潜在的配置冲突告警。

下列配置层级严格按照优先级**从低到高**排列；若出现同名配置项，高优先级层将无条件覆盖低优先级层。在任何环节，均可使用 `qatlib-env config explain` 实时核对最终生效的配置及其来源路径：

| 优先级（由低至高） | 配置来源层级 | 核心职责与解析机制 |
| :---: | --- | --- |
| 1（最低） | 默认值与项目推导 | 基于宿主硬件与 cgroup 环境动态探测的保守默认值；同时从项目根目录的 `lakefile.toml` 动态解析主库根、测试根及 `srcDir`，并结合工程目录拓扑自动推导仓库标识与对应工作流。遵循项目的架构约束与维护边界，具体工程设计原则可参阅 `DESIGN.md`。 |
| 2 | Runner 兼容配置 | 由环境变量 `QATLIB_RUNNER_CONFIG` 所显式指向的外部 Runner 实例配置文件。 |
| 3 | 个人本机设置 | 先读取 `~/.qatlib/env.toml`。设置 `QATLIB_HOME` 后，再读取其下的 `env.toml`，其中的同名字段覆盖前者。 |
| 4 | 项目共享策略 | 依次读取根目录 `pyproject.toml` 中的 `[tool.qatlib-env]` 和根目录的 `qatlib-env.toml`，后者的同名字段优先。两个根目录文件均不存在时，才读取 `python/pyproject.toml`。 |
| 5 | 工作树本地覆盖 | 单个工作树根目录下的 `.qatlib-env.local.toml`。当次级工作树（Secondary Worktree）未独立放置该文件时，可只读继承主检出目录中的同名配置。 |
| 6 | 环境变量覆盖 | 针对单机或 CI 临时注入的环境变量，例如 `QATLIB_POOL_BUDGET_MIB`、`QATLIB_POOL_CPU`、`QATLIB_JOBS` 等。在线程控制维度，变量优先级为 `QATLIB_LEAN_THREADS` 高于原生的 `LEAN_NUM_THREADS`。 |
| 7（最高） | 单次命令行参数 | 在命令执行时通过 CLI 标志即时传入的临时参数，如 `qatlib-env run --jobs 1 --lean-threads 2 -- lake build <模块>`。 |

在工程管理中，若项目团队希望统一限制本地冷编译的模块上限与反向依赖波及范围（同时避免将构建策略与特定宿主机的硬件规格强耦合），推荐在代码仓库中仅提交如下通用的预算策略约束（若统一维护在 `pyproject.toml` 中，将表头替换为 `[tool.qatlib-env.budget]` 即可）：

```toml
[budget]
max_cold_modules = 15
max_reverse_modules = 15
require_explicit_local_heavy = true
```

- **阈值语义与临时覆盖**：将模块上限阈值直接设为 `0` 时，表示严禁在本地触发对应类别的冷编译或反向波及编译。在日常排错或临时调试场景中，可通过环境变量 `QATLIB_MAX_COLD_MODULES` 与 `QATLIB_MAX_REVERSE_MODULES` 实现即时覆盖。若配置文件中存在拼写错误的未知键名或字段类型不匹配，工具会在启动阶段直接抛错终止，并明确指出错误所在的文件位置与字段路径。
- **高开销豁免与等待超时**：CLI 参数 `--allow-local-heavy` 仅对当前单次调用有效，且该豁免依然无条件服从于全局共享池的物理容量、系统级 cgroup 控制器以及租约超时硬限制；环境变量 `QATLIB_POOL_WAIT_TIMEOUT` 仅用于调整在租约排队阶段等待旧任务释放资源的超时时间，如果单次构建请求所需的资源体量本身已经超过了共享池的物理承载硬上限，系统依然会立即报错拒绝，而不会无效排队。

<a id="local-paths"></a>

## 本机数据目录

`qatlib-env config explain --json` 的 `paths` 块仅用于展示各数据存储区域的实际生效绝对路径，该命令属于只读检查，不会主动创建任何文件目录。下表中所指的工具根目录统一代表 `${QATLIB_HOME:-~/.qatlib}`：

| 数据分类 | 默认落盘位置 | 路径覆盖与多环境兼容规则 |
| --- | --- | --- |
| 个人配置文件 | `~/.qatlib/env.toml` | `$QATLIB_HOME/env.toml` 可逐字段覆盖默认文件。 |
| R2 凭据文件 | `${QATLIB_HOME:-~/.qatlib}/secrets` | 环境变量 `QATLIB_R2_KEY_FILE` 具备最高优先级。若重新指定了工具根目录但新路径下尚无凭据文件，工具会自动尝试读取旧位置以保持兼容。 |
| 构建池状态与租约账本 | `${QATLIB_HOME:-~/.qatlib}/state/pool/ledger.json` 与 `ledger.lock` | 当系统中显式配置了 `XDG_STATE_HOME` 或 `XDG_CACHE_HOME` 时，系统依然会沿用对应目录下的 `qatlib-env/pool` 路径；若历史默认目录 `~/.cache/qatlib-env/pool` 中已存在账本或锁文件，将继续保持就地读写。 |
| 检索索引库 | `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3` | CLI 参数 `--db` 具备最高优先级。在执行查询检索时，系统仍可兼容读取既有的 XDG 缓存文件，详见[本地索引](lean-docs.md#lexical-index)。 |
| 云端作业回执 | `${QATLIB_HOME:-~/.qatlib}/state/cloud/<corpus-key>.json` | CLI 参数 `--state` 具备最高优先级。若检出根目录下已存在 `.qatlib-env/cloud-state.json`，系统将优先复用并读写该回执文件。 |
| Lake 工件与本地检查点 | Lake 工件存储目录及其 `checkpoints/` 子目录 | 环境变量 `LAKE_CACHE_DIR` 具有最高优先级；若未特别声明，则沿用 elan 对应工具链的内置目录或独立 Lake 实例的 XDG 缓存路径，并完整保留历史数据。 |

在同一台宿主机上并发触发的各项构建调用，必须使用同一套全局账本与锁文件机制。归档或迁移前，应先结束该资源池的在途构建任务，再统一各调用方的工具版本与运行环境。若旧有账本或锁文件仍在原处，工具会复用该资源池。

用 `qatlib-env workspace scan /path/to/current /path/to/archive --inspect-ignored` 扫描显式指定的目录，并检查 Git 忽略的文件。扫描只读，可用 `--exact` 限定为已知检出根目录。工作树状态由对应工程仓库维护。
