"""Regression tests for source/request-bound raw archive receipts."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.steps.http_common import verify_raw_archive, write_fetched
from cnequity.storage.raw_archive import (
    RawArchiveError,
    RawPayloadArchive,
    begin_capture,
)


def _capture(
    config: Config,
    *,
    dataset: str,
    source: str,
    scope: str,
    run_id: str = "run-1",
):
    nonce = begin_capture(config, dataset, run_id, source=source, request_scope=scope)
    archive = RawPayloadArchive(
        config.meta_root,
        capture_owner=config,
        capture_run_id=run_id,
        capture_source=source,
        capture_scope=scope,
        capture_nonce=nonce,
        datasets=[dataset],
        compression="none",
    )
    record = archive.archive(
        dataset,
        f"{source}:{scope}".encode(),
        source=source,
        run_id=run_id,
        request_scope=scope,
        observation_id=f"{run_id}:{source}:{scope}",
        payload_format="bytes",
        http_metadata={"wire_exact": True},
    )
    assert record is not None
    return record


@pytest.mark.parametrize(
    ("written_source", "requested_source"),
    [
        ("tdx_protocol", "eastmoney"),
        ("tdx_protocol", "baostock"),
        ("tdx_protocol", "ths"),
        ("tdx_protocol", "eastmoney_migrated_bj"),
        ("baostock", "tdx_protocol"),
        ("ths", "tdx_protocol"),
        ("eastmoney_migrated_bj", "tdx_protocol"),
        ("cninfo", "cninfo"),
    ],
)
def test_receipt_cannot_borrow_another_source_or_range(
    tmp_path, written_source: str, requested_source: str
):
    config = Config(data_root=tmp_path / "lake")
    dataset = "announcement_index" if written_source == "cninfo" else "corporate_actions"
    old_scope = (
        "range:announcement:2024-01-01:2024-01-02"
        if written_source == "cninfo"
        else "chunk:batch-0-chunk-0000"
    )
    new_scope = (
        "range:announcement:2024-01-03:2024-01-04"
        if written_source == "cninfo"
        else "chunk:batch-0-chunk-0001"
    )
    _capture(
        config,
        dataset=dataset,
        source=written_source,
        scope=old_scope,
    )

    with pytest.raises(RawArchiveError, match="(no exact wire observation|capture is not active)"):
        verify_raw_archive(
            config,
            dataset,
            "run-1",
            source=requested_source,
            request_scope=new_scope,
        )


def test_publish_rejects_tdx_receipt_for_eastmoney_without_staging(tmp_path):
    config = Config(data_root=tmp_path / "lake")
    evidence = verify_raw_archive(
        config,
        "corporate_actions",
        "run-1",
        source="tdx_protocol",
        request_scope="daily:2024-01-02",
        records=[
            _capture(
                config,
                dataset="corporate_actions",
                source="tdx_protocol",
                scope="daily:2024-01-02",
            )
        ],
    )
    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "ex_date": [date(2024, 1, 2)],
            "action_type": ["cash_dividend"],
            "cash_dividend": [1.0],
            "bonus_ratio": [None],
            "transfer_ratio": [None],
            "allotment_ratio": [None],
            "allotment_price": [None],
        }
    )
    with pytest.raises(RawArchiveError, match="does not match source/scope/run"):
        write_fetched(
            config,
            "run-1",
            "corporate_actions",
            frame,
            source="eastmoney",
            raw_archive_evidence=evidence,
        )
    assert not (config.staging_root / "corporate_actions").exists()


def test_verified_receipt_contains_scope_and_observation_identity(tmp_path):
    config = Config(data_root=tmp_path / "lake")
    scope = "range:announcement:2024-01-01:2024-01-02"
    record = _capture(
        config,
        dataset="announcement_index",
        source="cninfo",
        scope=scope,
    )
    evidence = verify_raw_archive(
        config,
        "announcement_index",
        "run-1",
        source="cninfo",
        request_scope=scope,
    )
    assert evidence.source == "cninfo"
    assert evidence.request_scope == scope
    assert evidence.observation_ids == (record.observation_id,)
    assert evidence.record_keys == ((record.metadata_path, record.payload_sha256),)


def test_old_receipt_is_rejected_after_capture_is_replaced_without_staging(tmp_path):
    config = Config(data_root=tmp_path / "lake")
    scope = "daily:2024-01-02"
    _capture(config, dataset="corporate_actions", source="tdx_protocol", scope=scope)
    evidence = verify_raw_archive(
        config,
        "corporate_actions",
        "run-1",
        source="tdx_protocol",
        request_scope=scope,
    )

    replacement_nonce = begin_capture(
        config,
        "corporate_actions",
        "run-1",
        source="tdx_protocol",
        request_scope=scope,
    )
    assert replacement_nonce != evidence.capture_nonce
    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "ex_date": [date(2024, 1, 2)],
            "action_type": ["cash_dividend"],
            "cash_dividend": [1.0],
            "bonus_ratio": [None],
            "transfer_ratio": [None],
            "allotment_ratio": [None],
            "allotment_price": [None],
        }
    )
    with pytest.raises(RawArchiveError, match="capture is no longer active"):
        write_fetched(
            config,
            "run-1",
            "corporate_actions",
            frame,
            source="tdx_protocol",
            raw_archive_evidence=evidence,
        )
    assert not (config.staging_root / "corporate_actions").exists()


def test_supplied_old_record_cannot_bypass_current_capture(tmp_path):
    config = Config(data_root=tmp_path / "lake")
    scope = "chunk:batch-0-chunk-0000"
    record = _capture(
        config,
        dataset="corporate_actions",
        source="tdx_protocol",
        scope=scope,
    )
    begin_capture(
        config,
        "corporate_actions",
        "run-1",
        source="tdx_protocol",
        request_scope=scope,
    )
    with pytest.raises(RawArchiveError, match="not from the active capture"):
        verify_raw_archive(
            config,
            "corporate_actions",
            "run-1",
            source="tdx_protocol",
            request_scope=scope,
            records=[record],
        )


def test_current_receipt_publishes_once_and_replay_is_rejected(tmp_path):
    config = Config(data_root=tmp_path / "lake")
    scope = "daily:2024-01-02"
    _capture(config, dataset="corporate_actions", source="tdx_protocol", scope=scope)
    evidence = verify_raw_archive(
        config,
        "corporate_actions",
        "run-1",
        source="tdx_protocol",
        request_scope=scope,
    )
    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "ex_date": [date(2024, 1, 2)],
            "action_type": ["cash_dividend"],
            "cash_dividend": [1.0],
            "bonus_ratio": [None],
            "transfer_ratio": [None],
            "allotment_ratio": [None],
            "allotment_price": [None],
        }
    )
    result = write_fetched(
        config,
        "run-1",
        "corporate_actions",
        frame,
        source="tdx_protocol",
        raw_archive_evidence=evidence,
    )
    assert result["rows_written"] == 1
    staged = list((config.staging_root / "corporate_actions").rglob("*.parquet"))
    assert len(staged) == 1
    with pytest.raises(RawArchiveError, match="already consumed"):
        write_fetched(
            config,
            "run-1",
            "corporate_actions",
            frame,
            source="tdx_protocol",
            raw_archive_evidence=evidence,
        )
    assert staged == list((config.staging_root / "corporate_actions").rglob("*.parquet"))


def test_archive_disabled_keeps_write_fetched_compatibility(tmp_path):
    config = Config(data_root=tmp_path / "lake", raw_archive_enabled=False)
    frame = pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "ex_date": [date(2024, 1, 2)],
            "action_type": ["cash_dividend"],
            "cash_dividend": [1.0],
            "bonus_ratio": [None],
            "transfer_ratio": [None],
            "allotment_ratio": [None],
            "allotment_price": [None],
        }
    )
    result = write_fetched(
        config,
        "run-1",
        "corporate_actions",
        frame,
        source="tdx_protocol",
    )
    assert result["rows_written"] == 1
    assert list((config.staging_root / "corporate_actions").rglob("*.parquet"))


def test_pdf_payloads_are_stored_uncompressed_unless_a_gzip_copy_exists(tmp_path):
    import gzip
    import hashlib
    from datetime import datetime, timezone

    archive = RawPayloadArchive(tmp_path / "meta")
    stamp = datetime(2026, 10, 17, tzinfo=timezone.utc)
    pdf = b"%PDF-1.4 report body"
    record = archive.archive(
        "financial_statement_items", pdf, source="cninfo", payload_format="pdf", captured_at=stamp
    )
    assert record.compression == "none" and record.payload_path.endswith(".bin")
    assert archive.read(record) == pdf
    text = archive.archive(
        "financial_statement_items",
        b"{}",
        source="cninfo",
        payload_format="json",
        captured_at=stamp,
    )
    assert text.compression == "gzip"

    # An observation archived before this change keeps its gzip form.
    older = b"%PDF-1.4 archived earlier"
    legacy = RawPayloadArchive(tmp_path / "legacy")
    base = (
        tmp_path
        / "legacy"
        / "raw"
        / "financial_statement_items"
        / "source=cninfo"
        / "captured_date=2026-10-17"
    )
    base.mkdir(parents=True)
    (base / f"{hashlib.sha256(older).hexdigest()}.gz").write_bytes(gzip.compress(older, mtime=0))
    again = legacy.archive(
        "financial_statement_items", older, source="cninfo", payload_format="pdf", captured_at=stamp
    )
    assert again.compression == "gzip" and legacy.read(again) == older
