<div align="center">

<img src="./.github/assets/qatlib-tools-wordmark.svg" alt="qatlib-tools" width="520" />

**面向 Lean 4 形式化项目的工程维护手册与配套命令行工具**

[![CI](https://github.com/quantum-atlas-ai/qatlib-tools/actions/workflows/ci.yml/badge.svg)](https://github.com/quantum-atlas-ai/qatlib-tools/actions/workflows/ci.yml)
[![Docs](https://github.com/quantum-atlas-ai/qatlib-tools/actions/workflows/docs.yml/badge.svg)](https://github.com/quantum-atlas-ai/qatlib-tools/actions/workflows/docs.yml)
[![Handbook](https://img.shields.io/badge/docs-在线手册-263f5f)](https://quantum-atlas-ai.github.io/qatlib-tools/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)

简体中文 ｜ [English](./README.en.md)

</div>

---

## <a id="overview"></a>从这里选一条路

**qatlib-tools** 面向 [Qatlib](https://github.com/quantum-atlas-ai/qatlib) 及一般 [Lean 4](https://lean-lang.org/) 形式化仓库，将日常反复用到的环境准备、构建、检索和提交核验流程整理为一本工程手册，并配套提供三个可独立调用的命令：`qatlib-env`、`qatlib-retrieve`、`qatlib-forge`。查看分支、创建拉取请求和编译单个目标，仍直接使用 `git`、`gh`、`tea`、`gt` 和 `lake`。

在线手册位于 <https://quantum-atlas-ai.github.io/qatlib-tools/>。安装完成后，也可以在终端离线查阅：`qatlib-env docs` 打印本文件，`qatlib-env docs --list` 列出 `docs/` 目录下的全部中文专题，`qatlib-env docs <专题>` 打印对应专题正文。英文网页由 `docs/en/` 中的同名专题生成，离线命令读取的均为中文原文。

| 你现在要完成的事 | 对应文档 |
| --- | --- |
| 确认工具在本机已经能正常运行 | [五分钟验证](docs/quickstart.md) |
| 在已锁定工具版本的 Lean 仓库中修改模块 | [第二棵工作树](docs/worktrees.md) |
| 提交第一份草稿拉取请求，或修改本工具仓 | [拉取请求](docs/pull-requests.md) · [贡献指南](CONTRIBUTING.md) |
| 了解本仓工作流、审阅并行提交，或接入 Runner | [GitHub 工作流](docs/github-ci.md) · [审阅并行提交](docs/forge.md) · [自托管 Runner](docs/runner.md) |
| 复核 Lean 证明、导出接口和公理依赖 | [证明复核与公理审计](docs/verification.md) · `qatlib-env docs verification` |

<a id="quickstart"></a>

## 五分钟验证

按下面两步即可确认本机工具可用：

1. 在本工具仓的检出根目录执行 `uv tool install .` 完成安装。
2. 依次运行 `qatlib-env docs --list` 和 `qatlib-env config explain --json`。

若终端输出专题列表和一段配置 JSON，这一步就完成了。完整命令、执行目录及成功时需要核对的字段见[五分钟验证](docs/quickstart.md)；如需了解固定提交、wheel 安装、Windows 环境或下游仓库锁定的版本，请参见[安装方式](docs/install.md)。

## 三条命令

| 命令 | 主要职责 |
| --- | --- |
| `qatlib-env` | 依据锁定信息准备 Lean 依赖和共享缓存，在资源预算内执行构建，并管理本地工作树 |
| `qatlib-retrieve` | 检索 Lean 声明、源码签名和书目引用 |
| `qatlib-forge` | 围绕同一拉取请求 head 汇总检查结果，并在合并或删除分支前核对预期提交 |

各命令的详细参数，以当前已安装版本的 `--help` 输出为准。

<a id="configuration"></a>
<a id="artifact-cache"></a>

## 编译、缓存和本机预算

日常修改代码建议先按[第二棵工作树](docs/worktrees.md)的流程走一遍：先执行 `plan`，再执行 `prepare`，最后执行 `run -- lake build`。关于缓存开关、九种恢复组合、Windows 跨卷、七层配置优先级，以及 `~/.qatlib` 中各项数据的存放位置，请参见[编译环境、缓存与资源控制](docs/build.md)。

如需查看当前生效配置，可运行 `qatlib-env config explain --json`：该命令仅报告容量、生效值和路径，不会拉取依赖，也不会申请构建池租约。预算字段详见[配置优先级](docs/build.md#configuration)，目录说明详见[本机数据目录](docs/build.md#local-paths)。

<a id="retrieval"></a>

## 检索

在 Lean 项目根目录下按顺序操作：先运行 `qatlib-retrieve index`，再运行 `qatlib-retrieve query`，随后打开命中的源码进行核对。索引、书目与网站链接之间的差别，见[Lean 文档、书目与源码检索](docs/lean-docs.md)。

<a id="forge"></a>
<a id="cli-reference"></a>

## 拉取请求

- **发起与自检**：创建第一份草稿拉取请求、撰写可复现的验证记录，以及按当前提交查看 Checks，请参见[拉取请求](docs/pull-requests.md)。
- **审阅与合入**：审阅时需依次确认三件事——已有检查针对的是哪个 head、该结果能否用于当前拉取请求、当前是否可以合并。完整路径见[GitHub 和 Gitea 上的拉取请求](docs/forge.md)。

常用命令包括 `qatlib-forge pr checks`、`qatlib-forge pr paths`、`qatlib-forge pr merge` 和 `qatlib-forge branch delete`，具体参数见各自的 `--help`。

<a id="promotion"></a>

## 共享成果进入公开库

将共享成果推进至公开库时，涉及整文件迁移、普通 `module` 消费者探针、在干净工作树中验锁，以及正式版本发布后的升锁，详细步骤见[共享成果如何进入公开库](docs/promotion.md)。

<a id="field-notes"></a>
<a id="runner"></a>

## Runner

接入一台主机时，请按[自托管 Runner](docs/runner.md)中的顺序逐步完成：

1. 确认控制器，并阅读锁定源码的 `--help`。
2. 依次执行 `init` 与 `register`。
3. 核对标签并读回限额。
4. 使用该 lane 的服务账号完成验收。

如果遇到页缓存占满账面内存、取消后残留进程，或 Windows Job Object 相关问题，可按症状查阅[Runner 排障](docs/runner-troubleshooting.md)。

<a id="self-test"></a>

## 修改本工具仓

与 `.github/workflows/ci.yml` 一致的本地验证命令（包括人工契约检查和干净环境中的 wheel 冒烟测试），均写在[贡献指南](CONTRIBUTING.md#self-test)。需要注意的是，本仓的 `CI / test` 对草稿拉取请求和仅修改文档的拉取请求同样会运行；Pages 是否部署则由另一条工作流控制，说明见[GitHub 工作流](docs/github-ci.md)。

本项目采用 [Apache License, Version 2.0](./LICENSE)。
