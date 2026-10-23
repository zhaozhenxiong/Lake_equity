"""Sector index bars: code alignment, the history floor, and its silent failure."""

from datetime import date, datetime, timedelta, timezone

import polars as pl

from cnequity.adapters.ths_official.sectors import (
    BOARD_TAGS,
    HISTORY_FLOOR,
    fetch_sector_bars,
    fetch_sector_catalog,
)

CST = timezone(timedelta(hours=8))


def _ms(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=CST).timestamp() * 1000)


class _StubClient:
    def __init__(self, bars=None):
        self.bars = bars if bars is not None else []
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if "catalog" in path:
            tag = params["tag"]
            return {"item": [{"thscode": f"88{tag[:1]}101.TI", "name": f"board-{tag}"}]}
        return {"item": self.bars}


def _bar(day=date(2024, 6, 3)):
    return {
        "date_ms": _ms(day),
        "open_price": 1000.0,
        "high_price": 1010.0,
        "low_price": 995.0,
        "close_price": 1005.0,
        "volume": 12_345_678.0,
        "turnover": 9_876_543.0,
    }


def test_the_catalogue_drops_the_ti_suffix_so_codes_join_the_lake():
    """881101.TI and the lake's 881101 are the same industry index."""
    catalog = fetch_sector_catalog(_StubClient())
    assert set(catalog.get_column("sector_code").to_list()) == {"88i101", "88c101"}
    assert "." not in "".join(catalog.get_column("sector_code").to_list())


def test_only_board_types_the_lake_uses_are_requested():
    """`region` and `tszs` exist upstream but the lake carries no such boards."""
    client = _StubClient()
    fetch_sector_catalog(client)
    tags = {params["tag"] for _, params in client.calls}
    assert tags == set(BOARD_TAGS)
    assert set(BOARD_TAGS.values()) == {"industry", "concept"}


def test_a_start_before_the_floor_is_raised_rather_than_sent():
    """Below the floor every board returns an empty list with no error at all.

    Sent as asked, a 2018 request is indistinguishable from a catalogue of
    boards that never traded.
    """
    catalog = pl.DataFrame(
        {
            "thscode": ["881101.TI"],
            "sector_code": ["881101"],
            "sector_name": ["种植业与林业"],
            "board_type": ["industry"],
        }
    )
    client = _StubClient([_bar()])
    fetch_sector_bars(catalog, date(2018, 12, 4), date(2024, 12, 31), client=client)
    sent = min(params["start"] for _, params in client.calls)
    assert sent == _ms(HISTORY_FLOOR)


def test_change_pct_is_left_for_the_lake_to_derive():
    """The upstream reports no session change, and a guess would look like data."""
    catalog = pl.DataFrame(
        {
            "thscode": ["881101.TI"],
            "sector_code": ["881101"],
            "sector_name": ["种植业与林业"],
            "board_type": ["industry"],
        }
    )
    frame, counters = fetch_sector_bars(
        catalog, date(2024, 1, 1), date(2024, 12, 31), client=_StubClient([_bar()])
    )
    assert frame.height == 1
    assert frame.get_column("change_pct").null_count() == 1
    assert counters["boards"] == 1


def test_the_resource_step_defaults_to_a_dry_run():
    import inspect

    from cnequity.steps.rotation import resource_sector_bars_ths_official

    signature = inspect.signature(resource_sector_bars_ths_official)
    assert signature.parameters["dry_run"].default is True


def test_the_resource_step_is_inert_without_a_key(tmp_path):
    from cnequity.config import Config
    from cnequity.steps.rotation import resource_sector_bars_ths_official

    out = resource_sector_bars_ths_official(Config(data_root=tmp_path), "run-1")
    assert out["status"] == "skipped"
