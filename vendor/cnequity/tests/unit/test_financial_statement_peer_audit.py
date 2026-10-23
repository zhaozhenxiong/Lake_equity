from datetime import date, datetime, timezone

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.quality.cross_checks import financial_statement_peer_findings
from cnequity.storage.source_snapshots import SnapshotStore


def _row(value, *, announced=date(2025, 4, 20), statement="income"):
    return {
        "symbol": "600519.SH",
        "report_period": "2024Q4",
        "statement_type": statement,
        "item_code": "net_profit",
        "item_value": float(value),
        "announce_date": announced,
        "source": "eastmoney",
        "data_version": "v1",
        "fetched_at": datetime(2025, 9, 1, tzinfo=timezone.utc),
    }


def _findings(tmp_path, primary, peer):
    cfg = Config(data_root=tmp_path)
    root = cfg.curated_root / "financial_statement_items" / "report_period=2024Q4"
    root.mkdir(parents=True)
    pl.DataFrame(primary).write_parquet(root / "part.parquet")
    SnapshotStore(cfg.meta_root).write(
        "financial_statement_items",
        pl.DataFrame(peer).with_columns(pl.lit("ths_official").alias("source")),
        source="ths_official",
        data_version="v1",
        run_id="test",
    )
    return financial_statement_peer_findings(cfg)


@pytest.mark.parametrize("value", [10.0, -10.0])
def test_zero_primary_does_not_hide_nonzero_peer(tmp_path, value):
    (finding,) = _findings(tmp_path, [_row(0)], [_row(value)])
    assert finding["disagreed"] == 1
    assert finding["compared"] == 1


def test_two_zero_values_agree(tmp_path):
    assert _findings(tmp_path, [_row(0)], [_row(0)]) == []


@pytest.mark.parametrize("reverse", [False, True])
def test_peer_latest_disclosure_is_independent_of_file_order(tmp_path, reverse):
    peer = [_row(100), _row(120, announced=date(2025, 8, 30))]
    if reverse:
        peer.reverse()
    assert _findings(tmp_path, [_row(120, announced=date(2025, 8, 30))], peer) == []


def test_statement_types_are_not_joined_together(tmp_path):
    assert _findings(tmp_path, [_row(100)], [_row(200, statement="cashflow")]) == []


def _revenue(value):
    return {**_row(value), "item_code": "revenue"}


def test_peer_revenue_below_the_lakes_total_is_not_a_disagreement(tmp_path):
    # The lake's revenue is 营业总收入; the peer's 营业收入 leaves out a finance
    # subsidiary's interest income, so it can only be the smaller figure.
    assert _findings(tmp_path, [_revenue(100)], [_revenue(90)]) == []


def test_peer_revenue_above_the_lakes_total_still_disagrees(tmp_path):
    (finding,) = _findings(tmp_path, [_revenue(100)], [_revenue(110)])
    assert finding["disagreed"] == 1
