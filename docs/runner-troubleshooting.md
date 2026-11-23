# Runner 排障

部署、标签和验收在[自托管 Runner](runner.md)。这一页按症状查找。先记下作业页面上的 head、工作流和作业结论，再沿下面的顺序取证，最后才做受控恢复：

1. 读目标 cgroup 的 `memory.current`、`memory.high`、`memory.max`、`memory.stat`、`memory.events` 和 `memory.pressure`。准入看的是最紧的可见祖先层。
2. 核对进程的 PID、启动时间、完整命令行、工作目录和所属 cgroup。`PPID=1` 只说明父进程已经不在，不能单独当作可以结束该进程的理由。
3. 用当时的服务账号运行 `qatlib-env pool status`，看共享池里还有没有没释放的租约。
4. 回到作业记录，确认取消或超时对应的是哪一次运行。单次作业取消不会停止常驻的 Runner 服务。

恢复动作都有前提：确认文件已经冷置，或先暂停接新任务并确认控制组里没有活跃作业。不要在共享宿主机上写全局 `drop_caches`。

| 你看到的现象 | 阅读 |
| --- | --- |
| 宿主机内存看起来很多，也没有高 RSS 的编译进程，新任务却一直等准入 | [cgroup 内存计费](#note-memory) |
| 网页上作业已经取消或超时，内存仍高，进程表里有 `PPID=1` 的 `lake` 或 `lean` | [取消任务与回收子进程](#note-cancellation) |
| Windows 上任务结束后，作业里的进程还在 | [Windows Job Object](#windows-job-object) |

<a id="note-memory"></a>

## cgroup 内存计费

- **现象**：从宿主机上看物理内存十分充裕，也没有任何高 RSS 的编译进程在运行，但新提交的构建任务却一直卡在准入等待，最终因等待超时而报错退出。
- **底层原理**：Linux cgroup v2 的 `memory.current` 会对当前控制组及其全部后代统一记账，**既包含进程匿名内存（`anon`），也包含内核文件页缓存（`file`）**。在密集解包 `.ltar` 缓存归档、读写成百上千份 `.olean` 工件之后，哪怕编译进程早已退出，数十 GiB 的文件页缓存仍可能常驻在该 service 所属的 cgroup 账上。假设某层 cgroup 设置了 `memory.high = 40 GiB`，而残留的页缓存让 `memory.current` 停留在 `36 GiB`，该层账面剩余额度就只剩 `4 GiB`——即便宿主机还有上百 GiB 空闲内存，或者更外层的 `memory.max` 设得更高，准入器在逐层向上核查所有可见祖先时，依然会被最紧的这 `4 GiB` 拦住（参见[内核 cgroup v2 内存文档](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory)）。
- **为什么不能直接按 `current - inactive_file` 放宽硬准入？** 现场排障时，沿着目标 service 或进程的 cgroup 逐层读取 `memory.current`、`memory.high`、`memory.max`、`memory.stat`、`memory.events` 与 `memory.pressure`，再对照 `anon`、`file`、`shmem`、`inactive_file`、`file_dirty` 和 `file_writeback`，确实能直观区分“正在运行的进程内存”与“历史留下的文件页缓存”。但 `inactive_file` 只是内核 LRU 链表的冷热分类，并非“立即可无损释放”的承诺——其中可能包含正在回写或仍被映射的文件页，而且跨多个统计文件读取也不是原子快照；同时，`shmem` 虽然计入 `file`，实际却挂在 anon LRU 链表上，不能将内存类型计数与 LRU 链表计数混为一谈。如果前置估算擅自扣除 `inactive_file`，不仅会把未必能收回的内存当成确定配额放行，还会与执行后端针对原始 `max`/`current` 的硬性检查产生契约冲突。因此，`qatlib-env` 严格取各可见祖先层 `high`/`max - current` 的最小非负值，再结合宿主机 `MemAvailable`、系统预留额与共享资源池上限综合判定准入；一旦读到有限配额却缺失合法的 `current` 值，或配额文件本身不可读、格式非法，探针一律按**零余量**保守拒绝，并在 `config explain` 输出的 `resources.limits` 中明确标出 `memory.error`（容器内不可见的宿主机外层祖先需回到宿主侧核查）。
- **标准解法与适用边界（空闲维护窗口）**：
  1. **对确认冷置的归档或大体积产物发出用户态释放提示**：当归档已完整解包、所有写入句柄均已关闭且近期不再读取时，可重新打开文件描述符并调用 `posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)`。偏移与长度均传 `0` 表示作用于整个文件，若只做局部释放则须按页对齐；有持久化需求的数据须先由写者完成同步写回，未落盘的脏页不会借此释放。调用完成后关闭描述符并重新采样，观察 `file` 计数回落情况。由于该操作同样会影响同一物理文件的其他并发读者，请仅对确认冷置的文件执行（参见 [`posix_fadvise(2)`](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html)）。
  2. **对特定空闲 cgroup 执行管理员定量回收**：先暂停目标 Runner 接收新任务并确认控制组内已无活跃作业，再由具备权限的管理员向目标 cgroup 目录下的 `memory.reclaim` 写入温和的回收额度（例如 `printf '%s\n' 512M > memory.reclaim`；内核实际回收量可能略多或略少，可回收页不足请求量时可能返回 `EAGAIN`），随后复查 `memory.current`、`memory.stat`、`memory.events` 与 `memory.pressure`，确认余量恢复后再重新放开调度。切勿将自动回收逻辑塞进单次任务的热路径，更不要在共享宿主机上贸然执行全局 `drop_caches`，以免清空其他服务赖以维持性能的热缓存（参见 [`memory.reclaim`](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files) · [`drop_caches`](https://docs.kernel.org/admin-guide/sysctl/vm.html#drop-caches)）。

<a id="note-cancellation"></a>

## 取消任务与回收子进程

- **现象**：GitHub 或 Gitea 页面早已显示某次构建任务被取消或因超时失败，Runner 服务状态也显示在线正常，但机器内存长期卡在 90% 以上并伴随剧烈的回收抖动；排查进程表时，常能抓到挂在 `PPID=1` 下的孤儿 `lake` 或 `lean` 进程。
- **底层原理**：`systemd` 服务单元配置的 `KillMode=control-group` **只有在整个 service 停机或重启时，才会清扫控制组内的全部残留进程**。而自托管 Runner 是一个长期常驻的守护进程，在网页端取消单次 CI 作业只会让 Runner 终止顶层的作业包装进程，并不会触发 `systemd` 停止整个 Runner 服务。一旦深层派生的 `lake` 或 `lean` 子孙进程脱离父进程变成 `PPID=1` 的孤儿，就会继续留在原 service 所属的 cgroup 中消耗内存：账面用量冲破 `MemoryHigh` 后，内核会启动高频的直接内存回收与分配节流；但只要尚未触及 `MemoryMax` 硬上限触发组内 OOM，这些进程就会陷在“一边被强行节流回收、一边继续申请内存”的拉锯战中，拖着整个控制组长时间剧烈抖动（参见 [`systemd.kill`](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html) · [`systemd` 资源控制](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)）。
- **标准解法与适用边界**：
  1. **逐作业独立进程域与父死收割**：Linux 公共监督器会为每条受管命令启动独立的 `guardian` 守护进程，并由 `guardian` 设置 `PR_SET_PDEATHSIG`，确保在父监督器被硬杀后仍能收到信号并继续执行清理。由于仅对业务负载（workload）本身设置父死信号无法覆盖经 double-fork 或 `setsid` 脱离会话的子孙进程，`guardian` 会同时通过 `PR_SET_CHILD_SUBREAPER` 收养这些脱离父进程的后代，结合 PID 与启动时间核验进程身份，对已接管的进程组及全部后代依次执行 `TERM`、宽限期等待、`KILL` 与循环收割。无论是正常结束、用户手动取消，还是触发单模块超时或整阶段（stage）超时，都共用这套清理流程，并完整保留原命令的 `stdin`、输出捕获与信号退出码契约。实现上，`guardian` 作为独立进程启动，从而避开多线程父进程中使用 `preexec_fn` 可能引发的死锁；同时，在共享机器上严禁仅凭 `lean` 或 `lake` 进程名无差别通杀。
  2. **内核整组硬上限作为第二道防线**：受限命令会运行在逐作业创建的瞬态 `systemd` scope 中，通过 `MemoryMax`、`MemorySwapMax=0`、`CPUQuota`、`RuntimeMaxSec` 与 `KillMode=control-group` 为整组进程划定内存、交换空间、CPU、最长运行期限与整组终止边界。即便连 `guardian` 也在极端情况下被同时硬杀，内核与 `systemd` 依然能按既定的资源上限和运行期限收束该 scope，而不必依赖常驻 Runner service 停机时才顺带清理。这层保护要求 Linux 环境具备实际可用、且支持上述属性的 `systemd` 用户态 CPU 与内存控制器；一旦属性不受支持或权限不足，命令会直接报错失败，绝不静默退回无边界裸跑。不过，该机制无法跨宿主机宕机保护残留任务，也不替代外部容器 daemon 自身的生命周期管理。
  3. **将临时实验、目录枚举与文本搜索一并纳入边界**：日常的普通 Python 脚本与递归搜索同样可能派生残留子进程，或无意间扫遍多个工作树下的庞大缓存。即便不在 Lean 项目中，也可直接使用 `qatlib-env exec -- <命令>` 启动受限执行：默认限额为运行期限 **300 秒**、内存 **4 GiB**、**1 CPU**，可通过 `--timeout`、`--memory-mib`（至少 `2048` MiB）与 `--cpu` 按需调整。该入口直接复用本机共享资源池的 CPU 与内存租约，准入等待同样受超时约束，超出池硬上限的请求会被直接拒绝；既有 Lean 构建的预算与准入规则保持不变。若由用户态监督器判定超时，命令返回 `124`；若内核期限先一步触发，则保留底层执行链因信号终止产生的退出状态。需要注意，`qatlib-env exec` 需由调用方主动接入，无法自动拦截外部 agent harness 直接发出的裸 shell 命令；本机共享准入池也并非驻留后台的调度平台，不额外引入配额数据库或 I/O 调速器。

     ```bash
     qatlib-env exec --timeout 300 --memory-mib 4096 --cpu 1 -- python experiment.py
     # 只搜索明确的源码目录，并显式裁剪派生树
     qatlib-env exec --timeout 60 -- rg --glob '*.lean' --glob '!.lake/**' --glob '!**/.lake/**' --glob '!.venv/**' --glob '!**/.venv/**' --glob '!.agent-tasks/**' --glob '!**/.agent-tasks/**' 'pattern' Source verification
     ```

     `qatlib-tools` 仓库根目录下的 `.ignore` 已为遵循该规则的工具（如默认模式下的 `rg`）排除了 `.lake`、`.git`、`.venv`、`.agent-tasks`、`dist` 与 `build`，但不会自动保护其他工作树，也不会替下游消费仓库改写配置。普通的 `grep` 与 `find` 本就不读取 `.ignore`，`rg --no-ignore` 也会显式绕开忽略规则，因此在这些场景下必须手动排除 `.lake`、`.venv` 与 `.agent-tasks`；自行编写目录枚举脚本时，也应在递归进入这些目录前提前剪枝，而不是等全量扫盘结束后再过滤结果。应当先从源头避免无意义扫盘，再用有限的运行期限兜底控制执行时长。
  4. **跨平台句柄绑定与残留取证**：Windows 受管构建模式通过在子进程启动阶段挂载 `Job Object` 来实现整组 CPU、内存硬约束与生命周期绑定，只要关闭最后一个 Job 句柄，内核便会原子化地终止组内全部残留子进程（参见 [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)）；上文的通用 `qatlib-env exec` 入口则仅支持 Linux，在其他平台上调用会明确拒绝。线上排查疑似孤儿进程时，不应仅凭某一时刻看到 `PPID=1` 就下定论（`PPID=1` 只是线索之一），而应联合核对 PID、启动时间、完整命令行、工作目录与所属 cgroup，并对照 `memory.events` 中 `high`、`max`、`oom` 计数器的增长速率综合判断；若涉及容器任务，还须结合外部容器 daemon 的真实命名空间一并核实。

<a id="windows-job-object"></a>

## Windows Job Object

在 Windows 受管模式下，监督器会先启动一个处于等待放行状态的引导子进程，将其挂入配置好 CPU 限额、内存上限及 `KILL_ON_JOB_CLOSE` 标志的 [Windows Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)，确认挂载与限额设置全部成功后，再放行该子进程执行真实的构建命令；一旦挂入或限额设置失败，监督器会直接拒绝启动受管命令，避免退回无保护执行。无论任务是正常退出、中途取消还是触发超时，监督器都会关闭对应的 Job 句柄，连同那些尚未来得及被用户态轮询观察到的瞬时子孙进程一起由内核彻底回收；细粒度的单模块超时则继续由用户态进程观察机制跟踪。受管模式的启用方式与文件系统要求详见[编译环境的 Windows 小节](build.md#windows)；通用的 `qatlib-env exec` 入口目前仍仅支持具备相应 cgroup 控制器的 Linux 环境。

部署、读回限额和四种演练在[自托管 Runner](runner.md#runner)。排障时不要改注册信息；先按本页开头的顺序取证。
