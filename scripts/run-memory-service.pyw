"""Windowless Scheduler entry point; wait for and supervise the foreground service."""

import ctypes
import json
import os
import subprocess
import sys
import traceback
from ctypes import wintypes
from pathlib import Path


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


def own_process_tree():
    """Scheduler terminates the actual wrapper; its last job handle kills descendants."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    # Assign before spawning anything. Windows 10/11 allow this nested job inside Scheduler.
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return job  # Keep this non-inheritable handle open until wrapper process exit.


def main():
    runtime = Path(__file__).resolve().parent
    with (runtime / "bootstrap.stderr.log").open("a", encoding="utf-8") as errors:
        try:
            deployment = json.loads((runtime / "deployment.json").read_text("utf-8"))
            job = own_process_tree()
            environment = {**os.environ, "POWERSHELL_UPDATECHECK": "Off"}
            result = subprocess.run(
                [
                    deployment["pwsh"],
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(runtime / "run-memory-service.ps1"),
                ],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=errors,
                env=environment,
                check=False,
            )
            assert job
            return result.returncode if 0 <= result.returncode <= 255 else 1
        except Exception:
            traceback.print_exc(file=errors)
            return 1


sys.exit(main())
