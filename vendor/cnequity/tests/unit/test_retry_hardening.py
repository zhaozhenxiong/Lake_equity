import contextlib
import threading
from datetime import date

import pytest

import cnequity.steps  # noqa: F401
from cnequity.config import Config, WaveConfig
from cnequity.orchestrator.engine import JobEngine
from cnequity.orchestrator.manifest import Manifest
from cnequity.orchestrator.run_lock import RunLockError, run_lock
from cnequity.storage.layout import init_data_layout


def test_retry_pending_when_batches_still_running(tmp_path):
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(
        run_id,
        "batch-live",
        "daily_bars",
        "daily_bars",
        symbols=["600519.SH"],
        window_start="2024-06-28",
        window_end="2024-06-28",
    )
    manifest.finish_run(run_id, "failed")

    engine = JobEngine(cfg)
    result = engine.run_job(
        "retry",
        date(2024, 6, 28),
        run_id=run_id,
        retry_failed_only=True,
    )
    assert result["status"] == "pending"
    assert result["retried"] == 0
    assert result["incomplete_batches"] == 1
    assert result["incomplete_by_status"]["running"] == 1


def test_retry_uses_the_original_runs_trade_date_not_today(tmp_path, monkeypatch):
    """`cne run retry` has no trade_date of its own; run_job() defaults the
    parameter to today when the caller omits it, exactly as the CLI does. A
    run retried after its own trade_date has rolled over must still resume
    the session it was started for, or a backfill for a past date silently
    asks sources for data dated today, which never lands.
    """
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    stored_date = "2024-06-28"
    run_id = manifest.start_run("daily", {"trade_date": stored_date})
    manifest.start_batch(
        run_id,
        "batch-daily_bars",
        "daily_bars",
        "daily_bars",
        symbols=["600519.SH"],
        window_start=stored_date,
        window_end=stored_date,
    )
    manifest.finish_batch(run_id, "batch-daily_bars", "failed", error_message="boom")
    manifest.finish_run(run_id, "failed")

    engine = JobEngine(cfg)
    seen_dates: list[date] = []

    def fake_run_step(name, trade_date, run_id, context, *, retry_of=None):
        seen_dates.append(trade_date)
        manifest.start_batch(run_id, "batch-daily_bars", name, name)
        manifest.finish_batch(run_id, "batch-daily_bars", "success")
        return {"status": "success"}

    monkeypatch.setattr(engine, "_run_step", fake_run_step)
    monkeypatch.setattr(engine, "_run_finalize_steps", lambda *args, **kwargs: [])

    # No trade_date passed — mirrors `cne run retry --run-id <id>` exactly.
    engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert seen_dates == [date.fromisoformat(stored_date)]


def test_retry_targeted_run_does_not_start_unplanned_derives(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", retry_backoff_seconds=0)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run(
        "daily:etf_profiles_onboarding",
        {
            "trade_date": "2024-06-28",
            "planned_steps": ["etf_profiles", "compact"],
        },
    )
    manifest.start_batch(run_id, "etf-original", "etf_profiles", "etf_profiles")
    manifest.finish_batch(run_id, "etf-original", "failed", error_message="connection reset")
    # The first attempt reached compact with no ready ETF staging, as a real
    # degraded daily run does before its failed source is retried.
    manifest.start_batch(run_id, "compact-original", "compact", "compact")
    manifest.finish_batch(run_id, "compact-original", "success")
    engine = JobEngine(cfg)
    seen_steps: list[str] = []

    def recover(name, day, resumed_run_id, context, *, retry_of=None):
        seen_steps.append(name)
        assert day == date(2024, 6, 28)
        batch_id = f"retry-{name}"
        manifest.start_batch(resumed_run_id, batch_id, name, name)
        manifest.finish_batch(resumed_run_id, batch_id, "success")
        if retry_of:
            manifest.supersede_batches(resumed_run_id, retry_of, superseded_by=batch_id)
        return {"status": "success"}

    monkeypatch.setattr(engine, "_run_step", recover)
    result = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert result["status"] == "success"
    assert seen_steps == ["etf_profiles", "compact"]


def test_interrupted_daily_run_retries_immediately_and_does_not_block_another_job(
    tmp_path, monkeypatch
):
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    attempts = 0

    from cnequity.orchestrator import engine as eng_mod
    from cnequity.orchestrator.registry import StepEntry

    def _get_step(name: str):
        def _fn(config, trade_date, run_id, context):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise KeyboardInterrupt
            return {"rows_read": 1, "rows_written": 1}

        return StepEntry(fn=_fn, group="test", requires_workers=False)

    monkeypatch.setattr(eng_mod, "get_step", _get_step)
    monkeypatch.setattr(engine, "_run_finalize_steps", lambda *args, **kwargs: [])

    with pytest.raises(KeyboardInterrupt):
        engine.run_job("daily:test", date(2024, 6, 28), steps=["instruments"])

    interrupted = engine.manifest.latest_run("daily:test")
    assert interrupted["status"] == "failed"
    batch = engine.manifest.get_batches_for_run(interrupted["run_id"])[0]
    assert batch["status"] == "failed"

    retry = engine.run_job("retry", run_id=interrupted["run_id"], retry_failed_only=True)
    assert retry["status"] == "success"
    assert attempts == 2

    other = engine.run_job("daily:other", date(2024, 6, 28), steps=["instruments"])
    assert other["status"] == "success"
    assert attempts == 3


