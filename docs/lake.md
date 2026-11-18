# Lean 模块、编译与缓存

<a id="modules"></a>

## 模块的公开接口

Lean v4.35 的 `module` 文件只能导入也使用模块系统的文件。迁移时先处理依赖，再处理消费者。模块内声明默认私有，`public section` 或声明上的 `public` 使名称可被下游引用。`public import` 把被导入模块的公开接口传给下游。定义需要在下游展开时，还要核对 `@[expose]`，并用确实依赖展开的例子验证。`public` 使名称可见，不能替代展开所需的定义体。普通非 `module` 文件能读到模块的私有信息，作为测试时容易掩盖真正的模块消费者遇到的错误。 [Lean v4.35 模块手册](https://lean-lang.org/doc/reference/4.35.0/Source-Files-and-Modules/)

<a id="incremental-build"></a>

## 增量编译与单文件检查

`lake build <Module>` 根据导入关系和输入跟踪信息构建所选模块及所需依赖。已有且仍匹配输入的产物可直接复用。若导入顶层聚合模块，即使只检查一个文件也可能牵涉整个库。保留必要的叶模块导入，先看 `qatlib-env plan <Module>` 的冷模块数、反向依赖和依赖锁错误，再决定是否运行本机全量检查。

`lake --no-build lean <File.lean>` 让 Lake 按项目配置为该文件准备导入环境，并在已有依赖产物上进行类型检查。任何尚需构建的依赖会让命令失败。用 `qatlib-env run -- lake build <Module>` 补齐产物，再用 `qatlib-env run -- lake --no-build lean <File.lean>` 检查正在改的文件。在开启共享缓存的工作区，直接运行裸 `lean` 或遍历本地 `.lake/build` 都可能漏掉 Lake 已知的工件。 [Lake v4.35 命令实现](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/CLI/Main.lean)

<a id="cache-layout"></a>

## 工作树与工件存储

| 来源 | 工件在哪儿 |
| --- | --- |
| Lake 本地工件缓存 | 输入跟踪信息映射到内容哈希，Lake 从 `LAKE_CACHE_DIR` 读取对应的 `.olean` 等工件。启用 `enableArtifactCache = true` 并保持全量恢复关闭时，工件无需复制到每个工作树。适用于 Linux、macOS 和 Windows，不要求 reflink（写时复制）或跨工作树硬链接。需要在工作树出现的产物仍会按需写入，例如 `.ilean`，Lake 会在硬链接不可用时复制该文件。 |
| `qatlib-env prepare` 的 R2 工件 | 先按 `lake-manifest.json` 检出各工作树自己的依赖源码。对配置了远端的包，尝试确切提交、可用的同输入映射以及祖先映射。只把映射请求的 HTTP 404 当作候选缺失。下载、校验并将缺失工件安装到 `LAKE_CACHE_DIR/artifacts`，再通过 `lake cache add` 注册映射。安装经过校验的文件目前需要缓存目录内部支持硬链接。本机匹配的 checkpoint 可单独恢复到工作树。 |
| Mathlib 的专用缓存 | `lake exe cache get` 按自己的格式下载归档并解包。标准依赖布局下 Mathlib 产物落在当前树的 `.lake/packages/mathlib/.lake/build/`。压缩归档的下载缓存与这些解出的文件是两回事。 |

Lake 依据本机缓存来决定复用，Git 镜像复用的是 Git 对象。工作树的 `.lake`、构建状态和依赖检出应独立。只有外部工具依赖传统构建目录时才按次设置 `LAKE_RESTORE_ARTIFACTS=1`，先核对依赖自己的 `restoreAllArtifacts` 配置及目标磁盘空间。 [Lake 模块工件](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Module.lean) · [Lake 工件写入](https://github.com/leanprover/lean4/blob/v4.35.0/src/lake/Lake/Build/Common.lean)

只处理旧树的可再生成目录时，先由 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 预览 `.lake/build` 和 `.lake/packages`。核对本地变更后用 `--apply` 移入系统回收站。这不清理共享缓存。若回收整个关联工作树，用 `trash-checkouts`，成功移动后工具会整理所属仓库的失效工作树登记，保留分支引用。详情见 `qatlib-env workspace --help`。
