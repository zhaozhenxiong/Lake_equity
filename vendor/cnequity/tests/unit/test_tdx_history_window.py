"""Offline offset servers: compare date seeking with a complete local history."""

from datetime import date, datetime, timedelta

import pytest

from cnequity.adapters.tdx_protocol.bars import TdxBarsPaginationError, fetch_bars_paginated
from cnequity.adapters.tdx_protocol.minute_bars import (
    TdxMinuteBarsError,
    fetch_minute_bars_paginated,
)


class OffsetServer:
    def __init__(self, kind):
        self.kind = kind
        # Deliberately skip dates, as suspended/delisted names need not follow
        # the exchange calendar. Minute pages also split a trading session.
        days = [date(2026, 1, 1) + timedelta(days=i * 2) for i in range(100)]
        stamps = (
            [datetime.combine(d, datetime.min.time()) for d in days]
            if kind != "minute"
            else [
                datetime(d.year, d.month, d.day, 9, 31) + timedelta(minutes=m)
                for d in days
                for m in range(100)
            ]
        )
        self.rows = [
            dict(datetime=s, open=10, high=11, low=9, close=10, vol=5, amount=50) for s in stamps
        ]
        self.calls = []
        self.fail_at = None
        self.moving_tip = False
        self.revalue_tip = False
        self.repeat = False
        self.corrupt_at = None

    def bars(self, *, start, offset, **kwargs):
        self.calls.append((start, kwargs))
        if self.fail_at == start:
            raise ConnectionError("test failure")
        stop = len(self.rows) - (0 if self.repeat else start)
        page = [dict(r) for r in self.rows[max(0, stop - offset) : max(0, stop)]]
        if start == self.corrupt_at and page:
            for row in page:
                row["datetime"] = str(row["datetime"])
            page[0]["datetime"] = "unparseable"
        if start == 0 and len(self.calls) > 1 and page:
            if self.moving_tip:
                page[-1]["datetime"] += timedelta(days=1)
            if self.revalue_tip:
                page[-1]["vol"] += 1
        return page

    def index(self, **kwargs):
        return self.bars(**kwargs)


@pytest.fixture(params=["daily", "index", "minute"])
def history(request, monkeypatch):
    kind = request.param
    server = OffsetServer(kind)
    # Small daily pages make a many-decade archive unnecessary. Minutes use
    # the real wire page size, with multiple pages per date window.
    page_size = 3 if kind != "minute" else 800
    if kind != "minute":
        monkeypatch.setattr("cnequity.adapters.tdx_protocol.bars._PAGE_SIZE", page_size)

    def fetch(start, end, **kwargs):
        if kind == "minute":
            return fetch_minute_bars_paginated(server, "600519.SH", start, end, **kwargs)
        return fetch_bars_paginated(
            server, "600519.SH", start, end, is_index=kind == "index", **kwargs
        )

    return server, fetch, page_size


def test_seek_returns_exact_window_and_reuses_probes(history):
    server, fetch, size = history
    start, end = date(2026, 1, 11), date(2026, 1, 19)
    expected = [r for r in server.rows if start <= r["datetime"].date() <= end]
    rows = fetch(start, end, backfill=True)
    assert [r.get("bar_time", r["trade_date"]) for r in rows] == [
        r["datetime"] if server.kind == "minute" else r["datetime"].date() for r in expected
    ]
    sequential_requests = (len(server.rows) - server.rows.index(expected[0])) // size + 1
    assert len(server.calls) < sequential_requests
    offsets = [offset for offset, _ in server.calls]
    assert offsets.count(0) == 2  # one final timestamp-only consistency check
    assert len(offsets[1:-1]) == len(set(offsets[1:-1]))
    assert rows[0]["volume"] == (500 if server.kind == "daily" else 5)
    assert all(("market" in args) == (server.kind != "index") for _, args in server.calls)


@pytest.mark.parametrize("window", ["tip", "whole", "older", "short_tail", "gap"])
def test_seek_matches_sequential_for_boundaries(history, window):
    server, fetch, _ = history
    lo, hi = server.rows[0]["datetime"].date(), server.rows[-1]["datetime"].date()
    start, end = {
        "tip": (hi, hi),
        "whole": (lo, hi),
        "older": (lo - timedelta(days=9), lo - timedelta(days=1)),
        "short_tail": (lo, lo),
        "gap": (lo + timedelta(days=1), lo + timedelta(days=1)),
    }[window]
    baseline = fetch(start, end)
    baseline_calls = len(server.calls)
    server.calls.clear()
    assert fetch(start, end, backfill=True) == baseline
    if window in {"tip", "whole"}:
        assert len(server.calls) == baseline_calls


