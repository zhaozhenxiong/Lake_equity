"""Offline contract tests for the optional Tushare BJ ST adapter."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx
import polars as pl
import pytest

from cnequity.adapters.tushare import st_history as st
from cnequity.adapters.tushare.st_history import _post_json, fetch_st_history
from cnequity.config import Config
from cnequity.domain.http_policy import SourceCoolingDown
from cnequity.domain.schemas import with_provenance


def test_business_rate_limit_stops_remaining_symbols(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"code": -2001, "msg": "请求过于频繁"})

    cfg = Config(data_root=tmp_path, source_intervals={"tushare": 0})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        rows, failed = fetch_st_history(
            ["920001.BJ", "920002.BJ"],
            date(2017, 1, 1),
            date(2017, 1, 5),
            token="test-token",
            client=client,
            config=cfg,
            trading_dates={
                "920001.BJ": [date(2017, 1, 4)],
                "920002.BJ": [date(2017, 1, 4)],
            },
        )
    assert rows.is_empty()
    assert failed == ["920001.BJ", "920002.BJ"]
    assert len(calls) == 1


def test_http_429_does_not_sleep_and_retry(tmp_path):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "600"})

    cfg = Config(data_root=tmp_path, source_intervals={"tushare": 0}, max_retries=4)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(SourceCoolingDown):
            _post_json(
                client, {"token": "test-token"}, config=cfg, sleep=lambda _: pytest.fail("sleep")
            )
    assert len(calls) == 1


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Client:
    def __init__(self, rows_by_code, rows_by_date=None):
        self.rows_by_code = rows_by_code
        self.rows_by_date = rows_by_date or {}
        self.calls = []

    def post(self, url, *, json):
        self.calls.append(json)
        if json["api_name"] == "bak_basic":
            rows = self.rows_by_date.get(json["params"]["trade_date"], [])
            fields = ["ts_code", "name", "trade_date"]
        else:
            code = json["params"]["ts_code"]
            rows = self.rows_by_code.get(code, [])
            fields = ["ts_code", "name", "trade_date", "type", "type_name"]
        return _Response(
            {
                "code": 0,
                "data": {
                    "fields": fields,
                    "items": rows,
                },
            }
        )


class _FlakyClient(_Client):
    def __init__(self, rows_by_code, rows_by_date=None, transient_failures=0):
        super().__init__(rows_by_code, rows_by_date)
        self.transient_failures = transient_failures

    def post(self, url, *, json):
        if self.transient_failures:
            self.transient_failures -= 1
            raise httpx.ReadTimeout("temporary Tushare timeout")
        return super().post(url, json=json)


class _OwnedClient(_Client):
    def __init__(self, rows_by_date, *, fail_date=None):
        super().__init__({}, rows_by_date)
        self.fail_date = fail_date

    def post(self, url, *, json):
        if json["api_name"] == "bak_basic" and json["params"]["trade_date"] == self.fail_date:
            self.calls.append(json)
            self.fail_date = None
            raise httpx.ReadTimeout("temporary bak_basic failure")
        return super().post(url, json=json)

    def close(self):
        pass


def _row(code: str, trade_date: str, kind: str = "ST"):
    return [code, "*ST测试", trade_date, kind, "风险警示板"]


def test_emits_explicit_normal_rows_and_maps_legacy_bj_code():
    client = _Client({"920001.BJ": [_row("920001.BJ", "20170104")]})
    df, failed = fetch_st_history(
        ["873001.BJ"],
        date(2017, 1, 1),
        date(2017, 1, 5),
        token="secret",
        client=client,
        trading_dates={
            "873001.BJ": [date(2017, 1, 3), date(2017, 1, 4)],
        },
    )

    assert failed == []
    assert {call["params"]["ts_code"] for call in client.calls} == {
        "873001.BJ",
        "920001.BJ",
    }
    assert df.sort("trade_date")["status"].to_list() == ["normal", "normal"]
    assert df.sort("trade_date")["risk_warning"].to_list() == [False, True]
    assert df["symbol"].unique().to_list() == ["873001.BJ"]


def test_pre_floor_bars_remain_unresolved_without_network_call():
    client = _Client({})
    df, failed = fetch_st_history(
        ["920001.BJ"],
        date(2015, 1, 1),
        date(2017, 1, 5),
        token="secret",
        client=client,
        trading_dates={"920001.BJ": [date(2015, 12, 30)]},
    )

    assert df.is_empty()
    assert failed == ["920001.BJ"]
    assert client.calls == []


def test_bak_basic_name_covers_2016_and_stock_st_covers_2017():
    client = _Client(
        {"920001.BJ": [_row("920001.BJ", "20170104")]},
        {"20161230": [["920001.BJ", "*ST测试", "20161230"]]},
    )
    df, failed = fetch_st_history(
        ["920001.BJ"],
        date(2016, 12, 30),
        date(2017, 1, 5),
        token="secret",
        client=client,
        trading_dates={
            "920001.BJ": [date(2016, 12, 30), date(2017, 1, 4)],
        },
    )

    assert failed == []
    assert df.sort("trade_date")["status"].to_list() == ["normal", "normal"]
    assert df.sort("trade_date")["risk_warning"].to_list() == [True, True]
    assert [call["api_name"] for call in client.calls] == ["bak_basic", "stock_st", "stock_st"]


def test_bak_basic_day_reused_across_owned_client_runs_and_partitioned_by_token(
    tmp_path, monkeypatch
):
    day = date(2016, 12, 30)
    client = _OwnedClient(
        {"20161230": [["920001.BJ", "*ST甲", "20161230"], ["920002.BJ", "乙", "20161230"]]}
    )
    monkeypatch.setattr(st.httpx, "Client", lambda **kwargs: client)
    cfg = Config(data_root=tmp_path, source_intervals={"tushare": 0})

    def read(symbol):
        return fetch_st_history(
            [symbol],
            day,
            day,
            token="first-token",
            config=cfg,
            trading_dates={symbol: [day]},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        (first, failed), (second, failed_second) = pool.map(read, ["920001.BJ", "920002.BJ"])
    assert failed == failed_second == []
    assert first["risk_warning"].to_list() == [True]
    assert second["risk_warning"].to_list() == [False]
    assert first["fetched_at"].to_list() == second["fetched_at"].to_list()
    assert len(client.calls) == 1

    fetch_st_history(
        ["920001.BJ"],
        day,
        day,
        token="second-token",
        config=cfg,
        trading_dates={"920001.BJ": [day]},
    )
    assert len(client.calls) == 2

    supplied = _Client({}, {"20161230": [["920001.BJ", "*ST甲", "20161230"]]})
    for _ in range(2):
        fetch_st_history(
            ["920001.BJ"],
            day,
            day,
            token="supplied-client",
            client=supplied,
            config=cfg,
            trading_dates={"920001.BJ": [day]},
        )
    assert len(supplied.calls) == 2


def test_bak_basic_valid_day_survives_later_failure_and_retry_uses_cache(tmp_path, monkeypatch):
    first_day, second_day = date(2016, 12, 29), date(2016, 12, 30)
    client = _OwnedClient(
        {
            "20161229": [["920001.BJ", "*ST甲", "20161229"]],
            "20161230": [["920002.BJ", "乙", "20161230"]],
        },
        fail_date="20161230",
    )
    monkeypatch.setattr(st.httpx, "Client", lambda **kwargs: client)
    cfg = Config(
        data_root=tmp_path,
        source_intervals={"tushare": 0},
        max_retries=1,
    )
    dates = {"920001.BJ": [first_day], "920002.BJ": [second_day]}
    first, failed = fetch_st_history(
        list(dates),
        first_day,
        second_day,
        token="test-token",
        config=cfg,
        trading_dates=dates,
    )
    assert failed == ["920002.BJ"]
    assert first["symbol"].to_list() == ["920001.BJ"]
    assert [call["params"]["trade_date"] for call in client.calls] == ["20161229", "20161230"]

    second, failed = fetch_st_history(
        list(dates),
        first_day,
        second_day,
        token="test-token",
        config=cfg,
        trading_dates=dates,
    )
    assert failed == []
    assert set(second["symbol"].to_list()) == set(dates)
    assert [call["params"]["trade_date"] for call in client.calls] == [
        "20161229",
        "20161230",
        "20161230",
    ]


def test_bak_basic_damaged_or_expired_cache_refetches_and_write_failure_keeps_rows(
    tmp_path, monkeypatch
):
    day = date(2016, 12, 30)
    client = _Client({}, {"20161230": [["920001.BJ", "*ST甲", "20161230"]]})
    cfg = Config(data_root=tmp_path, source_intervals={"tushare": 0})

    def read():
        return st._cached_bak_basic_rows(
            client, "test-token", day, config=cfg, sleep=lambda _: None, use_cache=True
        )

    first_rows, first_capture = read()
    second_rows, second_capture = read()
    assert len(first_rows) == len(second_rows) == 1
    assert first_capture == second_capture
    assert len(client.calls) == 1
    path = next((cfg.meta_root / "source_cache" / "tushare" / "bak_basic").glob("*/*.json"))
    saved = json.loads(path.read_text())
    saved["sha256"] = "0" * 64
    path.write_text(json.dumps(saved))
    assert len(read()[0]) == 1 and len(client.calls) == 2

    saved = json.loads(path.read_text())
    saved["captured_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    path.write_text(json.dumps(saved))
    assert len(read()[0]) == 1 and len(client.calls) == 3

    path.unlink()
    monkeypatch.setattr(
        st, "write_json_atomic", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read-only"))
    )
    assert len(read()[0]) == 1 and len(client.calls) == 4
    assert not path.exists()


@pytest.mark.parametrize(
    "source_rows",
    [[], [["920001.BJ", "*ST甲", "20161229"]]],
)
def test_bak_basic_empty_or_wrong_day_never_becomes_normal_or_cached(
    tmp_path, monkeypatch, source_rows
):
    day = date(2016, 12, 30)
    client = _OwnedClient({"20161230": source_rows})
    monkeypatch.setattr(st.httpx, "Client", lambda **kwargs: client)
    cfg = Config(data_root=tmp_path, source_intervals={"tushare": 0}, max_retries=1)
    for _ in range(2):
        frame, failed = fetch_st_history(
            ["920001.BJ"],
            day,
            day,
            token="test-token",
            config=cfg,
            trading_dates={"920001.BJ": [day]},
        )
        assert frame.is_empty()
        assert failed == ["920001.BJ"]
    assert len(client.calls) == 2
    assert not list((cfg.meta_root / "source_cache" / "tushare").glob("**/*.json"))


def test_cached_source_capture_time_can_survive_provenance_stamping():
    captured_at = datetime(2016, 12, 30, 12, 0, tzinfo=timezone.utc)
    frame = pl.DataFrame({"fetched_at": [captured_at]})
    preserved = with_provenance(frame, "tushare", "v1", preserve_fetched_at=True)
    assert preserved["fetched_at"].to_list() == [captured_at]
    assert with_provenance(frame, "tushare", "v1")["fetched_at"][0] > captured_at


def test_unknown_st_type_fails_closed():
    client = _Client({"920001.BJ": [_row("920001.BJ", "20170104", "RISK")]})
    df, failed = fetch_st_history(
        ["920001.BJ"],
        date(2017, 1, 1),
        date(2017, 1, 5),
        token="secret",
        client=client,
        trading_dates={"920001.BJ": [date(2017, 1, 4)]},
    )

    assert df.is_empty()
    assert failed == ["920001.BJ"]


def test_missing_stock_st_identity_fails_closed():
    client = _Client({"920001.BJ": [[None, "*ST测试", "20170104", "ST", "风险警示板"]]})
    df, failed = fetch_st_history(
        ["920001.BJ"],
        date(2017, 1, 1),
        date(2017, 1, 5),
        token="secret",
        client=client,
        trading_dates={"920001.BJ": [date(2017, 1, 4)]},
    )

    assert df.is_empty()
    assert failed == ["920001.BJ"]


def test_invalid_stock_st_date_fails_closed():
    client = _Client({"920001.BJ": [_row("920001.BJ", "not-a-date")]})
    df, failed = fetch_st_history(
        ["920001.BJ"],
        date(2017, 1, 1),
        date(2017, 1, 5),
        token="secret",
        client=client,
        trading_dates={"920001.BJ": [date(2017, 1, 4)]},
    )

    assert df.is_empty()
    assert failed == ["920001.BJ"]


def test_retries_transient_timeout_before_emitting_evidence(tmp_path):
    from cnequity.domain.rate_limit import _read_json

    client = _FlakyClient(
        {"920001.BJ": [_row("920001.BJ", "20170104")]},
        transient_failures=1,
    )
    cfg = Config(data_root=tmp_path, max_retries=2, retry_backoff_seconds=0)
    df, failed = fetch_st_history(
        ["920001.BJ"],
        date(2017, 1, 1),
        date(2017, 1, 5),
        token="secret",
        client=client,
        config=cfg,
        sleep=lambda _: None,
        trading_dates={"920001.BJ": [date(2017, 1, 4)]},
    )

    assert failed == []
    assert client.transient_failures == 0
    assert df["status"].to_list() == ["normal"]
    assert df["risk_warning"].to_list() == [True]
    assert _read_json(cfg.rate_limit_root / "events-tushare.json")["retry"] == 1
