from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.query import load
from cnequity.storage.repairs.layout import LayoutRepairError, repair_layout
from cnequity.storage.revisions import RevisionStore


def _bar(symbol: str, day: date, *, amount, oi_change, close=100.0, fetched="2026-09-26") -> dict:
    return {
        "symbol": symbol,
        "exchange": "SHF",
        "exchange_code": symbol.split(".")[0],
        "product": "AG",
        "trade_date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "settle": close,
        "pre_settle": close,
        "volume": 10,
        "amount": amount,
        "open_interest": 100,
        "oi_change": oi_change,
        "source": "futures_exchange",
        "data_version": "v1",
        "fetched_at": datetime.fromisoformat(fetched).replace(tzinfo=timezone.utc),
    }


def _write(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows).write_parquet(path)
    return path


@pytest.fixture
def lake(tmp_path):
    """A committed futures lake: month partition beside a root file, as in 2026-09."""
    cfg = Config(data_root=tmp_path)
    root = cfg.curated_root / "futures_bars"
    day1, day2, day3 = date(2015, 1, 5), date(2015, 1, 6), date(2015, 2, 2)
    _write(
        root / "trade_date=2015-01/part-merged.parquet",
        [
            _bar("AG1501.SHF", day1, amount=None, oi_change=94),
            _bar("AG1502.SHF", day1, amount=None, oi_change=5, close=101.0),
        ],
    )
    _write(
        root / "merged.parquet",
        [
            _bar("AG1501.SHF", day1, amount=3.4e7, oi_change=None, fetched="2026-09-27"),
            # A real disagreement: prices differ, so nothing is mixed.
            _bar("AG1502.SHF", day1, amount=9.0, oi_change=None, close=102.0, fetched="2026-09-27"),
            _bar("AG1501.SHF", day2, amount=8.5e7, oi_change=-134, fetched="2026-09-27"),
            _bar("AG1501.SHF", day3, amount=1.0e7, oi_change=3, fetched="2026-09-27"),
        ],
    )
    # Adopt the legacy layout as it is (revision zero), like a real old lake.
    RevisionStore(cfg.meta_root, cfg.curated_root).ensure_current("futures_bars")
    return cfg


def test_plan_reports_without_writing(lake):
    report = repair_layout(lake, "futures_bars")
    assert report["applied"] is False
    assert report["misplaced_files"] == ["merged.parquet"]
    assert report["duplicate_keys"] == 2
    assert report["coalesced_keys"] == 1
    assert report["conflicting_keys"] == 1
    assert report["filled"] == {"oi_change": 1}
    assert (lake.curated_root / "futures_bars/merged.parquet").exists()


def test_apply_moves_rows_fills_complementary_fields_and_keeps_evidence(lake):
    report = repair_layout(lake, "futures_bars", apply=True)
    assert report["applied"] is True
    assert report["rows_in"] - report["rows_out"] == 2

    bars = load("futures_bars", config=lake).sort("symbol", "trade_date")
    assert bars.height == 4
    first = bars.filter(
        (pl.col("symbol") == "AG1501.SHF") & (pl.col("trade_date") == date(2015, 1, 5))
    )
    assert first["amount"].item() == pytest.approx(3.4e7)
    assert first["oi_change"].item() == 94
    conflict = bars.filter(pl.col("symbol") == "AG1502.SHF")
    # Canonical (latest) row whole; its null oi_change is not taken from a
    # copy that disagrees on price.
    assert conflict["close"].item() == 102.0
    assert conflict["oi_change"].item() is None

    store = RevisionStore(lake.meta_root, lake.curated_root)
    published = sorted(
        p.relative_to(store.current_root("futures_bars")).as_posix()
        for p in store.current_root("futures_bars").rglob("*.parquet")
    )
    assert published == [
        "trade_date=2015-01/part-merged.parquet",
        "trade_date=2015-02/part-merged.parquet",
    ]
    evidence = pl.read_parquet(Path(report["evidence"]) / "duplicate-observations.parquet")
    assert evidence.height == 4
    assert repair_layout(lake, "futures_bars")["misplaced_files"] == []


def test_point_in_time_datasets_are_refused(lake):
    with pytest.raises(LayoutRepairError, match="point-in-time"):
        repair_layout(lake, "financial_statement_items")


def test_a_root_only_layout_is_split_into_partitions(tmp_path):
    cfg = Config(data_root=tmp_path)
    _write(
        cfg.curated_root / "futures_bars/merged.parquet",
        [
            _bar("AG1501.SHF", date(2015, 1, 5), amount=1.0, oi_change=1),
            _bar("AG1501.SHF", date(2015, 2, 2), amount=2.0, oi_change=2),
        ],
    )
    RevisionStore(cfg.meta_root, cfg.curated_root).ensure_current("futures_bars")
    report = repair_layout(cfg, "futures_bars", apply=True)
    assert report["misplaced_files"] == ["merged.parquet"]
    assert report["partitions"] == 2
    assert report["duplicate_keys"] == 0
    assert load("futures_bars", config=cfg).height == 2
