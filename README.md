# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

`qatlib-tools` 维护 Lean 数学库和下游研究仓共用的工程手册与工具。各项目维护自己的数学接口、书目和合入标准。

日常操作直接使用 `git`、GitHub 的 `gh`、Gitea 的 `tea` 或 `gt`，也可以调用平台 API。多个仓库都需要处理的平台差异、检查结果汇总和写入前的提交校验，由公共工具负责。

## 命令

| 命令 | 用途 |
| --- | --- |
| `qatlib-env` | 准备 Lean 依赖和缓存，受控编译，管理工作树 |
| `qatlib-retrieve` | 检索 Lean 声明、源码签名和书目 |
| `qatlib-forge` | 核对 GitHub 和 Gitea 的 PR 检查结果与预期提交 |

安装后用 `qatlib-env docs` 离线阅读本页。用 `qatlib-env docs --list` 列出可读的专题，再用 `qatlib-env docs <专题>` 阅读。命令参数以当前安装版本的 `--help` 为准。

## 工程机制手册导览

| 专题 | 离线命令 | 内容 |
| --- | --- | --- |
| [编译环境、缓存与资源控制](docs/build.md) | `qatlib-env docs build` | Lake 配置、模块可见性、共享缓存、Windows 跨盘、本机预算 |
| [持续集成 Runner 与进程排障](docs/runner.md) | `qatlib-env docs runner` | cgroup 内存计费、取消任务时的进程回收和部署验收 |
| [Lean 文档、书目与源码检索](docs/lean-docs.md) | `qatlib-env docs lean-docs` | doc-gen4 链接、增量书目校验、静态词法索引 |
| [GitHub 和 Gitea 上的拉取请求](docs/forge.md) | `qatlib-env docs forge` | 两种草稿机制、CI 状态核对、并发合入与删分支 |
| [共享成果如何进入公开库](docs/promotion.md) | `qatlib-env docs promotion` | 整文件依赖、消费者验证、草稿演练和发布后切换依赖 |

<a id="quickstart"></a>

## 安装

一次安装即可提供 `qatlib-env`、`qatlib-retrieve` 与 `qatlib-forge` 三个命令行入口。请按使用场景选择对应方式，全程在隔离虚拟环境中安装，不要直接对系统解释器执行裸 `pip install`：

### 项目锁定版本

运行项目检查和日常开发时，使用该项目锁定的工具版本。如果项目把锁定依赖声明在 `python/pyproject.toml` 中，可在项目根目录新建虚拟环境并安装 `./python`。其他布局按项目自己的安装说明操作。

- **使用 `uv`**

  ```bash
  uv venv .venv
  uv pip install --python .venv/bin/python ./python
  source .venv/bin/activate
  qatlib-env --help
  qatlib-retrieve --help
  ```

- **使用 `venv` + `pip`**

  ```bash
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install ./python
  source .venv/bin/activate
  qatlib-env --help
  qatlib-retrieve --help
  ```

Windows PowerShell 下改用 `.venv\Scripts\Activate.ps1` 激活环境，并将安装命令中的解释器路径换为 `.venv\Scripts\python.exe`，例如运行 `uv pip install --python .venv\Scripts\python.exe ./python` 或 `.venv\Scripts\python.exe -m pip install ./python`。若需单独测试某个版本，可在另一个环境中安装该提交构建的 wheel，并根据构建记录核对它对应的提交。

### 独立命令行安装

若希望不依赖具体项目的虚拟环境、在任意目录下直接调用 `qatlib-env`、`qatlib-retrieve` 与 `qatlib-forge`，可将下列命令中的 `/path/to/qatlib-tools` 按需替换为本地检出路径、已下载的 `.whl` 文件，或可访问的仓库固定提交 `'git+https://github.com/quantum-atlas-ai/qatlib-tools.git@<提交 SHA>'`：

- **使用 `uv`**：运行 `uv tool install /path/to/qatlib-tools`，由 `uv` 自动维护独立工具环境；若终端提示找不到命令，可先用 `uv tool dir --bin` 查看命令目录，再运行 `uv tool update-shell` 将其加入 `PATH`。
- **使用 `pipx`**：运行 `pipx install /path/to/qatlib-tools`；若命令目录尚未加入 `PATH`，可执行 `pipx ensurepath` 补齐。
- **使用 `venv` + `pip`**：创建专用虚拟环境，将它的 `bin` 目录加入日常使用的 shell 的 `PATH`。Windows 下将对应的 `Scripts` 目录加入用户 `PATH`。

  ```bash
  python3 -m venv ~/.local/share/qatlib-tools-venv
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install --upgrade pip
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install /path/to/qatlib-tools
  export PATH="$HOME/.local/share/qatlib-tools-venv/bin:$PATH"
  ```

<a id="configuration"></a>

## 构建资源

未显式设置预算时，工具根据宿主可用内存、CPU 亲和性和可见的 cgroup v1/v2 限额计算默认资源池。计算宿主内存余量时，扣除 2 GiB 和物理内存 10% 中的较大值。完全使用默认值时，单次构建通常使用约一半的池容量，上限为 8 个 CPU、16 GiB 内存和 4 个编译进程。小池可用其全部余量，单核环境默认一个编译进程和一个 Lean 线程。

