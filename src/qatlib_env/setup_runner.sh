#!/usr/bin/env bash
# Provision isolated Linux runner lanes from QATLAS_LANES; register each runner instance separately.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  setup_runner.sh [init]
  setup_runner.sh register <lane> <instance> --url <URL>
      [--token TOKEN] [--labels LABELS] [--forge auto|github|gitea]
      [--jobs N] [--lean-threads N] [--memory-budget SIZE] [--heavy-jobs N]
      [--module-timeout SECONDS] [--stage-timeout SECONDS]
      [--cpu-quota PERCENT] [--memory-high SIZE] [--memory-max SIZE]

init uses QATLAS_HOST_MEMORY_HIGH (90%), QATLAS_HOST_MEMORY_MAX (95%) and
QATLAS_HOST_CPU_QUOTA (infinity) for the shared qatlas-ci.slice.
EOF
}

command=${1:-init}
case "$command" in init|register|--help|-h) ;; *) usage >&2; exit 1;; esac
if [ "$command" = "--help" ] || [ "$command" = "-h" ]; then usage; exit 0; fi

if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this script as root.' >&2
  exit 1
fi

base=${QATLAS_CI_ROOT:-/data/qatlas-ci}
systemd_dir=${QATLAS_SYSTEMD_DIR:-/etc/systemd/system}
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

valid_lane() { case "$1" in pub|dev|secondary) return 0;; *) return 1;; esac; }
valid_instance() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]]; }

host_packages=(build-essential ca-certificates curl git python3 python3-venv systemd tar zstd)
host_commands=(cc curl git install python3 runuser systemctl tar useradd zstd)

ensure_host_prerequisites() {
  local missing_packages=() package tool_name
  command -v apt-get >/dev/null 2>&1 || {
    echo 'Ubuntu/Debian host prerequisite check requires apt-get.' >&2
    return 1
  }
  command -v dpkg-query >/dev/null 2>&1 || {
    echo 'Ubuntu/Debian host prerequisite check requires dpkg-query.' >&2
    return 1
  }
  for package in "${host_packages[@]}"; do
    if [ "$(dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null || true)" != installed ]; then
      missing_packages+=("$package")
    fi
  done
  if [ "${#missing_packages[@]}" -gt 0 ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing_packages[@]}"
  fi
  for tool_name in "${host_commands[@]}"; do
    command -v "$tool_name" >/dev/null 2>&1 || {
      echo "Host prerequisite is unavailable after installation: $tool_name" >&2
      return 1
    }
  done
  if ! printf '%s\n' '#include <stdio.h>' '#include <stdlib.h>' '#include <stdint.h>' |
    cc -x c -fsyntax-only - >/dev/null 2>&1; then
    echo 'Host C standard headers are unavailable; install build-essential (required by Lake buildO).' >&2
    return 1
  fi
}

