# 本仓库的 GitHub 工作流

本文面向维护本仓库及为下游仓库接入 CI 的读者。若你是贡献者，想查看当前提交的检查状态，请查阅[拉取请求](pull-requests.md)；若你是审阅者，需要判断已有检查结论是否仍然有效以及当前能否合并，请查阅[审阅并行提交](forge.md)；若需注册自托管 Runner，相关命令见[自托管 Runner](runner.md)。

本仓库目前配置了两份工作流，各自承担不同的验证职责。

## 事件、作业、权限、验证

| | `CI`（`.github/workflows/ci.yml`） | `Documentation`（`.github/workflows/docs.yml`） |
| --- | --- | --- |
| 何时运行 | 触发 `pull_request`（`opened`、`synchronize`、`reopened`、`ready_for_review`、`converted_to_draft`）或推送到 `main` 时 | 推送到 `main` 且变更落在 `*.md`、`docs/**`、`site/**` 或本工作流文件自身时；也支持手动通过 `workflow_dispatch` 触发 |
| 路径过滤 | 无。即使拉取请求只修改文档也会运行 | 有。未命中上述路径的 `main` 推送不会构建文档站 |
| 运行器 | `ubuntu-latest` | `ubuntu-latest` |
| 权限 | 顶层 `contents: read` | 顶层 `contents: read`、`pages: write`、`id-token: write` |
| 作业 | `test`：包含人工契约检查、`uv lock --check`、`uv build`、pytest 以及干净虚拟环境中的 wheel 冒烟测试 | `build` 负责构建 VitePress 站点；`deploy` 仅在仓库公开且 `github.ref` 为 `refs/heads/main` 时运行，并使用名为 `github-pages` 的环境 |
| 绿色说明什么 | 该提交已通过 Python 3.12 下的工具仓检查。草稿拉取请求会向契约脚本追加 `--draft`；就绪拉取请求和推送到 `main` 的运行不追加 | `build` 成功说明文档站可以正常构建；`deploy` 成功才说明公开仓库的 `main` 已将站点交付给 Pages |

拉取请求页面上的 `CI` 通过，不能用来判断 Pages 是否已经构建或部署——`docs.yml` 的 `on` 触发条件中并不包含 `pull_request`。

两份工作流的 `actions/checkout` 均设置了 `persist-credentials: false`，因此作业中后续的 git 推送不会沿用检出阶段自动保存的凭据。

## 一张拉取请求会跑哪一份

查看 `.github/workflows/ci.yml` 中的 `on.pull_request.types` 可知：新建、新推送、重新打开、转为就绪或转为草稿，都会触发 `CI / test`，且不受修改文件类型的影响。

查看 `docs.yml` 中的 `on` 配置可知，拉取请求并不在其中。文档站仅在公开仓库的 `main` 分支收到推送、且本次变更触及上述路径时才会构建；部署作业还同时受公开仓库和 `refs/heads/main` 两个条件限制，因此在私有仓库中 `deploy` 作业会被跳过。

## 谁配置、哪一个入口使用、什么时候需要

| 凭据 | 谁配置 | 哪一个入口使用 | 什么时候需要 |
| --- | --- | --- | --- |
| 个人 `gh` 登录 | 执行命令的人 | `gh pr create`、`gh pr ready`、`gh pr checks` | 创建草稿、转为就绪或查看当前提交的检查状态时 |
| `GITHUB_TOKEN` | Actions 按工作流的 `permissions` 签发 | 上述两份工作流 | `CI` 仅需只读内容权限；Pages 写权限与 `id-token: write` 仅出现在 `docs.yml` 中 |
| Runner 注册令牌 | 有权为该仓库创建注册令牌的人，或通过已登录的 `gh` 代为获取 | `setup_runner.sh register` | 仅在注册自托管 Runner 时使用。本仓库这两份工作流使用 `ubuntu-latest`，不会消费这枚令牌 |
| `GITEA_MIRROR_SSH_KEY` | 下游仓库的密钥管理员 | `qatlib-env ci mirror`，以及 `lint` 末尾调用的同一函数 | 触发条件见下文“可选的 Gitea 镜像”一节；它不是 GitHub 的 `GITHUB_TOKEN` |
| R2 写密钥 | 允许发布共享缓存的人 | `qatlib-env ci build` 在 `may_publish` 为真时的上传流程 | 下游仓库触发 `main` 推送或 `workflow_dispatch` 且该包配置了写密钥时使用。切勿将其分发给所有协作者 |
| `QATLIB_PKG_MIRROR` | 主机或 lane 的环境 | 依赖源码镜像 | 让锁定的 Git 依赖从本地镜像拉取。它既不会改写 GitHub 网页 URL，也不会把 `main` 推送到 Gitea |