用只读命令 `qatlib-env config explain --json` 查看 `resources`（探测容量）、`automatic`（无覆盖时的默认值）以及 `values` 和 `configuration.routing`（生效值）。结合 `sources`、`configuration.sources` 和 `warnings` 判断是否需要调整。该命令不会准备依赖或申请构建配额。长期设置可写入 `~/.qatlib/env.toml`，也可用支持的命令行参数调整单次构建。工具保留显式配置的值，实际运行还受构建池和平台资源限额约束。 [配置来源与资源探测](docs/build.md#configuration)

## 本机目录

工具自己的默认根目录是 `~/.qatlib`，可用 `QATLIB_HOME` 改到另一处。个人配置为 `env.toml`，检索索引在 `retrieve/`，新构建池账本和云端作业回执分别在 `state/pool/` 和 `state/cloud/`。查看 `qatlib-env config explain --json` 的 `paths` 可以核对实际读写位置。升级后仍可读旧配置和索引，已有构建池继续使用原来的账本和锁。Lake 工件、上游 CLI 登录配置和历史工作树池各有自己的位置。 [目录与兼容规则](docs/build.md#local-paths)

<a id="artifact-cache"></a>

## 编译与共享工件缓存

```bash
qatlib-env prepare
qatlib-env plan MyLib.Facts
qatlib-env run -- lake build MyLib.Facts
```

Lake 按导入关系编译目标及其依赖。依赖工件已匹配时，可用 `qatlib-env run -- lake --no-build lean <File.lean>` 检查单个文件，缺少须构建的依赖时会报错。

Lean v4.35 的 `module` 中，声明默认私有。`public section` 或声明上的 `public` 让下游可引用名称，`public import` 转发被导入模块的公开接口。下游若还要展开普通定义体，需核对 `@[expose]`，并用确实依赖展开的例子验证。非 `module` 文件可以读到模块的私有信息，用它测试可能看不出真正的模块消费者拿不到名称或定义体。

三类工件的存放方式不同。

1. **Lake 本地共享工件**。直接运行 Lean v4.35 的 Lake 时，若未设置缓存选项，Lake 默认只读缓存，命中模块后仍会将工件恢复到该包的构建目录。多个工作树日常开发可设 `enableArtifactCache = true`、`restoreAllArtifacts = false`。命中共享目录里的原生工件时，Lake 根据输入跟踪信息找到内容哈希，直接读取 `LAKE_CACHE_DIR` 里的 `.olean`，各工作树仍保有自己的 `.lake`。供编辑器使用的 `.ilean` 仍会落盘。直接读取共享目录无需 reflink 或跨工作树硬链接。
2. **R2 远端工件**。对配置了 R2 缓存的包，`qatlib-env prepare` 按锁准备依赖源码，下载并校验缺失的工件，再把映射注册到 Lake 本地缓存。发布单个工件时所需的硬链接发生在缓存目录内部，所以 Windows 上可以让缓存和工作树分居两个盘符。
3. **Mathlib 专用缓存**。在标准依赖布局下，Mathlib 的 `lake exe cache get` 会把归档解包到当前工作树的 `.lake/packages/mathlib/.lake/build/`。

字段默认值、根包与依赖包覆盖次序，以及恢复和下载遇到跨盘时的行为，见 [Lake 缓存开关](docs/build.md#cache-flags)和 [Windows 跨盘与路径长度](docs/build.md#windows)。

要回收闲置工作树的磁盘空间，先用 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 预览 `.lake/build` 和 `.lake/packages`。回收整个关联工作树时，使用 `qatlib-env workspace trash-checkouts --checkout <路径>` 预览，确认后改用 `--apply`。成功移入回收站后，工具默认清理所属 Git 仓库的失效工作树登记。 [工作树与工件存储](docs/build.md#cache-layout)

<a id="retrieval"></a>

## Lean 文档和检索

`qatlib-retrieve index` 建立本地词法索引，默认写入 `${QATLIB_HOME:-~/.qatlib}/retrieve/<corpus-key>.sqlite3`。键取解析后的项目根绝对路径的 SHA-256 前 24 个十六进制字符。用 `--db` 可以另选文件，路径须在所有纳入索引的源码仓之外。查询可定位声明及其外层 `section` 的 `variable`，无需启动 Lean 编译器或 Lake。

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization' --kind theorem
qatlib-retrieve query --cite GilyenSuLowWiebe2019
```

用 `/-! ... -/` 说明模块职责，用紧接声明的 `/-- ... -/` 写语义、前提和用法。跨声明引用用反引号包住完整限定名，书目引用用 `[CiteKey]` 对应 `docs/references.bib`。开发仓可只维护新增书目，校验时和锁定的公开基线一同检查缺键、重复键和同键异文。

doc-gen4 生成整站时有符号表、页面路由和书目索引，因此可把声明名和书目键变成跨页链接。编辑器的 Lean LSP hover 只渲染当前声明的文档，没有整站路由和书目索引。要在 hover 中打开论文，可以加普通 Markdown 链接指向 DOI 或 arXiv。 [Lean 文档、书目与源码检索](docs/lean-docs.md)

<a id="forge"></a>

## GitHub 和 Gitea

日常查看、创建和评论 PR 可直接使用 `gh`、`tea` 或 `gt`。需要汇总多个接口的检查结果，或带预期提交执行写操作时，用下列命令。

<a id="cli-reference"></a>

| 命令 | 用途 |
| --- | --- |
| `qatlib-forge pr checks` | 汇总同一 PR head 上的相关检查和实际作业 |
| `qatlib-forge pr paths` | 核对构建路径和复用既有 CI 结果的条件 |
| `qatlib-forge pr activity` | 过滤可识别的机器人活动，查看冷置候选 |
| `qatlib-forge pr merge` | 带预期 head 合并 |
| `qatlib-forge branch delete` | 核对远端 SHA 与保护规则后删除分支 |

GitHub 有独立的 `isDraft`，并分别提供 check-runs、提交 statuses 和 Actions 作业接口。Gitea 的草稿状态跟随实例配置的标题前缀，如 `WIP:` 或 `[WIP]`。草稿里的 Build 可能已条件跳过，外层提交摘要仍是 `pending`。核查时按同一个 PR head SHA 查运行和具体作业，区分 `skipped`、尚未运行和成功。

多位贡献者和智能体同时提交草稿时，一次全库编译可能耗时数十分钟，静态文档检查常常只需约一分钟。文档改动没有命中构建路径时，按仓库规则执行静态检查。若受测 head 没变，主线新增的也是不命中构建规则的文档，且与 PR 改动互不相交，可以复用该 head 已验证的成功作业，省去一轮重复编译。路径规则、受测基线和作业证据仍要逐项核对。

合并或删分支时，审阅和操作之间仍可能有新推送。合并把预期 head 交给服务端核对。删分支先核对默认分支和保护规则，再由 Git 的 `--force-with-lease` 要求远端引用仍指向预期 SHA。 [GitHub 和 Gitea 上的拉取请求](docs/forge.md)

<a id="promotion"></a>

## 共享成果进入公开库

孵化仓整理可复用成果，预发仓核验整文件依赖和真实消费者，公开库发布稳定接口和预编译工件。预发通过后，下游可以开草稿提前演练切换接口。正式版本和工件就绪后，再用同一张 PR 更新依赖锁并清理旧源码和已收录的增量书目。各仓自己的晋升说明负责本项目的目录、评审和收录标准。 [共享成果如何进入公开库](docs/promotion.md)

<a id="field-notes"></a><a id="runner"></a>

## 构建排障与 Runner

Linux cgroup v2 的 `memory.current` 同时计入匿名内存和文件页缓存（`file`）。大量工件读写结束后，即使编译进程已经退出，页缓存仍可能占用该组的账面额度。排查准入等待时，应同时查看 `memory.current`、`memory.stat` 和各级限制。

常驻 Runner 取消单次作业通常不会停止整个服务，因此不会触发服务的 `KillMode=control-group`。在 Linux 上，工具以 `PR_SET_PDEATHSIG` 让守卫在父进程退出时收到信号，用 `PR_SET_CHILD_SUBREAPER` 接管失去父进程的子孙以便回收。在可用的 systemd 后端，重任务还可进入单独的瞬态 scope，取消时清扫该任务所属控制组。

`jobs` 是并行编译进程数，`lean_threads` 是每进程线程数。构建池按两者相乘计算 CPU 申请，所以四个进程各四线程会申请 16 个 CPU 配额。并行工作树应共用同一账本，各工作树的 `.lake` 仍各自独立。 [持续集成 Runner 与进程排障](docs/runner.md)

<a id="self-test"></a>

## 工具仓自测与离线手册校验

用 Python 3.12 运行 `uv lock --check`、`uv build` 和 `uv run --with pytest --with hatchling --with jsonschema pytest -q -rs`，与 GitHub CI 的构建及全量测试一致。

没有 `uv` 时，可以在新建的虚拟环境内用 `build` 生成分发包，再运行自测。直接调用 `uv` 的安装测试留待具备 `uv` 的环境复验。先完成构建，确保 `dist/` 中用于测试的 wheel 来自当前源码。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e . build pytest hatchling jsonschema
.venv/bin/python -m build
.venv/bin/python -m pytest -q -rs --ignore=tests/migrated_dev/test_migrated_dev_smoke_packages.py
```

wheel 将根 `README.md` 按原始字节打包为 `qatlib_env/_manual.md`，并将 `docs/` 打包到 `qatlib_env/docs/`。`qatlib-env docs --list` 只列出 `docs/` 直接下属的 Markdown 专题，用文件名（不含 `.md`）选择。用 `pipx` 或干净的 `venv` 安装当前 wheel 后，可在仓库根目录用 `qatlib-env docs | cmp README.md -` 比对主手册，用 `qatlib-env docs build | cmp docs/build.md -` 比对专题。确认命令来自刚安装的环境。

本项目采用 [Apache License, Version 2.0](./LICENSE) 开源许可。