def test_retry_automatically_repeats_with_persisted_budget(tmp_path, monkeypatch):
    cfg = Config(
        data_root=tmp_path / "data",
        tdx_allow_mock=True,
        max_retries=3,
        retry_backoff_seconds=1.25,
    )
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(
        run_id,
        "batch-daily_bars",
        "daily_bars",
        "daily_bars",
        symbols=["600519.SH"],
    )
    manifest.finish_batch(run_id, "batch-daily_bars", "failed", error_message="timeout")

    engine = JobEngine(cfg)
    attempts = 0
    sleeps: list[float] = []
    timeout_passes = iter(
        [
            {"running_to_stale": 1, "stale_to_failed": 0},
            {"running_to_stale": 0, "stale_to_failed": 2},
        ]
    )

    def flaky_run_step(name, trade_date, resumed_run_id, context, *, retry_of=None):
        nonlocal attempts
        attempts += 1
        manifest.start_batch(resumed_run_id, "batch-daily_bars", name, name)
        status = "success" if attempts == 2 else "failed"
        manifest.finish_batch(
            resumed_run_id,
            "batch-daily_bars",
            status,
            error_message=None if status == "success" else "TDX timeout",
        )
        return {"status": status}

    monkeypatch.setattr(engine, "_run_step", flaky_run_step)
    monkeypatch.setattr(engine, "_run_finalize_steps", lambda *args, **kwargs: [])
    monkeypatch.setattr("cnequity.orchestrator.engine.time.sleep", sleeps.append)
    monkeypatch.setattr(
        engine.manifest, "advance_batch_timeouts", lambda *args, **kwargs: next(timeout_passes)
    )
    result = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert result["status"] == "success"
    assert result["retry_passes"] == 2
    assert result["retried"] == 2
    assert result["retry_exhausted"] == 0
    assert [item["status"] for item in result["results"]] == ["failed", "success"]
    assert result["stale_marked_failed"] == 3
    assert result["batch_timeout"] == {"running_to_stale": 1, "stale_to_failed": 2}
    assert sleeps == [1.25]
    assert manifest.get_batches_for_run(run_id)[0]["retry_count"] == 2


def test_retry_stops_at_configured_budget(tmp_path, monkeypatch):
    cfg = Config(
        data_root=tmp_path / "data",
        tdx_allow_mock=True,
        max_retries=2,
        retry_backoff_seconds=0,
    )
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(run_id, "batch-0", "daily_bars", "daily_bars")
    manifest.finish_batch(run_id, "batch-0", "failed", error_message="timeout")

    engine = JobEngine(cfg)
    attempts = 0

    def always_fails(name, trade_date, resumed_run_id, context, *, retry_of=None):
        nonlocal attempts
        attempts += 1
        manifest.start_batch(resumed_run_id, "batch-0", name, name)
        manifest.finish_batch(resumed_run_id, "batch-0", "failed", error_message="timeout")
        return {"status": "failed"}

    monkeypatch.setattr(engine, "_run_step", always_fails)
    result = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert attempts == 2
    assert result["status"] == "failed"
    assert result["retry_passes"] == 2
    assert result["retry_exhausted"] == 1
    assert manifest.get_batches_for_run(run_id)[0]["retry_count"] == 2

    repeated = engine.run_job("retry", run_id=run_id, retry_failed_only=True)
    assert attempts == 2
    assert repeated["status"] == "failed"
    assert repeated["retried"] == 0
    assert repeated["retry_exhausted"] == 1


def test_retry_compacts_ready_dataset_while_another_still_fails(tmp_path, monkeypatch):
    import polars as pl

    from cnequity.storage import StagingWriter

    cfg = Config(data_root=tmp_path / "data", max_retries=1, retry_backoff_seconds=0)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(run_id, "failed", "daily_bars", "daily_bars")
    manifest.finish_batch(run_id, "failed", "failed", error_message="timeout")
    manifest.start_batch(run_id, "ready", "fund_flow", "fund_flow")
    manifest.finish_batch(run_id, "ready", "success", rows_written=1)
    StagingWriter(cfg.staging_root).write_batch(
        "fund_flow",
        run_id,
        "ready",
        pl.DataFrame(
            {
                "symbol": ["600000.SH"],
                "trade_date": [date(2024, 6, 28)],
                "main_net_inflow": [1.0],
                "super_large_net_inflow": [0.0],
                "large_net_inflow": [0.0],
                "medium_net_inflow": [0.0],
                "small_net_inflow": [0.0],
                "source": ["eastmoney"],
                "data_version": ["v1"],
                "fetched_at": ["2024-06-28T00:00:00Z"],
            }
        ),
    )
    engine = JobEngine(cfg)
    finalize = []

    def retry_failed(name, day, resumed_run_id, context, *, retry_of=None):
        manifest.start_batch(resumed_run_id, "failed", name, name)
        manifest.finish_batch(resumed_run_id, "failed", "failed", error_message="still down")
        return {"status": "failed"}

    monkeypatch.setattr(engine, "_run_step", retry_failed)
    monkeypatch.setattr(engine, "_run_finalize_steps", lambda *a, **kw: finalize.append(kw) or [])
    engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert finalize and finalize[0]["steps"] == ("compact",)