def test_seek_errors_do_not_return_a_partial_window(history):
    server, fetch, size = history
    server.fail_at = 4 * size
    with pytest.raises((TdxBarsPaginationError, TdxMinuteBarsError), match=f"start={4 * size}"):
        fetch(date(2026, 1, 1), date(2026, 1, 3), backfill=True)


def test_seek_rejects_a_moving_tip_but_allows_revalued_bars(history):
    server, fetch, _ = history
    server.revalue_tip = True
    assert fetch(date(2026, 1, 1), date(2026, 1, 3), backfill=True)
    server.calls.clear()
    server.moving_tip = True
    with pytest.raises((TdxBarsPaginationError, TdxMinuteBarsError), match="tip changed"):
        fetch(date(2026, 1, 1), date(2026, 1, 3), backfill=True)


def test_seek_refuses_nonadvancing_pages(history):
    server, fetch, _ = history
    server.repeat = True
    with pytest.raises((TdxBarsPaginationError, TdxMinuteBarsError), match="did not advance"):
        fetch(date(2026, 1, 1), date(2026, 1, 3), backfill=True)


def test_undated_probe_falls_back_without_skipping_window(history):
    server, fetch, size = history
    server.corrupt_at = 4 * size
    expected = fetch(date(2026, 1, 1), date(2026, 1, 3))
    server.calls.clear()
    assert fetch(date(2026, 1, 1), date(2026, 1, 3), backfill=True) == expected
    offsets = [offset for offset, _ in server.calls]
    assert 3 * size in offsets


def test_minute_seek_preserves_depth_cap():
    server = OffsetServer("minute")
    with pytest.raises(TdxMinuteBarsError, match="page limit 8.*window start"):
        fetch_minute_bars_paginated(
            server,
            "600519.SH",
            date(2026, 1, 1),
            date(2026, 1, 3),
            backfill=True,
            max_pages=8,
            require_complete=True,
        )
    assert max(offset for offset, _ in server.calls) == 7 * 800


def test_seek_rejects_out_of_order_history(monkeypatch):
    server = OffsetServer("minute")
    original = server.bars

    def reordered(**kwargs):
        rows = original(**kwargs)
        if kwargs["start"] == 4 * 800:
            for row in rows:
                row["datetime"] += timedelta(days=200)
        return rows

    monkeypatch.setattr(server, "bars", reordered)
    with pytest.raises(TdxMinuteBarsError, match="out of date order"):
        fetch_minute_bars_paginated(
            server, "600519.SH", date(2026, 1, 1), date(2026, 1, 3), backfill=True
        )


def test_history_depth_cannot_wrap_the_wire_offset():
    from cnequity.adapters.tdx_protocol.history_window import window_pages

    calls = []

    def fetch(offset):
        calls.append(offset)
        return [date(2026, 1, 1) - timedelta(days=offset + i) for i in range(800)]

    with pytest.raises(RuntimeError, match="wire limit"):
        list(
            window_pages(
                fetch,
                lambda page: page,
                date(1700, 1, 1),
                date(1700, 1, 2),
                page_size=800,
                page_limit=1000,
                seek=True,
                strict_limit=True,
                detect_repeats=True,
                error=RuntimeError,
                label="test",
                limit_message="page cap",
            )
        )
    assert max(calls) == 64800


def test_daily_seek_accounts_and_paces_every_actual_request(monkeypatch):
    from contextlib import contextmanager

    from cnequity.adapters.tdx_protocol import bars

    server = OffsetServer("daily")
    monkeypatch.setattr(bars, "_PAGE_SIZE", 3)
    paced, slots, heartbeats = [], [], []

    @contextmanager
    def slot(spec, **kwargs):
        slots.append(spec)
        yield

    monkeypatch.setattr(bars, "source_request_slot_spec", slot)
    monkeypatch.setattr(bars, "wait_spec", paced.append)
    metrics = {}
    rows = fetch_bars_paginated(
        server,
        "600519.SH",
        date(2026, 1, 1),
        date(2026, 1, 3),
        backfill=True,
        metrics=metrics,
        on_page=lambda: heartbeats.append(1),
    )
    assert metrics["requests"] == metrics["pages"] == len(server.calls) == len(paced) == len(slots)
    assert len(heartbeats) == len(server.calls) - 1
    assert metrics["rows_read"] == len(rows)
