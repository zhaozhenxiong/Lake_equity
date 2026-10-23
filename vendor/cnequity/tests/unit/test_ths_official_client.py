"""The 同花顺 official API client's protocol handling.

Each case encodes a shape measured against the live service on 2026-09-08/09.
Several contradict the published contract, which is why they are pinned here.
"""

import httpx
import pytest

from cnequity.adapters.ths_official import (
    ThsOfficialAuthError,
    ThsOfficialClient,
    ThsOfficialError,
    ThsOfficialParameterError,
    client_from_config,
)
from cnequity.adapters.ths_official.client import ThsOfficialRateLimited
from cnequity.config import Config


def _client(handler, **kwargs) -> ThsOfficialClient:
    return ThsOfficialClient(
        "sk-test", transport=httpx.MockTransport(handler), timeout=1.0, **kwargs
    )


def _envelope(code, message="success", data=None, status=200):
    return httpx.Response(
        status,
        json={"code": code, "message": message, "request_id": "abc123", "data": data},
    )


def test_a_successful_envelope_yields_its_data():
    client = _client(lambda request: _envelope(0, data={"item": [{"close_price": 1.0}]}))
    assert client.get("/api/a-share/prices/historical", thscode="600519.SH") == {
        "item": [{"close_price": 1.0}]
    }


def test_3002_is_an_empty_result_not_a_failure():
    """A security with no dividends must not fail the sweep that asks for them."""
    client = _client(
        lambda request: _envelope(3002, "No adjustment events for thscode=688062.SH", data=None)
    )
    assert client.get("/api/a-share/corporate-actions/adjustment-factors") is None


@pytest.mark.parametrize(
    ("message", "key_is_bad"),
    [
        ("Missing X-api-key", True),
        ("Invalid or revoked API key", True),
        ("capability not granted for this key", False),
    ],
)
def test_2003_is_overloaded_and_separated_by_message(message, key_is_bad):
    """The docs promise 2001 for a bad key; the service only ever sends 2003.

    So the code cannot tell "this key is broken" from "this key does not reach
    this endpoint", and only the message can. The first should stop a run, the
    second should disable one capability.
    """
    client = _client(lambda request: _envelope(2003, message))
    with pytest.raises(ThsOfficialAuthError) as excinfo:
        client.get("/api/meta/tickers/list")
    assert excinfo.value.granted_elsewhere is not key_is_bad
    assert excinfo.value.code == 2003


def test_parameter_errors_are_distinct_and_never_retried():
    calls = []

    def handler(request):
        calls.append(request.url)
        return _envelope(1003, "Invalid parameter format: interval")

    client = _client(handler)
    with pytest.raises(ThsOfficialParameterError):
        client.get("/api/a-share/prices/historical", interval="1M")
    assert len(calls) == 1


def test_rate_limiting_surfaces_without_repeated_requests(monkeypatch):
    monkeypatch.setattr("cnequity.adapters.ths_official.client.time.sleep", lambda _: None)
    calls = []

    def handler(request):
        calls.append(request.url)
        return _envelope(4001, "rate limited")

    client = _client(handler)
    with pytest.raises(ThsOfficialError):
        client.get("/api/a-share/prices/snapshot")
    assert len(calls) == 1


def test_http_429_stops_without_an_extra_retry(monkeypatch):
    monkeypatch.setattr(
        "cnequity.adapters.ths_official.client.time.sleep", lambda _: pytest.fail("sleep")
    )
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429)

    client = _client(respond)
    with pytest.raises(ThsOfficialRateLimited):
        client.get("/api/a-share/prices/snapshot")
    assert len(calls) == 1


def test_a_transport_failure_is_retried_then_reported(monkeypatch):
    monkeypatch.setattr("cnequity.adapters.ths_official.client.time.sleep", lambda _: None)
    attempts = []

    def handler(request):
        attempts.append(request.url)
        raise httpx.ConnectError("connection reset", request=request)

    client = _client(handler)
    with pytest.raises(ThsOfficialError, match="transport failed"):
        client.get("/api/a-share/calendar/trading-days")
    assert len(attempts) == 4


def test_a_transient_transport_failure_recovers(monkeypatch):
    monkeypatch.setattr("cnequity.adapters.ths_official.client.time.sleep", lambda _: None)
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _envelope(0, data={"item": []})

    assert _client(handler).get("/api/a-share/calendar/trading-days") == {"item": []}


def test_an_html_body_names_the_wrong_dump_path():
    """`/dump/**` is the browser entry point and is served by the docs SPA.

    A client that uses it receives a rendered page with a 200 status, so without
    this the failure surfaces as an opaque JSON decode error.
    """
    client = _client(
        lambda request: httpx.Response(
            200, text="<!doctype html><html>...", headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(ThsOfficialError, match="/api/dump"):
        client.get("/dump/market-dumps/daily-k/download-url")


def test_download_url_returns_the_presigned_link_and_its_lifetime():
    client = _client(
        lambda request: _envelope(
            0, data={"presigned_url": "https://o.thsi.cn/x", "expires_in_seconds": 300}
        )
    )
    url, ttl = client.download_url("adjustment-factors")
    assert url == "https://o.thsi.cn/x"
    assert ttl == 300


def test_a_dump_response_without_a_link_is_an_error():
    """The field is `presigned_url`; reading `url` finds nothing and reports success."""
    client = _client(lambda request: _envelope(0, data={"url": "https://o.thsi.cn/x"}))
    with pytest.raises(ThsOfficialError, match="presigned_url"):
        client.download_url("daily-k")


def test_the_key_never_appears_in_the_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["header"] = request.headers.get("x-api-key")
        return _envelope(0, data={"item": []})

    _client(handler).get("/api/meta/tickers/list", asset_type="a-share")
    assert "sk-test" not in seen["url"]
    assert seen["header"] == "sk-test"


def test_none_valued_parameters_are_dropped():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return _envelope(0, data={"item": []})

    _client(handler).get("/api/a-share/prices/historical", thscode="600519.SH", offset=None)
    assert "offset" not in seen["url"]


def test_no_key_means_no_client(tmp_path):
    """The source is optional: a lake without a key keeps its existing sources."""
    assert client_from_config(Config(data_root=tmp_path)) is None


def test_a_key_without_the_source_enabled_stays_off(tmp_path):
    config = Config(data_root=tmp_path, ths_official_api_key="sk-test")
    assert client_from_config(config) is None
    config.sources["ths_official"] = True
    client = client_from_config(config)
    assert client is not None
    client.close()


def test_an_empty_key_is_refused():
    with pytest.raises(ThsOfficialError, match="requires an API key"):
        ThsOfficialClient("   ")