def test_retry_does_not_repeat_non_transient_worker_failures(tmp_path, monkeypatch):
    cfg = Config(
        data_root=tmp_path / "data",
        tdx_allow_mock=True,
        max_retries=3,
        retry_backoff_seconds=0,
    )
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(run_id, "batch-0", "daily_bars", "daily_bars")
    manifest.finish_batch(run_id, "batch-0", "failed", error_message="invalid payload")

    engine = JobEngine(cfg)
    attempts = 0

    def still_fails(name, trade_date, resumed_run_id, context, *, retry_of=None):
        nonlocal attempts
        attempts += 1
        manifest.start_batch(resumed_run_id, "batch-0", name, name)
        manifest.finish_batch(resumed_run_id, "batch-0", "failed", error_message="invalid payload")
        return {"status": "failed"}

    monkeypatch.setattr(engine, "_run_step", still_fails)
    result = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert attempts == 1
    assert result["status"] == "failed"
    assert result["retry_passes"] == 1
    assert result["retry_exhausted"] == 0
    assert manifest.get_batches_for_run(run_id)[0]["retry_count"] == 1


def test_retry_does_not_automatically_repeat_non_worker_steps(tmp_path, monkeypatch):
    cfg = Config(
        data_root=tmp_path / "data",
        tdx_allow_mock=True,
        max_retries=3,
        retry_backoff_seconds=0,
    )
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(run_id, "batch-0", "trading_status", "trading_status")
    manifest.finish_batch(run_id, "batch-0", "failed", error_message="network timeout")

    engine = JobEngine(cfg)
    attempts = 0

    def still_fails(name, trade_date, resumed_run_id, context, *, retry_of=None):
        nonlocal attempts
        attempts += 1
        return {"status": "failed"}

    monkeypatch.setattr(engine, "_run_step", still_fails)
    result = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert attempts == 1
    assert result["status"] == "failed"
    assert result["retry_passes"] == 1
    assert manifest.get_batches_for_run(run_id)[0]["retry_count"] == 0


def test_non_worker_retry_lineage_remains_recoverable(tmp_path, monkeypatch):
    cfg = Config(
        data_root=tmp_path / "data",
        tdx_allow_mock=True,
        max_retries=1,
        retry_backoff_seconds=0,
    )
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(run_id, "batch-original", "trading_status", "trading_status")
    manifest.finish_batch(run_id, "batch-original", "failed", error_message="network timeout")

    engine = JobEngine(cfg)
    attempts = 0

    def retry_step(name, trade_date, resumed_run_id, context, *, retry_of=None):
        nonlocal attempts
        attempts += 1
        batch_id = f"batch-retry-{attempts}"
        manifest.start_batch(resumed_run_id, batch_id, name, name)
        status = "failed" if attempts == 1 else "success"
        manifest.finish_batch(resumed_run_id, batch_id, status, error_message="timeout")
        if status == "success":
            manifest.supersede_batches(
                resumed_run_id,
                retry_of or [],
                superseded_by=batch_id,
            )
        return {"status": status}

    monkeypatch.setattr(engine, "_run_step", retry_step)
    monkeypatch.setattr(engine, "_run_finalize_steps", lambda *args, **kwargs: [])

    first = engine.run_job("retry", run_id=run_id, retry_failed_only=True)
    assert first["status"] == "failed"
    second = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert second["status"] == "success"
    assert attempts == 2
    statuses = {
        batch["batch_id"]: batch["status"] for batch in manifest.get_batches_for_run(run_id)
    }
    assert statuses == {
        "batch-original": "superseded",
        "batch-retry-1": "superseded",
        "batch-retry-2": "success",
    }


def test_retry_routes_by_task_id_when_physical_dataset_differs(tmp_path, monkeypatch):
    cfg = Config(
        data_root=tmp_path / "data",
        tdx_allow_mock=True,
        max_retries=1,
        retry_backoff_seconds=0,
    )
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("backfill", {"trade_date": "2024-06-28"})
    manifest.start_batch(
        run_id,
        "history-failed",
        task_id="daily_bars_history",
        dataset="daily_bars",
    )
    manifest.finish_batch(run_id, "history-failed", "failed", error_message="network timeout")

    engine = JobEngine(cfg)
    seen_steps: list[str] = []

    def recover(name, trade_date, resumed_run_id, context, *, retry_of=None):
        seen_steps.append(name)
        manifest.start_batch(resumed_run_id, "history-success", name, "daily_bars")
        manifest.finish_batch(resumed_run_id, "history-success", "success")
        manifest.supersede_batches(
            resumed_run_id,
            retry_of or [],
            superseded_by="history-success",
        )
        return {"status": "success"}

    monkeypatch.setattr(engine, "_run_step", recover)
    monkeypatch.setattr(engine, "_run_finalize_steps", lambda *args, **kwargs: [])
    result = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert result["status"] == "success"
    assert seen_steps == ["daily_bars_history"]
    batches = {batch["batch_id"]: batch for batch in manifest.get_batches_for_run(run_id)}
    assert batches["history-failed"]["status"] == "superseded"
    assert batches["history-failed"]["retry_count"] == 0


