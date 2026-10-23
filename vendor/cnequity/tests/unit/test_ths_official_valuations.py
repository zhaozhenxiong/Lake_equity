"""Valuation snapshots: field mapping, and surviving a code the upstream rejects."""

from datetime import date

import pytest

from cnequity.adapters.ths_official.valuations import (
    BATCH_SIZE,
    VALUATION_FIELDS,
    fetch_valuation_snapshot,
)


class _StubClient:
    """Rejects any batch containing a code in ``bad``, as the service does."""

    def __init__(self, bad: set[str] | None = None):
        self.bad = bad or set()
        self.batches: list[list[str]] = []

    def get(self, path, **params):
        batch = params["thscodes"].split(",")
        self.batches.append(batch)
        if self.bad & set(batch):
            raise RuntimeError("code=3001 Target not found in A-share code table")
        return {
            "item": [
                {"thscode": code, "pe_ttm": 18.7, "pb_mrq": 6.6, "ps_ttm": 9.2, "pcf_ttm": 13.4}
                for code in batch
            ]
        }


def test_pb_reads_the_mrq_basis_and_market_cap_is_left_empty():
    """The upstream publishes no market cap; computing one would invent a share count."""
    assert VALUATION_FIELDS["pb"] == "pb_mrq"
    frame, _ = fetch_valuation_snapshot(
        ["600519.SH"], client=_StubClient(), as_of=date(2026, 9, 12)
    )
    row = frame.to_dicts()[0]
    assert row["pb"] == pytest.approx(6.6)
    assert row["total_mv"] is None
    assert row["float_mv"] is None


def test_one_rejected_code_does_not_lose_the_batch_around_it():
    """A whole 100-symbol request fails on one unknown code.

    Measured before the halving: four of five batches came back empty that way,
    and 400 perfectly good securities went with them.
    """
    symbols = [f"60{index:04d}.SH" for index in range(8)]
    client = _StubClient(bad={symbols[3]})
    frame, counters = fetch_valuation_snapshot(symbols, client=client, as_of=date(2026, 9, 12))
    assert sorted(frame.get_column("symbol").to_list()) == sorted(
        code for code in symbols if code != symbols[3]
    )
    assert counters["failed"] == 1
    assert counters["split_retries"] > 0


def test_halving_stops_at_a_single_symbol():
    """Every code rejected: the recursion must terminate, not spin."""
    symbols = [f"60{index:04d}.SH" for index in range(4)]
    frame, counters = fetch_valuation_snapshot(
        symbols, client=_StubClient(bad=set(symbols)), as_of=date(2026, 9, 12)
    )
    assert frame.is_empty()
    assert counters["failed"] == len(symbols)


def test_requests_respect_the_server_side_token_cap():
    symbols = [f"60{index:04d}.SH" for index in range(250)]
    client = _StubClient()
    fetch_valuation_snapshot(symbols, client=client, as_of=date(2026, 9, 12))
    assert max(len(batch) for batch in client.batches) <= BATCH_SIZE


def test_a_security_with_no_usable_ratio_is_dropped():
    class _Empty(_StubClient):
        def get(self, path, **params):
            return {
                "item": [{"thscode": "600519.SH", "pe_ttm": None, "pb_mrq": None, "ps_ttm": None}]
            }

    frame, _ = fetch_valuation_snapshot(["600519.SH"], client=_Empty(), as_of=date(2026, 9, 12))
    assert frame.is_empty()


def test_the_snapshot_is_inert_without_a_key(tmp_path):
    from cnequity.config import Config
    from cnequity.steps.capital import snapshot_valuations_ths_official

    out = snapshot_valuations_ths_official(Config(data_root=tmp_path), "run-1")
    assert out["status"] == "skipped"
    assert out["rows_written"] == 0
