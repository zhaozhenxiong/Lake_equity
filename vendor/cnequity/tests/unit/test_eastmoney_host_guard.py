"""EastMoney IP protection: push2 and datacenter breakers, budgets, lanes, shared sweep."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import polars as pl
import pytest

from cnequity.adapters.eastmoney import host_guard, push2_snapshot
from cnequity.adapters.eastmoney.em_auth import EastMoneyClient, is_transport_fail_fast
from cnequity.config import Config, load_config
from cnequity.config.bootstrap import path_for_toml

_CLIST = "https://push2.eastmoney.com/api/qt/clist/get?pn=1"
_DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def _cfg(tmp_path, **kwargs) -> Config:
    kwargs.setdefault("source_intervals", {"eastmoney": 0.0, "eastmoney_push2": 0.0})
    return Config(data_root=tmp_path / "data", **kwargs)


def _client(cfg, handler) -> tuple[EastMoneyClient, list[str]]:
    client = EastMoneyClient(config=cfg)
    sent: list[str] = []

    def _get(url, **kwargs):
        sent.append(url)
        return handler(url)

    client._client.get = _get  # type: ignore[method-assign]
    return client, sent


def _ok(url, payload=None):
    return httpx.Response(200, request=httpx.Request("GET", url), json=payload or {"data": {}})


# ---- breaker -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "exc", "trips"),
    [
        (502, None, True),
        (503, None, True),
        (403, None, True),
        (429, None, True),
        (200, None, False),
        (404, None, False),
        (None, httpx.RemoteProtocolError("empty reply"), True),
        (None, httpx.ReadTimeout("slow"), True),
        (None, httpx.ProxyError("clash down"), False),
        (None, host_guard.Push2PausedError("local"), False),
    ],
)
def test_what_counts_as_push2_refusing_this_ip(status, exc, trips):
    reason = host_guard.refusal_reason(status_code=status, exc=exc)
    assert (reason is not None) is trips


def test_one_refusal_closes_every_push2_host_until_tomorrow(tmp_path, monkeypatch):
    today = [date(2026, 9, 22)]
    monkeypatch.setattr(host_guard, "_today", lambda: today[0])
    cfg = _cfg(tmp_path)
    client, sent = _client(cfg, lambda url: httpx.Response(502, request=httpx.Request("GET", url)))

    assert client.get(_CLIST).status_code == 502
    # Backup hosts included: the ban spread by asking them after push2 refused.
    for url in (
        "https://push2delay.eastmoney.com/api/qt/clist/get",
        "https://40.push2.eastmoney.com/api/qt/clist/get",
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
    ):
        with pytest.raises(host_guard.Push2BreakerOpenError) as exc:
            client.get(url)
        assert is_transport_fail_fast(exc.value)
    assert len(sent) == 1

    today[0] = date(2026, 9, 23)
    client.get(_CLIST)
    assert len(sent) == 2
    client.close()


def test_a_dropped_connection_trips_the_breaker(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    cfg = _cfg(tmp_path)

    def _drop(url):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    client, sent = _client(cfg, _drop)
    with pytest.raises(httpx.RemoteProtocolError):
        client.get(_CLIST)
    with pytest.raises(host_guard.Push2BreakerOpenError):
        client.get(_CLIST)
    assert len(sent) == 1
    state = json.loads((cfg.rate_limit_root / "eastmoney_guard.json").read_text())
    assert state["push2"]["breaker"]["reason"] == "RemoteProtocolError"
    client.close()


def test_breaker_leaves_datacenter_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    monkeypatch.setattr("cnequity.adapters.eastmoney.em_auth.get_nid", lambda *a, **k: "")
    cfg = _cfg(tmp_path)
    host_guard.trip(cfg, _CLIST, "HTTP 502")
    client, sent = _client(cfg, _ok)
    assert client.get(_DATACENTER).status_code == 200
    assert sent == [_DATACENTER]
    client.close()


def test_breaker_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    cfg = _cfg(tmp_path, eastmoney_push2_breaker=False, eastmoney_push2_daily_budget=0)
    client, sent = _client(cfg, lambda url: httpx.Response(502, request=httpx.Request("GET", url)))
    client.get(_CLIST)
    client.get(_CLIST)
    assert len(sent) == 2
    client.close()


# ---- budget --------------------------------------------------------------------


def test_daily_budget_is_a_hard_cap_that_resets_at_midnight(tmp_path, monkeypatch):
    today = [date(2026, 9, 22)]
    monkeypatch.setattr(host_guard, "_today", lambda: today[0])
    cfg = _cfg(tmp_path, eastmoney_push2_daily_budget=3)
    client, sent = _client(cfg, _ok)
    for _ in range(3):
        client.get(_CLIST)
    with pytest.raises(host_guard.Push2BudgetExhaustedError) as exc:
        client.get(_CLIST)
    assert is_transport_fail_fast(exc.value)
    assert len(sent) == 3

    today[0] = date(2026, 9, 23)
    client.get(_CLIST)
    assert len(sent) == 4
    client.close()


def test_budget_is_shared_across_clients(tmp_path, monkeypatch):
    """Separate processes each build their own client; the ledger is on disk."""
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    cfg = _cfg(tmp_path, eastmoney_push2_daily_budget=2)
    first, _ = _client(cfg, _ok)
    second, _ = _client(_cfg(tmp_path, eastmoney_push2_daily_budget=2), _ok)
    first.get(_CLIST)
    second.get(_CLIST)
    with pytest.raises(host_guard.Push2BudgetExhaustedError):
        first.get(_CLIST)
    first.close()
    second.close()


def test_vendor_budget_counts_push2_and_datacenter_together(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    cfg = _cfg(
        tmp_path,
        eastmoney_daily_budget=2,
        eastmoney_push2_daily_budget=0,
        eastmoney_datacenter_daily_budget=0,
    )
    client, sent = _client(cfg, _ok)
    client.get(_CLIST)
    client.get(_DATACENTER)
    with pytest.raises(host_guard.EastMoneyHostBlockedError, match="shared daily budget"):
        client.get(_DATACENTER)
    assert len(sent) == 2
    assert host_guard.status(cfg)["vendor"]["requests"] == 2
    client.close()


def test_budget_and_breaker_are_shared_across_lakes_on_one_egress(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 28))
    monkeypatch.setenv("CNE_RATE_LIMIT_ROOT", str(tmp_path / "egress"))
    first_cfg = Config(data_root=tmp_path / "lake-a", eastmoney_push2_daily_budget=3)
    second_cfg = Config(data_root=tmp_path / "lake-b", eastmoney_push2_daily_budget=2)
    first, first_sent = _client(first_cfg, _ok)
    second, second_sent = _client(second_cfg, _ok)
    first.get(_CLIST)
    second.get(_CLIST)
    with pytest.raises(host_guard.Push2BudgetExhaustedError):
        first.get(_CLIST)
    assert len(first_sent) == len(second_sent) == 1

    host_guard.trip(first_cfg, _CLIST, "HTTP 502")
    with pytest.raises(host_guard.Push2BreakerOpenError):
        second.get(_CLIST)
    first.close()
    second.close()


def test_legacy_lake_ledgers_merge_once_without_exposing_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 28))
    monkeypatch.setenv("CNE_RATE_LIMIT_ROOT", str(tmp_path / "egress"))
    a = Config(data_root=tmp_path / "a", eastmoney_push2_daily_budget=3)
    b = Config(data_root=tmp_path / "b", eastmoney_push2_daily_budget=3)
    for cfg in (a, b):
        path = cfg.meta_root / "state" / "eastmoney_guard.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"day": "2026-09-28", "push2": {"requests": 1}}))
    first, _ = _client(a, _ok)
    first.get(_CLIST)
    second, sent = _client(b, _ok)
    with pytest.raises(host_guard.Push2BudgetExhaustedError):
        second.get(_CLIST)
    assert sent == []
    state = host_guard.status(a)
    assert state["push2"]["requests"] == 3
    assert str(a.data_root) not in (a.rate_limit_root / "eastmoney_guard.json").read_text()
    first.close()
    second.close()


# ---- own pacing lane -------------------------------------------------------------


def test_push2_has_its_own_concurrency_family():
    from cnequity.adapters.throttle import _source_family

    assert _source_family("eastmoney_push2") == "eastmoney_push2"
    assert _source_family("eastmoney") == "eastmoney"


def test_push2_requests_use_the_push2_source(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney import em_auth

    seen: list[str] = []

    class _Ctx:
        def __init__(self, source):
            seen.append(source)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(em_auth, "source_request", lambda config, source, **k: _Ctx(source))
    monkeypatch.setattr(em_auth, "get_nid", lambda *a, **k: "")
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    client, _ = _client(_cfg(tmp_path), _ok)
    client.get(_CLIST)
    client.get(_DATACENTER)
    client.get("https://np-listapi.eastmoney.com/comm/web/getFastNewsList")
    client.close()
    assert seen == ["eastmoney_push2", "eastmoney_dc", "eastmoney"]


def test_config_gives_push2_a_slow_single_lane_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("CNE_PUSH2_PAUSED", raising=False)
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n[sources.eastmoney]\nenabled = true\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.source_intervals["eastmoney_push2"] == 4.0
    assert cfg.source_concurrency_for("eastmoney_push2") == 1
    assert cfg.eastmoney_push2_breaker is True
    assert cfg.eastmoney_push2_daily_budget == 150
    assert cfg.eastmoney_push2_shared_snapshot is True
    assert cfg.eastmoney_push2_paused is False


def test_config_reads_push2_keys_and_the_stale_pass_env(tmp_path, monkeypatch):
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f"""[data]
root = "{path_for_toml(tmp_path / "data")}"
[sources.eastmoney]
enabled = true
push2_breaker = false
push2_daily_budget = 40
push2_shared_snapshot = false
push2_min_interval_seconds = 6
push2_max_concurrency = 2
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("CNE_PUSH2_PAUSED", raising=False)
    cfg = load_config(path)
    assert (cfg.eastmoney_push2_breaker, cfg.eastmoney_push2_daily_budget) == (False, 40)
    assert cfg.eastmoney_push2_shared_snapshot is False
    assert cfg.source_intervals["eastmoney_push2"] == 6.0
    assert cfg.source_concurrency_for("eastmoney_push2") == 2
    assert cfg.eastmoney_push2_paused is False

    monkeypatch.setenv("CNE_PUSH2_PAUSED", "1")
    assert load_config(path).eastmoney_push2_paused is True


