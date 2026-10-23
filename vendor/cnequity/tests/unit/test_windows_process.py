"""Process identity and conservative lock checks, plus native Windows probes."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

import pytest

from cnequity import windows_process

CREATED = (1 << 56) + 123


class Kernel:
    def __init__(self, *, handle=1 << 40, wait=258, readable=True):
        self.handle = handle
        self.wait = wait
        self.readable = readable
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        return self.handle

    def GetProcessTimes(self, handle, created, exited, kernel_time, user_time):
        value = ctypes.cast(created, ctypes.POINTER(wintypes.FILETIME)).contents
        value.dwHighDateTime = CREATED >> 32
        value.dwLowDateTime = CREATED & 0xFFFFFFFF
        return self.readable

    def WaitForSingleObject(self, handle, timeout):
        assert handle == self.handle
        assert timeout == 0
        return self.wait

    def CloseHandle(self, handle):
        self.closed.append(handle)


def test_creation_time_preserves_large_filetime_and_closes_handle(monkeypatch):
    kernel = Kernel()
    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel)
    assert windows_process.creation_time(123) == str(CREATED)
    assert kernel.closed == [kernel.handle]


@pytest.mark.parametrize(
    "expected,wait,readable,matched",
    [
        (str(CREATED), 258, True, True),
        (str(CREATED + 1), 258, True, False),
        (None, 258, True, False),
        (str(CREATED), 0, True, False),
        (str(CREATED), 0xFFFFFFFF, True, False),
        (str(CREATED), 258, False, False),
    ],
)
def test_verified_identity_rejects_reused_unknown_and_exited_processes(
    monkeypatch, expected, wait, readable, matched
):
    kernel = Kernel(wait=wait, readable=readable)
    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel)
    with windows_process.verified_process(123, expected) as result:
        assert result is matched
        assert kernel.closed == []
    assert kernel.closed == ([] if expected is None else [kernel.handle])


@pytest.mark.parametrize("error,alive", [(87, False), (5, True), (999, True)])
def test_missing_pid_is_reclaimable_but_access_denied_is_not(monkeypatch, error, alive):
    kernel = Kernel(handle=None)
    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    assert windows_process.pid_alive(123) is alive
    assert windows_process.creation_time(123) is None
    with windows_process.verified_process(123, str(CREATED)) as matched:
        assert not matched
    assert kernel.closed == []


@pytest.mark.parametrize("wait,alive", [(0, False), (258, True), (0xFFFFFFFF, True)])
def test_liveness_uses_the_process_object_not_exit_code(monkeypatch, wait, alive):
    kernel = Kernel(wait=wait)
    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel)
    assert windows_process.pid_alive(123) is alive
    assert kernel.closed == [kernel.handle]


def test_creation_query_failure_still_closes_handle(monkeypatch):
    kernel = Kernel(readable=False)
    monkeypatch.setattr(windows_process, "_kernel32", lambda: kernel)
    assert windows_process.creation_time(123) is None
    assert kernel.closed == [kernel.handle]


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows process identity")
def test_native_creation_time_identifies_the_current_process():
    pid = os.getpid()
    created = windows_process.creation_time(pid)
    assert created is not None
    assert windows_process.pid_alive(pid)
    with windows_process.verified_process(pid, created) as matched:
        assert matched
    with windows_process.verified_process(pid, str(int(created) + 1)) as matched:
        assert not matched


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows process wait")
def test_native_process_exiting_with_259_is_not_alive():
    with subprocess.Popen([sys.executable, "-c", "import os; os._exit(259)"]) as child:
        child.wait(timeout=10)
        # Popen still owns a handle, so the exited process object remains queryable.
        assert windows_process.creation_time(child.pid) is not None
        assert not windows_process.pid_alive(child.pid)