def test_retry_legacy_unregistered_task_falls_back_to_dataset(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True, retry_backoff_seconds=0)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily", {"trade_date": "2024-06-28"})
    manifest.start_batch(
        run_id,
        "ownership-failed",
        task_id="daily_bars_ownership",
        dataset="daily_bars",
    )
    manifest.finish_batch(run_id, "ownership-failed", "warning", error_message="incomplete")

    engine = JobEngine(cfg)
    seen_steps: list[str] = []

    def recover(name, trade_date, resumed_run_id, context, *, retry_of=None):
        seen_steps.append(name)
        manifest.start_batch(resumed_run_id, "ownership-failed", name, name)
        manifest.finish_batch(resumed_run_id, "ownership-failed", "success")
        return {"status": "success"}

    monkeypatch.setattr(engine, "_run_step", recover)
    monkeypatch.setattr(engine, "_run_finalize_steps", lambda *args, **kwargs: [])
    result = engine.run_job("retry", run_id=run_id, retry_failed_only=True)

    assert result["status"] == "success"
    assert seen_steps == ["daily_bars"]


def test_worker_batch_specs_reads_manifest_window(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    manifest = engine.manifest
    run_id = manifest.start_run("daily", {})
    manifest.start_batch(
        run_id,
        "2016-01-01_2024-06-27-batch-0",
        "daily_bars",
        "daily_bars",
        symbols=["600519.SH"],
        window_start="2016-01-01",
        window_end="2024-06-27",
    )
    manifest.finish_batch(run_id, "2016-01-01_2024-06-27-batch-0", "failed", error_message="boom")

    specs = engine._worker_batch_specs(manifest.get_failed_batches(run_id), date(2024, 6, 28))
    assert specs == [
        (
            "2016-01-01_2024-06-27-batch-0",
            ["600519.SH"],
            date(2016, 1, 1),
            date(2024, 6, 27),
        )
    ]


def test_run_lock_blocks_concurrent_retry(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    run_id = "run-lock-test"
    with run_lock(cfg.meta_root, run_id):
        try:
            with run_lock(cfg.meta_root, run_id):
                raise AssertionError("should not acquire twice")
        except RunLockError:
            pass


def test_daily_ingestion_lock_blocks_overlapping_runs(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    monkeypatch.setattr(engine, "_run_wave", lambda *args, **kwargs: ([], 0, 0, False, False))

    with run_lock(cfg.meta_root, "daily_ingestion"):
        try:
            engine.run_job("daily:core", date(2024, 6, 28), waves=[])
        except RunLockError:
            pass
        else:
            raise AssertionError("expected RunLockError")


@contextlib.contextmanager
def _held_by_another_holder(meta_root, name):
    """Hold *name* from another thread, which flock treats like another process."""
    taken, release = threading.Event(), threading.Event()

    def hold():
        with run_lock(meta_root, name):
            taken.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert taken.wait(5)
    try:
        yield
    finally:
        release.set()
        holder.join(5)


@pytest.mark.parametrize("job", ["daily:core", "events", "backfill", "retry"])
def test_ingestion_refuses_to_run_beside_an_init(tmp_path, monkeypatch, job):
    """`cne run daily` in a second terminal used to fetch and compact while
    `cne init` was still building the lake; only the dashboard refused it."""
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    monkeypatch.setattr(engine, "_run_wave", lambda *a, **k: ([], 0, 0, False, False))
    with _held_by_another_holder(cfg.meta_root, "init_job"):
        with pytest.raises(RunLockError, match="An init is running"):
            engine.run_job(job, date(2024, 6, 28), waves=[], backfill=True)
    assert Manifest(cfg.manifest_path).latest_run() is None, "refused before recording a run"


@pytest.mark.parametrize(
    ("lock", "name"), [("daily_ingestion", "daily update"), ("events_ingestion", "event sweep")]
)
def test_init_refuses_to_start_beside_ingestion(tmp_path, monkeypatch, lock, name):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    monkeypatch.setattr(
        engine, "_run_init_phases_locked", lambda *a, **k: pytest.fail("init must not start")
    )
    with _held_by_another_holder(cfg.meta_root, lock):
        with pytest.raises(RunLockError, match=name):
            engine.run_init_phases(date(2024, 6, 28))


def test_retrying_an_init_takes_the_init_lock(tmp_path, monkeypatch):
    """`cne run retry --run-id <init>` resumed without it, beside a live init."""
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    run_id = engine.manifest.start_run("init", {"phases": ["phase1_reference"]})
    monkeypatch.setattr(
        engine, "_resume_init_locked", lambda *a, **k: pytest.fail("resume must not start")
    )
    with _held_by_another_holder(cfg.meta_root, "init_job"):
        with pytest.raises(RunLockError):
            engine.resume_init(date(2024, 6, 28), run_id=run_id)


def test_an_init_runs_its_own_phases_and_resume_under_one_lock(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    run_id = engine.manifest.start_run("init", {"phases": ["phase1_reference"]})
    monkeypatch.setattr(engine, "_resume_init_locked", lambda *a, **k: {"resumed": True})
    assert engine.run_init_phases(date(2024, 6, 28), resume_run_id=run_id) == {"resumed": True}


def test_reconcile_orphaned_runs_closes_stale_running(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {})
    manifest.start_batch(run_id, "batch-0", "daily_bars", "daily_bars")
    out = manifest.reconcile_orphaned_runs(stale_after_seconds=0)
    assert out["runs_closed"] == 1
    assert out["batches_closed"] == 1
    assert manifest.get_run(run_id)["status"] == "failed"


def test_reconcile_keeps_a_run_with_fresh_batch_heartbeat(tmp_path):
    """Long jobs must not be closed just because run.started_at is old."""
    from datetime import datetime, timedelta, timezone

    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("valuation_2001", {})
    # Backdate the run start so a started_at-only check would kill it.
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    with manifest._connect() as conn:
        conn.execute(
            "UPDATE ingestion_runs SET started_at = ? WHERE run_id = ?",
            (old, run_id),
        )
    manifest.start_batch(run_id, "batch-0", "valuation_metrics", "valuation_metrics")
    # Fresh heartbeat (just set by start_batch).
    out = manifest.reconcile_orphaned_runs(stale_after_seconds=3600)
    assert out["runs_closed"] == 0
    assert manifest.get_run(run_id)["status"] == "running"


def test_reconcile_skips_a_run_whose_lock_is_held(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {})
    manifest.start_batch(run_id, "batch-0", "daily_bars", "daily_bars")
    with run_lock(cfg.meta_root, run_id):
        out = manifest.reconcile_orphaned_runs(stale_after_seconds=0, locks_root=cfg.meta_root)
    assert out["runs_closed"] == 0
    assert out["skipped_locked"] == 1
    assert manifest.get_run(run_id)["status"] == "running"


def test_reconcile_is_idempotent(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {})
    manifest.start_batch(run_id, "batch-0", "daily_bars", "daily_bars")
    assert manifest.reconcile_orphaned_runs(stale_after_seconds=0)["runs_closed"] == 1
    assert manifest.reconcile_orphaned_runs(stale_after_seconds=0)["runs_closed"] == 0


def test_retry_finishes_a_zombie_run_with_all_batches_success(tmp_path):
    """Crash after the last batch left status=running; retry must close it."""
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {"trade_date": "2024-06-28"})
    manifest.start_batch(run_id, "batch-0", "instruments", "instruments")
    manifest.finish_batch(run_id, "batch-0", "success", rows_written=1)
    # Deliberately leave the run in 'running'.

    engine = JobEngine(cfg)
    result = engine.run_job(
        "retry",
        date(2024, 6, 28),
        run_id=run_id,
        retry_failed_only=True,
    )
    assert result["status"] == "success"
    assert manifest.get_run(run_id)["status"] == "success"


def test_run_job_reconciles_orphans_on_entry(tmp_path, monkeypatch):
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True, batch_stale_seconds=0)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    zombie = manifest.start_run("valuation_2001", {})
    manifest.start_batch(zombie, "batch-0", "valuation_metrics", "valuation_metrics")

    engine = JobEngine(cfg)
    monkeypatch.setattr(engine, "_run_wave", lambda *args, **kwargs: ([], 0, 0, False, False))
    engine.run_job("daily:core", date(2024, 6, 28), waves=[])

    assert manifest.get_run(zombie)["status"] == "failed"


def test_retry_reconciles_peer_orphans_on_entry(tmp_path):
    """cne run retry used to skip reconcile; peer zombies sat until the next daily."""
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True, batch_stale_seconds=0)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    zombie = manifest.start_run("valuation_2001", {})
    manifest.start_batch(zombie, "batch-0", "valuation_metrics", "valuation_metrics")

    target = manifest.start_run("daily:core", {"trade_date": "2024-06-28"})
    manifest.start_batch(target, "batch-0", "instruments", "instruments")
    manifest.finish_batch(target, "batch-0", "success", rows_written=1)
    # Leave target running so retry's all-green path closes it.

    engine = JobEngine(cfg)
    engine.run_job(
        "retry",
        date(2024, 6, 28),
        run_id=target,
        retry_failed_only=True,
    )
    assert manifest.get_run(zombie)["status"] == "failed"
    assert manifest.get_run(target)["status"] == "success"


def _backdate(cfg, run_id: str, when: str = "2020-01-01T00:00:00+00:00") -> None:
    """Age a run past the unlocked grace window, i.e. "killed a while ago"."""
    import sqlite3

    with sqlite3.connect(cfg.manifest_path) as conn:
        conn.execute("UPDATE ingestion_runs SET started_at=? WHERE run_id=?", (when, run_id))
        conn.execute(
            "UPDATE ingestion_batches SET started_at=?, heartbeat_at=? WHERE run_id=?",
            (when, when, run_id),
        )


def test_a_dead_run_is_reaped_without_waiting_out_the_stale_window(tmp_path):
    """Liveness is the lock, not the clock. A run killed two minutes ago used
    to stay `running` for `batch_stale_seconds` — an hour by default — and
    every command that reads run status was wrong for that hour."""
    cfg = Config(data_root=tmp_path / "data", batch_stale_seconds=3600)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {"trade_date": "2026-09-16"})
    manifest.start_batch(run_id, "b1", task_id="daily_bars", dataset="daily_bars")
    _backdate(cfg, run_id)

    out = manifest.reconcile_orphaned_runs(
        stale_after_seconds=cfg.batch_stale_seconds, locks_root=cfg.meta_root
    )

    assert out["runs_closed"] == 1
    assert manifest.get_run(run_id)["status"] == "failed"


