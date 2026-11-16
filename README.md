# qatlib-tools

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**`qatlib-tools`** 是面向 [Lean 4](https://lean-lang.org/) 形式化工程的 Python 3.11+ 命令行工具集与基础库，提供工作树与缓存管理、GitHub / Gitea 协作接口，以及声明和证明检索。回收站操作使用成熟的 `Send2Trash`，构建进程观察使用 `psutil`，Windows 另安装平台限定的 `pywin32`；支持在 Linux、macOS 和 Windows 的隔离环境中安装。

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
- `plan [targets...]`、`cloud submit|status|logs|wait|fetch`：只读估算目标预算，并显式操作远端工作流。
- `ci lint|build|build-gate|mirror`：供下游仓库 CI 使用，也可通过 `python -m qatlib_env.ci` 调用；具体门禁和镜像依赖目标仓库的配置。

本机构建使用带心跳的租约账本控制并发准入。Linux 在支持时使用 `systemd --user` cgroup v2 scope，Windows 使用 Job Object，macOS 在可用时使用 `taskpolicy` 调整调度优先级；各平台能力不同，macOS 不提供聚合内存硬上限。编译器包装层通过 `LEAN_SYSROOT` 覆盖和 `LAKE_OVERRIDE_LEAN` 注入线程数 `-j` 与内存参数 `-M`，其中 `-M` 最低为 `2048 MiB`。

以下工作树与构建示例应在已有的 Lean Git 项目中执行，并将模块名换成项目中的真实目标：

```bash
qatlib-env config explain
qatlib-env pool status

qatlib-env worktree ../feature-branch feat/new-lemma
qatlib-env --project ../feature-branch plan MyLib.TargetModule
qatlib-env --project ../feature-branch run -- lake build MyLib.TargetModule
```

`plan` 接受模块名、源码文件路径或库名，留空使用 Lake 默认目标。它读取真实 Git HEAD、依赖锁与检出身份、词法导入图、项目内反向依赖，以及 `.olean` / `.trace` 的保守有效性检查，输出目标数、包/工具链闭包、反向失效、热命中与必须重编数、未知导入、内存与耗时区间、`mode` 和结构化 `reasons`。它不运行 Lake、不恢复缓存、不写 `.lake`。耗时暂用每冷模块5–60秒及并发估算，重模块按至少8GiB进程峰值计；结果仍需结合实际测量，未涵盖链接与公理审计开销，也不替代 Lake 的原生 depHash 校验。

```bash
qatlib-env plan MyLib.TargetModule
qatlib-env plan MyLib/TargetModule.lean
qatlib-env plan --cold --full-gate
qatlib-env plan MyLib.TargetModule --check-remote-cache
```

`--check-remote-cache` 只将精确修订清单取到内存，输出可达性与映射行数；清单行数不等于模块命中率，探测不会减少本机重编计数。缺失/损坏输出、依赖锁失配和未解析导入均不能被认证为热缓存。

`run` 与 `check` 共用默认拒绝门禁。冷编/重编或反向波及超过配置阈值、整库目标、lint/test/可执行驱动或公理审计、依赖身份不符与导入未知时，本机执行返回非零并输出预算诊断。`--no-prepare` 只跳过缓存准备；`--policy local` 只表达偏好。任意 Python/shell 驱动按可能触发编译的完整操作处理；Lake 的跨目录参数必须与 `--project` 一致。

获得用户对当次重任务的明确许可后，才使用下面的单次标志：

```bash
qatlib-env run --no-prepare --allow-local-heavy -- lake build MyLib
qatlib-env check --allow-local-heavy MyLib.TargetModule
qatlib-env ci build --allow-local-heavy
```

许可仅在本次进程调用内有效，诊断记录 `allow_local_heavy=true`，不会写入配置或传给子进程作为绕过变量。它不能提高池的 CPU/内存硬上限，也不能禁用控制器或超时。Linux 重任务要求可用的 systemd 用户 CPU/内存 scope，Windows 要求成功配置并附加 Job Object；缺少硬限制的后端拒绝本机重任务。更新消费项目固定版本时，应同步核对其 CI 调用显式许可和 Runner 资源配置。

实际远端提交由 `cloud submit` 显式执行。云端命令需要先配置仓库和工作流、提供目标平台的 API 凭据，并确保工作流接受手动触发。排队、超时或平台不可用会保持远端状态或报错，不会自动回退本机。`--head` 填目标工作流所在的真实分支，例如 `main`：

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

路由、运行资源、CI/缓存项目资料及预算门禁共用一个校验加载器。优先级从低到高为：自适应默认、项目共享配置、仓内 local、环境变量、CLI。共享配置先读取根 `pyproject.toml` 的 `[tool.qatlib-env]`，再读取根 `qatlib-env.toml`；两者都不存在时回退到 `python/pyproject.toml`。兼容的 Runner 与已存在用户资源配置只作为低于项目的只读基础层，工具不会创建用户级配置文件。

根 `.qatlib-env.local.toml` 同时覆盖路由、资源和预算；次级 worktree 缺失该文件时，只读回退到主检出根目录同名文件，当前 worktree 自己的文件优先。私有覆盖不存令牌、不提交 Git；格式损坏、类型错误和未知键会报告文件路径并停止。以下是共享配置示例：

```toml
# qatlib-env.toml
cloud_platform = "github"
repository = "quantum-atlas-ai/qatlib"
workflow = "build.yml"
local_pool_mib = 24576
local_pool_cpu = 4

[resources]
jobs = 2
lean_threads = 1
memory_budget = "8GiB"
heavy_jobs = 1
module_timeout = 1800
stage_timeout = 10800
heavy_patterns = []

[budget]
max_cold_modules = 15
max_reverse_modules = 15
require_explicit_local_heavy = true
```

也可以把所有设置放进 `pyproject.toml` 或本地覆盖的 `[tool.qatlib-env]` 命名空间：

```toml
# .qatlib-env.local.toml
[tool.qatlib-env]
cloud_platform = "github"
repository = "quantum-atlas-ai/qatlib"
workflow = "build.yml"
local_pool_mib = 24576
local_pool_cpu = 4

[tool.qatlib-env.resources]
jobs = 2
lean_threads = 1
memory_budget = "8GiB"
heavy_jobs = 1
heavy_patterns = ["HeavyCertificate"]

[tool.qatlib-env.budget]
max_cold_modules = 15
max_reverse_modules = 15
require_explicit_local_heavy = true
```

`[resources]`、`[tool.qatlib-env]` 的资源键及其 `[resources]` 子表均受支持，子表覆盖同层平铺键。`default_jobs` 是 `jobs` 的兼容别名，`default_memory_mib` 转为字节单位的 `memory_budget`，同表的规范键优先。`require_explicit_local_heavy` 须保持 `true`；不接受把一次性重任务许可持久化。`config explain` 输出运行值、路由/预算及各自来源；超配保留原值供诊断，执行准入会硬拒绝。

`local_pool_mib` / `local_pool_cpu` 同时约束计划与执行，环境变量 `QATLAS_POOL_BUDGET_MIB` / `QATLAS_POOL_CPU` 可显式覆盖；并发 CPU 请求按 `jobs × lean_threads` 计。未配置的池按宿主可用资源自适应；单次请求绝不反向放大显式上限，活跃租约也不能被另一个调用者放大。`QATLAS_POOL_WAIT_TIMEOUT` 只控制等待其他租约的时间，超过硬上限的单次请求立即拒绝。预算阈值可用 `QATLAS_MAX_COLD_MODULES` / `QATLAS_MAX_REVERSE_MODULES` 覆盖，0表示不允许任何对应重编/波及，不表示无限制。

本机工件缓存目录仍由 `LAKE_CACHE_DIR` 指定，池账本由 `XDG_STATE_HOME` / `XDG_CACHE_HOME` 定位；工具不会为两仓自动创建用户级共享配置。检索 `build` 的源码根由位置参数指定。需要共同使用机器池的项目必须指向同一账本，并采用一致的池上限。

## 多根工作区盘点与官方回收站

普通环境命令不扫描机器。维护时使用独立入口；扫描不初始化环境、不 fetch、不刷新
Git index，也不创建缓存锁。使用 `python -B` 避免 Python 导入生成字节码：

```bash
python -B -m qatlib_env workspace scan ~/work ~/.legacy-worktrees/tmp --json
python -B -m qatlib_env workspace scan ~/work --measure --check-remote --json
python -B -m qatlib_env workspace scan /known/checkout /another/checkout --exact --json
python -B -m qatlib_env workspace clean-caches --checkout /exact/idle/checkout --dry-run --json
python -B -m qatlib_env workspace trash-checkouts --checkout /exact/redundant/checkout --dry-run --json
```

默认扫描当前目录的父目录与 `~/.legacy-worktrees/tmp`。显式根可以跨挂载；递归不进入
其他挂载、符号链接、`.git`、`.lake`、回收站和常见依赖/容器目录。输出同时记录 Git、
Lake/Lean 与研究目录标记，并按 `common-dir` 汇总全部分支、stash 与 worktree。
已知清单可用 `--exact` 跳过递归发现。默认不遍历 `.lake` 测大小；`--measure`
测分配/逻辑字节，耗时取决于文件数量。
`--check-remote` 只读 `ls-remote`；缺失远端对象或浅克隆会保守标为未知，不自动 fetch。

两个清理命令默认仅预览。保留常用 clone 时选 `clean-caches`，整树回收只选明确多余的
`trash-checkouts`。两者均列出完整目标、逻辑/分配大小及结构化 `warnings`：近期活动、
dirty/stash、未推或未知分支、Git 忽略的任务目录、被追踪的 `.lake` 内容、修改的依赖，
以及 `.lake/task-*` 等非标准目录。文件提示列出具体路径，非标准目录另测整树大小；
Git 元数据提示共享目标总量，不虚构分支的磁盘大小。这些发现供操作者审阅和归档，
不会因为命中提示就禁止用户确认后的回收。默认不查询远端；需要 live 引用时加 `--check-remote`。

审阅具体路径和状态并得到用户批准后，才用同一选择列表加 `--apply`。执行会重新预检，
只有活跃 Lean/Lake/构建占用、路径越界（含会破坏未选 worktree 的共享 Git 目录）、
不存在或非法链接、显式 `--protect` 排除和回收站后端故障才拒绝；`reasons` 列出这些阻断。
提示项在 `--apply` 下可直接回收，无需第二套覆盖开关。后端途中失败保留已成功项的回执并停止；仍需操作者协调写者，
无法排除检查后新进程启动的竞态。工具不保存批准，也不自动恢复或清空回收站。
三端均交给 Send2Trash：Linux 使用 Gio 或库提供的 FreeDesktop Trash 实现（含外盘
`.Trash/$uid`、`.Trash-$uid`），macOS 使用 Cocoa/CoreServices，Windows 强制使用
pywin32 的原生 `IFileOperation`，不使用 .NET 或旧 `SHFileOperation` 兜底。Windows
同时补齐上游取消回调的异常契约，并拒绝已知不安全的长路径情况；其他普通 Windows 路径可以回收。
库没有通用只读能力探测接口，预览只确认后端可加载、路径和挂载的已知条件，不调用移动函数，
不创建 Trash 目录；具体文件系统是否接受移动以执行结果为准。失败即报告，没有永久删除后备路径。
跨盘可能需要复制到用户 Trash 后移动，耗时和临时占用取决于数据量。同盘回收通常保留物理块，
大小和回执均不代表已经释放磁盘空间；共享 extents/硬链接也不能重复计算为可释放量。

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
