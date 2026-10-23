"""Slow-call coverage for every source boundary that previously only paced."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace

import pytest

from cnequity.config import Config


class _Tracker:
    def __init__(self, delay: float = 0.03):
        self.delay = delay
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def call(self, result):
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.delay)
            return result
        finally:
            with self._lock:
                self.active -= 1


def _config(tmp_path, source: str, limit: int = 1) -> Config:
    return Config(
        data_root=tmp_path / source,
        workers=4,
        source_intervals={source: 0.0},
        source_concurrency={source: limit},
    )


def _run_four(function):
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(function, range(4)))


def test_nbs_curl_calls_hold_the_nbs_lease(monkeypatch, tmp_path):
    from cnequity.adapters.nbs import pmi_release

    tracker = _Tracker()
    index = '<a href="./202607/t20260731_1964253.html">2026年7月中国采购经理指数运行情况</a>'
    release = "<p>制造业采购经理指数（PMI）为49.2%</p>"

    class Response:
        def __init__(self, url):
            self.text = index if url == pmi_release.RELEASE_INDEX else release
            self.encoding = None

        def raise_for_status(self):
            return None

    def get(url, **_kwargs):
        return tracker.call(Response(url))

    from curl_cffi import requests as cr

    monkeypatch.setattr(cr, "get", get)
    cfg = _config(tmp_path, "nbs", limit=1)
    out = _run_four(lambda _index: pmi_release.fetch_latest_pmi(config=cfg))

    assert all(item and item["value"] == 49.2 for item in out)
    assert tracker.peak <= 1


def test_pboc_text_calls_hold_the_pboc_lease(monkeypatch, tmp_path):
    from cnequity.adapters.pboc import _tables

    tracker = _Tracker()

    class Response:
        text = "pbc"
        encoding = None

        def raise_for_status(self):
            return None

    class Client:
        def get(self, _url, **_kwargs):
            return tracker.call(Response())

    monkeypatch.setattr(_tables, "client", lambda: Client())
    cfg = _config(tmp_path, "pboc", limit=1)
    out = _run_four(lambda _index: _tables.get_text("https://pbc.test", config=cfg))

    assert out == ["pbc"] * 4
    assert tracker.peak <= 1


def test_sina_bar_calls_hold_the_sina_bars_lease(tmp_path):
    from cnequity.adapters.sina import bars

    tracker = _Tracker()

    class Client:
        def get(self, _url, **_kwargs):
            response = SimpleNamespace(
                text=json.dumps(
                    [
                        {
                            "day": "2026-01-02",
                            "open": "1",
                            "high": "1",
                            "low": "1",
                            "close": "1",
                            "volume": "100",
                        }
                    ]
                ),
                raise_for_status=lambda: None,
            )
            return tracker.call(response)

    cfg = _config(tmp_path, "sina_bars", limit=1)
    client = Client()
    out = _run_four(lambda index: bars._request(f"6005{index:02d}.SH", 10, client, config=cfg))

    assert all(result and result[0]["day"] == "2026-01-02" for result in out)
    assert tracker.peak <= 1


def test_baostock_injected_deadline_calls_hold_the_baostock_lease(tmp_path):
    from cnequity.adapters.baostock._session import fetch_per_symbol

    tracker = _Tracker()

    class Baostock:
        def login(self):
            return SimpleNamespace(error_code="0", error_msg="")

        def logout(self):
            return None

    cfg = _config(tmp_path, "baostock", limit=1)

    def run(index):
        def fetch(_bs, symbol, _start, _end):
            tracker.call(None)
            return [{"symbol": symbol}]

        return fetch_per_symbol(
            [f"6005{index:02d}.SH"],
            date(2026, 1, 1),
            date(2026, 1, 2),
            fetch,
            bs=Baostock(),
            config=cfg,
            sleep=lambda _seconds: None,
            deadline=1.0,
        )[0]

    out = _run_four(run)
    assert sum(len(rows) for rows in out) == 4
    assert tracker.peak <= 1


def test_delisted_retry_wrapper_holds_the_sina_bars_lease(tmp_path):
    from cnequity.steps.delisted import _run_sina_with_retry

    tracker = _Tracker()
    cfg = _config(tmp_path, "sina_bars", limit=1)

    def run(index):
        return _run_sina_with_retry(
            lambda: tracker.call(index),
            symbol=f"6005{index:02d}.SH",
            operation_name="test",
            config=cfg,
        )

    assert sorted(_run_four(run)) == [0, 1, 2, 3]
    assert tracker.peak <= 1


@pytest.mark.parametrize("source", ["nbs", "pboc", "sina_bars", "baostock"])
def test_source_request_releases_after_an_exception(tmp_path, source):
    cfg = _config(tmp_path, source, limit=1)
    with pytest.raises(RuntimeError, match="fixture failure"):
        with cfg.source_request(source):
            raise RuntimeError("fixture failure")

    # If the failed context leaked its lease this second request would time out.
    with cfg.source_request(source, timeout=0.2):
        pass