register_github() {
  local lane=$1 instance=$2 url=$3 token=$4 labels=$5
  local account="qatlas-ci-$lane" root="$base/$lane"
  local github_proxy=${QATLAS_GITHUB_PROXY:-}
  if [ -z "$github_proxy" ] && [ -f "$root/.gitconfig" ]; then
    github_proxy=$(runuser -u "$account" -- env HOME="$root" git config --global --get http.https://github.com.proxy 2>/dev/null || true)
  fi
  if [ -z "$token" ]; then
    [[ "$url" =~ github\.com/([^/]+)/([^/]+) ]] || { echo 'Cannot derive GitHub owner/repo from --url; pass --token.' >&2; exit 1; }
    local owner=${BASH_REMATCH[1]} repo=${BASH_REMATCH[2]}; repo=${repo%%/*}; repo=${repo%.git}
    local api_cmd="gh api -X POST repos/${owner}/${repo}/actions/runners/registration-token --jq .token"
    if command -v gh >/dev/null 2>&1; then
      if [ -n "${SUDO_USER:-}" ]; then
        token=$(runuser -u "$SUDO_USER" -- gh api -X POST "repos/${owner}/${repo}/actions/runners/registration-token" --jq .token 2>/dev/null) || true
      fi
      if [ -z "$token" ]; then token=$(gh api -X POST "repos/${owner}/${repo}/actions/runners/registration-token" --jq .token 2>/dev/null) || true; fi
    fi
    if [ -z "$token" ]; then
      echo "GitHub registration token unavailable. Authenticate gh or pass --token." >&2
      echo "Exact command: $api_cmd" >&2
      echo "Then rerun with --token <REGISTRATION_TOKEN>." >&2
      exit 1
    fi
  fi
  local version=${QATLAS_GITHUB_RUNNER_VERSION:-2.329.0}
  local runner_dir="$root/runners/$instance" archive="$root/tmp/actions-runner-linux-x64-${version}.tar.gz"
  install -d -m 0700 -o "$account" -g "$account" "$runner_dir"
  if [ ! -x "$runner_dir/config.sh" ]; then
    local download_url="https://github.com/actions/runner/releases/download/v${version}/actions-runner-linux-x64-${version}.tar.gz"
    if [ -n "$github_proxy" ]; then HTTPS_PROXY="$github_proxy" curl --fail --location "$download_url" -o "$archive"; else curl --fail --location "$download_url" -o "$archive"; fi
    tar -xzf "$archive" -C "$runner_dir"
    chown -R "$account:$account" "$runner_dir"
  fi
  runuser -u "$account" -- env RUNNER_URL="$url" RUNNER_TOKEN="$token" RUNNER_NAME="qatlas-${lane}-${instance}" RUNNER_LABELS="${labels:-self-hosted,linux,x64}" RUNNER_DIR="$runner_dir" bash -c 'cd "$RUNNER_DIR" && ./config.sh --unattended --url "$RUNNER_URL" --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" --labels "$RUNNER_LABELS" --work _work --replace'
  printf '#!/usr/bin/env bash\nexec "$(dirname "$0")/run.sh" "$@"\n' > "$runner_dir/run"
  chmod 0755 "$runner_dir/run"
  systemctl daemon-reload
  systemctl enable --now "qatlas-${lane}-runner@${instance}.service"
}

register_gitea() {
  local lane=$1 instance=$2 url=$3 token=$4 labels=$5
  local account="qatlas-ci-$lane" root="$base/$lane"
  if [ -z "$token" ]; then
    echo 'Gitea requires --token. Get it from the Gitea Actions/Runners settings or gt, then pass --token <TOKEN>.' >&2
    exit 1
  fi
  local version=${QATLAS_ACT_RUNNER_VERSION:-0.2.11}
  local runner_dir="$root/runners/$instance" binary="$root/runners/$instance/act_runner"
  install -d -m 0700 -o "$account" -g "$account" "$runner_dir"
  if [ ! -x "$binary" ]; then
    local download_url="https://gitea.com/gitea/act_runner/releases/download/v${version}/act_runner-${version}-linux-amd64"
    curl --fail --location "$download_url" -o "$binary"
    chmod 0755 "$binary"
    chown "$account:$account" "$binary"
  fi
  local raw_labels="${labels:-ubuntu-latest:host,self-hosted:host}"
  local normalized_labels="" item
  IFS=',' read -ra _gitea_label_items <<< "$raw_labels"
  for item in "${_gitea_label_items[@]}"; do
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    [ -z "$item" ] && continue
    [[ "$item" == *:* ]] || item="${item}:host"
    normalized_labels="${normalized_labels:+${normalized_labels},}${item}"
  done
  runuser -u "$account" -- env RUNNER_URL="$url" RUNNER_TOKEN="$token" RUNNER_NAME="qatlas-${lane}-${instance}" RUNNER_LABELS="$normalized_labels" RUNNER_DIR="$runner_dir" bash -c 'cd "$RUNNER_DIR" && ./act_runner register --no-interactive --instance "$RUNNER_URL" --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" --labels "$RUNNER_LABELS"'
  printf '#!/usr/bin/env bash\ncd "$(dirname "$0")"\nexec ./act_runner daemon "$@"\n' > "$runner_dir/run"
  chmod 0755 "$runner_dir/run"
  systemctl daemon-reload
  systemctl enable --now "qatlas-${lane}-runner@${instance}.service"
}

register() {
  local lane=${1:-} instance=${2:-} url='' token='' labels='' forge=auto option value
  local resource_args=()
  valid_lane "$lane" || { echo "Invalid lane: $lane" >&2; exit 1; }
  valid_instance "$instance" || { echo "Invalid instance: $instance" >&2; exit 1; }
  shift 2
  while [ "$#" -gt 0 ]; do
    option=$1; shift
    case "$option" in
      --url|--token|--labels|--forge)
        [ "$#" -gt 0 ] || { echo "$option needs a value" >&2; exit 1; }
        value=$1; shift
        case "$option" in --url) url=$value;; --token) token=$value;; --labels) labels=$value;; --forge) forge=$value;; esac
        ;;
      --jobs|--lean-threads|--memory-budget|--heavy-jobs|--module-timeout|--stage-timeout|--cpu-quota|--memory-high|--memory-max)
        [ "$#" -gt 0 ] || { echo "$option needs a value" >&2; exit 1; }
        resource_args+=("$option" "$1"); shift
        ;;
      --help|-h) usage; exit 0;;
      *) echo "Unknown register option: $option" >&2; exit 1;;
    esac
  done
  [ -n "$url" ] || { echo 'register requires --url' >&2; exit 1; }
  case "$forge" in
    auto) [[ "$url" == *github.com* ]] && forge=github || forge=gitea;;
    github|gitea) ;;
    *) echo 'Forge must be auto, github, or gitea.' >&2; exit 1;;
  esac
  [ -d "$base/$lane/runners" ] || { echo "Lane $lane is not provisioned; run setup_runner.sh init first." >&2; exit 1; }
  ensure_host_prerequisites
  PYTHONPATH="$script_dir/..${PYTHONPATH:+:$PYTHONPATH}" python3 -B -m qatlib_env.runner_config \
    --base "$base" --systemd-dir "$systemd_dir" --lane "$lane" --instance "$instance" "${resource_args[@]}"
  chown "qatlas-ci-$lane:qatlas-ci-$lane" "$base/$lane/runners/$instance/.env" "$base/$lane/runners/$instance/resources.toml"
  if [ "$forge" = github ]; then register_github "$lane" "$instance" "$url" "$token" "$labels"; else register_gitea "$lane" "$instance" "$url" "$token" "$labels"; fi
}

if [ "$command" = "register" ]; then
  shift
  register "$@"
  exit 0
fi
ensure_host_prerequisites

base=${QATLAS_CI_ROOT:-/data/qatlas-ci}
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Soft defaults keep several lanes responsive on one host; every budget is tunable.
cpu_weight=${QATLAS_CPU_WEIGHT:-200}
cpu_quota=${QATLAS_CPU_QUOTA:-infinity}
nice_level=${QATLAS_NICE:-10}
io_class=${QATLAS_IO_CLASS:-best-effort}
io_priority=${QATLAS_IO_PRIORITY:-7}
mem_high=${QATLAS_RUNNER_MEM_HIGH:-infinity}
mem_max=${QATLAS_RUNNER_MEM_MAX:-infinity}
tasks_max=${QATLAS_TASKS_MAX:-512}
install -d -m 0755 "$base" "$systemd_dir"

lane_list() {
  local raw=${QATLAS_LANES:-"pub dev secondary"} lane
  raw=${raw//,/ }
  for lane in $raw; do
    case "$lane" in pub|dev|secondary) printf '%s\n' "$lane";; *) echo "Invalid lane: $lane" >&2; exit 1;; esac
  done
}

if [ "$command" = "init" ]; then
cat > "$systemd_dir/qatlas-ci.slice" <<EOF
[Unit]
Description=Shared QAtlas CI host resource budget

[Slice]
CPUAccounting=true
MemoryAccounting=true
CPUQuota=${QATLAS_HOST_CPU_QUOTA:-infinity}
MemoryHigh=${QATLAS_HOST_MEMORY_HIGH:-90%}
MemoryMax=${QATLAS_HOST_MEMORY_MAX:-95%}
EOF
while read -r lane; do
  account="qatlas-ci-$lane"
  root="$base/$lane"
  if ! id "$account" >/dev/null 2>&1; then
    useradd --system --user-group --create-home --home-dir "$root" --shell /bin/bash "$account"
  fi
  install -d -m 0700 -o "$account" -g "$account" "$root"
  for directory in elan venv node lake-cache xdg-cache pkg-mirror runners tmp bin; do
    install -d -m 0700 -o "$account" -g "$account" "$root/$directory"
  done
  if [ ! -x "$root/venv/bin/pip" ]; then
    runuser -u "$account" -- python3 -m venv --clear "$root/venv"
  fi
  runuser -u "$account" -- "$root/venv/bin/python3" -m pip install --quiet 'jsonschema>=4.18,<5' hatchling
  # Complete pkg-mirror clones still accept Lake's --filter=tree:0 fetches;
  # the materialized package trees themselves must never be shallow or partial.
  runuser -u "$account" -- env HOME="$root" git config --global uploadpack.allowFilter true
  if [ -n "${QATLAS_GITHUB_PROXY:-}" ]; then
    runuser -u "$account" -- env HOME="$root" git config --global http.https://github.com.proxy "$QATLAS_GITHUB_PROXY"
    runuser -u "$account" -- env HOME="$root" git config --global http.https://raw.githubusercontent.com.proxy "$QATLAS_GITHUB_PROXY"
    if command -v nc >/dev/null 2>&1 && { [ "$lane" = pub ] || [ "$lane" = dev ]; }; then
      install -d -m 0700 -o "$account" -g "$account" "$root/.ssh"
      proxy=${QATLAS_GITHUB_PROXY#http://}; proxy=${proxy#https://}; proxy=${proxy%/}
      cat > "$root/.ssh/config" <<EOF
Host github.com
  HostName ssh.github.com
  Port 443
  User git
  ProxyCommand nc -X connect -x ${proxy} %h %p
EOF
      chown "$account:$account" "$root/.ssh/config"
      chmod 0600 "$root/.ssh/config"
    fi
  fi
  if [ -n "${QATLAS_GITHUB_MIRROR:-}" ]; then
    runuser -u "$account" -- env HOME="$root" git config --global "url.${QATLAS_GITHUB_MIRROR}.insteadOf" "https://github.com/"
  fi
  # Copy the single distribution first: the runner account may not be able to
  # traverse the maintainer's home. Maintenance uses the installed package CLI.
  package_source=$(mktemp -d "$root/tmp/qatlas-python-install.XXXXXX")
  install -d -m 0700 -o "$account" -g "$account" "$package_source"
  install -m 0600 -o "$account" -g "$account" "$script_dir/../../pyproject.toml" "$package_source/pyproject.toml"
  for package in qatlib_env qatlib_forge qatlib_search; do
    install -d -m 0700 -o "$account" -g "$account" "$package_source/$package"
    for package_file in "$script_dir/../$package"/*.py "$script_dir/../$package"/*.sh \
                        "$script_dir/../$package"/*.lean "$script_dir/../$package"/*.json \
                        "$script_dir/../$package"/*.txt "$script_dir/../$package"/README.md; do
      [ -f "$package_file" ] && install -m 0600 -o "$account" -g "$account" "$package_file" "$package_source/$package/"
    done
    if [ -d "$script_dir/../$package/docs" ]; then
      cp -R "$script_dir/../$package/docs" "$package_source/$package/"
      chown -R "$account:$account" "$package_source/$package/docs"
    fi
  done
  runuser -u "$account" -- "$root/venv/bin/python3" -m pip install --quiet \
    --no-deps --no-build-isolation "$package_source"
  if [ ! -x "$root/elan/bin/elan" ]; then
    installer="$root/tmp/elan-init.sh"
    github_proxy=${QATLAS_GITHUB_PROXY:-}
    if [ -z "$github_proxy" ] && [ -f "$root/.gitconfig" ]; then
      github_proxy=$(runuser -u "$account" -- env HOME="$root" git config --global --get http.https://raw.githubusercontent.com.proxy 2>/dev/null || true)
    fi
    if [ -n "$github_proxy" ]; then
      HTTPS_PROXY="$github_proxy" curl --fail --location --proto '=https' --tlsv1.2 \
        https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o "$installer"
    else
      curl --fail --location --proto '=https' --tlsv1.2 \
        https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -o "$installer"
    fi
    chown "$account:$account" "$installer"
    runuser -u "$account" -- env ELAN_HOME="$root/elan" sh "$installer" -y --default-toolchain none --no-modify-path
  fi
  default_toolchain=${QATLAS_DEFAULT_TOOLCHAIN:-leanprover/lean4:v4.35.0}
  runuser -u "$account" -- env ELAN_HOME="$root/elan" "$root/elan/bin/elan" toolchain install "$default_toolchain"
  if ! PATH="$root/node/bin:/usr/local/bin:/usr/bin:/bin" command -v node >/dev/null 2>&1; then
    node_version=${QATLAS_NODE_VERSION:-22.14.0}
    node_arch=$(uname -m)
    case "$node_arch" in x86_64) node_arch=x64;; aarch64|arm64) node_arch=arm64;; esac
    node_archive="$root/tmp/node-v${node_version}-linux-${node_arch}.tar.xz"
    curl --fail --location --proto '=https' --tlsv1.2 \
      "https://nodejs.org/dist/v${node_version}/node-v${node_version}-linux-${node_arch}.tar.xz" -o "$node_archive"
    tar -xJf "$node_archive" --strip-components=1 -C "$root/node"
    chown -R "$account:$account" "$root/node"
  fi
  cat > "$root/runners/.env" <<EOF
HOME=$root
USER=$account
LOGNAME=$account
ELAN_HOME=$root/elan
LAKE_CACHE_DIR=$root/lake-cache
MATHLIB_CACHE_DIR=$root/xdg-cache/mathlib
XDG_CACHE_HOME=$root/xdg-cache
QATLAS_PKG_MIRROR=$root/pkg-mirror
TMPDIR=$root/tmp
TMP=$root/tmp
TEMP=$root/tmp
RUNNER_MANUALLY_TRAP_SIG=1
PATH=$root/venv/bin:$root/elan/bin:$root/node/bin:/usr/local/bin:/usr/bin:/bin
EOF
  chown "$account:$account" "$root/runners/.env"
  chmod 0600 "$root/runners/.env"
  cat > "$systemd_dir/qatlas-${lane}-runner@.service" <<EOF
[Unit]
Description=QAtlas ${lane} CI runner (%i)
After=network-online.target
Wants=network-online.target

[Service]
User=${account}
Group=${account}
WorkingDirectory=${root}/runners/%i
EnvironmentFile=${root}/runners/%i/.env
ExecStart=${root}/runners/%i/run
Slice=qatlas-ci.slice
CPUAccounting=true
CPUWeight=${cpu_weight}
CPUQuota=${cpu_quota}
Nice=${nice_level}
IOSchedulingClass=${io_class}
IOSchedulingPriority=${io_priority}
MemoryHigh=${mem_high}
MemoryMax=${mem_max}
TasksMax=${tasks_max}
Restart=always
RestartSec=10
KillSignal=SIGINT
TimeoutStopSec=30s
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF
done < <(lane_list)

systemctl daemon-reload
while read -r lane; do
  account="qatlas-ci-$lane"
  root="$base/$lane"
  cat > "$systemd_dir/qatlas-${lane}-maint.service" <<EOF
[Unit]
Description=QAtlas ${lane} package mirror maintenance
After=network-online.target

[Service]
Type=oneshot
User=qatlas-ci-${lane}
Group=qatlas-ci-${lane}
EnvironmentFile=${base}/${lane}/runners/.env
ExecStart=${base}/${lane}/venv/bin/python3 -B -m qatlib_env.workspace maintain-mirror --gc-cache
Slice=qatlas-ci.slice
CPUAccounting=true
CPUWeight=${cpu_weight}
CPUQuota=${cpu_quota}
Nice=${nice_level}
IOSchedulingClass=${io_class}
IOSchedulingPriority=${io_priority}
MemoryHigh=${mem_high}
MemoryMax=${mem_max}
TasksMax=${tasks_max}
EOF
  cat > "$systemd_dir/qatlas-${lane}-maint.timer" <<EOF
[Unit]
Description=Daily QAtlas ${lane} maintenance

[Timer]
OnCalendar=daily
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl enable --now "qatlas-${lane}-maint.timer"
  runuser -u "$account" -- env HOME="$root" ELAN_HOME="$root/elan" QATLAS_DEFAULT_TOOLCHAIN="${QATLAS_DEFAULT_TOOLCHAIN:-leanprover/lean4:v4.35.0}" \
    "$root/venv/bin/python3" -B -m qatlib_env.workspace maintain-mirror --mirror "$root/pkg-mirror" ||
    echo "Warning: initial toolchain prewarm failed for lane $lane" >&2
done < <(lane_list)
echo "Lanes provisioned under $base. Register with: $0 register <lane> <instance> --url <URL>"
exit 0
fi

