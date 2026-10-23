"""daily_bars.volume is 股 from every adapter, and the audit says so if it isn't."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from cnequity.adapters.baostock.delisted_bars import _fetch_one
from cnequity.adapters.eastmoney import bars as em_bars
from cnequity.adapters.sina import bars as sina_bars
from cnequity.adapters.tdx_protocol.bars import _parse_bar_rows
from cnequity.adapters.ths.stock_bars import _parse_stock_kline
from cnequity.domain.schemas import data_version_for
from cnequity.domain.units import SHARES_PER_LOT, lots_to_shares
from cnequity.quality.unit_checks import (
    UNIT_CHECK_MIN_ROWS,
    daily_bars_amount_completeness_findings,
    daily_bars_volume_unit_findings,
)

# One bar, priced so amount = close × shares exactly. 40,000 shares = 400 手
# at 12.5 元 turns over 500,000 元 — the invariant the whole check rests on.
CLOSE = 12.5
SHARES = 40_000
LOTS = SHARES // SHARES_PER_LOT
AMOUNT = CLOSE * SHARES


def _ratio(row: dict) -> float:
    return row["amount"] / row["close"] / row["volume"]


# --- the conversion itself ---------------------------------------------------


def test_lots_to_shares_scales_and_handles_none():
    assert lots_to_shares(LOTS) == SHARES
    assert lots_to_shares(0) == 0
    assert lots_to_shares(None) == 0


# --- per-adapter: every path lands on 股 -------------------------------------


def test_tdx_daily_bars_convert_lots_to_shares():
    pdf = pl.DataFrame(
        [
            {
                "datetime": date(2024, 6, 28),
                "open": CLOSE,
                "high": CLOSE,
                "low": CLOSE,
                "close": CLOSE,
                "volume": LOTS,
                "amount": AMOUNT,
            }
        ]
    )
    row = _parse_bar_rows(pdf, "600519.SH", date(2024, 6, 1), date(2024, 6, 30))[0]
    assert row["volume"] == SHARES
    assert _ratio(row) == pytest.approx(1.0)


def test_tdx_index_bars_keep_the_vendor_unit():
    """index_bars is a separate contract whose unit is not pinned down; the
    daily conversion must not leak onto it."""
    pdf = pl.DataFrame(
        [
            {
                "datetime": date(2024, 6, 28),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": LOTS,
                "amount": AMOUNT,
            }
        ]
    )
    row = _parse_bar_rows(
        pdf, "000001.SH", date(2024, 6, 1), date(2024, 6, 30), volume_in_lots=False
    )[0]
    assert row["volume"] == LOTS


@pytest.mark.parametrize("volume", [0.0, 2**-127])
@pytest.mark.parametrize("amount", [0.0, 201.025])
def test_tdx_lot_rounded_zero_with_turnover_needs_another_source(volume, amount):
    pdf = pl.DataFrame(
        [
            {
                "datetime": date(2026, 9, 8),
                "open": 2.04,
                "high": 2.053,
                "low": 2.04,
                "close": 2.053,
                "volume": volume,
                "amount": amount,
            }
        ]
    )
    rows = _parse_bar_rows(pdf, "160806.SZ", date(2026, 9, 8), date(2026, 9, 8))
    if amount > 0:
        assert rows == []
    else:
        assert rows[0]["volume"] == 0
        assert rows[0]["amount"] == 0


@pytest.mark.parametrize("amount", ["0", "-", "201.025"])
def test_eastmoney_zero_lots_only_proves_no_trade_without_positive_turnover(amount, monkeypatch):
    class Client:
        def get(self, *args, **kwargs):
            class Response:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"data": {"klines": [f"20260908,2.04,2.053,2.053,2.04,0,{amount}"]}}

            return Response()

    monkeypatch.setattr(em_bars, "fetch_clist_pages", lambda client, fields: [{}])
    monkeypatch.setattr(
        em_bars,
        "clist_rows_to_symbols",
        lambda rows: [
            (
                "160806.SZ",
                {"f17": 2.04, "f15": 2.053, "f16": 2.04, "f2": 2.053, "f5": 0, "f6": amount},
            )
        ],
    )
    on = date(2026, 9, 8)
    for frame in (
        em_bars.fetch_daily_bars(["160806.SZ"], on, on, client=Client()),
        em_bars.fetch_daily_bars_clist(on, client=Client()),
    ):
        if amount == "201.025":
            assert frame.is_empty()
        else:
            assert frame["volume"].to_list() == [0]
            assert frame["amount"].to_list() == [None if amount == "-" else 0]


def test_eastmoney_kline_converts_lots_to_shares():
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {"klines": [f"20240628,{CLOSE},{CLOSE},{CLOSE},{CLOSE},{LOTS},{AMOUNT}"]}
            }

    class _Client:
        def get(self, url, params=None):
            return _Resp()

        def close(self):
            return None

    df = em_bars.fetch_daily_bars(
        ["600519.SH"], date(2024, 6, 1), date(2024, 6, 30), client=_Client()
    )
    row = df.row(0, named=True)
    assert row["volume"] == SHARES
    assert _ratio(row) == pytest.approx(1.0)


def test_eastmoney_clist_converts_lots_to_shares(monkeypatch):
    monkeypatch.setattr(em_bars, "fetch_clist_pages", lambda client, fields: [{}])
    monkeypatch.setattr(
        em_bars,
        "clist_rows_to_symbols",
        lambda rows: [
            (
                "600519.SH",
                {"f17": CLOSE, "f15": CLOSE, "f16": CLOSE, "f2": CLOSE, "f5": LOTS, "f6": AMOUNT},
            )
        ],
    )

    class _Client:
        def close(self):
            return None

    df = em_bars.fetch_daily_bars_clist(date(2024, 6, 28), client=_Client())
    row = df.row(0, named=True)
    assert row["volume"] == SHARES
    assert _ratio(row) == pytest.approx(1.0)


def test_sina_passes_shares_through(monkeypatch):
    """Sina reports 股 natively — converting here is what broke it before."""
    monkeypatch.setattr(
        sina_bars,
        "_request",
        lambda symbol, datalen, client: [
            {
                "day": "2024-06-28",
                "open": CLOSE,
                "high": CLOSE,
                "low": CLOSE,
                "close": CLOSE,
                "volume": SHARES,
            }
        ],
    )
    df = sina_bars.fetch_daily_bars_sina("600519.SH")
    assert df["volume"][0] == SHARES


def test_ths_passes_shares_through():
    payload = {"data": f"20150105,{CLOSE},{CLOSE},{CLOSE},{CLOSE},{SHARES},{AMOUNT}"}
    row = _parse_stock_kline(payload, "600519.SH")[0]
    assert row["volume"] == SHARES
    assert _ratio(row) == pytest.approx(1.0)


def test_baostock_passes_shares_through():
    class _Rs:
        error_code = "0"

        def __init__(self):
            self._left = 1

        def next(self):
            self._left -= 1
            return self._left >= 0

        def get_row_data(self):
            return ["2020-06-30", CLOSE, CLOSE, CLOSE, CLOSE, SHARES, AMOUNT, "1"]

    class _Bs:
        def query_history_k_data_plus(self, *a, **k):
            return _Rs()

    row = _fetch_one(_Bs(), "600519.SH", date(2020, 6, 1), date(2020, 6, 30))[0]
    assert row["volume"] == SHARES
    assert _ratio(row) == pytest.approx(1.0)


# --- data_version ------------------------------------------------------------


def test_daily_bars_is_on_v2_and_other_datasets_are_not():
    assert data_version_for("daily_bars") == "v2"
    assert data_version_for("index_bars") == "v1"
    assert data_version_for("anything_else") == "v1"


@pytest.mark.parametrize("execution", ["serial", "spawned-worker"])
def test_worker_output_stamps_the_dataset_version(config, execution):
    """Both normalization paths must write the volume contract into Parquet."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor

    from cnequity.orchestrator import worker_pool

    day = date(2024, 6, 28)
    symbols = ["600519.SH"]
    assert config.tdx_allow_mock  # The child must never contact a live provider.
    if execution == "serial":
        result = worker_pool.fetch_daily_bars_parallel(config, symbols, day, day, "version-test")
        assert not result["had_error"]
    else:
        args = (
            symbols,
            day.isoformat(),
            day.isoformat(),
            str(config.staging_root),
            "daily_bars",
            "version-test",
            "batch-0",
            None,
            True,
            None,
            False,
            str(config.config_path),
        )
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ) as pool:
            result = pool.submit(worker_pool._worker_fetch_batch, args).result(timeout=30)
    assert result["rows_written"] == 1
    files = list(config.staging_root.glob("daily_bars/**/*.parquet"))
    assert files
    frame = pl.read_parquet(files)
    assert frame["symbol"].to_list() == symbols
    assert frame["data_version"].to_list() == ["v2"]
    assert frame["source"].to_list() == ["mock"]


