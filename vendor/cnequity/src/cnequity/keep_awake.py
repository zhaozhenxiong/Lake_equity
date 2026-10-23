"""Hold off idle system sleep while a long command runs.

A whole-market `cne init` runs for an hour or more, usually on a laptop left
alone with it. Open sockets do not count as activity to macOS or Windows, so
the machine idles to sleep partway through, the TDX connections die with it,
and the build looks hung or fails on wake. Only *idle* sleep is held: closing
the lid or choosing Sleep still sleeps, and the hold ends with the process.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator

logger = logging.getLogger(__name__)

#: Set to any non-empty value to let the machine sleep as usual.
ALLOW_SLEEP_ENV = "CNE_ALLOW_SLEEP"

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake() -> Iterator[bool]:
    """Yield whether idle sleep is held off for the duration of the block."""
    if os.environ.get(ALLOW_SLEEP_ENV):
        yield False
        return
    if sys.platform == "darwin":
        with _caffeinate() as held:
            yield held
    elif sys.platform == "win32":
        with _execution_state() as held:
            yield held
    else:
        # Linux hosts running a long init are servers that do not idle-sleep,
        # and `systemd-inhibit` can only wrap a process, not join one.
        yield False


@contextlib.contextmanager
def _caffeinate() -> Iterator[bool]:
    binary = shutil.which("caffeinate")
    if binary is None:
        yield False
        return
    try:
        # `-w` ties the assertion to this process, so a crash or SIGKILL here
        # cannot leave the machine held awake.
        proc = subprocess.Popen(
            [binary, "-i", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.debug("caffeinate unavailable: %s", exc)
        yield False
        return
    try:
        yield True
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


@contextlib.contextmanager
def _execution_state() -> Iterator[bool]:
    try:
        import ctypes

        set_state = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
    except (ImportError, AttributeError, OSError):
        yield False
        return
    if not set_state(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED):
        yield False
        return
    try:
        yield True
    finally:
        set_state(_ES_CONTINUOUS)
