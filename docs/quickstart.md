# 五分钟验证

本页只做一件事：在**本工具仓的检出**中确认 `qatlib-env` 已安装到位，并且能正常读取手册和本机配置。本页步骤既不下载 Lean 工具链，也不编译 Lean。

开始前，请先确认当前目录位于 `qatlib-tools` 的检出根目录（目录下能看到 `pyproject.toml` 和 `docs/`），且本机已安装 `uv` 以及 Python 3.11 或更高版本（本仓的 GitHub CI 使用 Python 3.12）。

## 安装这个检出

在检出根目录运行：

```bash
uv tool install .
```

`uv` 会为 `qatlib-env`、`qatlib-retrieve`、`qatlib-forge` 这三个命令单独准备一个运行环境。这一步只写入 `uv` 的工具环境，不会改动当前检出目录里的源码。

安装完成后，新开一个终端运行 `qatlib-env --help`，如果能正常输出帮助文本且其中包含 `docs`，就说明安装成功。如果提示找不到命令，请先查看[安装方式](install.md)中的 `PATH` 说明，把 `uv tool dir --bin` 所指的目录加入 `PATH`，然后再回到这里继续。

如果需要固定某次提交、安装 wheel、在 Windows 上调整路径，或在下游 Lean 仓库里安装该仓库锁定的版本，请参阅[安装方式](install.md)。日常验证执行上面这一条命令即可。

## 读到专题名单

运行下面的命令，确认能够读取文档专题列表：

```bash
qatlib-env docs --list
```

运行成功时，标准输出会按行列出若干不带 `.md` 后缀的专题名，其中应能看到 `quickstart`、`worktrees`、`pull-requests` 和 `github-ci`。这份名单对应的就是 `docs/` 目录下的中文专题。要阅读其中某一篇，把专题名作为参数传入即可，例如 `qatlib-env docs worktrees`。

不带参数运行 `qatlib-env docs` 会打印仓库根目录的 `README.md`。英文页面位于网站的 `/en/` 路径下；这条离线命令打印的是中文。

## 看一眼配置从哪来

运行下面的命令，查看本机配置的解析结果：

```bash
qatlib-env config explain --json
```

运行成功时，进程退出码为 0，标准输出会返回一个 JSON 对象，包含以下字段：

| 字段 | 用来确认什么 |
| --- | --- |
| `resources` | 探测到的内存、CPU 和可见限额 |
| `automatic` | 未作额外覆盖时的自动推导值 |
| `values` | 本次生效的参数 |
| `configuration.routing` | 路由和仓库相关的生效配置 |
| `sources` 与 `configuration.sources` | 每个配置值来自哪一层 |
| `warnings` | 需要人工查看的提示；没有提示时为空数组 |
| `paths` | `QATLIB_HOME`（默认 `~/.qatlib`）下各项数据的路径 |

这条命令不会创建上述目录，不会拉取依赖，也不会申请构建池租约。看到正常输出的 JSON 后就可以停下，不必在这里修改配置。

## 第一次编译会花多久

完成上面三步后，工具本身的验证就已经结束。之后在 Lean 仓库中首次执行 `qatlib-env prepare` 或 `lake build` 时，才可能下载 Lean 工具链、拉取锁定的依赖源码，并解包或编译工件。那一次的耗时往往明显长于这五分钟；如果依赖已经在共享缓存里，同样的过程会快很多。这部分流程从[第二棵工作树](worktrees.md)开始。

## 接下来去哪

- 如果要在 Lean 仓库里修改代码：请阅读[第二棵工作树](worktrees.md)。
- 如果要修改 `qatlib-tools` 本身：请阅读[拉取请求](pull-requests.md)，本地自测命令见[贡献指南](../CONTRIBUTING.md#self-test)。
