from __future__ import annotations

import os
import shutil
import sys


def macos_limits(command: list[str], *, qos: str = "utility") -> list[str]:
    if sys.platform != "darwin" or shutil.which("taskpolicy") is None:
        return list(command)
    return ["taskpolicy", "-b", "-p", qos, "--", *command]