# --- the audit check ---------------------------------------------------------


def _write_bars(config, rows: list[dict]) -> None:
    root = config.curated_root / "daily_bars"
    by_day: dict[date, list[dict]] = {}
    for row in rows:
        by_day.setdefault(row["trade_date"], []).append(row)
    for day, day_rows in by_day.items():
        part = root / f"trade_date={day.isoformat()}"
        part.mkdir(parents=True, exist_ok=True)
        pl.DataFrame(day_rows).write_parquet(part / "part-0.parquet")


def _rows(
    source: str,
    *,
    volume: int,
    n: int,
    anchor: date,
    symbol_start: int = 600000,
) -> list[dict]:
    """*n* rows spread over the days before *anchor*, all with the same unit."""
    return [
        {
            "symbol": f"{symbol_start + i}.SH",
            "trade_date": anchor - timedelta(days=i % 5),
            "close": CLOSE,
            "volume": volume,
            "amount": AMOUNT,
            "source": source,
            "data_version": "v2",
        }
        for i in range(n)
    ]


def test_no_finding_when_every_source_is_in_shares(config):
    anchor = date(2024, 6, 28)
    _write_bars(
        config,
        _rows(
            "tdx_protocol",
            volume=SHARES,
            n=UNIT_CHECK_MIN_ROWS + 10,
            anchor=anchor,
            symbol_start=600000,
        )
        + _rows(
            "ths",
            volume=SHARES,
            n=UNIT_CHECK_MIN_ROWS + 10,
            anchor=anchor,
            symbol_start=601000,
        ),
    )
    assert daily_bars_volume_unit_findings(config, anchor) == []


