"""A broken later page cannot certify a complete news window."""

from datetime import date, datetime

import httpx
import pytest

from cnequity.adapters.eastmoney import news_feed, rotation, stock_news


def _client(payloads):
    class Client:
        def __init__(self, **kwargs):
            self.pages = iter(payloads)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def close(self):
            pass

        def get(self, url, **kwargs):
            return httpx.Response(200, json=next(self.pages), request=httpx.Request("GET", url))

    return Client


@pytest.mark.parametrize("data", [None, {}, {"fastNewsList": None}])
@pytest.mark.parametrize("incremental", [False, True])
def test_null_later_page_fails_the_news_walk(monkeypatch, data, incremental):
    first = {
        "data": {
            "fastNewsList": [{"code": "n1", "title": "news", "showTime": "2026-10-09 16:00:00"}],
            "sortEnd": "next",
        }
    }
    adapter = news_feed if incremental else rotation
    monkeypatch.setattr(adapter, "EastMoneyClient", _client([first, {"data": data}]))
    with pytest.raises(RuntimeError, match="response (data|rows)"):
        if incremental:
            adapter.fetch_news_since(datetime(2026, 10, 9, tzinfo=news_feed.CST))
        else:
            adapter.fetch_news_headlines(date(2026, 10, 9))


@pytest.mark.parametrize("data", [None, {}, {"list": None}])
def test_stock_news_null_later_page_reports_error(data):
    first = {
        "data": {"list": [{"art_code": "n1", "title": "news", "showtime": "2026-10-09 16:00:00"}]}
    }
    result = stock_news.fetch_stock_news(
        "600519.SH",
        on_date=date(2026, 10, 9),
        limit=1,
        use_snownlp=False,
        client=_client([first, {"data": data}])(),
    )
    assert "response data" in result["error"]
    assert result["items"] == []


def test_valid_empty_news_list_is_not_a_source_error(monkeypatch):
    monkeypatch.setattr(
        news_feed, "EastMoneyClient", _client([{"data": {"fastNewsList": [], "sortEnd": ""}}])
    )
    frame, reached = news_feed.fetch_news_since(datetime(2026, 10, 9, tzinfo=news_feed.CST))
    assert frame.is_empty()
    assert not reached