def test_a_run_inside_its_grace_window_is_left_alone(tmp_path):
    """The window covers the gap between `start_run` and taking the lock."""
    cfg = Config(data_root=tmp_path / "data", batch_stale_seconds=3600)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {"trade_date": "2026-09-16"})

    out = manifest.reconcile_orphaned_runs(
        stale_after_seconds=cfg.batch_stale_seconds, locks_root=cfg.meta_root
    )

    assert out["runs_closed"] == 0
    assert manifest.get_run(run_id)["status"] == "running"


def test_a_shorter_configured_window_is_still_honoured(tmp_path):
    """The grace period caps how long a corpse may linger; it is never a reason
    to wait longer than the caller asked for."""
    cfg = Config(data_root=tmp_path / "data", batch_stale_seconds=0)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {"trade_date": "2026-09-16"})

    out = manifest.reconcile_orphaned_runs(stale_after_seconds=0, locks_root=cfg.meta_root)

    assert out["runs_closed"] == 1
    assert manifest.get_run(run_id)["status"] == "failed"


def test_a_running_job_holds_its_own_run_lock(tmp_path, monkeypatch):
    """Which is what makes the lock usable as proof of life."""
    from cnequity.orchestrator.run_lock import is_run_locked

    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    engine = JobEngine(cfg)
    seen: dict = {}

    def _wave(*args, **kwargs):
        seen["locked"] = is_run_locked(cfg.meta_root, seen["run_id"])
        return ([], 0, 0, False, False)

    original = engine.manifest.start_run

    def _remember(job_name, metadata=None):
        seen["run_id"] = original(job_name, metadata)
        return seen["run_id"]

    monkeypatch.setattr(engine.manifest, "start_run", _remember)
    monkeypatch.setattr(engine, "_run_wave", _wave)
    engine.run_job(
        "daily:core",
        date(2026, 9, 16),
        waves=[WaveConfig(name="w", parallel=False, steps=["instruments"])],
    )

    assert seen["locked"] is True
    # And released when it ends, or the next run could never be reconciled.
    assert is_run_locked(cfg.meta_root, seen["run_id"]) is False


