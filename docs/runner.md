# 自托管 Runner

这一页把一台 Ubuntu 或 Debian 主机交给某个仓库跑作业。先确认这台机器可以执行哪些代码，再动账号和 systemd。本工具仓的 `ci.yml` 和 `docs.yml` 都写着 `runs-on: ubuntu-latest`。把 Runner 注册到别的标签上，不会让这两份工作流改到这台机器。下游工作流只有在 `runs-on` 命中你登记的标签时才会派到这里。

页缓存、取消后的残留进程和 Windows Job Object 按症状查[Runner 排障](runner-troubleshooting.md)。部署过程中不要写 `memory.reclaim`。

下面用 lane `pub` 和实例名 `builder-a` 作例子。lane 只能是 `pub`、`dev`、`secondary`。实例名要匹配 `[A-Za-z0-9][A-Za-z0-9._-]*`。默认根目录是 `/data/qatlas-ci`，可用 `QATLIB_CI_ROOT` 改；systemd 单元目录默认 `/etc/systemd/system`，可用 `QATLIB_SYSTEMD_DIR` 改。把 `OWNER/REPO` 换成目标仓库。

## 1. 核实控制器和信任范围

在 Linux 或 WSL2 上：

```bash
stat -fc %T /sys/fs/cgroup
```

成功时输出是 `cgroup2fs`，并且服务由 systemd 管理。把容器、WSL 虚拟机和物理宿主机三层的内存上限分别记下来。只把你愿意在这台机器上编译的仓库登记到对应 lane。不要把未审计的外部代码接到高权限的私有 Runner。

## 2. 先读锁定源码的 `--help`

在你准备执行的那一版 `qatlib-tools` 检出根目录：

```bash
bash src/qatlib_env/setup_runner.sh --help
```

wheel 安装则定位随包脚本后再看帮助：

```bash
bash "$(python -c 'from pathlib import Path; import qatlib_env; print(Path(qatlib_env.__file__).with_name("setup_runner.sh"))')" --help
```

帮助文本列出 `init`、`known-hosts` 和 `register`。`init` 与 `register` 都要以 root 运行。对照你锁定的脚本，核对将要写入的路径和配额，再执行。

## 3. `init`：按 lane 准备账号和共享 slice

```bash
sudo bash src/qatlib_env/setup_runner.sh init
```

`QATLIB_LANES` 决定准备哪些 lane，默认是 `pub dev secondary`。每个 lane 得到：

- 服务账号和用户组 `qatlas-ci-<lane>`，家目录为 `$QATLIB_CI_ROOT/<lane>`
- 该 lane 自己的 `elan`、`venv`、`lake-cache`、`pkg-mirror` 和 `runners/`
- 一份模板单元 `qatlas-<lane>-runner@.service`，其中 `User=` 和 `Group=` 都是 `qatlas-ci-<lane>`

同一次 `init` 还写入共享的 `qatlas-ci.slice`。slice 的默认内存水位是 `MemoryHigh=90%`、`MemoryMax=95%`，可用 `QATLIB_HOST_MEMORY_HIGH`、`QATLIB_HOST_MEMORY_MAX` 和 `QATLIB_HOST_CPU_QUOTA` 覆盖。

成功时，`id qatlas-ci-pub` 能解析，并且 `systemctl cat qatlas-ci.slice` 能看到上述水位。

## 4. 账号按 lane 分，目录和限额按实例分

`setup_runner.sh` 写出模板单元时，`User=` 使用的是 lane 账号。`src/qatlib_env/runner_config.py` 把实例目录放在 `base / lane / "runners" / instance`。因此：

| | 同一 lane 的实例共用 | 每个实例各有一份 |
| --- | --- | --- |
| 身份 | 服务账号 `qatlas-ci-<lane>` | 注册名 `qatlas-<lane>-<instance>`，以及登记时用的那枚注册令牌 |
| 目录 | lane 根下的工具链、虚拟环境和包镜像 | `$QATLIB_CI_ROOT/<lane>/runners/<instance>/`，包括该实例的 `.env`、`resources.toml` 和工作目录 |
| systemd | 模板 `qatlas-<lane>-runner@.service` 和父 slice | 实例 `qatlas-<lane>-runner@<instance>.service`，以及 `qatlas-<lane>-runner@<instance>.service.d/resources.conf` |

`pub` 的两个实例都是 `User=qatlas-ci-pub`。它们的工作目录和资源 drop-in 不同。

## 5. `register`：登记一个实例

GitHub 上，已登录且能创建注册令牌的 `gh` 可以省略 `--token`，脚本会自己取一枚短期令牌。否则传入令牌，不要把它写进仓库：

```bash
sudo bash src/qatlib_env/setup_runner.sh register pub builder-a \
  --url https://github.com/OWNER/REPO \
  --labels self-hosted,linux,x64 \
  --cpu-quota 200% \
  --memory-high 16G \
  --memory-max 20G
```

省略 `--labels` 时，GitHub 实例使用 `self-hosted,linux,x64`。Gitea 必须传入 `--token`；省略标签时默认 `ubuntu-latest:host,self-hosted:host`，其他不含冒号的标签会被补上 `:host`。URL 里出现 `github.com` 时默认按 GitHub 登记，否则按 Gitea。可以用 `--forge github` 或 `--forge gitea` 写明。

`register` 总会写一份实例 drop-in。若不传 `--cpu-quota`、`--memory-high` 和 `--memory-max`，这份 drop-in 把三者写成 `infinity`，从而盖过模板单元里的同名项。要把某个实例限制在 slice 之内的更小额度，就在这条命令里传入这三个参数。

