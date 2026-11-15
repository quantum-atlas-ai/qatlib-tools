# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**`qatlib-tools`** 是面向 [Lean 4](https://lean-lang.org/) 形式化工程的 Python 3.11+ 命令行工具集与基础库，提供工作树与缓存管理、GitHub / Gitea 协作接口，以及声明和证明检索。核心运行时仅使用 Python 标准库，**零第三方依赖**，支持在 Linux、macOS 和 Windows 的隔离环境中安装。

## 组件概览

| 命令行入口 | Python 包 | 主要用途 |
| :--- | :--- | :--- |
| `qatlib-env` | `qatlib_env` | Lean 工作树、依赖镜像、编译产物缓存、本机构建资源池与云端 CI |
| `qatlib-forge` | `qatlib_forge` | GitHub / Gitea 仓库、PR、Issue、工作流日志与制品接口 |
| `qatlib-search` | `qatlib_search` | Lean 源码解析、依赖闭包分析、声明与证明索引和查询 |

## 安装

需要 **Python 3.11 及以上版本**与 [`uv`](https://docs.astral.sh/uv/)。运行 Lean 构建还需要目标项目指定的 Lean / Lake 工具链和 Git。

GitHub 源仓为私有仓库，远程安装前需为 Git 配置有读取权限的认证。HTTPS 使用 Git credential helper，SSH 使用已授权的密钥；仅设置一个 API token 环境变量不等于已配置 Git 认证。其他托管实例的镜像也可能要求登录，请使用维护者提供的地址与访问方式。

```bash
# 从已获授权的 GitHub 源仓安装三套 CLI
uv tool install git+https://github.com/quantum-atlas-ai/qatlib-tools.git

# 或在本地检出目录中安装
uv tool install .

# 作为项目依赖写入 pyproject.toml
uv add git+https://github.com/quantum-atlas-ai/qatlib-tools.git
```

需要复现环境时，在 Git URL 末尾加上 `@` 和完整提交 SHA。可选 extra `validation` 提供 `jsonschema>=4`，用于完整的 JSON Schema 校验；未安装时使用内置结构校验。在本地检出目录中可执行：

```bash
uv venv .venv
uv pip install -e '.[validation]'
```

## 工作区、缓存与 CI

`qatlib-env` 的主要命令包括：

- `env`、`shell`：输出目标项目的运行环境。
- `worktree`：创建工作树；`--no-bootstrap` 可跳过创建后的缓存准备。
- `bootstrap`、`prepare`：准备锁定的依赖并恢复可用缓存；`--offline` 用于离线准备。
- `run -- <命令>`、`check [模块…]`：运行受管命令，或执行项目的检查与构建。
- `config explain`、`cache explain`、`cache upload`：查看资源配置与缓存状态，或显式上传已验证缓存。
- `docs [--path]`：查看安装包手册，或输出手册位置。
- `clean-setup`、`gc`、`maintain-mirror`：检查或清理派生产物、维护本机依赖镜像；清理需显式指定 `--apply`。
- `pool status`、`pool prune`：查看资源池租约，清除失效记录。
- `plan`、`cloud submit|status|logs|wait|fetch`：评估本地/云端路由并操作远端工作流。
- `ci lint|build|build-gate|mirror`：供下游仓库 CI 使用，也可通过 `python -m qatlib_env.ci` 调用；具体门禁和镜像依赖目标仓库的配置。

本机构建使用带心跳的租约账本控制并发准入。Linux 在支持时使用 `systemd --user` cgroup v2 scope，Windows 使用 Job Object，macOS 在可用时使用 `taskpolicy` 调整调度优先级；各平台能力不同，macOS 不提供聚合内存硬上限。编译器包装层通过 `LEAN_SYSROOT` 覆盖和 `LAKE_OVERRIDE_LEAN` 注入线程数 `-j` 与内存参数 `-M`，其中 `-M` 最低为 `2048 MiB`。

以下工作树与构建示例应在已有的 Lean Git 项目中执行，并将模块名换成项目中的真实目标：

```bash
qatlib-env config explain
qatlib-env pool status

qatlib-env worktree ../feature-branch feat/new-lemma
qatlib-env --project ../feature-branch check MyLib.TargetModule
```

`plan` 只输出路由建议；实际提交由 `cloud submit` 显式执行。云端命令需要先配置仓库和工作流、提供目标平台的 API 凭据，并确保工作流接受手动触发。`--head` 填目标工作流所在的真实分支，例如 `main`：

```bash
qatlib-env plan --policy auto --cold
qatlib-env cloud submit --head main
qatlib-env cloud status
qatlib-env cloud wait

# 提交状态中已有 run_id 后，使用该次运行中的真实 job ID
qatlib-env cloud logs --job-id 123456789

# 目标路径须尚不存在；远端制品须符合验证包清单格式
qatlib-env cloud fetch --destination .lake/verified-bundle
```

云端状态默认保存在当前目录的 `.qatlib-env/cloud-state.json`，可用 `--state` 指定；若切换目录，后续命令需指向同一状态文件。日志按字节范围读取并提取 Lean 编译诊断。远端验证包校验清单及各文件的大小、SHA-256，若 API 提供整包摘要也会验证；解包拒绝路径越界与符号链接。`fetch` 将包放到指定的新目录，缓存接入由调用方按项目布局处理。

## GitHub / Gitea 协作接口

`qatlib-forge` 以 JSON 输出仓库、PR、Issue、工作流运行与日志信息，并按目标主机解析认证。

- `pr` 支持 `read`、`list`、`create`、`update`、`comment`、`comments`、`review-comment`、`draft`、`ready`。
- `issue` 支持 `list`、`read`、`create`、`update`、`close`、`reopen`、`comment`。
- `repo` 解析仓库身份，`compare` 比较两个修订，`status read|write` 操作本地正文文件中的任务状态块。
- `runs list|read` 查询工作流，`logs` 读取任务日志，`artifacts list` 列出制品。制品下载另由 Python `RepoAPI.download_artifact` 和 `qatlib_forge.artifacts` 提供。

使用 `owner/repo` 简写时需明确平台或主机。GitHub 示例中的 `--platform`、`--repo` 可放在具体操作后：

```bash
qatlib-forge pr list --platform github --repo quantum-atlas-ai/qatlib --state open
qatlib-forge runs list --platform github --repo quantum-atlas-ai/qatlib --branch main

# 将示例 job ID 换成指定仓库的真实任务 ID
qatlib-forge logs 123456789 --platform github --repo quantum-atlas-ai/qatlib --start 0 --end 131071 --diagnostics
qatlib-forge artifacts list --platform github --repo quantum-atlas-ai/qatlib --run-id 123456789
```

Gitea 使用 `--platform gitea` 和完整仓库 URL，或用 `--web-host` 明确主机；该主机必须与凭据匹配。查看具体参数可运行 `qatlib-forge pr create --help` 等子命令帮助。

## Lean 源码与证明检索

`qatlib-search build` 扫描 Lean 源码生成声明索引，`query` 按名称片段和声明种类检索：

```bash
# 在包含 Qatlib/ 源码目录的项目根目录中运行
qatlib-search build Qatlib -o .cache/search-index.json
qatlib-search query .cache/search-index.json 'gram' --kind theorem --limit 10
```

`retrieve` 提供统一源码检索入口。更完整的索引导出、增补、校验和目标检查由 `build_search_index`、`enrich_search_index`、`check_search_index`、`proof_index`、`unified_retrieval`、`query_library`、`find_symbol`、`check_goal` 等 Python 模块提供，其中部分操作需要准备好的 Lean 环境。

共享解析器支持嵌套块注释和 `«…»` 转义导入；`lean_declarations`、`lean_doc_source` 提取声明及文档，`promotion`、`promotion_closure` 处理跨包查重与依赖闭包。检索命中表示源码候选，使用前仍需检查完整类型、前提与目标工具链下的 Lean 验证结果。

## 项目配置

云端路由设置依次读取 `pyproject.toml` 的 `[tool.qatlib-env]`、`qatlib-env.toml`、`.qatlib-env.local.toml`，后者覆盖前者。独立的 `qatlib-env.toml` 可以使用顶层键；下面同时包含顶层云端设置和单独的 `[resources]` 构建资源节：

```toml
# qatlib-env.toml
cloud_platform = "github"
repository = "quantum-atlas-ai/qatlib"
workflow = "build.yml"
local_pool_mib = 24576

[resources]
jobs = 2
lean_threads = 1
memory_budget = "8GiB"
heavy_jobs = 1
module_timeout = 1800
stage_timeout = 10800
heavy_patterns = []
```

若把**云端设置**放进 `pyproject.toml`，必须使用配置节，不能直接使用文件顶层键：

```toml
# pyproject.toml
[tool.qatlib-env]
cloud_platform = "github"
repository = "quantum-atlas-ai/qatlib"
workflow = "build.yml"
local_pool_mib = 24576
```

实际构建资源由独立的资源解析器处理：`jobs`、`lean_threads`、`memory_budget` 等请放在 `qatlib-env.toml` 的 `[resources]` 节，或用 `--jobs`、`--lean-threads`、`--memory-budget` 等 CLI 参数覆盖。该资源解析器当前不读取 `pyproject.toml` 或 `.qatlib-env.local.toml`；`config explain` 可查看实际生效值与来源。

`local_pool_mib` 用于 `plan` 的路由比较；实际本机准入总预算使用 `QATLAS_POOL_BUDGET_MIB` 与 `QATLAS_POOL_CPU` 环境变量，等待超时使用 `QATLAS_POOL_WAIT_TIMEOUT`。本机编译缓存目录可通过 `LAKE_CACHE_DIR` 指定。私有覆盖文件可保存本机云端配置，但不要纳入版本控制或放入访问令牌。检索入口 `build` 的源码根目录由位置参数指定，与上述云端路由配置分别处理。

## 本地开发与测试

在检出目录中执行以下命令。测试工具通过 `uv run --with` 临时加入隔离环境；它们不属于核心运行时依赖。构建检查也会核验随 wheel 分发的手册、Lean 导出器与 JSON schema。

```bash
uv venv .venv
uv pip install -e .
uv lock --check
uv build
uv run --with pytest --with hatchling --with jsonschema pytest -q -rs
```

## 开源许可

本项目采用 [Apache License, Version 2.0](./LICENSE)（`Apache-2.0`）许可证，与 [`quantum-atlas-ai/qatlib`](https://github.com/quantum-atlas-ai/qatlib) 保持一致。