def test_retry_failed_groups_sees_a_crash_it_has_not_reconciled_yet(tmp_path, monkeypatch):
    """The one command an operator reaches for after a crash used to answer
    "No failed daily group run to retry" about the crash they were looking at."""
    from cnequity.cli.run_cmds import _failed_daily_group_runs

    cfg = Config(data_root=tmp_path / "data", batch_stale_seconds=3600)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {"trade_date": "2026-09-16"})
    manifest.start_batch(run_id, "b1", task_id="daily_bars", dataset="daily_bars")
    _backdate(cfg, run_id)

    runs = _failed_daily_group_runs(JobEngine(cfg))

    assert [r["run_id"] for r in runs] == [run_id]


def test_status_counts_the_same_orphans_the_next_run_will_close(tmp_path):
    """`cne status` reporting a different orphan count from the one the engine
    acts on is how an operator ends up arguing with the tool about a crash."""
    cfg = Config(data_root=tmp_path / "data", batch_stale_seconds=3600)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("daily:core", {"trade_date": "2026-09-16"})
    manifest.start_batch(run_id, "b1", task_id="daily_bars", dataset="daily_bars")
    _backdate(cfg, run_id)

    counted = manifest.count_stale_running_runs(
        stale_after_seconds=cfg.batch_stale_seconds, locks_root=cfg.meta_root
    )
    closed = manifest.reconcile_orphaned_runs(
        stale_after_seconds=cfg.batch_stale_seconds, locks_root=cfg.meta_root
    )["runs_closed"]

    assert counted == closed == 1


def test_a_stalled_lock_holder_is_named_rather_than_waited_on_forever(tmp_path):
    """A crashed holder releases its lock as the kernel reaps it, so anything
    still holding one at the deadline is alive and stuck — and waiting on that
    forever, silently, parks every command behind it."""
    import subprocess
    import sys
    import time as _time

    from cnequity.orchestrator.run_lock import RunLockError, lock_path, run_lock

    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\n"
            "from pathlib import Path\n"
            "from cnequity.orchestrator.run_lock import run_lock\n"
            f"with run_lock(Path({str(cfg.meta_root)!r}), 'compact', blocking=False):\n"
            "    print('held', flush=True)\n"
            "    time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        started = _time.monotonic()
        with pytest.raises(RunLockError) as caught:
            with run_lock(cfg.meta_root, "compact", blocking=True, timeout=1):
                pass
    finally:
        holder.terminate()
        holder.wait(30)

    assert _time.monotonic() - started < 30
    message = str(caught.value)
    assert "alive and stuck" in message
    assert str(lock_path(cfg.meta_root, "compact")) in message


def _one_batch_run(cfg, *, job: str = "backfill") -> tuple[Manifest, str]:
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run(job, {"trade_date": "2024-06-28"})
    manifest.start_batch(
        run_id,
        "batch-live",
        "daily_bars",
        "daily_bars",
        symbols=["600519.SH"],
        window_start="2024-06-28",
        window_end="2024-06-28",
    )
    return manifest, run_id


