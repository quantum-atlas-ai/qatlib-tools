# CI Runner 与进程排障

<a name="note-memory"></a>

## cgroup 内存计费

- **现象**：在宿主机上看物理内存充裕，也没有任何高 RSS 的编译进程在跑，但新提交的构建任务却一直卡在准入等待，最终报资源准入超时。
- **底层原理**：Linux cgroup v2 的 `memory.current` 对当前控制组及其全部后代统一记账，**既包含进程匿名内存（`anon`），也包含内核文件页缓存（`file`）**。在密集解包 `.ltar` 缓存归档、读写成百上千份 `.olean` 工件后，哪怕编译进程早已退出，数十 GiB 的文件页缓存仍可能常驻在该 service 所属的 cgroup 账上。假如某层 cgroup 设了 `memory.high = 40 GiB`，而页缓存让 `memory.current` 停在 `36 GiB`，那么该层账面剩余额度就只有 `4 GiB`——哪怕宿主机还有上百 GiB 空闲、或者外层 `memory.max` 设得更高，准入器逐层向上核查所有可见祖先时，依然会被最紧的这 `4 GiB` 卡住（参见[内核 cgroup v2 内存文档](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory)）。
- **为什么不能直接拿 `current - inactive_file` 放宽硬准入？** 排查时沿着目标 service 或进程的 cgroup 逐层读取 `memory.current`、`memory.high`、`memory.max`、`memory.stat`、`memory.events` 与 `memory.pressure`，对照 `anon`、`file`、`shmem`、`inactive_file`、`file_dirty` 和 `file_writeback`，确实能一眼分清“真正在跑的进程内存”和“历史留下的页缓存”。但 `inactive_file` 只是内核 LRU 链表的冷热分类，不是“立即可无损释放”的承诺（其中可能有正在回写或仍被映射的文件页，且多个统计文件之间并非原子快照）；`shmem` 虽计入 `file`，却位于 anon LRU 链表，不能把类型计数与 LRU 计数混为一谈。若前置估算擅自扣除 `inactive_file`，不仅会把未必能收回的内存当成确定配额放行，还会与执行后端对原始 `max`/`current` 的硬检查产生契约冲突。因此 `qatlib-env` 严格按各可见祖先 `high`/`max - current` 的最小非负值结合宿主 `MemAvailable`、预留额与共享池上限判定准入；一旦读到有限配额却缺失合法 `current`（或配额文件本身不可读/格式非法），探针一律按**零余量**保守拒绝，并在 `config explain` 的 `resources.limits` 中明确标出 `memory.error`（容器内不可见的宿主机外层祖先需在宿主侧核查）。
- **标准解法与适用边界（空闲维护窗口）**：
  1. **针对明确不再使用的冷归档/大产物文件（用户态精确提示）**：当归档已完整解包、所有写入句柄已关闭且近期不再读取时，可重新打开文件描述符调用 `posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)`（偏移与长度均传 `0` 代表整文件，局部释放须按页对齐；若有持久化需求须先由写者完成同步写回，未落盘脏页不会借此释放），随后关闭描述符并重新采样观察 `file` 回落情况。注意该调用会影响同一物理文件的其他并发读者，仅对确认冷置的文件使用（参见 [`posix_fadvise(2)`](https://man7.org/linux/man-pages/man2/posix_fadvise.2.html)）。
  2. **针对特定空闲 cgroup 的定量回收（管理员维护操作）**：先暂停该 Runner 接收新任务并确认组内无活跃作业，再由具备权限的管理员向目标 cgroup 目录的 `memory.reclaim` 写入温和额度（如 `printf '%s\n' 512M > memory.reclaim`；内核实际回收量可能略多或略少，不足请求量时可能返回 `EAGAIN`），随后复查 `memory.current`、`memory.stat`、`memory.events` 与 `memory.pressure` 确认余量恢复后再重新放开调度。切勿把自动回收塞进逐次任务的热路径，更不要在共享宿主机上贸然执行全局 `drop_caches` 清空其他服务赖以维持性能的热缓存（参见 [`memory.reclaim`](https://docs.kernel.org/admin-guide/cgroup-v2.html#memory-interface-files) · [`drop_caches`](https://docs.kernel.org/admin-guide/sysctl/vm.html#drop-caches)）。

<a name="note-cancellation"></a>

## 取消任务与回收子进程

- **现象**：GitHub / Gitea 页面早已显示某次构建任务已取消或超时失败，Runner 服务状态也显示在线正常，但机器内存长期卡在 90% 以上剧烈回收抖动，排查进程表常能抓到挂在 `PPID=1` 下的孤儿 `lake` 或 `lean` 进程。
- **底层原理**：systemd 服务单元配置的 `KillMode=control-group` **只在整个 service 停机或重启时才会清扫控制组内的所有残留进程**；而自托管 Runner 是一个长期常驻的守护进程，网页端取消单次 CI job 只会让 Runner 终止顶层作业包装进程，并不会触发 systemd 停掉整个 Runner 服务！一旦深层派生的 `lake` / `lean` 子孙进程脱离父进程变成 `PPID=1` 的孤儿，它们就会留在原 service 的 cgroup 里继续狂吃内存：账面用量冲破 `MemoryHigh` 后内核启动高频直接内存回收与分配节流，但只要还没撞上 `MemoryMax` 硬上限触发组内 OOM，进程就会在“一边被强行节流回收、一边继续申请内存”的拉锯战中把整组拖入长时抖动（参见 [`systemd.kill`](https://www.freedesktop.org/software/systemd/man/latest/systemd.kill.html) · [`systemd` 资源控制](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html)）。
- **标准解法与适用边界**：
  1. **逐作业独立进程域与父死收割**：Linux 公共监督器为每个命令启动独立 guardian 进程，由 guardian 设置 `PR_SET_PDEATHSIG`，在父监督器被硬杀后接收信号并继续清理。只给 workload 本身设置父死信号，无法覆盖 double-fork 或 `setsid` 脱离会话的子孙；guardian 同时通过 `PR_SET_CHILD_SUBREAPER` 收养这些后代，按 PID 与启动时间跟踪进程身份，对已拥有的进程组和子孙执行 TERM、宽限期、KILL 与反复收割。正常结束、用户取消、单模块超时与整阶段（stage）超时共用这套清理流程，保留命令的 stdin、捕获输出与信号退出码契约。guardian 独立启动，避免在线程化父进程里使用可能死锁的 `preexec_fn`；共享机器上严禁仅凭 `lean`/`lake` 进程名通杀。
  2. **内核整组硬上限作为第二道防线**：受限命令进入逐作业瞬态 systemd scope，以 `MemoryMax`、`MemorySwapMax=0`、`CPUQuota`、`RuntimeMaxSec` 和 `KillMode=control-group` 对整组进程施加内存、CPU、运行期限和终止边界。即使 guardian 也被同时硬杀，内核与 systemd 仍可按有限资源和期限收束该 scope，不能只依赖常驻 Runner service 的停机清理。此保护要求 Linux 上实际可用且支持这些属性的 systemd 用户 CPU/内存控制器；属性或权限不支持时命令失败，不会静默退回无界执行。它不能保护宿主消失后的任务，也不代替外部容器 daemon 的生命周期管理。
  3. **临时实验、枚举与搜索也要接入边界**：普通 Python 和递归搜索同样可能派生残留进程或扫遍多工作树缓存。无需 Lean 项目即可使用 `qatlib-env exec -- <命令>`，默认期限 **300 秒、内存 4 GiB、1 CPU**；可用 `--timeout`、`--memory-mib`（至少 2048 MiB）与 `--cpu` 调整。它复用现有本机共享资源池的 CPU/内存租约，等待准入也有期限；请求超出池硬上限会直接拒绝。监督器超时返回 `124`，内核期限先触发时保留执行链的信号终止状态。现有 Lean 构建的预算与准入规则继续适用。这个入口需要调用方主动接入，无法自动截获外部 agent harness 的裸 shell；共享准入池也不是驻留调度平台，不新增配额数据库或 I/O 调速器。

     ```bash
     qatlib-env exec --timeout 300 --memory-mib 4096 --cpu 1 -- python experiment.py
     # 只搜索明确的源码目录，并显式裁剪派生树
     qatlib-env exec --timeout 60 -- rg --glob '*.lean' --glob '!.lake/**' --glob '!**/.lake/**' --glob '!.venv/**' --glob '!**/.venv/**' --glob '!.agent-tasks/**' --glob '!**/.agent-tasks/**' 'pattern' Source verification
     ```

     本工具仓根 `.ignore` 为遵循它的工具（如默认 `rg`）排除 `.lake`、`.git`、`.venv`、`.agent-tasks`、`dist` 与 `build`，但不会自动保护其他工作树，也不会替消费仓写配置。普通 `grep` / `find` 不读取 `.ignore`，`rg --no-ignore` 也会绕开它，调用方须显式排除 `.lake`、`.venv` 与 `.agent-tasks`；自写枚举脚本应在进入这些目录前剪枝，而不是扫描后再过滤结果。先避免无意义扫盘，再用有限 runtime 控制执行期限。
  4. **跨平台句柄绑定与残留取证**：Windows 受管构建模式通过启动即挂载 `Job Object` 实现整组 CPU/内存硬约束与生命周期绑定，关闭最后一个 Job 句柄即可由内核原子终止组内全部残留子进程（参见 [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)）；上述通用 `exec` 入口则仅支持 Linux，其他平台会明确拒绝。线上排查疑似孤儿进程时，应联合核对 PID、启动时间、完整命令行、工作目录与所属 cgroup，并对照 `memory.events` 中 `high` / `max` / `oom` 计数器的增长速率（单看某刻 `PPID=1` 只是线索；若涉及容器任务，还需结合外部容器 daemon 的真实命名空间核实）。

<a id="windows-job-object"></a>

## Windows Job Object

Windows 先启动等待放行的子进程，把它挂入配置了 CPU、内存及 `KILL_ON_JOB_CLOSE` 的 Job，再让它执行真实命令。挂入或设置失败时，拒绝启动受管命令。正常退出、取消和超时都会关闭 Job，连同来不及被轮询观察的子孙一起回收。模块级超时仍由进程观察跟踪。启用方式及文件系统要求见[编译环境的 Windows 小节](build.md#windows)。通用 `qatlib-env exec` 仍只支持具备相应控制器的 Linux。 [Windows Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)

<a name="runner"></a>

## 自托管 Runner

1. **核实系统控制器与信任边界**：在 Linux / WSL2 上确认服务由 `systemd` 托管，且 `stat -fc %T /sys/fs/cgroup` 返回 `cgroup2fs`；厘清容器、WSL 虚拟机与物理宿主机三层各自的内存上限。为每个 Runner 实例分配独立工作目录、独立注册凭据与隔离运行账号，严禁将未受审的外部不受信任代码接入私有高权限 Runner。
2. **审阅安装脚本与双阶段职责**：在锁定的 `qatlib-tools` 源码检出中运行 `bash src/qatlib_env/setup_runner.sh --help`（若为 wheel 安装环境，可运行 `bash "$(python -c 'from pathlib import Path; import qatlib_env; print(Path(qatlib_env.__file__).with_name("setup_runner.sh"))')" --help`）。该脚本面向 Ubuntu / Debian + `systemd` 环境：`init` 负责配置宿主依赖、专用运行账户与共享父 `slice`；`register` 负责注册单个 Runner 实例并写入服务级资源限制。执行前务必人工核对目标路径、凭据范围与配额数值。
3. **协同预算父 `slice` 与单实例配额**：在服务 drop-in 配置的 `[Service]` 段同时声明 `Slice`、`CPUQuota`、`MemoryHigh`、`MemoryMax`、`TasksMax` 与 `KillMode=control-group`，确保各实例并发峰值之和与父 `slice` 上限相匹配，并为 Runner 自身进程、文件页缓存及同机其他服务留足安全余量。落盘重载后，执行 `systemctl show <服务> -p ControlGroup -p Slice -p EffectiveMemoryHigh -p EffectiveMemoryMax -p KillMode` 读回核实，并直接检查对应 `/sys/fs/cgroup/...` 节点的实时计费。
4. **切换至实际运行账户做准入验收**：在 Runner 实际服务用户下运行 `qatlib-env config explain` 与 `qatlib-env pool status`。Linux 重型任务优先走受限的 `systemd` 用户态瞬态 scope；若受环境所限只能沿用外层既有 cgroup，执行后端会强制校验有限的 `memory.max`、CPU 配额及所有可见祖先层的真实余量——仅配置软水线 `memory.high` 或 CPU 亲和性（affinity）不满足硬隔离准入条件。
5. 用会派生多层子进程的任务演练正常退出、手动取消、单模块超时与整阶段超时。复查进程、cgroup、共享池租约和 `memory.events`。如果内存一直偏高，分别核对[内存计费](#note-memory)与[子进程回收](#note-cancellation)。
