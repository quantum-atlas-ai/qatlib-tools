# Lean 模块、编译与缓存

<a id="modules"></a>

## 模块的公开接口

Lean v4.35 的 `module` 文件只能导入同样使用模块系统的文件。迁移时先处理依赖，再迁移消费者。模块内声明默认私有，可通过 `public section` 或声明上的 `public` 让名称对下游可见。`public import` 会把被导入模块的公开接口传给下游。下游需要展开定义时，还要核对 `@[expose]`，并用依赖展开的例子验证。普通非 `module` 文件能读取模块的私有信息，使用这类文件作为测试容易掩盖模块消费者遇到的错误。 [Lean v4.35 模块手册](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/)

<a id="incremental-build"></a>

## 增量编译与单文件检查

`lake build <Module>` 根据导入关系和输入跟踪信息构建所选模块及其依赖。已有且仍匹配输入的产物可直接复用。导入顶层聚合模块可能让单文件检查牵涉整个库。保留必要的叶模块导入，先用 `qatlib-env plan <Module>` 查看冷模块数、反向依赖和依赖锁状态，再决定检查范围。

`lake --no-build lean <File.lean>` 让 Lake 按项目配置准备导入环境，再用已有依赖产物检查文件。如果有必须构建的依赖，命令会失败。可先用 `qatlib-env run -- lake build <Module>` 补齐产物，再用 `qatlib-env run -- lake --no-build lean <File.lean>` 检查正在改的文件。启用共享缓存时，直接运行裸 `lean` 或只遍历本地 `.lake/build` 都可能漏掉 Lake 已知的工件。 [Lake v4.35 命令实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/CLI/Main.lean)

<a id="cache-layout"></a>

## 工作树与工件存储

| 来源 | 工件在哪儿 |
| --- | --- |
| Lake 本地工件缓存 | 输入跟踪信息映射到内容哈希后，Lake 会从 `LAKE_CACHE_DIR` 读取对应的 `.olean` 等工件。当启用 `enableArtifactCache = true` 并保持全量恢复关闭时，工件无需被复制到每个工作树中。该机制适用于 Linux、macOS 和 Windows，且不强制要求文件系统支持 reflink（写时复制）或跨工作树硬链接；而确实需要在工作树中就位的产物（例如 `.ilean`）仍会按需写入，若硬链接不可用，Lake 会自动回退为复制文件。 |
| `qatlib-env prepare` 的 R2 工件 | 先依据 `lake-manifest.json` 检出各工作树各自的依赖源码。对于配置了远端的包，依次尝试确切提交、可用的同输入映射以及祖先映射，且只把映射请求返回的 HTTP 404 判定为候选缺失。随后下载、校验缺失工件并安装至 `LAKE_CACHE_DIR/artifacts`，再通过 `lake cache add` 注册映射关系。目前安装经校验的文件需要缓存目录内部支持硬链接；若本机存在匹配的 checkpoint，也可单独恢复到工作树。 |
| Mathlib 的专用缓存 | `lake exe cache get` 会按 Mathlib 自身的专用格式下载归档并解包。在标准依赖布局下，Mathlib 产物会落入当前工作树的 `.lake/packages/mathlib/.lake/build/`；需要注意，压缩归档本身的下载缓存与这些解包后的产物文件是相互独立的。 |

Lake 复用本机编译工件，Git 镜像复用 Git 对象。各工作树仍应保有独立的 `.lake`、构建状态和依赖检出。外部工具依赖传统构建目录时，可以按次设置 `LAKE_RESTORE_ARTIFACTS=1`。先核对依赖包的 `restoreAllArtifacts` 设置和可用磁盘空间。 [Lake 模块工件](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Module.lean) · [Lake 工件写入](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Common.lean)

先用 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 预览 `.lake/build` 和 `.lake/packages`。核对本地改动后，将 `--dry-run` 换成 `--apply`，把相关目录移入系统回收站。此操作保留共享工件目录。回收整个关联工作树时使用 `qatlib-env workspace trash-checkouts --checkout <路径>` 预览，确认后加 `--apply`。成功移动后默认清理所属仓库的失效工作树登记，并保留分支引用。用 `--no-prune` 可保留登记。