def test_interrupting_a_retry_closes_the_run_it_was_repairing(tmp_path, monkeypatch):
    """`cne run retry` returns from `run_job` before the try/finally that
    closes an interrupted run, so Ctrl-C there used to leave exactly the state
    the retry existed to clear: a run still `running` and batches its own
    process had planned but will never execute."""
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    manifest, run_id = _one_batch_run(cfg, job="daily")
    manifest.finish_batch(run_id, "batch-live", "failed", error_message="boom")

    from cnequity.orchestrator import engine as eng_mod
    from cnequity.orchestrator.registry import StepEntry

    def _get_step(name: str):
        def _fn(config, trade_date, run_id_, context):
            manifest.queue_batches(
                run_id_,
                "daily_bars",
                "daily_bars",
                [("batch-planned", ["000001.SZ"], "2024-06-28", "2024-06-28")],
            )
            raise KeyboardInterrupt

        return StepEntry(fn=_fn, group="test", requires_workers=True)

    monkeypatch.setattr(eng_mod, "get_step", _get_step)

    engine = JobEngine(cfg)
    with pytest.raises(KeyboardInterrupt):
        engine.run_job("retry", date(2024, 6, 28), run_id=run_id, retry_failed_only=True)

    assert manifest.get_run(run_id)["status"] == "failed"
    assert manifest.get_batch(run_id, "batch-planned")["status"] == "failed"


def test_interrupting_a_backfill_closes_its_run_even_though_it_defers_finish(tmp_path, monkeypatch):
    """`cne backfill` passes finalize_run=False so compact can close the run
    after it. That says who finishes a *successful* run and was never a claim
    about interrupts — read as one, every backfill Ctrl-C left the ledger open.
    """
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)

    from cnequity.orchestrator import engine as eng_mod
    from cnequity.orchestrator.registry import StepEntry

    def _get_step(name: str):
        def _fn(config, trade_date, run_id, context):
            raise KeyboardInterrupt

        return StepEntry(fn=_fn, group="test", requires_workers=False)

    monkeypatch.setattr(eng_mod, "get_step", _get_step)

    engine = JobEngine(cfg)
    with pytest.raises(KeyboardInterrupt):
        engine.run_job(
            "backfill",
            date(2024, 6, 28),
            steps=["daily_bars"],
            backfill=True,
            finalize_run=False,
        )

    manifest = Manifest(cfg.manifest_path)
    latest = manifest.latest_run("backfill")
    assert latest is not None and latest["status"] == "failed"
    batches = manifest.get_batches_for_run(latest["run_id"])
    assert batches and all(b["status"] == "failed" for b in batches)


def test_a_queued_batch_is_retryable_without_waiting_for_the_stale_window(tmp_path):
    """A batch the plan recorded but no worker reached is work this run can
    still do. Making it wait out `batch_stale_seconds` was the reason an
    interrupted sweep could not be resumed for an hour."""
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("backfill", {"trade_date": "2024-06-28"})
    manifest.queue_batches(
        run_id,
        "daily_bars",
        "daily_bars",
        [("batch-0", ["600519.SH"], "2024-06-28", "2024-06-28")],
    )

    assert manifest.incomplete_batch_count(run_id) == 1
    assert [b["batch_id"] for b in manifest.get_retryable_batches(run_id)] == ["batch-0"]
    assert manifest.incomplete_batch_counts_by_dataset(run_id) == {"daily_bars": 1}


def test_queueing_never_demotes_a_batch_that_already_succeeded(tmp_path):
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("backfill", {"trade_date": "2024-06-28"})
    manifest.start_batch(run_id, "batch-0", "daily_bars", "daily_bars", symbols=["600519.SH"])
    manifest.finish_batch(run_id, "batch-0", "success", rows_read=7, rows_written=7)

    manifest.queue_batches(
        run_id,
        "daily_bars",
        "daily_bars",
        [
            ("batch-0", ["600519.SH"], "2024-06-28", "2024-06-28"),
            ("batch-1", ["000001.SZ"], "2024-06-28", "2024-06-28"),
        ],
    )

    assert manifest.get_batch(run_id, "batch-0")["status"] == "success"
    assert manifest.get_batch(run_id, "batch-0")["rows_written"] == 7
    assert manifest.get_batch(run_id, "batch-1")["status"] == "queued"


def test_a_finished_batch_proves_the_owner_was_alive(tmp_path):
    """The `cne init` self-reconcile, reproduced.

    Orphan detection read only unfinished batches, so a phase that ended
    cleanly left the run's own start time as its last sign of life. A phase
    longer than the unlocked grace window therefore looked like a corpse the
    moment it succeeded: 201 seconds of `instruments`, then a 60-second
    deadline measured from a timestamp 201 seconds old.
    """
    import sqlite3
    from datetime import datetime, timedelta, timezone

    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    manifest = Manifest(cfg.manifest_path)
    run_id = manifest.start_run("init", {"trade_date": "2024-06-28"})

    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=201)).isoformat()
    with sqlite3.connect(cfg.manifest_path) as conn:
        conn.execute(
            "UPDATE ingestion_runs SET started_at = ? WHERE run_id = ?", (long_ago, run_id)
        )

    # Phase one: started long ago, finished just now, nothing left running.
    manifest.start_batch(run_id, "batch-0", "instruments", "instruments")
    manifest.finish_batch(run_id, "batch-0", "success", rows_read=1, rows_written=1)

    out = manifest.reconcile_orphaned_runs(
        stale_after_seconds=3600,
        locks_root=cfg.meta_root,
        unlocked_grace_seconds=60.0,
    )

    assert out["runs_closed"] == 0, "a batch that finished a moment ago proves the owner is alive"
    assert manifest.get_run(run_id)["status"] == "running"