成功时，`systemctl is-active qatlas-pub-runner@builder-a.service` 为 `active`。仓库的 Runner 设置里能看到名称 `qatlas-pub-builder-a` 和你传入的标签。

## 6. 标签要和将要运行的工作流一致

打开下游工作流的 `runs-on`。它列出的标签需要这个实例已经登记。本工具仓的两份工作流写的是 `ubuntu-latest`，它们继续留在 GitHub 托管的运行器上。

## 7. 读回已经生效的限额

```bash
systemctl show qatlas-pub-runner@builder-a.service \
  -p User -p Group -p WorkingDirectory -p Slice \
  -p ControlGroup -p CPUQuota \
  -p EffectiveMemoryHigh -p EffectiveMemoryMax -p KillMode
```

核对：

- `User` 和 `Group` 是 `qatlas-ci-pub`
- `WorkingDirectory` 是该实例目录
- `Slice` 是 `qatlas-ci.slice`
- `EffectiveMemoryHigh` 和 `EffectiveMemoryMax` 等于你在 `register` 传入的值
- `KillMode` 是 `control-group`。它在整个服务停机时清扫这个单元，不在网页取消单次作业时触发

再看 drop-in 文件 `qatlas-pub-runner@builder-a.service.d/resources.conf`，以及该实例 cgroup 目录里的实时 `memory.current`。

## 8. 用 lane 的服务账号验收

单元实际加载的是实例目录里的 `.env`。用同一个账号和这份环境读配置，不在 root 的 shell 里代替它：

```bash
runuser -u qatlas-ci-pub -- bash -c 'set -a && . /data/qatlas-ci/pub/runners/builder-a/.env && set +a && qatlib-env config explain --json'
runuser -u qatlas-ci-pub -- bash -c 'set -a && . /data/qatlas-ci/pub/runners/builder-a/.env && set +a && qatlib-env pool status'
```

若你改过 `QATLIB_CI_ROOT`，把路径换成 `systemctl show -p EnvironmentFiles` 打印的那一份。两条命令都成功时，第一条打印带 `resources` 和 `paths` 的 JSON，且不准备依赖、不申请租约；第二条打印池状态 JSON。

Linux 上的重型构建优先跑在带内存和 CPU 硬限额的 systemd 用户态瞬态 scope 里。若只能沿用外层 cgroup，后端要求有限的 `memory.max`、明确的 CPU 配额，以及每一层可见祖先的真实余量。只写 `memory.high`，或只绑 CPU 亲和性，不够。

## 9. 四种演练

接正式作业之前，用一个会派生多层子进程的任务分别做完：正常退出、在网页上取消、单模块超时、整阶段超时。每次之后查看：

- 进程表里是否还有属于该次作业 cgroup 的 `lake` 或 `lean`
- 该 cgroup 的 `memory.events`
- 用第 8 步的账号执行的 `qatlib-env pool status` 里，租约是否已经释放
- 作业记录上的结论是不是你刚刚演练的那一种

四次都收干净，这台实例才算验收通过。内存降不下去，或取消后进程还在，按现象打开[Runner 排障](runner-troubleshooting.md)，不要在部署命令里夹带回收。

<a id="note-memory"></a>

## 账面内存偏高

宿主机看起来还有内存、却没有高 RSS 进程、新任务仍在等准入时，读[cgroup 内存计费](runner-troubleshooting.md#note-memory)。硬准入使用各可见祖先层的 `high` 或 `max` 减去 `current`，不把 `inactive_file` 当成可立即释放的配额。

<a id="note-cancellation"></a>

## 作业取消后进程还在

网页已经显示取消，服务却仍在线，并且能看到 `PPID=1` 的编译进程时，读[取消任务与回收子进程](runner-troubleshooting.md#note-cancellation)。`PPID=1` 只说明父进程已经不在。单次取消不会停止常驻服务，因此也不会靠服务单元的 `KillMode=control-group` 做这次清扫。

<a id="windows-job-object"></a>

## Windows Job Object

在 Windows 受管模式下，监督器先启动一个等待放行的引导子进程，把它挂进设有 CPU 限额、内存上限和 `KILL_ON_JOB_CLOSE` 的 [Windows Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。挂载和限额都成功之后才放行真实命令。任一步失败，监督器拒绝启动，不退回无保护执行。任务正常结束、被取消或超时后，监督器关闭 Job 句柄，由内核收回组内进程。单模块超时仍由用户态观察跟踪。启用条件和文件系统要求见[编译环境的 Windows 小节](build.md#windows)。`qatlib-env exec` 目前只在具备相应 cgroup 控制器的 Linux 上可用。取消之后如何核对进程身份，见[排障页的同一节](runner-troubleshooting.md#windows-job-object)。

<a id="runner"></a>

## 部署顺序

1. `stat -fc %T /sys/fs/cgroup` 得到 `cgroup2fs`，并写明这台机器可以跑哪些仓库。
2. 在锁定的源码上阅读 `setup_runner.sh --help`。
3. root 执行 `init`，确认 lane 账号 `qatlas-ci-<lane>` 和 `qatlas-ci.slice`。
4. root 执行 `register <lane> <instance>`，传入标签和该实例的 CPU、内存限额。
5. 用 `systemctl show` 读回 `User`、`WorkingDirectory`、`Slice` 和 `EffectiveMemoryMax`。
6. 用该 lane 的服务账号、加载实例 `.env` 后，运行 `qatlib-env config explain --json` 和 `qatlib-env pool status`。
7. 做完正常退出、取消、单模块超时、整阶段超时。异常转到[排障](runner-troubleshooting.md)。