此外，宿主机上还支持通过 `git config url.<镜像>.insteadOf` 配置可选的 GitHub 拉取 URL 改写。`setup_runner.sh init` 仅在设置了 `QATLIB_GITHUB_MIRROR` 时，才会为该 lane 对应的账号写入 `https://github.com/` 的替换规则。这仅用于拉取依赖时的 URL 替换，与下文的 Gitea 推送是两回事。

## 下游仓库接入 `qatlib-env ci`

本仓库的 `ci.yml` 不调用 `qatlib-env ci`。下游 Lean 仓库若要使用这四个子命令，请在其自身的工作流中先安装所锁定的工具版本，再按需调用：

| 命令 | 作用 |
| --- | --- |
| `qatlib-env ci lint` | 按该仓库的剖面执行静态检查；若当前事件同时满足镜像条件，末尾会尝试执行镜像推送 |
| `qatlib-env ci build` | 准备依赖并编译该剖面的目标；仅在满足发布条件时上传缓存 |
| `qatlib-env ci build-gate` | 读取 `GITHUB_EVENT_PATH` 或 `GITEA_EVENT_PATH`：草稿会将 `build=false` 写入 `GITHUB_OUTPUT` 或 `GITEA_OUTPUT`，非草稿则写入 `build=true` |
| `qatlib-env ci mirror` | 仅执行镜像推送，触发条件与下文一致 |

`python -m qatlib_env.ci --help` 与 `qatlib-env ci --help` 共用同一个命令解析器。下游仓库的必需检查名称由其自身工作流决定，不由本仓库的 `CI / test` 决定。

`lint` 和 `build` 会执行本地资源准入检查，因此需要运行在已配置好资源限额的运行器上。Runner 注册步骤见[自托管 Runner](runner.md)，依赖镜像和工件缓存的目录规则见[三类缓存和工作树](build.md#cache-layout)。

## 可选的 Gitea 镜像

`qatlib-env ci mirror` 以及 `lint` 中对同一函数的调用，只要以下任一条件不成立就会直接返回，不执行推送：

- 环境中存在 `GITEA_MIRROR_SSH_KEY`
- 事件名为 `push`
- ref 为 `refs/heads/main`

目标 URL 优先取自 `GITEA_MIRROR_URL`，未设置时取配置中的 `gitea_mirror_url`。主机密钥取自 `GITEA_KNOWN_HOST` 或 `SSH_KNOWN_HOSTS`；两者均为空时，命令会打印跳过原因并直接返回。

以上条件全部满足时，命令会使用这把密钥执行 `git push --force`，将当前 `HEAD` 推送至镜像的 `refs/heads/main`，并将 `refs/tags/*` 推送至镜像的同名标签。`--force` 会按本次推送更新镜像上的 `main` 和标签。若推送失败，函数会打印 `::warning::` 并返回，不会因此导致 `lint` 以非零状态码退出。

这把密钥的授权范围仅限于对应的 Gitea 镜像，既不能代替 GitHub 上的合并权限，也不能当作 Runner 注册令牌或 R2 写密钥使用。

## 哪些改动需要维护者审阅

`.github/CODEOWNERS` 将 `DESIGN.md`、`docs/design/human-contracts.sha256` 和 `.github/` 交由文件中列出的维护者负责，文件本身即用于声明审阅覆盖范围——修改工作流或契约摘要都属于这份覆盖。至于 GitHub 是否会在合并前强制要求这些审阅通过，取决于仓库规则中是否启用了 Code Owners。

受保护设计正文的草稿流程写在 [DESIGN.md](../DESIGN.md) 中，本页不改写那份正文。
