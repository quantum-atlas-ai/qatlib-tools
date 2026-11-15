from __future__ import annotations

import os
import sys


class JobObject:
    """Small stdlib ctypes hook for Windows process-tree limits.

    The object is deliberately inert off Windows; callers can attach the
    current process and close the handle on supported hosts.
    """
    def __init__(self, name: str = "qatlib-build"):
        self.name, self.handle = name, None
        self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        self.JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
        self.JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
        self.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000080
        if sys.platform == "win32":
            import ctypes
            kernel = ctypes.windll.kernel32
            self.handle = kernel.CreateJobObjectW(None, name)

    def configure(self, *, memory_bytes: int, cpu_percent: int = 100) -> None:
        if sys.platform != "win32" or not self.handle:
            return
        import ctypes
        kernel = ctypes.windll.kernel32
        class Limit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", ctypes.c_byte * 40), ("IoInfo", ctypes.c_byte * 48),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        limit = Limit(); limit.ProcessMemoryLimit = memory_bytes; limit.JobMemoryLimit = memory_bytes
        kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limit), ctypes.sizeof(limit))

        class CpuRate(ctypes.Structure):
            _fields_ = [("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32)]
        rate = CpuRate(self.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE, max(1, min(10000, cpu_percent * 100)))
        kernel.SetInformationJobObject(self.handle, 15, ctypes.byref(rate), ctypes.sizeof(rate))

    def attach_pid(self, pid: int) -> bool:
        if sys.platform != "win32" or not self.handle:
            return False
        import ctypes
        kernel = ctypes.windll.kernel32
        process = kernel.OpenProcess(0x1F0FFF, False, int(pid))
        if not process: return False
        try: return bool(kernel.AssignProcessToJobObject(self.handle, process))
        finally: kernel.CloseHandle(process)

    def close(self) -> None:
        if self.handle and sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def windows_limits(command: list[str], *, memory_mib: int, cpu_percent: int = 100) -> list[str]:
    """Return the command and expose the Job Object contract to the caller.

    Job Objects are attached by the runner on Windows via stdlib ctypes; the
    command remains unchanged so this module is importable on every platform.
    """
    return list(command)

