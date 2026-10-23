from datetime import date

import polars as pl

from cnequity.config import Config
from cnequity.quality.audit import _index_bars_coverage_findings


def _write_calendar(root, rows):
    base = root / "curated" / "trading_calendar"
    for d, is_trading in rows:
        part = base / f"trade_date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"trade_date": [d], "is_trading": [is_trading]}).write_parquet(
            part / "part.parquet"
        )


def _write_index_bars(root, rows):
    base = root / "curated" / "index_bars"
    by_day: dict[date, list[str]] = {}
    for sym, d in rows:
        by_day.setdefault(d, []).append(sym)
    for d, syms in by_day.items():
        part = base / f"trade_date={d.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": syms, "trade_date": [d] * len(syms)}).write_parquet(
            part / "part.parquet"
        )


def test_coverage_clean_when_bars_match_calendar(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
    _write_calendar(cfg.data_root, [(d, True) for d in days])
    _write_index_bars(cfg.data_root, [("000300.SH", d) for d in days])

    assert _index_bars_coverage_findings(cfg, date(2024, 6, 5)) == []


def test_coverage_flags_missing_trading_day(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
    _write_calendar(cfg.data_root, [(d, True) for d in days])
    # Bar missing on the interior day 2024-06-04.
    _write_index_bars(cfg.data_root, [("000300.SH", days[0]), ("000300.SH", days[2])])

    findings = _index_bars_coverage_findings(cfg, date(2024, 6, 5))
    assert len(findings) == 1
    f = findings[0]
    assert f["check"] == "index_bars_calendar_coverage"
    assert f["symbol"] == "000300.SH"
    assert f["missing_count"] == 1
    assert f["missing_sample"] == ["2024-06-04"]
    assert f["orphan_count"] == 0
    assert f["known_source_gap_count"] == 0
    assert f["unexpected_missing_count"] == 1
    assert f["source_limited"] is False


def test_coverage_uses_canonical_calendar_rows(tmp_path):
    """A superseded calendar row must not turn a closed day into a session."""
    cfg = Config(data_root=tmp_path / "data")
    days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
    _write_calendar(cfg.data_root, [(days[0], True), (days[2], True)])
    calendar_dir = cfg.curated_root / "trading_calendar" / f"trade_date={days[1].isoformat()}"
    calendar_dir.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(
        {
            "trade_date": [days[1]],
            "is_trading": [True],
            "source": ["seed"],
            "data_version": ["v1"],
            "fetched_at": ["2024-06-04T07:00:00+00:00"],
        }
    ).write_parquet(calendar_dir / "part-old.parquet")
    pl.DataFrame(
        {
            "trade_date": [days[1]],
            "is_trading": [False],
            "source": ["exchange"],
            "data_version": ["v1"],
            "fetched_at": ["2024-06-04T08:00:00+00:00"],
        }
    ).write_parquet(calendar_dir / "part-new.parquet")
    _write_index_bars(cfg.data_root, [("000300.SH", d) for d in days if d != days[1]])

    assert _index_bars_coverage_findings(cfg, days[2]) == []


def test_coverage_classifies_verified_399001_source_gaps(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    days = [date(1991, 9, 27), date(1991, 9, 30), date(1991, 10, 1)]
    _write_calendar(cfg.data_root, [(d, True) for d in days])
    # 1991-09-30 is a verified common gap in both historical sources.
    _write_index_bars(
        cfg.data_root,
        [("399001.SZ", days[0]), ("399001.SZ", days[2])],
    )

    findings = _index_bars_coverage_findings(cfg, date(1991, 10, 1))

    assert len(findings) == 1
    finding = findings[0]
    assert finding["severity"] == "info"
    assert finding["known_source_gap_count"] == 1
    assert finding["unexpected_missing_count"] == 0
    assert finding["source_limited"] is True


def test_coverage_keeps_new_gap_as_warning_alongside_known_gap(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    days = [
        date(1991, 9, 27),
        date(1991, 9, 30),
        date(1991, 10, 1),
        date(1991, 10, 2),
    ]
    _write_calendar(cfg.data_root, [(d, True) for d in days])
    _write_index_bars(
        cfg.data_root,
        [("399001.SZ", days[0]), ("399001.SZ", days[3])],
    )

    findings = _index_bars_coverage_findings(cfg, date(1991, 10, 2))

    assert len(findings) == 1
    finding = findings[0]
    assert finding["severity"] == "warning"
    assert finding["known_source_gap_count"] == 1
    assert finding["unexpected_missing_count"] == 1
    assert finding["source_limited"] is False


def test_coverage_flags_orphan_bar_on_non_trading_day(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    _write_calendar(
        cfg.data_root,
        [(date(2024, 6, 3), True), (date(2024, 6, 4), False), (date(2024, 6, 5), True)],
    )
    # Bar exists on 2024-06-04 which the calendar marks non-trading.
    _write_index_bars(
        cfg.data_root,
        [
            ("000300.SH", date(2024, 6, 3)),
            ("000300.SH", date(2024, 6, 4)),
            ("000300.SH", date(2024, 6, 5)),
        ],
    )

    findings = _index_bars_coverage_findings(cfg, date(2024, 6, 5))
    assert len(findings) == 1
    assert findings[0]["orphan_count"] == 1
    assert findings[0]["orphan_sample"] == ["2024-06-04"]
    assert findings[0]["missing_count"] == 0


def test_coverage_empty_lake_no_findings(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    assert _index_bars_coverage_findings(cfg, date(2024, 6, 5)) == []