def test_a_second_engine_in_this_process_is_not_told_another_process_holds_it(tmp_path):
    """The re-entrancy registry is process-wide because the file lock is.

    Scoped to one JobEngine instance, a second instance asking for a lock this
    process already holds got "locked by another process" — the one
    explanation that is certainly wrong, and the one that sends an operator
    hunting for a peer that does not exist.
    """
    from cnequity.orchestrator.run_lock import reentrant_run_lock

    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)

    with reentrant_run_lock(cfg.meta_root, "run-1"):
        with reentrant_run_lock(cfg.meta_root, "run-1"):
            pass  # same thread re-enters

        with pytest.raises(RunLockError) as caught:
            with run_lock(cfg.meta_root, "run-1", blocking=False):
                pass
        assert "another process" in str(caught.value)

    # Released on exit, so an unrelated caller can take it normally.
    with run_lock(cfg.meta_root, "run-1", blocking=False):
        pass


def test_a_run_lock_held_by_another_thread_says_so(tmp_path):
    import threading

    from cnequity.orchestrator.run_lock import reentrant_run_lock

    cfg = Config(data_root=tmp_path / "data")
    init_data_layout(cfg)
    entered = threading.Event()
    release = threading.Event()
    failure: list[str] = []

    def _hold():
        with reentrant_run_lock(cfg.meta_root, "run-2"):
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert entered.wait(10)
        with pytest.raises(RunLockError) as caught:
            with reentrant_run_lock(cfg.meta_root, "run-2"):
                pass
        failure.append(str(caught.value))
    finally:
        release.set()
        holder.join(10)

    assert "another thread in this process" in failure[0]


def test_a_running_run_reports_the_rows_it_has_already_written(tmp_path, monkeypatch):
    """`finish_run` used to be the only writer of the run's row counts, which
    made them an epitaph: a run still working reported 0 while its own batch
    metadata held thousands, and a failed run reported 0 forever — the case
    where "how far did it get" is the actual question."""
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    init_data_layout(cfg)

    from cnequity.orchestrator import engine as eng_mod
    from cnequity.orchestrator.registry import StepEntry

    seen: list[tuple[int, int]] = []

    def _get_step(name: str):
        def _fn(config, trade_date, run_id, context):
            run = Manifest(cfg.manifest_path).get_run(run_id)
            seen.append((int(run["rows_read"] or 0), int(run["rows_written"] or 0)))
            if name == "trading_calendar":
                raise KeyboardInterrupt
            return {"rows_read": 7, "rows_written": 5}

        return StepEntry(fn=_fn, group="test", requires_workers=False)

    monkeypatch.setattr(eng_mod, "get_step", _get_step)

    engine = JobEngine(cfg)
    with pytest.raises(KeyboardInterrupt):
        engine.run_job(
            "daily",
            date(2024, 6, 28),
            waves=[
                WaveConfig(name="w1", parallel=False, steps=["instruments"]),
                WaveConfig(name="w2", parallel=False, steps=["trading_calendar"]),
            ],
        )

    # Wave two saw what wave one had published, instead of a pair of zeros.
    assert seen[0] == (0, 0)
    assert seen[1] == (7, 5)
    latest = Manifest(cfg.manifest_path).latest_run("daily")
    assert (latest["rows_read"], latest["rows_written"]) == (7, 5)
    assert latest["status"] == "failed"


def test_an_init_phase_does_not_reset_the_runs_counters(tmp_path, monkeypatch):
    """Each phase's own totals start at zero, so publishing them unqualified
    would walk the run's counters backwards once per phase."""
    cfg = Config(data_root=tmp_path / "data", tdx_allow_mock=True)
    cfg.init_phases = [
        "phase1_reference",
        "phase2a_corporate_actions",
        "phase3_index_and_status",
    ]
    init_data_layout(cfg)

    from cnequity.orchestrator import engine as eng_mod
    from cnequity.orchestrator.registry import StepEntry

    seen: dict[str, tuple[int, int]] = {}

    def _get_step(name: str):
        def _fn(config, trade_date, run_id, context):
            run = Manifest(cfg.manifest_path).get_run(run_id)
            seen[name] = (int(run["rows_read"] or 0), int(run["rows_written"] or 0))
            return {"rows_read": 10, "rows_written": 4}

        return StepEntry(fn=_fn, group="test", requires_workers=False)

    monkeypatch.setattr(eng_mod, "get_step", _get_step)

    engine = JobEngine(cfg)
    result = engine.run_init_phases(date(2024, 6, 28))

    # Phase 1 runs instruments + trading_calendar in one job (20/8). Phase 2
    # adds one step, and phase 3 reads the counters phase 2 published: without
    # a baseline that read would be 10/4, the run's own history erased.
    assert seen["corporate_actions"] == (20, 8), "phase two must see phase one's rows"
    assert seen["index_bars"] == (30, 12), "phase three must see the running total"
    run = Manifest(cfg.manifest_path).get_run(result["run_id"])
    assert (run["rows_read"], run["rows_written"]) == (50, 20)
