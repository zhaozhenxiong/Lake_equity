"""Quarantine maintenance: read-only preview and explicitly confirmed cleanup."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import json
import sqlite3

from filelock import FileLock, Timeout
import pytest

from lake_equity.learning.quarantine import QuarantineService

BUILD = ".backtest-" + "a" * 32 + ".building"
SAVED = "backtest-" + "b" * 32


@pytest.fixture
def workspace(tmp_path):
    workflows = tmp_path / "var/learning/workflows"
    workflows.mkdir(parents=True)
    failed = workflows / BUILD
    failed.mkdir()
    (failed / "scores.parquet").write_bytes(b"partial")
    (failed / "prices.parquet").write_bytes(b"partial")
    saved = workflows / SAVED
    saved.mkdir()
    (saved / "report.json").write_text("{}", encoding="utf-8")
    (tmp_path / "var/logs").mkdir(parents=True, exist_ok=True)
    return tmp_path, workflows, failed


def test_preview_is_read_only_and_lists_only_build_directories(workspace):
    root, workflows, failed = workspace
    listing = QuarantineService(root).listing()
    assert listing["plan_id"].startswith("qplan-")
    assert [item["id"] for item in listing["items"]] == ["a" * 32]
    item = listing["items"][0]
    assert item["kind"] == "backtest"
    assert item["dir_name"] == BUILD
    assert item["file_count"] == 2
    assert item["bytes"] == len(b"partial") * 2
    assert failed.is_dir()
    assert not (root / "var/logs/ai_changes.jsonl").exists()


def test_confirm_requires_a_fresh_plan_and_explicit_boolean(workspace):
    root, workflows, failed = workspace
    service = QuarantineService(root)
    listing = service.listing()
    with pytest.raises(ValueError, match="confirm=true"):
        service.confirm(listing["plan_id"], ["a" * 32], False)
    with pytest.raises(ValueError, match="plan_id"):
        service.confirm("not-a-plan", ["a" * 32], True)
    with pytest.raises(ValueError, match="ids"):
        service.confirm(listing["plan_id"], [], True)
    assert failed.is_dir()


def test_confirm_recomputes_the_plan_and_deletes_exact_ids_with_audit(workspace):
    root, workflows, failed = workspace
    service = QuarantineService(root)
    listing = service.listing()
    (failed / "late.tmp").write_bytes(b"new")
    with pytest.raises(ValueError, match="清单已变化"):
        service.confirm(listing["plan_id"], ["a" * 32], True)
    fresh = service.listing()
    result = service.confirm(fresh["plan_id"], ["a" * 32], True)
    assert result["bytes_freed"] == len(b"partial") * 2 + len(b"new")
    assert not failed.exists()
    assert (workflows / SAVED).is_dir()
    events = [json.loads(line) for line in (root / "var/logs/ai_changes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "workflow_build_quarantine_confirmed"
    assert events[-1]["deleted"] == [f"var/learning/workflows/{BUILD}"]
    assert len(events[-1]["deleted"]) == 1


def test_symlinked_or_misnamed_directories_are_never_deleted(workspace):
    root, workflows, failed = workspace
    service = QuarantineService(root)
    listing = service.listing()
    outside = root / "outside"
    outside.mkdir()
    (workflows / (".backtest-" + "c" * 32 + ".building")).symlink_to(outside, target_is_directory=True)
    assert [item["id"] for item in service.listing()["items"]] == ["a" * 32]
    with pytest.raises(ValueError, match="ids must belong"):
        service.confirm(listing["plan_id"], ["c" * 32], True)
    assert outside.is_dir()


def write_job(root, status, *, updated_at=None, run_id=None):
    database = root / 'var/jobs/jobs.db'
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS jobs (body TEXT)')
        connection.execute('DELETE FROM jobs')
        job = dict(id='lake-active', kind='backtest', status=status, run_id=run_id,
                   updated_at=updated_at or datetime.now().astimezone().isoformat())
        connection.execute('INSERT INTO jobs VALUES (?)', (json.dumps(job),))
        connection.commit()


@pytest.mark.parametrize('status', ['queued', 'running', 'cancelling'])
def test_active_builds_cannot_be_deleted_even_if_they_start_after_preview(workspace, status):
    root, workflows, failed = workspace
    service = QuarantineService(root)
    preview = service.listing()
    write_job(root, status)
    assert service.listing()['items'] == []
    with pytest.raises(ValueError, match='清单已变化'):
        service.confirm(preview['plan_id'], ['a' * 32], True)
    assert failed.is_dir()
    assert (workflows / SAVED).is_dir()
    assert not (root / 'var/logs/ai_changes.jsonl').exists()


def test_exact_active_run_is_protected_without_a_recent_progress_timestamp(workspace):
    root, workflows, failed = workspace
    write_job(root, 'running', updated_at=(datetime.now().astimezone() - timedelta(hours=2)).isoformat(),
              run_id='backtest-' + 'a' * 32)
    assert QuarantineService(root).listing()['items'] == []
    assert failed.is_dir()


def test_confirmation_requires_the_existing_compute_lease(workspace):
    root, workflows, failed = workspace
    service = QuarantineService(root)
    preview = service.listing()
    lease = root / 'var/learning/compute.lock'
    with FileLock(str(lease), timeout=0):
        with pytest.raises(Timeout):
            service.confirm(preview['plan_id'], ['a' * 32], True)
    assert failed.is_dir()
    assert not (root / 'var/logs/ai_changes.jsonl').exists()
    # A finished computation releases the same lease, so cleanup can proceed.
    result = service.confirm(preview['plan_id'], ['a' * 32], True)
    assert result['deleted'] == [f'var/learning/workflows/{BUILD}']
