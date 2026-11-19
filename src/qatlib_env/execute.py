"""Bounded Linux execution for experiments and workspace searches."""
from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import uuid

from qatlib_env import supervisor
from qatlib_env.backends.linux import linux_command, systemd_available
from qatlib_env.workspace import _pool_scope


def main(argv=None):
    parser = argparse.ArgumentParser(prog="qatlib-env exec", description=__doc__)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--memory-mib", type=int, default=4096)
    parser.add_argument("--cpu", type=int, default=1)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.args[1:] if args.args[:1] == ["--"] else args.args
    if not command or not math.isfinite(args.timeout) or args.timeout <= 0 or args.memory_mib < 2048 or args.cpu < 1:
        parser.error("supply a command, positive finite timeout/CPU, and memory >= 2048 MiB")
    if not sys.platform.startswith("linux") or not systemd_available():
        parser.error("exec requires Linux systemd user CPU/memory controllers; no unbounded fallback")
    unit = f"qatlib-exec-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    pool_env = {**os.environ, "QATLIB_MEMORY_BUDGET": str(args.memory_mib * 1024**2),
                "QATLIB_JOBS": str(args.cpu), "QATLIB_LEAN_THREADS": "1"}
    try:
        bounded = linux_command(command, memory_mib=args.memory_mib, cpu=args.cpu, unit=unit,
                                runtime_sec=args.timeout)
        with _pool_scope(pool_env, building=True):
            code = supervisor.run(bounded, timeout=args.timeout).returncode
            return code if code >= 0 else 128 - code
    except subprocess.TimeoutExpired:
        return 124
    except supervisor.ProcessCancelled as error:
        return error.returncode
    except (ValueError, OSError) as error:
        parser.exit(2, f"exec: {error}\n")
