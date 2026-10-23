"""EastMoney auth helpers: NID cookie fetch/cache, header building, the push2
``ut`` token injection, and the EastMoneyClient request path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

import cnequity.adapters.eastmoney.em_auth as em
from cnequity.adapters.eastmoney.em_auth import EastMoneyClient


def _reset_state() -> None:
    em._NID_CACHE["nid"] = None
    em._NID_CACHE["expires"] = 0.0


def test_fetch_nid_returns_cookie_value(monkeypatch):
    class _Cookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class _Resp:
        class cookies:
            jar = [_Cookie("nid", "abc123"), _Cookie("other", "x")]

    class _Client:
        def post(self, url, json=None):
            return _Resp()

    assert em.fetch_nid(client=_Client()) == "abc123"


def test_fetch_nid_returns_empty_when_cookie_missing():
    class _Resp:
        class cookies:
            jar = []

    class _Client:
        def post(self, url, json=None):
            return _Resp()

    assert em.fetch_nid(client=_Client()) == ""


def test_fetch_nid_returns_empty_on_exception_and_closes_owned_client(monkeypatch):
    closed = {"v": False}

    class _OwnClient:
        def post(self, url, json=None):
            raise RuntimeError("network down")

        def close(self):
            closed["v"] = True

    monkeypatch.setattr(em.httpx, "Client", lambda timeout=10.0: _OwnClient())
    assert em.fetch_nid() == ""
    assert closed["v"] is True


def test_fetch_nid_does_not_close_caller_provided_client():
    closed = {"v": False}

    class _Client:
        def post(self, url, json=None):
            raise RuntimeError("boom")

        def close(self):
            closed["v"] = True

    assert em.fetch_nid(client=_Client()) == ""
    assert closed["v"] is False


def test_get_nid_caches_until_expiry(monkeypatch):
    _reset_state()
    calls = {"n": 0}

    def _fake_fetch(client=None):
        calls["n"] += 1
        return "nid-1"

    monkeypatch.setattr(em, "fetch_nid", _fake_fetch)
    assert em.get_nid() == "nid-1"
    assert em.get_nid() == "nid-1"
    assert calls["n"] == 1  # second call served from cache


def test_get_nid_refetches_after_expiry(monkeypatch):
    _reset_state()
    monkeypatch.setattr(em, "fetch_nid", lambda client=None: "")
    assert em.get_nid() == ""  # empty nid never populates the cache


def test_build_eastmoney_headers_push2_domain_no_nid_lookup(monkeypatch):
    monkeypatch.setattr(em, "get_nid", lambda: pytest.fail("must not be called for push2"))
    headers = em.build_eastmoney_headers("https://push2.eastmoney.com/api/qt/clist/get")
    assert headers["Referer"] == em._QUOTE_REFERER
    assert "Cookie" not in headers


def test_build_eastmoney_headers_eastmoney_domain_sets_cookie(monkeypatch):
    monkeypatch.setattr(em, "get_nid", lambda client=None: "nid-xyz")
    headers = em.build_eastmoney_headers("https://datacenter-web.eastmoney.com/api/data/v1/get")
    assert headers["Cookie"] == "nid=nid-xyz"


def test_build_eastmoney_headers_eastmoney_domain_no_nid(monkeypatch):
    monkeypatch.setattr(em, "get_nid", lambda client=None: "")
    headers = em.build_eastmoney_headers("https://datacenter-web.eastmoney.com/api/data/v1/get")
    assert "Cookie" not in headers


def test_build_eastmoney_headers_unknown_domain_is_plain():
    headers = em.build_eastmoney_headers("https://example.com/other")
    assert "Cookie" not in headers
    assert "Referer" not in headers


# --- push2 ut token ---------------------------------------------------------
# Every push2 call site relied on the old Chrome-TLS path to add ``ut``; the
# API rejects requests without it, so these pin the replacement.


def test_is_push2_url_matches_kline_and_clist_hosts():
    assert em.is_push2_url("https://push2his.eastmoney.com/api/qt/stock/kline/get")
    assert em.is_push2_url("https://91.push2his.eastmoney.com/api/qt/stock/kline/get")
    assert em.is_push2_url("https://push2.eastmoney.com/api/qt/clist/get")
    assert not em.is_push2_url("https://datacenter-web.eastmoney.com/api/data/v1/get")


def test_apply_push2_token_adds_ut_when_params_absent():
    assert em.apply_push2_token("https://push2his.eastmoney.com/api/qt/stock/kline/get", None) == {
        "ut": em._PUSH2_UT
    }


def test_apply_push2_token_merges_into_existing_params():
    out = em.apply_push2_token(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        {"secid": "1.600519"},
    )
    assert out == {"secid": "1.600519", "ut": em._PUSH2_UT}


def test_apply_push2_token_adds_ut_for_query_string_call_sites():
    # capital.py builds the fflow kline URL as a formatted query string with no
    # params dict; httpx merges the returned dict into that query.
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get?secid=1.000001&klt=101"
    assert em.apply_push2_token(url, None) == {"ut": em._PUSH2_UT}


def test_apply_push2_token_respects_caller_supplied_ut():
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    assert em.apply_push2_token(url, {"ut": "own-token"}) == {"ut": "own-token"}
    assert em.apply_push2_token(f"{url}?ut=own-token", None) is None


def test_apply_push2_token_leaves_non_push2_urls_alone():
    assert (
        em.apply_push2_token("https://datacenter-web.eastmoney.com/api/data/v1/get", None) is None
    )


def test_client_get_sends_ut_on_push2_requests():
    client = EastMoneyClient(min_interval=0.0)
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": None})

    client._client = httpx.Client(transport=httpx.MockTransport(_handler))
    client.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={"secid": "1.600519"},
    )
    client.close()
    assert f"ut={em._PUSH2_UT}" in seen["url"]
    assert "secid=1.600519" in seen["url"]


def test_client_get_preserves_a_prebuilt_query_string_when_adding_ut():
    """clist.py builds the whole query itself and passes no ``params``.

    Injecting ``ut`` must not cost the caller's own query. httpx <= 0.27 merged
    a ``params`` dict into an existing query; 0.28 replaces the query outright,
    which turned every clist request into ``?ut=...`` and silently under-fetched
    fund_flow, the ST board, instruments, rotation and valuation.
    """
    client = EastMoneyClient(min_interval=0.0)
    seen: dict = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": None})

    client._client = httpx.Client(transport=httpx.MockTransport(_handler))
    client.get(
        "https://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs=m%3A0%2Bt%3A6&fields=f12%2Cf14"
    )
    client.close()

    query = httpx.URL(seen["url"]).params
    assert query.get("ut") == em._PUSH2_UT
    # The caller's fields survive alongside it.
    for key, value in (("pn", "1"), ("pz", "100"), ("fid", "f12"), ("fs", "m:0+t:6")):
        assert query.get(key) == value, f"{key} was dropped from the clist query"
    assert query.get("fields") == "f12,f14"


def test_client_get_merges_ut_into_the_url_not_the_params_kwarg(monkeypatch):
    """Pin the mechanism, not just the result.

    The behavioral test above passes on httpx <= 0.27 even without the merge,
    because httpx did it for us. Asserting that nothing is handed to httpx as
    ``params`` keeps the guard meaningful on whichever version is installed.
    """
    client = EastMoneyClient(min_interval=0.0)
    seen: dict = {}

    def _fake_get(url, headers=None, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(client._client, "get", _fake_get)
    client.get("https://push2.eastmoney.com/api/qt/clist/get?fs=m%3A0&fields=f12")
    client.close()

    assert "params" not in seen["kwargs"], "params must be merged into the URL, not forwarded"
    query = httpx.URL(seen["url"]).params
    assert query.get("ut") == em._PUSH2_UT
    assert query.get("fs") == "m:0"
    assert query.get("fields") == "f12"


# --- client plumbing --------------------------------------------------------


def test_eastmoney_client_default_min_interval():
    client = EastMoneyClient()
    assert client.min_interval == 1.0
    client.close()


def test_eastmoney_client_httpx_client_fallback_to_proxies_kwarg(monkeypatch):
    calls: list[dict] = []
    real_client = httpx.Client

    def _flaky_client(**kwargs):
        calls.append(kwargs)
        if "proxy" in kwargs:
            raise TypeError("proxy kwarg not supported by this httpx version")
        return real_client(**{k: v for k, v in kwargs.items() if k != "proxies"})

    monkeypatch.setattr(em.httpx, "Client", _flaky_client)

    class _Cfg:
        eastmoney_proxy = "http://127.0.0.1:7890"
        eastmoney_timeout_sec = 5.0

    client = EastMoneyClient(config=_Cfg())
    assert len(calls) == 2
    assert "proxy" in calls[0]
    assert "proxies" in calls[1]
    client.close()


def test_eastmoney_client_throttle_sleeps_when_below_min_interval(monkeypatch):
    client = EastMoneyClient(min_interval=10.0)
    sleeps: list[float] = []
    monkeypatch.setattr(em.time, "sleep", lambda s: sleeps.append(s))
    client._last_request = em.time.time()
    client._throttle()
    assert sleeps and sleeps[0] > 0
    client.close()


def test_eastmoney_client_throttle_zero_interval_is_noop(monkeypatch):
    client = EastMoneyClient(min_interval=0.0)
    monkeypatch.setattr(
        em.time, "sleep", lambda s: pytest.fail("must not sleep with min_interval<=0")
    )
    client._throttle()
    client.close()


def test_eastmoney_client_throttle_delegates_to_config_rate_limiter():
    calls: list[str] = []

    class _Cfg:
        eastmoney_proxy = None
        eastmoney_timeout_sec = 15.0

        def rate_limit(self, source):
            calls.append(source)

    client = EastMoneyClient(config=_Cfg())
    client._throttle()
    assert calls == ["eastmoney"]
    client.close()


def test_eastmoney_client_get_uses_plain_httpx(monkeypatch):
    client = EastMoneyClient(min_interval=0.0)
    seen = {}

    def _fake_get(url, headers=None, **kwargs):
        seen["url"] = url
        seen["headers"] = headers
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(client._client, "get", _fake_get)
    resp = client.get("https://datacenter-web.eastmoney.com/api/data/v1/get")
    assert resp.status_code == 200
    assert "User-Agent" in seen["headers"]
    client.close()


def test_eastmoney_client_get_routes_push2his_through_the_same_pool(monkeypatch):
    client = EastMoneyClient(min_interval=0.0)
    seen = {}

    def _fake_get(url, headers=None, **kwargs):
        seen.update(url=url, params=kwargs.get("params"), timeout=kwargs.get("timeout"))
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(client._client, "get", _fake_get)
    resp = client.get(
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        params={"secid": "1.600519"},
        timeout=30.0,
    )
    assert resp.status_code == 200
    # Caller params and the injected ut both ride in the URL now — see
    # test_client_get_merges_ut_into_the_url_not_the_params_kwarg.
    assert seen["params"] is None
    query = httpx.URL(seen["url"]).params
    assert query.get("secid") == "1.600519"
    assert query.get("ut") == em._PUSH2_UT
    assert seen["timeout"] == 30.0
    client.close()


def test_eastmoney_client_post_builds_headers_and_calls_underlying_client(monkeypatch):
    client = EastMoneyClient(min_interval=0.0)
    seen = {}

    def _fake_post(url, headers=None, **kwargs):
        seen["headers"] = headers
        resp = MagicMock()
        resp.status_code = 200
        return resp

    monkeypatch.setattr(client._client, "post", _fake_post)
    resp = client.post("https://www.cninfo.com.cn/new/hisAnnouncement/query", data={"a": 1})
    assert resp.status_code == 200
    assert "User-Agent" in seen["headers"]
    client.close()


def test_eastmoney_client_context_manager_closes_on_exit(monkeypatch):
    closed = {"v": False}
    client = EastMoneyClient(min_interval=0.0)
    monkeypatch.setattr(client, "close", lambda: closed.__setitem__("v", True))
    with client as ctx:
        assert ctx is client
    assert closed["v"] is True
