# Self-Hosted Runners

This page attaches an Ubuntu or Debian host to a repository. Decide which code that machine may run before creating accounts or systemd units. In this tools repository, both `ci.yml` and `docs.yml` set `runs-on: ubuntu-latest`. Registering a runner under other labels does not move those two workflows onto this machine. A downstream workflow is scheduled here when its `runs-on` matches the labels you register.

Page cache, processes left after cancellation, and the Windows Job Object are indexed by symptom in [Runner troubleshooting](runner-troubleshooting.md). Do not write `memory.reclaim` during deployment.

The examples use lane `pub` and instance `builder-a`. A lane is `pub`, `dev`, or `secondary`. An instance name matches `[A-Za-z0-9][A-Za-z0-9._-]*`. The default root is `/data/qatlas-ci`, overridable with `QATLIB_CI_ROOT`. The default systemd directory is `/etc/systemd/system`, overridable with `QATLIB_SYSTEMD_DIR`. Replace `OWNER/REPO` with the target repository.

## 1. Confirm the controllers and the trust boundary

On Linux or WSL2:

```bash
stat -fc %T /sys/fs/cgroup
```

On success the output is `cgroup2fs`, and services are managed by systemd. Write down the memory ceilings of the container, the WSL virtual machine, and the physical host as three separate numbers. Register only repositories you are willing to compile on this machine. Do not attach unaudited external code to a high-privilege private runner.

## 2. Read `--help` from the pinned source

From the root of the `qatlib-tools` checkout you intend to run:

```bash
bash src/qatlib_env/setup_runner.sh --help
```

From a wheel install, locate the bundled script and read its help:

```bash
bash "$(python -c 'from pathlib import Path; import qatlib_env; print(Path(qatlib_env.__file__).with_name("setup_runner.sh"))')" --help
```

The help lists `init`, `known-hosts`, and `register`. Both `init` and `register` run as root. Compare the paths and quotas with the script you pinned, then run it.

## 3. `init`: accounts and the shared slice, per lane

```bash
sudo bash src/qatlib_env/setup_runner.sh init
```

`QATLIB_LANES` selects the lanes. The default is `pub dev secondary`. Each lane receives:

- the service account and group `qatlas-ci-<lane>`, with home `$QATLIB_CI_ROOT/<lane>`
- that lane's `elan`, `venv`, `lake-cache`, `pkg-mirror`, and `runners/`
- a template unit `qatlas-<lane>-runner@.service` whose `User=` and `Group=` are `qatlas-ci-<lane>`

The same `init` writes the shared `qatlas-ci.slice`. The slice defaults are `MemoryHigh=90%` and `MemoryMax=95%`, overridable with `QATLIB_HOST_MEMORY_HIGH`, `QATLIB_HOST_MEMORY_MAX`, and `QATLIB_HOST_CPU_QUOTA`.

On success, `id qatlas-ci-pub` resolves, and `systemctl cat qatlas-ci.slice` shows those watermarks.

## 4. The account follows the lane; the directory and limits follow the instance

When `setup_runner.sh` writes the template unit, `User=` is the lane account. `src/qatlib_env/runner_config.py` places the instance directory at `base / lane / "runners" / instance`. Therefore:

| | Shared by every instance of one lane | Separate for each instance |
| --- | --- | --- |
| Identity | Service account `qatlas-ci-<lane>` | Registration name `qatlas-<lane>-<instance>`, and the registration token used for that registration |
| Directories | The toolchain, virtual environment, and package mirror under the lane root | `$QATLIB_CI_ROOT/<lane>/runners/<instance>/`, including that instance's `.env`, `resources.toml`, and work directory |
| systemd | Template `qatlas-<lane>-runner@.service` and the parent slice | Instance `qatlas-<lane>-runner@<instance>.service`, plus `qatlas-<lane>-runner@<instance>.service.d/resources.conf` |

Both instances of `pub` have `User=qatlas-ci-pub`. Their work directories and resource drop-ins differ.

## 5. `register`: one instance

On GitHub, an authenticated `gh` that may create a registration token can omit `--token`. The script requests a short-lived token. Otherwise pass the token, and do not commit it:

```bash
sudo bash src/qatlib_env/setup_runner.sh register pub builder-a \
  --url https://github.com/OWNER/REPO \
  --labels self-hosted,linux,x64 \
  --cpu-quota 200% \
  --memory-high 16G \
  --memory-max 20G
```

If `--labels` is omitted, a GitHub instance uses `self-hosted,linux,x64`. Gitea requires `--token`. Its default labels are `ubuntu-latest:host,self-hosted:host`, and any other label without a colon receives a `:host` suffix. A URL containing `github.com` registers with GitHub; any other URL registers with Gitea. `--forge github` or `--forge gitea` states the choice.

`register` always writes an instance drop-in. If you omit `--cpu-quota`, `--memory-high`, and `--memory-max`, that drop-in sets all three to `infinity` and overrides the same keys in the template unit. To keep one instance inside a smaller budget than the slice, pass the three options on this command.

On success, `systemctl is-active qatlas-pub-runner@builder-a.service` prints `active`. The repository's runner settings show the name `qatlas-pub-builder-a` and the labels you passed.

