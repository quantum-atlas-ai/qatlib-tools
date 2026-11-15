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
        self.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x00000001
        self.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x00000004
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.windll.kernel32
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.SetInformationJobObject.restype = wintypes.BOOL
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.handle = kernel.CreateJobObjectW(None, name)
            if not self.handle:
                raise OSError("CreateJobObjectW failed")

    def configure(self, *, memory_bytes: int, cpu_percent: int = 100) -> None:
        if sys.platform != "win32":
            return
        if not self.handle:
            raise OSError("Job Object is unavailable")
        import ctypes
        kernel = ctypes.windll.kernel32
        class BasicLimit(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]
        class Limit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimit), ("IoInfo", ctypes.c_uint64 * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        limit = Limit(); limit.ProcessMemoryLimit = memory_bytes; limit.JobMemoryLimit = memory_bytes
        limit.BasicLimitInformation.LimitFlags = (self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
            self.JOB_OBJECT_LIMIT_PROCESS_MEMORY | self.JOB_OBJECT_LIMIT_JOB_MEMORY)
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limit), ctypes.sizeof(limit)):
            raise OSError("Job Object memory limit setup failed")

        class CpuRate(ctypes.Structure):
            _fields_ = [("ControlFlags", ctypes.c_uint32), ("CpuRate", ctypes.c_uint32)]
        rate = CpuRate(self.JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | self.JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
                       max(1, min(10000, int(cpu_percent * 100))))
        if not kernel.SetInformationJobObject(self.handle, 15, ctypes.byref(rate), ctypes.sizeof(rate)):
            raise OSError("Job Object CPU hard-cap setup failed")

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

