# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

<!-- human-contract:start -->
`qatlib-tools` 维护 Lean 数学库和下游研究仓共用的工程手册与工具。各项目维护自己的数学接口、书目和合入标准。

日常操作直接使用 `git`、GitHub 的 `gh`、Gitea 的 `tea` 或 `gt`，也可以调用平台 API。多个仓库都需要处理的平台差异、检查结果汇总和写入前的提交校验，由公共工具负责。
<!-- human-contract:end -->

## 命令

| 命令 | 用途 |
| --- | --- |
| `qatlib-env` | 准备 Lean 依赖和缓存，受控编译，管理工作树 |
| `qatlib-retrieve` | 检索 Lean 声明、源码签名和书目 |
| `qatlib-forge` | 核对 GitHub 和 Gitea 的 PR 检查结果与预期提交 |

安装后用 `qatlib-env docs` 离线阅读本页。用 `qatlib-env docs --list` 列出可读的专题，再用 `qatlib-env docs <专题>` 阅读。命令参数以当前安装版本的 `--help` 为准。

<a id="quickstart"></a>

## 安装

一次安装即可提供 `qatlib-env`、`qatlib-retrieve` 与 `qatlib-forge` 三个命令行入口。请按使用场景选择对应方式，全程在隔离虚拟环境中安装，不要直接对系统解释器执行裸 `pip install`：

### 项目锁定版本

运行项目门禁与日常开发时，应使用项目内 `python/pyproject.toml` 锁定的配套 `qatlib-tools` 版本，在 `example-library` 根目录创建虚拟环境并安装 `./python` 包：

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

用只读命令 `qatlib-env config explain --json` 查看 `resources`（探测容量）、`automatic`（无覆盖时的默认值）以及 `values` 和 `configuration.routing`（生效值）。结合 `sources`、`configuration.sources` 和 `warnings` 判断是否需要调整。该命令不会准备依赖或申请构建配额。长期设置可写入 `~/.qatlib/env.toml`，也可用支持的命令行参数调整单次构建。工具保留显式配置的值，实际运行还受构建池和平台资源限额约束。 [配置来源与资源探测](docs/operations.md#configuration)

<a id="artifact-cache"></a>

## 编译与共享工件缓存

```bash
qatlib-env prepare
qatlib-env plan ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
qatlib-env run -- lake build ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
```

有匹配的依赖产物后，可以运行 `qatlib-env run -- lake --no-build lean <File.lean>` 检查单个文件。发布下游可用的定义时，用普通 `module` 消费者验证名称可见性和实际展开行为。非 `module` 文件可读到模块的私有信息，容易掩盖下游编译错误。

三类工件的存放方式不同。

1. **Lake 本地共享工件**。Lean v4.35 启用 `enableArtifactCache = true` 并保持全量恢复关闭时，Lake 根据输入跟踪信息找到工件的内容哈希，直接从 `LAKE_CACHE_DIR` 读取 `.olean` 等匹配工件。各工作树仍有独立的 `.lake`，无需重复拷贝或解包这些共享工件。这种共享读取适用于 Linux、macOS 和 Windows，无需 reflink 或跨工作树硬链接。
2. **R2 远端工件**。对配置了 R2 缓存的包，`qatlib-env prepare` 校验可用的共享工件，补齐缺失文件并注册映射。
3. **Mathlib 专用缓存**。在标准依赖布局下，Mathlib 的 `lake exe cache get` 会把归档解包到当前工作树的 `.lake/packages/mathlib/.lake/build/`。 [Lean 模块、编译与缓存](docs/lake.md)

要回收闲置工作树的磁盘空间，先用 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 预览 `.lake/build` 和 `.lake/packages`。回收整个关联工作树时，使用 `qatlib-env workspace trash-checkouts --checkout <路径>` 预览，确认后改用 `--apply`。成功移入回收站后，工具默认清理所属 Git 仓库的失效工作树登记。 [工作树与工件存储](docs/lake.md#cache-layout)

<a id="retrieval"></a>

## Lean 文档和检索

`qatlib-retrieve index` 建立本地词法索引，默认写入 `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/<corpus-key>.sqlite3`。键取解析后的项目根绝对路径的 SHA-256 前 24 个十六进制字符。用 `--db` 可以另选文件，路径须在所有纳入索引的源码仓之外。查询可定位声明及其外层 `section` 的 `variable`，无需启动 Lean 编译器或 Lake。

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization' --kind theorem
qatlib-retrieve query --cite GilyenSuLowWiebe2019
```

模块文档使用 `/-! ... -/`，声明注释使用 `/-- ... -/`。跨声明链接使用反引号包裹完整限定名，书目引用使用 `[CiteKey]`。文档网站和编辑器 hover 的渲染条件不同。 [Lean 文档、书目与源码检索](docs/lean-docs.md)

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

GitHub 有独立的草稿状态。Gitea 用实例配置的标题前缀标记草稿，提交状态摘要也可能与实际作业结论不同。核查时以同一 `head` SHA 为准。 [GitHub 和 Gitea 上的拉取请求](docs/forge.md)

<a id="promotion"></a>

## 共享成果进入公开库

开发仓整理可复用成果，预发仓核验整文件依赖和真实消费者，公开库发布稳定接口和预编译工件。下游在公开版本和工件就绪后更新依赖锁，再清理已收录的旧源码和增量书目。 [共享成果如何进入公开库](docs/promotion.md)

<a id="field-notes"></a><a id="runner"></a>

## 构建排障与 Runner

构建范围、cgroup 内存计费、取消与子进程回收、Runner 账户配置各有不同的检查方法。 [构建资源与排障](docs/operations.md)

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

wheel 将根 `README.md` 按原始字节打包为 `qatlib_env/_manual.md`，并将 `docs/` 打包到 `qatlib_env/docs/`。`qatlib-env docs --list` 只列出 `docs/` 直接下属的 Markdown 专题，用文件名（不含 `.md`）选择。用 `pipx` 或干净的 `venv` 安装当前 wheel 后，可在仓库根目录用 `qatlib-env docs | cmp README.md -` 比对主手册，用 `qatlib-env docs lake | cmp docs/lake.md -` 比对专题。确认命令来自刚安装的环境。

本项目采用 [Apache License, Version 2.0](./LICENSE) 开源许可，与公开库 [qatlib](https://github.com/quantum-atlas-ai/qatlib) 保持一致。