## 6. Match the labels to the workflow that should run here

Open `runs-on` in the downstream workflow. The labels it lists need to be registered on this instance. The two workflows in this tools repository say `ubuntu-latest`, so they stay on GitHub-hosted runners.

## 7. Read the effective limits back

```bash
systemctl show qatlas-pub-runner@builder-a.service \
  -p User -p Group -p WorkingDirectory -p Slice \
  -p ControlGroup -p CPUQuota \
  -p EffectiveMemoryHigh -p EffectiveMemoryMax -p KillMode
```

Check:

- `User` and `Group` are `qatlas-ci-pub`
- `WorkingDirectory` is that instance directory
- `Slice` is `qatlas-ci.slice`
- `EffectiveMemoryHigh` and `EffectiveMemoryMax` equal the values passed to `register`
- `KillMode` is `control-group`. It sweeps the unit when the whole service stops. Cancelling one job in the web UI does not stop the service

Also read the drop-in `qatlas-pub-runner@builder-a.service.d/resources.conf` and the live `memory.current` in that instance's cgroup directory.

## 8. Accept the result as the lane's service account

The unit loads `.env` from the instance directory. Read the configuration as that account with that file, not from root's shell:

```bash
runuser -u qatlas-ci-pub -- bash -c 'set -a && . /data/qatlas-ci/pub/runners/builder-a/.env && set +a && qatlib-env config explain --json'
runuser -u qatlas-ci-pub -- bash -c 'set -a && . /data/qatlas-ci/pub/runners/builder-a/.env && set +a && qatlib-env pool status'
```

If `QATLIB_CI_ROOT` is different, use the path from `systemctl show -p EnvironmentFiles`. On success, the first command prints JSON containing `resources` and `paths`, without preparing dependencies or acquiring a lease. The second prints pool-status JSON.

On Linux, a heavy build prefers a systemd user transient scope with hard memory and CPU limits. If the command has to stay in an outer cgroup, the backend requires a finite `memory.max`, an explicit CPU quota, and real headroom on every visible ancestor. `memory.high` alone, or a CPU affinity mask alone, is not that check.

## 9. Four drills

Before accepting production jobs, run a task that spawns several levels of child processes through each of these: a normal exit, a cancellation from the web UI, a per-module timeout, and a whole-stage timeout. After each one, look at:

- whether any `lake` or `lean` from that job's cgroup is still in the process table
- `memory.events` for that cgroup
- `qatlib-env pool status`, run as the account from step 8, and whether the lease was released
- the job conclusion, which should be the drill you just ran

The instance is accepted when all four leave a clean machine. If memory stays high, or a process remains after cancellation, open [Runner troubleshooting](runner-troubleshooting.md) for that symptom. Do not fold a reclaim into the deploy commands.

<a id="note-memory"></a>

## Accounted memory stays high

When the host still looks to have free memory, no high-RSS compiler is running, and new jobs wait for admission, read [cgroup memory accounting](runner-troubleshooting.md#note-memory). Hard admission uses `high` or `max` minus `current` on each visible ancestor. It does not treat `inactive_file` as quota that can be released immediately.

<a id="note-cancellation"></a>

## A process remains after the job is cancelled

When the web UI already shows a cancellation, the service is still online, and a compiler process has `PPID=1`, read [Job cancellation and subprocess cleanup](runner-troubleshooting.md#note-cancellation). `PPID=1` means the parent is gone. Cancelling one job does not stop the long-running service, so that cancellation does not sweep the unit through the service's `KillMode=control-group`.

<a id="windows-job-object"></a>

## Windows Job Object

In Windows managed mode, the supervisor starts a bootstrap child that waits for release, assigns it to a [Windows Job Object](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) with a CPU limit, a memory ceiling, and `KILL_ON_JOB_CLOSE`, and releases the real command only after both the assignment and the limits succeed. If either step fails, the supervisor refuses to start and does not fall back to an unconfined run. After a normal exit, a cancellation, or a timeout, the supervisor closes the Job handle and the kernel reclaims the processes in the job. Per-module timeouts stay with the user-space watcher. Enabling the mode and the filesystem requirements are in the [Windows section of the build guide](build.md#windows). `qatlib-env exec` is available on Linux hosts that have the required cgroup controllers. How to correlate process identity after a cancellation is in the [same section of the troubleshooting page](runner-troubleshooting.md#windows-job-object).

<a id="runner"></a>

## Deployment order

1. `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`, and you have written down which repositories this machine may run.
2. Read `setup_runner.sh --help` from the pinned source.
3. As root, run `init` and confirm the lane account `qatlas-ci-<lane>` and `qatlas-ci.slice`.
4. As root, run `register <lane> <instance>` with labels and that instance's CPU and memory limits.
5. Read back `User`, `WorkingDirectory`, `Slice`, and `EffectiveMemoryMax` with `systemctl show`.
6. As that lane's service account, with the instance `.env` loaded, run `qatlib-env config explain --json` and `qatlib-env pool status`.
7. Finish a normal exit, a cancellation, a per-module timeout, and a whole-stage timeout. Anything left over goes to [troubleshooting](runner-troubleshooting.md).