# ---- clist: primary host only ------------------------------------------------------


def test_clist_does_not_fail_over_to_backup_hosts_under_the_breaker(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney.clist import fetch_clist_pages

    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    cfg = _cfg(tmp_path)
    client, sent = _client(cfg, lambda url: httpx.Response(502, request=httpx.Request("GET", url)))
    with pytest.raises(RuntimeError, match="failed on all hosts"):
        fetch_clist_pages(client, fields="f12,f13,f14", fs="m:0+f:4")
    client.close()
    assert [urlparse(u).hostname for u in sent] == ["push2.eastmoney.com"]


# ---- shared full-market sweep ------------------------------------------------------


def _paged_market(rows_total: int):
    """A fake push2 clist that pages *rows_total* securities, pz from the URL."""

    def handler(url):
        query = parse_qs(urlparse(url).query)
        page, size = int(query["pn"][0]), int(query["pz"][0])
        fields = query["fields"][0].split(",")
        start = (page - 1) * size
        diff = []
        for i in range(start, min(start + size, rows_total)):
            row = {f: 1.5 for f in fields}
            row.update({"f12": f"{600000 + i:06d}", "f13": 1, "f26": 20010827})
            diff.append(row)
        return _ok(url, {"data": {"total": rows_total, "diff": diff}})

    return handler


def test_full_market_callers_share_one_sweep(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney.clist import fetch_clist_pages

    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    closed = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)  # 17:00 Beijing
    monkeypatch.setattr(push2_snapshot, "_now", lambda: closed)
    cfg = _cfg(tmp_path)
    client, sent = _client(cfg, _paged_market(250))

    valuation = fetch_clist_pages(client, fields="f12,f13,f9,f23,f130,f20,f21", page_size=100)
    pages_after_first = len(sent)
    fund_flow = fetch_clist_pages(client, fields="f12,f13,f62,f66,f72,f78,f84", page_size=100)
    list_dates = fetch_clist_pages(client, fields="f12,f13,f26", page_size=100)
    client.close()

    assert pages_after_first == 3
    assert len(sent) == 3  # the second and third callers paged nothing
    assert all("f62" in parse_qs(urlparse(u).query)["fields"][0] for u in sent)
    assert len(valuation) == len(fund_flow) == len(list_dates) == 250
    assert fund_flow[0]["f62"] == 1.5 and list_dates[0]["f26"] == 20010827


def test_a_board_outside_the_snapshot_is_paged_on_its_own(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney.clist import fetch_clist_pages

    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    cfg = _cfg(tmp_path)
    client, sent = _client(cfg, _paged_market(50))
    fetch_clist_pages(client, fields="f12,f13,f14", fs="m:0+f:4")  # f14 not in the union
    fetch_clist_pages(client, fields="f12,f13,f14", fs="m:0+f:4")
    client.close()
    assert len(sent) == 2


@pytest.mark.parametrize(
    ("captured", "now", "reuse"),
    [
        # Beijing 16:15 → 23:00 same evening: same closed window.
        ("2026-09-21T08:15:00+00:00", "2026-09-21T15:00:00+00:00", True),
        # 16:15 → next morning 08:30: still before the 09:15 open.
        ("2026-09-21T08:15:00+00:00", "2026-09-22T00:30:00+00:00", True),
        # 16:15 → next day 16:15: a new session closed in between.
        ("2026-09-21T08:15:00+00:00", "2026-09-22T08:15:00+00:00", False),
        # Captured mid-session, reused 5 minutes later (same run).
        ("2026-09-21T03:00:00+00:00", "2026-09-21T03:05:00+00:00", True),
        # Captured mid-session, 20 minutes later the market has moved.
        ("2026-09-21T03:00:00+00:00", "2026-09-21T03:20:00+00:00", False),
    ],
)
def test_snapshot_is_reused_only_while_the_market_cannot_have_moved(captured, now, reuse):
    assert (
        push2_snapshot.reusable(datetime.fromisoformat(captured), datetime.fromisoformat(now))
        is reuse
    )


def test_a_stale_snapshot_is_fetched_again(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney.clist import fetch_clist_pages

    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    clock = [datetime(2026, 9, 21, 8, 15, tzinfo=timezone.utc)]
    monkeypatch.setattr(push2_snapshot, "_now", lambda: clock[0])
    cfg = _cfg(tmp_path)
    client, sent = _client(cfg, _paged_market(50))
    fetch_clist_pages(client, fields="f12,f13,f9")
    clock[0] = clock[0] + timedelta(days=1)
    fetch_clist_pages(client, fields="f12,f13,f9")
    client.close()
    assert len(sent) == 2


def test_fund_flow_archives_the_replayed_wire_bytes(tmp_path, monkeypatch):
    """fund_flow must archive an exact wire observation even when it reuses."""
    from cnequity.adapters.eastmoney.capital import fetch_fund_flow
    from cnequity.adapters.eastmoney.clist import fetch_clist_pages
    from cnequity.storage.raw_archive import captured_records

    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 22))
    monkeypatch.setattr(
        push2_snapshot, "_now", lambda: datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
    )
    cfg = _cfg(tmp_path)
    client, sent = _client(cfg, _paged_market(120))
    fetch_clist_pages(client, fields="f12,f13,f9")  # valuation pages the market first
    df = fetch_fund_flow(date(2026, 9, 22), client=client, config=cfg, run_id="run-ff")
    client.close()

    assert len(sent) == 2
    assert isinstance(df, pl.DataFrame) and df.height == 120
    records = captured_records(
        cfg, "fund_flow", "run-ff", source="eastmoney", request_scope="daily:2026-09-22"
    )
    assert len(records) == 2


# ---- listing dates: baostock first ---------------------------------------------


def _instruments(rows: list[tuple[str, str, date | None]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [r[0] for r in rows],
            "asset_type": [r[1] for r in rows],
            "list_date": [r[2] for r in rows],
        },
        schema={"symbol": pl.Utf8, "asset_type": pl.Utf8, "list_date": pl.Date},
    )


def _basics(rows: dict[str, date]) -> pl.DataFrame:
    return pl.DataFrame(
        {"symbol": list(rows), "list_date": list(rows.values())},
        schema={"symbol": pl.Utf8, "list_date": pl.Date},
    )


def test_list_dates_come_from_baostock_without_touching_push2(tmp_path, monkeypatch):
    from cnequity.adapters.baostock import instruments as bs_inst
    from cnequity.adapters.eastmoney import instruments as em_inst

    cfg = _cfg(tmp_path, sources={"eastmoney": True, "baostock": True})
    monkeypatch.setattr(
        bs_inst,
        "fetch_instrument_basics",
        lambda **k: _basics({"600519.SH": date(2001, 8, 27), "510300.SH": date(2012, 5, 28)}),
    )
    em_calls: list[dict] = []
    monkeypatch.setattr(em_inst, "fetch_list_date_map", lambda **k: em_calls.append(k) or {})

    df = _instruments([("600519.SH", "stock", None), ("510300.SH", "etf", None)])
    out = em_inst.enrich_instrument_list_dates(cfg, df)
    assert out["list_date"].to_list() == [date(2001, 8, 27), date(2012, 5, 28)]
    assert em_calls == []


def test_push2_pages_only_the_board_baostock_left_gaps_on(tmp_path, monkeypatch):
    from cnequity.adapters.baostock import instruments as bs_inst
    from cnequity.adapters.eastmoney import instruments as em_inst

    cfg = _cfg(tmp_path, sources={"eastmoney": True, "baostock": True})
    monkeypatch.setattr(
        bs_inst, "fetch_instrument_basics", lambda **k: _basics({"600519.SH": date(2001, 8, 27)})
    )
    em_calls: list[dict] = []

    def _em(**kwargs):
        em_calls.append({k: kwargs[k] for k in ("equity", "etf")})
        return {"920229.BJ": date(2026, 9, 22)}

    monkeypatch.setattr(em_inst, "fetch_list_date_map", _em)
    df = _instruments([("600519.SH", "stock", None), ("920229.BJ", "stock", None)])
    out = em_inst.enrich_instrument_list_dates(cfg, df)
    assert out["list_date"].to_list() == [date(2001, 8, 27), date(2026, 9, 22)]
    assert em_calls == [{"equity": True, "etf": False}]


def test_a_future_baostock_ipo_date_is_not_a_listing(tmp_path, monkeypatch):
    from cnequity.adapters.baostock import instruments as bs_inst
    from cnequity.adapters.eastmoney import instruments as em_inst

    cfg = _cfg(tmp_path, sources={"baostock": True, "eastmoney": False})
    monkeypatch.setattr(
        bs_inst, "fetch_instrument_basics", lambda **k: _basics({"603999.SH": date(2099, 1, 1)})
    )
    out = em_inst.enrich_instrument_list_dates(cfg, _instruments([("603999.SH", "stock", None)]))
    assert out["list_date"].to_list() == [None]


def test_a_baostock_failure_falls_through_to_eastmoney(tmp_path, monkeypatch):
    from cnequity.adapters.baostock import instruments as bs_inst
    from cnequity.adapters.eastmoney import instruments as em_inst

    cfg = _cfg(tmp_path, sources={"eastmoney": True, "baostock": True})

    def _boom(**kwargs):
        raise RuntimeError("baostock login failed")

    monkeypatch.setattr(bs_inst, "fetch_instrument_basics", _boom)
    monkeypatch.setattr(
        em_inst, "fetch_list_date_map", lambda **k: {"600519.SH": date(2001, 8, 27)}
    )
    out = em_inst.enrich_instrument_list_dates(cfg, _instruments([("600519.SH", "stock", None)]))
    assert out["list_date"].to_list() == [date(2001, 8, 27)]


# ---- datacenter --------------------------------------------------------------------


def _status(url, code):
    return httpx.Response(code, request=httpx.Request("GET", url))


def test_datacenter_trips_after_three_refusals_in_a_row(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr("cnequity.adapters.eastmoney.em_auth.get_nid", lambda *a, **k: "")
    # Explicit 4xx refusals now enter shared cooldown after the first call;
    # this older consecutive-strike gate still applies to server failures.
    client, sent = _client(_cfg(tmp_path), lambda url: _status(url, 502))
    for _ in range(3):
        assert client.get(_DATACENTER).status_code == 502
    with pytest.raises(host_guard.DatacenterBreakerOpenError) as exc:
        client.get(_DATACENTER)
    assert is_transport_fail_fast(exc.value)
    assert len(sent) == 3
    # push2 has its own breaker; datacenter's does not close it.
    client._client.get = lambda url, **k: _ok(url)  # type: ignore[method-assign]
    assert client.get(_CLIST).status_code == 200
    client.close()


def test_a_datacenter_success_clears_the_strikes(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr("cnequity.adapters.eastmoney.em_auth.get_nid", lambda *a, **k: "")
    codes = iter([502, 502, 200, 502, 502, 200])
    client, sent = _client(_cfg(tmp_path), lambda url: _status(url, next(codes)))
    for _ in range(6):
        client.get(_DATACENTER)
    client.close()
    assert len(sent) == 6  # never three in a row, never tripped


def test_a_slow_datacenter_report_is_not_a_refusal(tmp_path, monkeypatch):
    """RPT_SHAREBONUS_DET timed out on 2026-09-17; that must not close datacenter."""
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr("cnequity.adapters.eastmoney.em_auth.get_nid", lambda *a, **k: "")

    def _slow(url):
        raise httpx.ReadTimeout("The read operation timed out")

    client, sent = _client(_cfg(tmp_path), _slow)
    for _ in range(5):
        with pytest.raises(httpx.ReadTimeout):
            client.get(_DATACENTER)
    client.close()
    assert len(sent) == 5


def test_datacenter_rate_limit_envelope_cools_after_one_attempt(tmp_path, monkeypatch):
    from cnequity.adapters.eastmoney.datacenter import fetch_datacenter
    from cnequity.domain.http_policy import SourceCoolingDown

    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr("cnequity.adapters.eastmoney.em_auth.get_nid", lambda *a, **k: "")
    busy = {"success": False, "message": "请求过于频繁，请稍后再试", "result": None}
    client, sent = _client(_cfg(tmp_path), lambda url: _ok(url, busy))
    with pytest.raises(SourceCoolingDown, match="请求过于频繁"):
        fetch_datacenter(client, "RPT_X", "A", max_retries=2, retry_backoff_seconds=0)
    with pytest.raises(SourceCoolingDown):
        client.get(_DATACENTER)
    client.close()
    assert len(sent) == 1


def test_datacenter_budget_counts_and_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(host_guard, "_today", lambda: date(2026, 9, 28))
    monkeypatch.setattr("cnequity.adapters.eastmoney.em_auth.get_nid", lambda *a, **k: "")
    counted = _cfg(tmp_path)  # default: count only
    client, _ = _client(counted, _ok)
    for _ in range(4):
        client.get(_DATACENTER)
    client.close()
    assert host_guard.status(counted)["datacenter"]["requests"] == 4

    capped = _cfg(tmp_path, eastmoney_datacenter_daily_budget=5)
    client, _ = _client(capped, _ok)
    client.get(_DATACENTER)
    with pytest.raises(host_guard.DatacenterBudgetExhaustedError):
        client.get(_DATACENTER)
    client.close()


def test_config_gives_datacenter_its_own_lane_and_breaker(tmp_path, monkeypatch):
    monkeypatch.delenv("CNE_PUSH2_PAUSED", raising=False)
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n[sources.eastmoney]\nenabled = true\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.source_intervals["eastmoney_dc"] == 1.0
    assert cfg.source_concurrency_for("eastmoney_dc") == 2
    assert (cfg.eastmoney_datacenter_breaker, cfg.eastmoney_datacenter_breaker_strikes) == (True, 3)
    assert cfg.eastmoney_datacenter_daily_budget == 0

    from cnequity.adapters.throttle import _source_family

    assert _source_family("eastmoney_dc") == "eastmoney_dc"
