"""Linux parent-death guardian; run as a separate, single-threaded process."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import signal
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qatlib_env import supervisor


def main():
    expected_parent, grace = int(sys.argv[1]), float(sys.argv[2])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0):  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "cannot arm parent-death guardian")
    # Close the fork/exec/prctl race before starting any workload.
    if os.getppid() != expected_parent:
        return 128 + signal.SIGTERM
    try:
        result = supervisor.run(sys.argv[3:], grace=grace, _guard_parent=False)
        if result.returncode < 0:
            # Preserve subprocess.run's negative signal return-code contract.
            signum = -result.returncode
            if signum != signal.SIGKILL:
                signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        return result.returncode
    except supervisor.ProcessCancelled as error:
        return error.returncode


if __name__ == "__main__":
    raise SystemExit(main())
