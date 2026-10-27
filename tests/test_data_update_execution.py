from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from lake_equity import pipeline
from lake_equity.jobs import JobManager, JobTerminationError


def test_update_forwards_current_run_progress_and_ignores_old_log(tmp_path):
    log = tmp_path / "var/logs/cne-daily.log"
    log.parent.mkdir(parents=True)
    log.write_text("Step old_dataset starting\n", encoding="utf-8")
    source = (
        "import time; print('Step daily_bars starting',flush=True); "
        "print('daily_bars 2/53 batches (100/100 symbols failed)',flush=True); "
        "time.sleep(1.5)"
    )
    updates = []
    code, text = pipeline._execute_update(
        [sys.executable, "-u", "-c", source], tmp_path, log, updates.append, None
    )
    assert code == 0
    assert "old_dataset" not in text
    batch = next(value for value in updates if value.get("total") == 53)
    assert batch["current"] == 2
    assert batch["progress_scope"] == "current_stage"
    assert "daily_bars" in batch["message"]


@pytest.mark.skipif(os.name != "nt", reason="Windows venv launcher regression")
def test_cancel_stops_the_owned_launcher_and_its_descendants(tmp_path):
    psutil = pytest.importorskip("psutil")
    log = tmp_path / "child.log"
    source = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
        "print('child='+str(child.pid),flush=True);time.sleep(60)"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", source],
        stdout=log.open("w", encoding="utf-8"),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    children = []
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if log.exists() and "child=" in log.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        child_pid = int(log.read_text(encoding="utf-8").split("child=")[1].splitlines()[0])
        children = psutil.Process(process.pid).children(recursive=True)
        assert any(child.pid == child_pid for child in children)
        pipeline._terminate(process)
        _, alive = psutil.wait_procs(children, timeout=3)
        assert not alive, f"Cancellation left running descendants: {[p.pid for p in alive]}"
        assert process.poll() is not None
    finally:
        for child in children:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_progress_callback_failure_also_stops_collector(tmp_path, monkeypatch):
    events = []

    class Process:
        pid = 9000001

        def poll(self):
            return None

    monkeypatch.setattr(pipeline.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(pipeline, "_terminate", lambda child: events.append(child.pid))

    def broken_progress(value):
        raise RuntimeError("synthetic persistence failure")

    with pytest.raises(RuntimeError, match="persistence failure"):
        pipeline._execute_update(
            ["synthetic"], tmp_path, tmp_path / "collector.log", broken_progress, None
        )
    assert events == [9000001]


@pytest.mark.skipif(os.name != "nt", reason="Windows taskkill failure")
@pytest.mark.parametrize("launcher_exits", [False, True])
def test_failed_tree_termination_is_reported_instead_of_claiming_cancelled(monkeypatch, launcher_exits):
    killed = []

    class Process:
        pid = 9000001

        def poll(self):
            return 1 if killed and launcher_exits else None

    def taskkill(*args, **kwargs):
        killed.append(True)
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(
        pipeline.subprocess, "run", taskkill
    )
    with pytest.raises(JobTerminationError, match="无法确认"):
        pipeline._terminate(Process())


def test_failed_termination_remains_failed_in_job_history(tmp_path):
    import threading

    started = threading.Event()
    release = threading.Event()

    def dispatch(kind, params, progress, cancelled):
        started.set()
        release.wait(5)
        raise JobTerminationError("无法确认采集进程树已停止")

    manager = JobManager(tmp_path, dispatch)
    try:
        job = manager.submit("update", {})
        assert started.wait(3)
        manager.cancel(job["id"])
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = manager.get(job["id"])
            if result["status"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.01)
        assert result["status"] == "failed"
        assert result["cancel_effective"] is False
        assert "无法确认" in result["cancel_note"]
    finally:
        release.set()
        manager.close()
