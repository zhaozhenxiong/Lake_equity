"""The no-network guard, including the half that lives outside this process.

`-m 'not network'` skips the tests that declare a dependency on the network.
Nothing stopped one that reaches it by accident, and eight did — seven still
passed, because the adapter fell back, so the only symptom was time. The guard
turns that into one named failure.

`worker_pool` runs on `ProcessPoolExecutor`. The start method is `spawn` on
Windows and macOS, and `forkserver` on Linux 3.14+: a fresh interpreter that
inherits no patching. Half a guard is worse than none, because the parent
looks clean while the work happens elsewhere.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest


def _child_reaches_out() -> str:
    """Run in a pool worker: report whether the guard reached this process."""
    import socket as child_socket

    try:
        child_socket.create_connection(("push2.eastmoney.com", 443), timeout=5)
        return "connected"
    except AssertionError:
        return "blocked"
    except Exception as exc:  # noqa: BLE001 — any other failure is not the answer
        return f"inconclusive:{type(exc).__name__}"


def _child_reaches_loopback(port: int) -> str:
    import socket as child_socket

    conn = child_socket.socket()
    try:
        conn.connect(("127.0.0.1", port))
        return "ok"
    except AssertionError:
        return "blocked"
    finally:
        conn.close()


@contextmanager
def _loopback_listener():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(0.1)
    stopping = threading.Event()

    def accept():
        while not stopping.is_set():
            try:
                conn, _ = server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.close()
            return

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        yield server, server.getsockname()[1]
    finally:
        stopping.set()
        thread.join(timeout=1)
        server.close()


@contextmanager
def _unix_listener():
    """A local AF_UNIX server on a short path.

    pytest's tmp_path under /var/folders already exceeds macOS's ~104-byte
    AF_UNIX limit. Multiprocessing itself uses `/tmp/pymp-*/sock-*`.
    """
    if not getattr(socket, "AF_UNIX", None):
        pytest.skip("AF_UNIX not available")
    bases: list[str] = []
    if Path("/tmp").is_dir():
        bases.append("/tmp")
    tmp = tempfile.gettempdir()
    if tmp not in bases:
        bases.append(tmp)
    last_exc: OSError | None = None
    for base in bases:
        directory = tempfile.mkdtemp(prefix="cnes", dir=base)
        path = str(Path(directory) / "s")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
        except OSError as exc:
            last_exc = exc
            server.close()
            shutil.rmtree(directory, ignore_errors=True)
            continue
        server.listen(1)
        try:
            yield server, path
        finally:
            server.close()
            shutil.rmtree(directory, ignore_errors=True)
        return
    pytest.skip(f"AF_UNIX bind failed: {last_exc}")


def test_this_process_cannot_reach_an_external_host():
    with pytest.raises(AssertionError, match="outbound connection"):
        socket.create_connection(("push2.eastmoney.com", 443), timeout=5)


def test_this_process_can_still_use_loopback():
    """`cne serve` and `cne mcp` are exercised over a real local socket."""
    with _loopback_listener() as (_, port):
        conn = socket.socket()
        conn.connect(("127.0.0.1", port))
        conn.close()


def test_this_process_can_still_use_a_unix_socket():
    """Linux 3.14's forkserver talks to `/tmp/pymp-*/sock-*`; that is not outbound."""
    with _unix_listener() as (_, path):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.connect(path)
        conn.close()


def test_a_pool_worker_cannot_reach_an_external_host():
    with ProcessPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_child_reaches_out).result(timeout=30) == "blocked"


def test_a_pool_worker_can_still_use_loopback():
    with _loopback_listener() as (_, port):
        with ProcessPoolExecutor(max_workers=1) as pool:
            assert pool.submit(_child_reaches_loopback, port).result(timeout=30) == "ok"


def test_a_plain_python_subprocess_is_covered_too():
    """`sitecustomize` runs before user code, so `-c` scripts are guarded."""
    script = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('push2.eastmoney.com', 443), timeout=5)\n"
        "    print('connected')\n"
        "except AssertionError:\n"
        "    print('blocked')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert done.stdout.strip() == "blocked", done


def test_a_plain_python_subprocess_can_still_use_a_unix_socket():
    """The sitecustomize half must allow the same local IPC the parent does."""
    with _unix_listener() as (_, path):
        script = (
            "import socket\n"
            "conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            f"conn.connect({path!r})\n"
            "conn.close()\n"
            "print('ok')\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        assert done.stdout.strip() == "ok", done


@pytest.mark.network
def test_the_marker_means_the_same_thing_in_a_subprocess():
    """Otherwise the marker would let the test out and trap its pool workers."""
    with ProcessPoolExecutor(max_workers=1) as pool:
        assert pool.submit(_child_reaches_out).result(timeout=30) != "blocked"
