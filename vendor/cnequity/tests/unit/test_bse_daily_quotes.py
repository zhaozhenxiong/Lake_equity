"""The BSE adapter must accept only well-formed, correctly dated quote rows."""

import json
from datetime import date

import httpx
import pytest

from cnequity.adapters.bse.daily_quotes import BseMarketDataError, fetch_daily_quotes
from cnequity.config import Config


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        return None

    @property
    def content(self):
        return self.text.encode("utf-8")


class _Client:
    def __init__(self, pages: dict[int, str], redirects: int = 0, landing_code: int = 200):
        self.pages = pages
        self.redirects = redirects
        self.landing_code = landing_code
        self.landing_calls = 0
        self.posted: list[int] = []
        self.closed = False

    def get(self, url):
        self.landing_calls += 1
        return httpx.Response(self.landing_code, request=httpx.Request("GET", url))

    def post(self, url, *, data):
        page = int(data["page"])
        self.posted.append(page)
        if self.redirects:
            self.redirects -= 1
            return _Response("", status_code=307)
        return _Response(self.pages[page])

    def close(self):
        self.closed = True


def _jsonp(rows, total: int):
    return "null(" + json.dumps([{"content": rows, "totalElements": total}]) + ")"


def _row(code: str = "920571", trade_date: str = "20260821"):
    return {
        "hqzqdm": code,
        "hqjsrq": trade_date,
        "hqjrkp": "9.09",
        "hqzgcj": "9.66",
        "hqzdcj": "9.06",
        "hqzjcj": "9.60",
        "hqzrsp": "9.10",
        "hqcjsl": "33952730",
        "hqcjje": "322779288.68",
    }


def test_fetches_current_bse_quote_and_paginates(tmp_path):
    first_page = [_row(str(920571 + index)) for index in range(20)]
    client = _Client(
        {
            0: _jsonp(first_page, total=21),
            1: _jsonp([_row("920591")], total=21),
        }
    )
    cfg = Config(
        data_root=tmp_path / "data",
        sources={"bse": True},
        source_intervals={"bse": 0.0},
    )

    out = fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)

    assert client.posted == [0, 1]
    assert out.height == 21
    assert {"920571.BJ", "920591.BJ"}.issubset(set(out["symbol"].to_list()))
    assert out.filter(out["symbol"] == "920571.BJ")["amount"].item() == pytest.approx(322779288.68)
    assert out.filter(out["symbol"] == "920571.BJ")["pre_close"].item() == 9.10


def test_rejects_other_sessions_and_malformed_payload(tmp_path):
    client = _Client({0: _jsonp([_row(trade_date="20260820")], total=1)})
    cfg = Config(data_root=tmp_path / "data", source_intervals={"bse": 0.0})
    out = fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)

    assert out.is_empty()
    assert out.schema["amount"].is_float()

    broken = _Client({0: "null(not-json)"})
    with pytest.raises(BseMarketDataError, match="not valid JSON"):
        fetch_daily_quotes(date(2026, 8, 21), client=broken, config=cfg)


def test_retries_a_same_endpoint_waf_redirect(tmp_path):
    from cnequity.domain.rate_limit import _read_json

    client = _Client({0: _jsonp([_row()], total=1)}, redirects=1)
    cfg = Config(data_root=tmp_path / "data", source_intervals={"bse": 0.0})

    out = fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)

    assert out.height == 1
    assert client.posted == [0, 0]
    assert _read_json(cfg.rate_limit_root / "events-bse.json")["retry"] == 1


def test_fails_loud_on_an_empty_page_before_advertised_total(tmp_path):
    client = _Client({0: _jsonp([], total=21)})
    cfg = Config(data_root=tmp_path / "data", source_intervals={"bse": 0.0})

    with pytest.raises(BseMarketDataError, match="advertised total"):
        fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)


def test_landing_refusal_stops_before_quotation_api(tmp_path):
    from cnequity.domain.http_policy import SourceCoolingDown

    client = _Client({}, landing_code=429)
    cfg = Config(data_root=tmp_path / "data", source_intervals={"bse": 0.0})
    with pytest.raises(httpx.HTTPStatusError):
        fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)
    with pytest.raises(SourceCoolingDown):
        fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)
    assert client.landing_calls == 1
    assert client.posted == []


def test_quotation_challenge_cools_down_before_next_request(tmp_path):
    from cnequity.domain.http_policy import SourceCoolingDown

    client = _Client({0: "<html>captcha challenge</html>"})
    cfg = Config(data_root=tmp_path / "data", source_intervals={"bse": 0.0})

    with pytest.raises(BseMarketDataError):
        fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)
    with pytest.raises(SourceCoolingDown):
        fetch_daily_quotes(date(2026, 8, 21), client=client, config=cfg)
    assert client.landing_calls == 1
    assert client.posted == [0]


def test_rejects_underfilled_or_inconsistent_board_pages(tmp_path):
    from cnequity.adapters.bse.instruments import fetch_bse_instruments

    cfg = Config(data_root=tmp_path / "data", source_intervals={"bse": 0.0})
    with pytest.raises(BseMarketDataError, match="advertised rows"):
        fetch_daily_quotes(
            date(2026, 8, 21),
            client=_Client({0: _jsonp([_row()], total=21), 1: _jsonp([_row("920572")], total=21)}),
            config=cfg,
        )
    with pytest.raises(BseMarketDataError, match="incomplete"):
        fetch_bse_instruments(
            date(2026, 8, 21),
            client=_Client({0: _jsonp([_row()], total=21), 1: _jsonp([_row("920572")], total=21)}),
            config=cfg,
        )
    with pytest.raises(BseMarketDataError, match="changed during pagination"):
        fetch_daily_quotes(
            date(2026, 8, 21),
            client=_Client({0: _jsonp([_row()], total=21), 1: _jsonp([_row("920572")], total=22)}),
            config=cfg,
        )


def test_complete_board_is_shared_by_quotes_names_and_status(tmp_path, monkeypatch):
    from cnequity.adapters.bse import daily_quotes
    from cnequity.adapters.bse.instruments import fetch_bse_instruments
    from cnequity.adapters.bse.trading_status import fetch_trading_status_bse

    cfg = Config(data_root=tmp_path / "data")
    calls = []

    def fetch_uncached(*, client=None, config=None):
        calls.append(1)
        return [{**_row(), "hqzqjc": "中裕科技"}], 1

    monkeypatch.setattr(daily_quotes, "_read_board_uncached", fetch_uncached)
    day = date(2026, 8, 21)
    assert fetch_daily_quotes(day, config=cfg).height == 1
    assert fetch_bse_instruments(day, config=cfg).height == 1
    assert fetch_trading_status_bse(["920571.BJ"], day, config=cfg).complete
    assert len(calls) == 1
    from cnequity.diagnostics.source_limits import build_source_limits

    assert build_source_limits(cfg)["sources"]["bse"]["cache_reuse_today"]["caches"] == {
        "board_snapshot": 2
    }

    # A complete old session must not become today's board or absence proof.
    assert fetch_daily_quotes(date(2026, 8, 24), config=cfg).is_empty()
    assert len(calls) == 2