def test_flags_the_source_that_wrote_lots(config):
    anchor = date(2024, 6, 28)
    _write_bars(
        config,
        _rows(
            "tdx_protocol",
            volume=LOTS,
            n=UNIT_CHECK_MIN_ROWS + 10,
            anchor=anchor,
            symbol_start=600000,
        )
        + _rows(
            "ths",
            volume=SHARES,
            n=UNIT_CHECK_MIN_ROWS + 10,
            anchor=anchor,
            symbol_start=601000,
        ),
    )
    findings = daily_bars_volume_unit_findings(config, anchor)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["source"] == "tdx_protocol"
    assert finding["severity"] == "error"
    assert finding["check"] == "daily_bars_volume_unit"
    assert finding["median_ratio"] == pytest.approx(SHARES_PER_LOT)
    assert "手" in finding["message"]


def test_unit_check_uses_newest_canonical_bar_on_conflicting_retry(config):
    anchor = date(2024, 6, 28)
    rows: list[dict] = []
    for i in range(UNIT_CHECK_MIN_ROWS + 10):
        symbol = f"{600000 + i}.SH"
        day = anchor - timedelta(days=i % 5)
        base = {
            "symbol": symbol,
            "trade_date": day,
            "close": CLOSE,
            "amount": AMOUNT,
            "source": "tdx_protocol",
            "data_version": "v2",
        }
        rows.extend(
            [
                {
                    **base,
                    "volume": LOTS,
                    "fetched_at": "2024-06-28T07:00:00+00:00",
                },
                {
                    **base,
                    "volume": SHARES,
                    "fetched_at": "2024-06-28T08:00:00+00:00",
                },
            ]
        )
    _write_bars(config, rows)

    assert daily_bars_volume_unit_findings(config, anchor) == []


def test_thin_sources_are_not_judged(config):
    """A gap-fill contributing a handful of rows is noise, not evidence."""
    anchor = date(2024, 6, 28)
    _write_bars(
        config,
        _rows("eastmoney", volume=LOTS, n=UNIT_CHECK_MIN_ROWS - 1, anchor=anchor),
    )
    assert daily_bars_volume_unit_findings(config, anchor) == []


def test_amount_completeness_surfaces_sina_null_turnover(config):
    anchor = date(2024, 6, 28)
    rows = _rows("sina", volume=SHARES, n=UNIT_CHECK_MIN_ROWS, anchor=anchor)
    for row in rows:
        row["amount"] = None
    _write_bars(config, rows)

    findings = daily_bars_amount_completeness_findings(config, anchor)
    assert len(findings) == 1
    assert findings[0]["check"] == "daily_bars_amount_completeness"
    assert findings[0]["source"] == "sina"
    assert findings[0]["severity"] == "info"
    assert findings[0]["expected_missing"] is True
    assert findings[0]["missing_amount"] == UNIT_CHECK_MIN_ROWS


def test_rows_without_amount_are_skipped_not_flagged(config):
    """Sina serves no amount — unmeasurable must not read as broken."""
    anchor = date(2024, 6, 28)
    rows = _rows("sina", volume=SHARES, n=UNIT_CHECK_MIN_ROWS + 10, anchor=anchor)
    for row in rows:
        row["amount"] = None
    _write_bars(config, rows)
    assert daily_bars_volume_unit_findings(config, anchor) == []


def test_amount_completeness_sample_ignores_zero_volume_placeholders(config):
    anchor = date(2024, 6, 28)
    traded = _rows("eastmoney", volume=SHARES, n=10, anchor=anchor)
    for row in traded:
        row["amount"] = None
    placeholders = _rows("eastmoney", volume=0, n=30, anchor=anchor)
    placeholders = [dict(row, amount=0.0) for row in placeholders]
    _write_bars(config, traded + placeholders)

    assert daily_bars_amount_completeness_findings(config, anchor) == []


def test_no_curated_data_is_not_a_finding(config):
    assert daily_bars_volume_unit_findings(config, date(2024, 6, 28)) == []
