from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from lake_equity.data import DataOperationCancelled, LakeService


@pytest.fixture
def lake(tmp_path):
    source = tmp_path / "var" / "legacy" / "raw" / "khquant_raw.db"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE stock_daily_normalized(stock TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,factor_open REAL,factor_high REAL,factor_low REAL,factor_close REAL,volume REAL,amount REAL,turnover REAL,pct_chg REAL,source TEXT,source_file TEXT,duplicate_source_rows INTEGER,has_trade_price INTEGER,imported_at TEXT,PRIMARY KEY(stock,date))")
        connection.execute("CREATE TABLE securities(stock TEXT PRIMARY KEY,exchange TEXT,name TEXT,first_date TEXT,last_date TEXT,raw_rows INTEGER,source_file TEXT,updated_at TEXT,imported_at TEXT NOT NULL)")
        connection.execute("INSERT INTO securities VALUES('600000.SH','SH','Synthetic test security','2025-01-02','2026-10-22',3,'fixture',NULL,'2026-10-22T15:10:00+08:00')")
        for day in ("2025-01-02", "2026-01-05", "2026-10-22"):
            connection.execute("INSERT INTO stock_daily_normalized VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("600000.SH", day, 10.0, 12.0, 9.0, 11.0, 10.0, 12.0, 9.0, 11.0, 100.0, 1100.0, 0.5, 2.0, "sina_unadjusted_v1", "fixture://synthetic", 1, 1, "2026-10-22T15:10:00+08:00"))
    return LakeService(tmp_path, source_db=source)


def test_catalog_keeps_missing_datasets_explicit(lake):
    catalog = lake.catalog()
    assert catalog["count"] == 55
    assert not any(row["has_data"] for row in catalog["datasets"])
    assert lake.quality()["historical_completeness"] == "not_assessed"


def test_import_publishes_replayable_rows_with_original_sources(lake):
    before = lake.source_db.read_bytes()
    report = lake.import_legacy(batch_size=2)
    assert report["rows_read"] == report["rows_published"] == 3
    assert report["revision_id"]
    assert lake.source_db.read_bytes() == before
    result = lake.read("daily_bars", symbol="600000.SH", start="2025-01-01", end="2026-10-22", limit=2)
    assert result["count"] == 3
    assert result["returned_count"] == 2
    assert all(row["source"] == "sina_unadjusted_v1" for row in result["rows"])
    assert result["receipt"]["replayable"] is True
    assert result["receipt"]["coverage"]["completeness"] == "not_assessed"
    repeated = lake.import_legacy(batch_size=1)
    assert repeated["unchanged"] is True
    assert repeated["revision_id"] == report["revision_id"]


def test_materialization_preserves_original_research_evidence(lake):
    lake.import_legacy()
    result = lake.materialize_legacy()
    with sqlite3.connect(result["source_db"]) as connection:
        assert connection.execute("SELECT count(*),min(has_trade_price),max(pct_chg) FROM stock_daily_normalized").fetchone() == (3, 1, 2.0)
        assert connection.execute("SELECT name,raw_rows FROM securities").fetchone() == ("Synthetic test security", 3)
        assert connection.execute("SELECT count(*) FROM lake_bar_evidence WHERE available_at IS NULL AND legacy_import_id IS NOT NULL").fetchone()[0] == 3
    assert result["legacy_price_evidence_rows"] == 3
    assert result["receipt_dependencies"]["daily_bars"]["revision_id"] == result["revision_map"]["daily_bars"]
    assert len(result["receipts"]) == 2
    assert result["strict_history_eligible"] is False
    assert Path(result["source_db"]).with_suffix(".manifest.json").is_file()


def test_cancelled_import_does_not_publish(lake):
    with pytest.raises(DataOperationCancelled):
        lake.import_legacy(check_cancel=lambda: True)
    assert lake.state("daily_bars")["revision_id"] is None


def test_invalid_legacy_price_evidence_is_rejected(lake):
    with sqlite3.connect(lake.source_db) as connection:
        connection.execute("UPDATE stock_daily_normalized SET has_trade_price=0 WHERE date='2025-01-02'")
    with pytest.raises(ValueError, match="trade-price evidence"):
        lake.import_legacy()
    assert lake.state("daily_bars")["revision_id"] is None
    reports = [json.loads(path.read_text()) for path in lake._import_dir().glob("*.json")]
    assert reports[0]["status"] == "failed"


def test_changed_legacy_snapshot_cannot_reuse_evidence(lake):
    lake.import_legacy()
    with sqlite3.connect(lake.source_db) as connection:
        connection.execute("UPDATE stock_daily_normalized SET pct_chg=999")
    with pytest.raises(ValueError, match="evidence snapshot changed"):
        lake.materialize_legacy()


def _publish_new_source(lake):
    from cnequity.domain.contracts import contract_fingerprint
    from cnequity.domain.datasets import DATASETS
    from cnequity.storage.parquet import StagingWriter, compact_dataset
    from cnequity.storage.revisions import RevisionStore
    import polars as pl

    frame = lake.query("daily_bars", start="2025-01-01", end="2026-10-22").frame.with_columns(pl.lit("tdx_protocol").alias("source"))
    writer = StagingWriter(lake.config.staging_root)
    writer.write_batch("daily_bars", "synthetic-new-source", "001", frame)
    store = RevisionStore(lake.config.meta_root, lake.config.curated_root, lake.config.derived_root)
    base = store.ensure_current("daily_bars")
    changed = []
    compact_dataset(lake.config.staging_root, lake.config.curated_root, "daily_bars", "synthetic-new-source", changed_files=changed, base_root=base)
    return store.commit("daily_bars", run_id="synthetic-new-source", changed_files=changed, schema_version=DATASETS["daily_bars"].schema_version, contract_fingerprint=contract_fingerprint("daily_bars"))


def test_new_lake_source_is_not_certified_as_original_trade_price(lake):
    lake.import_legacy()
    _publish_new_source(lake)
    result = lake.materialize_legacy()
    with sqlite3.connect(result["source_db"]) as connection:
        rows = connection.execute("SELECT source,has_trade_price,pct_chg FROM stock_daily_normalized").fetchall()
    assert rows == [("tdx_protocol", 0, None)] * 3
    assert result["legacy_price_evidence_rows"] == 0


def test_materialization_uses_frozen_revision_during_concurrent_publication(lake, monkeypatch):
    imported = lake.import_legacy()
    actual_query = lake.query
    published = False

    def publish_then_read(dataset, **options):
        nonlocal published
        if not published:
            published = True
            monkeypatch.setattr(lake, "query", actual_query)
            _publish_new_source(lake)
        return actual_query(dataset, **options)

    monkeypatch.setattr(lake, "query", publish_then_read)
    result = lake.materialize_legacy()
    assert result["revision_map"]["daily_bars"] == imported["revision_id"]
    assert lake.state("daily_bars")["revision_id"] != imported["revision_id"]
    with sqlite3.connect(result["source_db"]) as connection:
        assert connection.execute("SELECT DISTINCT source,has_trade_price FROM stock_daily_normalized").fetchall() == [("sina_unadjusted_v1", 1)]


def test_outputs_cannot_overwrite_legacy_source(lake):
    with pytest.raises(ValueError, match="separate"):
        lake.materialize_legacy(lake.source_db)


def test_project_cne_config_resolves_relative_paths_from_project(lake, tmp_path, monkeypatch):
    config = lake.project_root / "configs" / "cnequity.toml"
    config.parent.mkdir()
    config.write_text('[data]\nroot="./var/lake"\n[orchestrator]\nworkers=3\n[sources.eastmoney]\nenabled=false\n[duckdb]\npath="{data.root}/duckdb/custom.duckdb"\n', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    configured = LakeService(lake.project_root, source_db=lake.source_db)
    assert configured.config.config_path == config
    assert configured.data_root == lake.project_root / "var" / "lake"
    assert configured.config.duckdb_path == configured.data_root / "duckdb" / "custom.duckdb"
    assert configured.config.workers == 3
    assert configured.config.sources["eastmoney"] is False


def test_project_cne_config_cannot_escape_private_var_directory(lake):
    config = lake.project_root / "configs" / "cnequity.toml"
    config.parent.mkdir()
    config.write_text('[data]\nroot="../outside"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="var directory"):
        LakeService(lake.project_root)


def test_materialization_can_cancel_while_waiting_for_input_lock(lake, monkeypatch):
    from filelock import FileLock

    lock_path = lake.var_root / "research_inputs" / "input.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = FileLock(str(lock_path))
    first_check = threading.Event()
    cancellation = threading.Event()
    finished = threading.Event()
    errors = []
    checks = 0

    def check_cancel():
        nonlocal checks
        checks += 1
        if checks == 1:
            first_check.set()
            return False
        return cancellation.is_set()

    def unexpected_materialization(*args, **kwargs):
        raise AssertionError("Materialization entered while another holder owns the input lock")

    def materialize():
        try:
            lake.materialize_legacy(check_cancel=check_cancel)
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    monkeypatch.setattr(lake, "_materialize_legacy", unexpected_materialization)
    worker = threading.Thread(target=materialize, daemon=True)
    holder.acquire(timeout=0)
    try:
        worker.start()
        assert first_check.wait(1), "Cancellation was not checked before waiting for the input lock"
        requested_at = time.monotonic()
        cancellation.set()
        assert finished.wait(1.5), "Cancellation remained blocked behind the held input lock"
        assert time.monotonic() - requested_at < 1.5
        assert len(errors) == 1
        assert isinstance(errors[0], DataOperationCancelled)
        assert holder.is_locked
        assert checks >= 2
    finally:
        cancellation.set()
        holder.release()
        worker.join(timeout=2)
    assert not worker.is_alive()
