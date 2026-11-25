# GitHub Actions 阶段计时与缓存回退排障

先核对 PR 的完整 head SHA。打开对应的工作流运行，记录运行轮次以及作业和步骤的起止时间。重跑时，执行的作业、下载内容和排队时间都可能变化。可在 Actions 页面查看，也可用命令读取：

```sh
qatlib-forge pr checks 123 --repo "$REPO" --platform github --expected-head "$HEAD_SHA"
gh api "repos/$REPO/actions/runs/$RUN_ID/attempts/$ATTEMPT/jobs" --paginate
```

`qatlib-forge` 提供 `run_attempt`、作业链接，以及各步骤的状态、时间戳和可计算的 `duration_seconds`。排队作业没有执行耗时，运行中的步骤还没有结束时间。找同一工作流、同类 Runner、同工具链和相同缓存结果的运行作对照，用作业的开始时间和排队状态区分等 Runner 与命令卡住。 [GitHub Actions 作业 API](https://docs.github.com/en/rest/actions/workflow-jobs)

## 从最后开始的一段查起

| 阶段 | 如何判断 |
| --- | --- |
| 查找清单 | `r2-manifest` 给出尝试的键和 HTTP 状态。跨 Git 历史时，提交键未命中而输入树键命中很常见。`200` 表示服务器返回了内容，接着还需确认清单可用、工件恢复成功。 |
| 下载 R2 工件 | `r2-artifacts` 统计单个包的缺失工件下载时间。冷 Runner 可能正常耗时较长。与上次成功运行比较缺失对象数量，再查传输超时和重试。 |
| 本地物化 `.ltar` | 网络传输结束后，Lake 还可能校验映射、恢复输出和更新本地 trace。`ltar-restore` 记录回退路径上归档硬链接或复制的耗时。Lake 也可能在不重编探测时物化输出。若下载已结束，检查磁盘和 Runner 的资源活动。 |
| 不重编探测 | `CACHE_HIT kind=exact` 表示找到候选清单。`no-build` 阶段结束并报告 `result=verified` 和退出码 0，才确认指定目标无需重编。检查日志正文中的实际结果。Actions 会先打印命令，再执行。 |
| 回退编译 | `no-build` 为 `result=rejected` 或 `result=skipped` 时，后续可能进入 `fallback-build`。原因字段为 `no-build-rejected`、`ancestor` 或 `none`。从 Lake 的模块日志判断重编范围。成功的作业或名为“恢复”的步骤也可能包含编译。`output-manifest` 单独记录清单补全。 |
| Lint 与审计 | `lake-lint` 与 `lake-test` 分段计时。如测试驱动检查公理，分别记录审计开始标记和结束时间。一个步骤可以包含驱动本身的编译及其内核审计，即使库已确认无需重编也仍需执行审计。 |
| Pages 快照 | 分别查看 HTTP GET、摘要核验、解包、站点拼装和部署的耗时。精确命中并通过验证时可以跳过 Lean 初始化。若返回 `404`，按配置走无写凭据的生成路径，接着查分析、渲染和图谱生成进度。 |

`qatlib-env ci build` 在 stderr 输出 `QATLIB_PHASE` JSON，其中 `state` 为 `start`、`done` 或 `error`，结束记录含 `elapsed_s`。如果设置了 `GITHUB_STEP_SUMMARY`，它会汇总准备、不重编探测、回退、产物清单、Lint 和测试阶段。报错或取消后，查看最后开始的阶段对应的命令日志。

一次 Lean 构建下载依赖及项目缓存约两分钟，合并的恢复与构建步骤约十二分钟。审计步骤接近三分钟，其中驱动打印审计开始后约一分钟。一次验证通过的 HTML 快照从命中到完成 Pages 部署约两分半。判断是否异常时，以同等条件的运行为基线。

## 区分快照缺失与下载失败

同时记录 curl 进程退出码和 HTTP 状态。传输成功且返回 `404`，表示该 URL 上缺少对象。HTTP/2 流错误（curl 退出码 92）即使收到了部分响应头或字节，也无法确认对象缺失。如果 Runner 的 HTTP/2 链路不稳定，使用 `--http1.1` 和有限超时重试同一个匿名 GET。只有下载成功且摘要相符才解包。让 `403`、持续传输失败和无效归档在作业日志中明确报错。 [curl 退出码](https://curl.se/libcurl/c/libcurl-errors.html)

用 [Cloudflare R2 跨仓缓存指南](r2-cache.md) 核对桶、命名空间、工具链键，以及生产端是否先发布工件再发布清单。
