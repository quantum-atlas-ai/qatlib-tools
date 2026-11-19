"""Render per-instance runner settings without touching running services."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re

from qatlib_env.envvars import legacy_name
from qatlib_env.resources import memory_bytes


def render(base: Path, systemd: Path, lane: str, instance: str, *, values=None,
           cpu_quota="infinity", memory_high="infinity", memory_max="infinity") -> dict:
    if lane not in {"pub", "dev", "secondary"} or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", instance):
        raise ValueError("invalid lane or instance")
    if not re.fullmatch(r"(?:infinity|[0-9]+(?:\.[0-9]+)?%)", cpu_quota):
        raise ValueError("CPU quota must be infinity or a percentage")
    for value in (memory_high, memory_max):
        if not re.fullmatch(r"(?:infinity|[0-9]+(?:\.[0-9]+)?[%KMGTPE]?)", value):
            raise ValueError("memory limits must be infinity, a percentage or a systemd memory size")
    values = dict(values or {})
    for key, value in values.items():
        if key == "memory_budget":
            values[key] = memory_bytes(value)
        elif key in {"jobs", "lean_threads", "heavy_jobs"}:
            if isinstance(value, bool) or not str(value).isdigit():
                raise ValueError(f"{key} must be a positive integer")
            values[key] = int(value)
            if values[key] < 1:
                raise ValueError(f"{key} must be positive")
        elif key in {"module_timeout", "stage_timeout"}:
            if isinstance(value, bool):
                raise ValueError(f"{key} must be a nonnegative number")
            values[key] = float(value)
            if not math.isfinite(values[key]) or values[key] < 0:
                raise ValueError(f"{key} must be nonnegative")
        else:
            raise ValueError(f"unknown runner setting: {key}")
    root = base / lane
    directory = root / "runners" / instance
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = directory / "resources.toml"
    config.write_text("[resources]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in sorted(values.items())))
    config.chmod(0o600)
    common = root / "runners/.env"
    environment = common.read_text() if common.is_file() else ""
    environment = "\n".join(line for line in environment.splitlines()
                            if not line.startswith(("LEAN_NUM_THREADS=", "QATLIB_RUNNER_CONFIG=",
                                                    legacy_name("QATLIB_RUNNER_CONFIG") + "=")))
    environment += f"\nQATLIB_RUNNER_CONFIG={config}\n"
    path = directory / ".env"
    path.write_text(environment)
    path.chmod(0o600)
    dropin = systemd / f"qatlas-{lane}-runner@{instance}.service.d/resources.conf"
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text(f"[Service]\nCPUQuota={cpu_quota}\nMemoryHigh={memory_high}\nMemoryMax={memory_max}\n")
    return {"environment": str(path), "config": str(config), "systemd": str(dropin)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, type=Path)
    parser.add_argument("--systemd-dir", required=True, type=Path)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--instance", required=True)
    for key in ("jobs", "lean_threads", "memory_budget", "heavy_jobs", "module_timeout", "stage_timeout"):
        parser.add_argument("--" + key.replace("_", "-"))
    parser.add_argument("--cpu-quota", default="infinity")
    parser.add_argument("--memory-high", default="infinity")
    parser.add_argument("--memory-max", default="infinity")
    args = parser.parse_args(argv)
    keys = ("jobs", "lean_threads", "memory_budget", "heavy_jobs", "module_timeout", "stage_timeout")
    print(json.dumps(render(args.base, args.systemd_dir, args.lane, args.instance,
                            values={key: getattr(args, key) for key in keys if getattr(args, key) is not None},
                            cpu_quota=args.cpu_quota, memory_high=args.memory_high, memory_max=args.memory_max)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

