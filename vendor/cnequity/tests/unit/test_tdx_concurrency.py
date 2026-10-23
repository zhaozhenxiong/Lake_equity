"""TDX request ownership and concurrency contracts."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Lock

from cnequity.adapters.tdx_protocol import client as tdx
from cnequity.adapters.tdx_protocol.bars import fetch_bars_paginated
from cnequity.domain.rate_limit import RateLimitSpec


def test_daily_bar_calls_overlap_with_one_client_per_invocation(monkeypatch):
    """Independent fetches must reach the wire at the same time.

    This deliberately exercises ``fetch_daily_bars`` rather than mocking the
    worker pool: a process-wide session lock around this function would make
    the second request wait forever at the barrier.  The fake factory also
    proves that the overlapping requests do not share a socket client.
    """

    barrier = Barrier(2)
    clients = []
    clients_lock = Lock()

    class FakeClient:
        pass

    def factory(_config=None):
        client = FakeClient()
        with clients_lock:
            clients.append(client)
        return client

    def fetch_page(_client, symbol, start, _end, **_kwargs):
        barrier.wait(timeout=2.0)
        return [{"symbol": symbol, "trade_date": start, "close": 10.0}]

    monkeypatch.setattr(tdx, "_quotes_client", factory)
    monkeypatch.setattr(tdx, "_close_quotes_client", lambda _client: None)
    monkeypatch.setattr(tdx, "fetch_bars_paginated", fetch_page)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                tdx.fetch_daily_bars,
                [symbol],
                date(2026, 8, 28),
                date(2026, 8, 28),
            )
            for symbol in ("600000.SH", "000001.SZ")
        ]
        frames = [future.result() for future in futures]

    assert all(frame.height == 1 for frame in frames)
    assert len(clients) == 2
    assert clients[0] is not clients[1]


def test_tdx_page_boundary_uses_global_inflight_cap(tmp_path):
    active = 0
    peak = 0
    lock = Lock()
    first_pair = Barrier(2)

    class FakeClient:
        def bars(self, *, symbol, frequency, market, start, offset):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if peak <= 2:
                    first_pair.wait(timeout=2.0)
                time.sleep(0.01)
                return [
                    {
                        "date": "2026-08-28",
                        "open": 1.0,
                        "high": 1.0,
                        "low": 1.0,
                        "close": 1.0,
                        "volume": 1,
                        "amount": 1.0,
                    }
                ]
            finally:
                with lock:
                    active -= 1

    spec = RateLimitSpec(
        str(tmp_path / "rate_limits"),
        "tdx_protocol",
        0.0,
        concurrency_limit=2,
        concurrency_state_dir=str(tmp_path / "rate_limits"),
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                fetch_bars_paginated,
                FakeClient(),
                f"{code}.SH",
                date(2026, 8, 28),
                date(2026, 8, 28),
                rate_limit=spec,
            )
            for code in ("600000", "600001", "600002", "600003")
        ]
        for future in futures:
            assert future.result()

    assert peak == 2
    assert active == 0
