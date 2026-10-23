"""Diagnostics describe the selected revision without becoming a read gate."""

from datetime import date

import polars as pl

from cnequity.config import Config
from cnequity.quality.factor_diagnostics import _path, factor_read_quality
from cnequity.query.parquet_scan import _scan_parquet_paths
from cnequity.storage.atomic import write_json_atomic


def test_read_diagnostics_do_not_borrow_from_a_newer_factor_revision(tmp_path):
    cfg = Config(data_root=tmp_path / "lake")
    old, new = tmp_path / "revision-old", tmp_path / "revision-new"
    bars = pl.DataFrame(
        {"symbol": ["600001.SH"], "trade_date": [date(2024, 1, 8)], "adj_is_exact": [True]}
    )
    write_json_atomic(
        _path(cfg, new),
        {
            "factor_root": str(new),
            "scope": None,
            "issues": [
                {"symbol": "600001.SH", "ex_date": "2024-01-05", "kind": "action_without_step"}
            ],
        },
    )
    assert factor_read_quality(cfg, bars, factor_root=old)["diagnostics_assessed"] is False
    quality = factor_read_quality(cfg, bars, factor_root=new)
    assert quality["related_event_count"] == 1
    assert quality["historical_accuracy"] == "not_certified"
    assert quality["missing_factor_rows"] == 0 and quality["gate_impact"] == "none"


def test_mixed_schema_grouping_preserves_file_order_and_optional_columns(tmp_path):
    paths = []
    for i, extra in enumerate([False, False, True, True, False]):
        path = tmp_path / f"{i}.parquet"
        frame = pl.DataFrame({"ordinal": [i], "value": [float(i)]})
        if extra:
            frame = frame.with_columns(pl.lit(10.0).alias("optional"))
        frame.write_parquet(path)
        paths.append(path)
    result = _scan_parquet_paths(paths, hive=False).collect()
    assert result["ordinal"].to_list() == list(range(5))
    assert result["optional"].to_list() == [None, None, 10.0, 10.0, None]


def test_malformed_optional_diagnostics_do_not_block_valid_adjusted_rows(tmp_path):
    cfg = Config(data_root=tmp_path / "lake")
    root = tmp_path / "factor-revision"
    bars = pl.DataFrame(
        {"symbol": ["600001.SH"], "trade_date": [date(2024, 1, 8)], "adj_is_exact": [True]}
    )
    for payload in (
        [],
        {"factor_root": str(root), "issues": [1]},
        {
            "factor_root": str(root),
            "issues": [{"symbol": "600001.SH", "kind": "action_without_step", "ex_date": "broken"}],
        },
    ):
        write_json_atomic(_path(cfg, root), payload)
        quality = factor_read_quality(cfg, bars, factor_root=root)
        assert quality["diagnostics_assessed"] is False
        assert quality["gate_impact"] == "none"


def test_source_summary_counts_only_the_hfq_factors_used_by_reads(tmp_path):
    cfg = Config(data_root=tmp_path / "lake")
    bars = pl.DataFrame(
        {"symbol": ["600001.SH"], "trade_date": [date(2024, 1, 8)], "adj_is_exact": [True]}
    )
    factors = pl.DataFrame(
        {
            "symbol": ["600001.SH"] * 2,
            "trade_date": [date(2024, 1, 8)] * 2,
            "adjust_type": ["hfq", "qfq"],
            "source": ["sina", "baostock"],
        }
    )
    assert factor_read_quality(cfg, bars, factor_root=tmp_path / "missing", factors=factors)[
        "factor_sources"
    ] == {"sina": 1}
