import io
import json
from datetime import date

import httpx
import pandas as pd
import polars as pl
import pytest

from cnequity.adapters.exchange.fund_volume import parse_sse_dayk
from cnequity.adapters.exchange.turnover_prices import (
    fetch_exchange_turnover_prices,
    parse_szse_history_prices,
)
from cnequity.config import Config
from cnequity.storage.raw_archive import RawPayloadArchive

DAY = date(2001, 4, 4)


def workbook(day=DAY):
    buffer = io.BytesIO()
    pd.DataFrame(
        [
            {
                "交易日期": str(day),
                "证券代码": "000820",
                "开盘": 11.78,
                "最高": 11.96,
                "最低": 11.63,
                "今收": 11.64,
                "成交量(万股)": 9999999,
                "成交金额(万元)": 1,
            }
        ]
    ).to_excel(buffer, index=False)
    return buffer.getvalue()


def test_szse_identity_date_and_prices_are_bound_without_turnover():
    rows = parse_szse_history_prices(workbook(), DAY, {"000820.SZ"})
    assert rows["close"].to_list() == [11.64]
    assert "volume" not in rows.columns and "amount" not in rows.columns
    with pytest.raises(ValueError, match="requested historical date"):
        parse_szse_history_prices(workbook(date(2026, 10, 13)), DAY, {"000820.SZ"})


def test_sse_selected_history_checks_identity_and_exact_target_range():
    payload = {
        "code": "600276",
        "total": 2,
        "begin": 0,
        "end": 2,
        "kline": [[20010404, 34.81, 35.0, 33.4, 33.6, 100, 3400], [20010405, 0, 0, 0, 0, 0, 0]],
    }
    rows = parse_sse_dayk(json.dumps(payload).encode(), "600276.SH", trade_dates={DAY})
    assert rows["close"].to_list() == [33.6]
    with pytest.raises(ValueError, match="identity"):
        parse_sse_dayk(json.dumps(payload).encode(), "600315.SH", trade_dates={DAY})


def test_exchange_review_archives_exact_wire_bytes(tmp_path, monkeypatch):
    import cnequity.adapters.exchange.turnover_prices as module

    cfg = Config(data_root=tmp_path)
    wire = json.dumps(
        {
            "code": "600276",
            "total": 1,
            "begin": 0,
            "end": 1,
            "kline": [[20010404, 34.81, 35.0, 33.4, 33.6, 100, 3400]],
        }
    ).encode()
    monkeypatch.setattr(
        module, "_get_official_response", lambda *a, **k: httpx.Response(200, content=wire)
    )
    targets = pl.DataFrame({"symbol": ["600276.SH"], "trade_date": [DAY]})
    rows, failures = fetch_exchange_turnover_prices(targets, config=cfg, run_id="review")
    assert rows.height == 1 and not failures
    archive = RawPayloadArchive(cfg.meta_root, enabled=True, datasets=["daily_bars"])
    # Archive path is owned by the registry; inspect its actual sidecars.
    receipts = list(cfg.meta_root.rglob("*.json"))
    records = [json.loads(p.read_text()) for p in receipts if "payload_path" in p.read_text()]
    assert records and any(archive.read(r) == wire for r in records)
