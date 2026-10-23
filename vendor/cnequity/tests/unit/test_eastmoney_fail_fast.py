"""EastMoney transport errors must not burn full retry budgets."""

from __future__ import annotations

import httpx
import pytest

from cnequity.adapters.eastmoney.clist import _fetch_clist_page
from cnequity.adapters.eastmoney.datacenter import (
    EastMoneyDatacenterError,
    fetch_datacenter,
)
from cnequity.adapters.eastmoney.em_auth import is_transport_fail_fast


def test_is_transport_fail_fast():
    assert is_transport_fail_fast(httpx.TimeoutException("t"))
    assert is_transport_fail_fast(httpx.ConnectError("c"))
    assert is_transport_fail_fast(httpx.RemoteProtocolError("r"))
    # A dead proxy is the overseas equivalent of a dead route: with
    # [sources.eastmoney].proxy set, every retry goes back through it.
    assert is_transport_fail_fast(httpx.ProxyError("p"))
    assert not is_transport_fail_fast(RuntimeError("other"))


def test_read_timeout_is_fail_fast_not_retryable():
    """A read timeout is a dead-route signal here, not a transient to retry.

    Pinned because the predicate reads backwards to anyone expecting a
    generic HTTP client: `httpx.TimeoutException` is the base class of
    `ReadTimeout`, so read timeouts fail fast on purpose. A blocked egress to
    EastMoney times out rather than refusing, and retrying it cost 151 s per
    daily run to return nothing (see `commodity_bars.py`). `bars.py` also
    counts these toward a circuit breaker that only works if a dead route is
    reported dead on the first request.
    """
    assert is_transport_fail_fast(httpx.ReadTimeout("read"))
    assert is_transport_fail_fast(httpx.ConnectTimeout("connect"))
    assert is_transport_fail_fast(httpx.PoolTimeout("pool"))


def test_edge_5xx_fails_fast_but_app_level_busy_does_not():
    """Only the HTTP edge's 5xx is a route failure.

    EastMoney signals throttling in the response *body* with HTTP 200, which
    `datacenter.py` raises as `_ServerBusy`. That must stay retryable, or
    ordinary throttling would abort the sweep.
    """
    request = httpx.Request("GET", "https://push2.eastmoney.com/api")

    def status_error(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(
            f"{code}", request=request, response=httpx.Response(code, request=request)
        )

    assert is_transport_fail_fast(status_error(503))
    assert is_transport_fail_fast(status_error(500))
    # 4xx is the caller's own bad request, not a dead route: not fail-fast.
    assert not is_transport_fail_fast(status_error(404))

    from cnequity.adapters.eastmoney.datacenter import _ServerBusy

    assert not is_transport_fail_fast(_ServerBusy("busy on page 1"))


def test_datacenter_does_not_retry_timeout():
    calls = {"n": 0}

    class FakeClient:
        def get(self, url):
            calls["n"] += 1
            raise httpx.TimeoutException("slow")

    with pytest.raises(EastMoneyDatacenterError, match="failed after"):
        fetch_datacenter(
            FakeClient(),
            "RPT_TEST",
            "SECUCODE",
            max_retries=3,
            retry_backoff_seconds=0,
        )
    assert calls["n"] == 1


def test_clist_does_not_retry_connect_error():
    calls = {"n": 0}

    class FakeClient:
        def get(self, url):
            calls["n"] += 1
            raise httpx.ConnectError("down")

    with pytest.raises(RuntimeError, match="clist page"):
        _fetch_clist_page(
            FakeClient(),
            host="https://push2.eastmoney.com",
            fields="f12",
            fs="m:1",
            page=1,
            page_size=10,
            max_retries=3,
            retry_backoff_seconds=0,
        )
    assert calls["n"] == 1
