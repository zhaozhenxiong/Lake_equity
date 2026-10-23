"""Windows process identity without depending on shell output or its encoding.

Keep the verified handle open during termination: Windows cannot recycle a
process ID while a handle to that process object remains open.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from contextlib import contextmanager
from ctypes import wintypes
from functools import lru_cache

_QUERY_AND_SYNC = 0x1000 | 0x00100000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_ERROR_INVALID_PARAMETER = 87


@lru_cache(maxsize=1)
def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    return kernel


@contextmanager
def _open_process(pid: int):
    kernel = _kernel32()
    handle = kernel.OpenProcess(_QUERY_AND_SYNC, False, pid)
    try:
        yield kernel, handle
    finally:
        if handle:
            kernel.CloseHandle(handle)


def _creation_time(kernel, handle) -> str | None:
    created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None
    # FILETIME exceeds JavaScript's exact integer range; persist it as text.
    return str((created.dwHighDateTime << 32) | created.dwLowDateTime)


def creation_time(pid: int) -> str | None:
    with _open_process(pid) as (kernel, handle):
        return _creation_time(kernel, handle) if handle else None


def pid_alive(pid: int) -> bool:
    """Do not reclaim locks on access denied or an unknown wait result."""
    with _open_process(pid) as (kernel, handle):
        if not handle:
            return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
        # A process can exit with code 259 (STILL_ACTIVE). Wait on its handle
        # rather than interpreting that exit code as proof it is still alive.
        return kernel.WaitForSingleObject(handle, 0) != _WAIT_OBJECT_0


@contextmanager
def verified_process(pid: int, expected_creation_time: str | None) -> Iterator[bool]:
    """Yield a match while holding the exact process object open.

    Missing identity (including records from an older serve), a failed query,
    an exited process and a reused PID all fail closed for cancellation.
    """
    if not isinstance(expected_creation_time, str) or not expected_creation_time.isdigit():
        yield False
        return
    with _open_process(pid) as (kernel, handle):
        matched = bool(
            handle
            and _creation_time(kernel, handle) == expected_creation_time
            and kernel.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
        )
        yield matched
