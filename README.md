# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

<!-- human-contract:start -->
`qatlib-tools` 维护 Lean 数学库和下游研究仓共用的工程手册与工具。各项目维护自己的数学接口、书目和接收规则。

日常操作直接使用 `git`、GitHub 的 `gh`、Gitea 的 `tea` 或 `gt`，也可以调用平台 API。多个仓库都需要处理的平台差异、检查汇总或写入前置条件，由公共工具负责。
<!-- human-contract:end -->

## 命令

| 命令 | 用途 |
| --- | --- |
| `qatlib-env` | 准备 Lean 依赖和缓存，受控编译，管理工作树 |
| `qatlib-retrieve` | 检索 Lean 声明、源码签名和书目 |
| `qatlib-forge` | 核对 GitHub 和 Gitea 的 PR 检查结果与预期提交 |

安装后用 `qatlib-env docs` 阅读本页。用 `qatlib-env docs --list` 列出专题，再用 `qatlib-env docs <专题>` 离线阅读。命令参数以安装版本的 `--help` 为准。

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
- **使用 `venv` + `pip`**。创建专用虚拟环境，将它的 `bin` 目录加入日常使用的 shell 的 `PATH`。Windows 下将对应的 `Scripts` 目录加入用户 `PATH`。

  ```bash
  python3 -m venv ~/.local/share/qatlib-tools-venv
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install --upgrade pip
  ~/.local/share/qatlib-tools-venv/bin/python -m pip install /path/to/qatlib-tools
  export PATH="$HOME/.local/share/qatlib-tools-venv/bin:$PATH"
  ```

<a id="configuration"></a>

## 构建资源

不提供个人配置时，工具读取当前可用内存、CPU 亲和性和可见的 cgroup v1/v2 限制。共享池按可用 CPU 和内存余量设置。个人构建使用池的一部分容量，内存有限时可使用剩余的可用容量。单核环境默认用一个编译进程和一个 Lean 线程。

用 `qatlib-env config explain` 查看 JSON。Agent 可以直接读取 `resources`（探测容量）、`automatic`（无配置时的默认值）、`values` 和 `configuration.routing`（实际值），再用 `sources`、`configuration.sources` 和 `warnings` 判断是否需要调整。此命令不准备依赖或占用构建资源。需要持续调整时可写 `~/.qatlib/env.toml`，临时验证可传命令行参数。显式配置保留其原值，执行仍受准入和内核限制约束。 [配置来源与资源探测](docs/operations.md#configuration)

<a id="artifact-cache"></a>

## 编译与共享工件缓存

```bash
qatlib-env prepare
qatlib-env plan ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
qatlib-env run -- lake build ExampleLibrary.Foundations.ScaledSqrt.SqrtBounds.RootCountOrder
```

有匹配的依赖产物后，可用 `qatlib-env run -- lake --no-build lean <File.lean>` 检查单个文件。发布可供下游使用的定义时，用普通 `module` 消费者检查可见性和实际展开行为。

Lean v4.35 启用 `enableArtifactCache = true` 且保持全量恢复关闭时，会按内容哈希直接从 `LAKE_CACHE_DIR` 读取匹配工件。各工作树保留独立的 `.lake`。共享读取无需 reflink 或跨工作树硬链接。配置了 R2 的包由 `prepare` 校验并注册共享工件，Mathlib 自带的 `cache get` 会把归档解到当前工作树。 [Lean 模块、编译与缓存](docs/lake.md)

回收闲置树时，先用 `qatlib-env workspace clean-caches --checkout <路径> --derived-only --dry-run` 预览可再生成目录。回收整个关联工作树用 `trash-checkouts`，成功移动后默认整理所属 Git 仓库的失效登记。 [工作树与工件存储](docs/lake.md#cache-layout)

<a id="retrieval"></a>

## Lean 文档和检索

`qatlib-retrieve index` 建立本地词法索引，默认写入 `${XDG_CACHE_HOME:-~/.cache}/qatlib/retrieve/<corpus-key>.sqlite3`。键取项目根绝对路径哈希的前 24 位。用 `--db` 可以另选文件，路径须在所有入索引的源码仓之外。查询直接从索引定位声明和外层 `section` 的 `variable`，无需启动 Lean。

```bash
qatlib-retrieve index
qatlib-retrieve query 'postselection normalization' --kind theorem
qatlib-retrieve query --cite GilyenSuLowWiebe2019
```

给文件写模块文档、给声明写说明。跨声明引用用完整限定名，书目引用用 `[CiteKey]`。生成网站和本地 hover 的渲染条件不同。 [Lean 文档、书目与源码检索](docs/lean-docs.md)

<a id="forge"></a>

## GitHub 和 Gitea

PR 的常规查看、创建和评论用 `gh`、`tea` 或 `gt`。需要跨接口核对证据或在写入时核对预期提交，可以使用下列命令。

<a id="cli-reference"></a>

| 命令 | 用途 |
| --- | --- |
| `qatlib-forge pr checks` | 汇总同一 PR head 上的相关检查和实际作业 |
| `qatlib-forge pr paths` | 核对构建路径和复用既有 CI 结果的条件 |
| `qatlib-forge pr activity` | 过滤可识别的机器人活动，查看冷置候选 |
| `qatlib-forge pr merge` | 带预期 head 合并 |
| `qatlib-forge branch delete` | 核对远端 SHA 与保护规则后删除分支 |

GitHub 有独立草稿状态。Gitea 使用实例配置的标题前缀，状态摘要也可能与实际作业不同。按对应平台的事件、运行和 head 核对。 [GitHub 和 Gitea 上的拉取请求](docs/forge.md)

<a id="promotion"></a>

## 共享成果进入公开库

开发仓整理可复用成果，预发仓验证整文件依赖和真实消费者，公开库发布稳定接口与预编译工件。下游在公开版本和工件就绪后升锁，再清理已经进入公开库的旧源码和增量书目。 [共享成果如何进入公开库](docs/promotion.md)

<a id="field-notes"></a><a id="runner"></a>

## 构建排障与 Runner

构建范围、内存计费、取消与子进程回收、运行账户配置各有不同的检查方法。 [构建资源与排障](docs/operations.md)

<a id="self-test"></a>

## 工具仓自测与离线手册校验

使用 Python 3.12 运行 `uv lock --check`、`uv build` 和 `uv run --with pytest --with hatchling --with jsonschema pytest -q -rs`，与 GitHub CI 的构建及全量测试一致。

没有 `uv` 时，可在新建的虚拟环境中安装本项目及自测依赖，用 `build` 生成分发包，再运行自测。直接调用 `uv` 的安装测试留待有 `uv` 的环境复验。先完成构建，确保 `dist/` 中用于测试的 wheel 来自当前源码。

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e . build pytest hatchling jsonschema
.venv/bin/python -m build
.venv/bin/python -m pytest -q -rs --ignore=tests/migrated_dev/test_migrated_dev_smoke_packages.py
```

构建 wheel 时，根 `README.md` 按原始字节打包为 `qatlib_env/_manual.md`。用 `pipx` 或干净的 `venv` 安装当前 wheel 后，可在仓库根目录运行 `qatlib-env docs | cmp README.md -`。确认命令来自刚安装的环境，避免误用已在 `PATH` 上的旧版本。

本项目采用 [Apache License, Version 2.0](./LICENSE) 开源许可，与公开库 [qatlib](https://github.com/quantum-atlas-ai/qatlib) 保持一致。
