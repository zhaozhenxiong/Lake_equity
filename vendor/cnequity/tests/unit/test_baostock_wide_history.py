"""One verified BaoStock year serves independent daily-field consumers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import ModuleType

import pytest

from cnequity.adapters.baostock.wide_history import FIELDS, query_history
from cnequity.config import Config
from cnequity.domain.http_policy import SourceCoolingDown, cooldown_status


class Result:
    error_code = "0"
    error_msg = ""

    def __init__(self, rows):
        self.rows = rows
        self.pos = -1

    def next(self):
        self.pos += 1
        return self.pos < len(self.rows)

    def get_row_data(self):
        return self.rows[self.pos]


def _module(calls):
    bs = ModuleType("baostock")

    def fetch(code, fields, **kwargs):
        calls.append((code, fields, kwargs["start_date"]))
        assert fields == FIELDS
        year = kwargs["start_date"][:4]
        row = dict.fromkeys(FIELDS.split(","), "1")
        row.update(date=f"{year}-06-28", code=code, tradestatus="1", isST="0")
        return Result([[row[name] for name in FIELDS.split(",")]])

    bs.query_history_k_data_plus = fetch
    return bs


def _read(bs, cfg, fields, year="2024"):
    rs = query_history(
        bs,
        "sh.600000",
        fields,
        start_date=f"{year}-06-01",
        end_date=f"{year}-06-30",
        frequency="d",
        adjustflag="3",
        config=cfg,
    )
    assert rs.error_code == "0"
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    return rows


def test_wide_year_is_reused_for_valuation_status_and_bars(tmp_path):
    cfg = Config(data_root=tmp_path / "data", source_intervals={"baostock": 0.0})
    calls = []
    bs = _module(calls)

    assert _read(bs, cfg, "date,code,close,amount,turn,peTTM,pbMRQ,psTTM")
    assert _read(bs, cfg, "date,code,tradestatus,isST") == [["2024-06-28", "sh.600000", "1", "0"]]
    assert _read(bs, cfg, "date,code,open,high,low,close,volume,amount,tradestatus")
    assert len(calls) == 1
    from cnequity.diagnostics.source_limits import build_source_limits

    assert build_source_limits(cfg)["sources"]["baostock"]["cache_reuse_today"]["caches"] == {
        "wide_year": 2
    }
    _read(bs, cfg, "date,code,isST", year="2025")
    assert len(calls) == 2


def test_concurrent_consumers_singleflight_the_same_year(tmp_path):
    cfg = Config(data_root=tmp_path / "data", source_intervals={"baostock": 0.0})
    calls = []
    bs = _module(calls)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(
            pool.map(lambda fields: _read(bs, cfg, fields), ["date,code,isST", "date,code,peTTM"])
        )
    assert all(outputs)
    assert len(calls) == 1


def test_changed_source_field_order_does_not_poison_shared_cache(tmp_path):
    cfg = Config(data_root=tmp_path / "data", source_intervals={"baostock": 0.0})
    calls = []
    bs = _module(calls)
    original = bs.query_history_k_data_plus

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        result.fields = ["code", "date", *FIELDS.split(",")[2:]]
        return result

    bs.query_history_k_data_plus = changed
    result = query_history(
        bs,
        "sh.600000",
        "date,code,isST",
        start_date="2024-06-01",
        end_date="2024-06-30",
        frequency="d",
        adjustflag="3",
        config=cfg,
    )
    assert result.error_code == "invalid_fields"
    assert not list((cfg.meta_root / "source_cache" / "baostock").glob("*.json"))


def test_sdk_blacklist_stops_future_queries_across_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("CNE_RATE_LIMIT_ROOT", str(tmp_path / "egress"))
    cfg = Config(data_root=tmp_path / "one", source_intervals={"baostock": 0})
    other = Config(data_root=tmp_path / "two", source_intervals={"baostock": 0})
    bs = ModuleType("baostock")
    calls = []

    def refused(*args, **kwargs):
        calls.append(1)
        response = Result([])
        response.error_code = "10001011"
        response.error_msg = "黑名单用户"
        return response

    bs.query_history_k_data_plus = refused
    with pytest.raises(SourceCoolingDown, match="黑名单"):
        query_history(
            bs,
            "sh.600000",
            "date,code,isST",
            start_date="2024-01-01",
            end_date="2024-01-02",
            frequency="d",
            adjustflag="3",
            config=other,
        )
    status = cooldown_status(cfg.rate_limit_root, "baostock")
    assert status["kind"] == "ip_blacklist"
    assert status["cooldown_until"] is not None
    with pytest.raises(SourceCoolingDown):
        query_history(
            bs,
            "sh.600001",
            "date,code,isST",
            start_date="2024-01-01",
            end_date="2024-01-02",
            frequency="d",
            adjustflag="3",
            config=cfg,
        )
    assert len(calls) == 1
