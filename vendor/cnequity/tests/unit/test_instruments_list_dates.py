"""The instruments step asks vendors only for listing dates it does not hold."""

from datetime import date

import polars as pl
import pytest

import cnequity.adapters.eastmoney.instruments as em_instruments
import cnequity.steps.reference as reference
from cnequity.config import Config
from cnequity.storage.state import StateStore

DAY = date(2026, 10, 16)


def _rows(dates: dict[str, date | None]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": list(dates),
            "name": list(dates),
            "exchange": [s.rsplit(".", 1)[1] for s in dates],
            "asset_type": ["stock"] * len(dates),
            "list_date": list(dates.values()),
            "delist_date": [None] * len(dates),
            "prev_symbol": [None] * len(dates),
        },
        schema_overrides={"list_date": pl.Date, "delist_date": pl.Date, "prev_symbol": pl.Utf8},
    )


@pytest.fixture
def lake(tmp_path, monkeypatch):
    cfg = Config(
        data_root=tmp_path / "data",
        sources={"baostock": True, "bse": False, "eastmoney": False, "exchange": False},
    )
    curated = {"600519.SH": date(2001, 8, 27), "000001.SZ": date(1991, 4, 3), "159999.SZ": None}
    monkeypatch.setattr(reference, "load_curated_instruments", lambda *_a, **_k: _rows(curated))
    snapshot = {"600519.SH": None, "000001.SZ": None, "159999.SZ": None}
    monkeypatch.setattr(reference, "fetch_instruments", lambda **_: _rows(snapshot))
    monkeypatch.setattr(reference, "normalize_with_source", lambda df, *_a, **_k: df)
    asked: list[int] = []

    def _basics(**_kwargs):
        asked.append(1)
        return _rows({"600519.SH": date(2001, 8, 27)})

    import cnequity.adapters.baostock.instruments as bs_instruments

    monkeypatch.setattr(bs_instruments, "fetch_instrument_basics", _basics)
    monkeypatch.setattr(em_instruments, "fetch_list_date_map", lambda **_: {})
    written: dict = {}

    def _capture(config, run_id, dataset, df):
        written["df"] = df
        return {}

    monkeypatch.setattr(reference, "write_simple", _capture)
    return cfg, snapshot, asked, written


def _run(cfg, on=DAY):
    reference.step_instruments(cfg, on, "run-1", {})


def test_known_dates_are_carried_and_vendors_skipped_once_checked(lake):
    cfg, _snapshot, asked, written = lake
    _run(cfg)
    assert asked == [1], "the undated fund is worth one vendor round"
    dates = dict(zip(written["df"]["symbol"], written["df"]["list_date"], strict=True))
    assert dates["000001.SZ"] == date(1991, 4, 3), "a date the lake holds stands"
    assert dates["159999.SZ"] is None

    _run(cfg, date(2026, 10, 19))
    assert asked == [1], "the same undated name is not re-asked within the week"


def test_a_new_listing_is_asked_about_the_next_day(lake):
    cfg, snapshot, asked, _written = lake
    _run(cfg)
    snapshot["920099.BJ"] = None
    _run(cfg, date(2026, 10, 19))
    assert asked == [1, 1]


def test_the_undated_answer_expires(lake):
    cfg, _snapshot, asked, _written = lake
    _run(cfg)
    _run(cfg, date(2026, 10, 23))
    assert asked == [1, 1]


def test_a_failed_vendor_round_is_not_remembered(lake, monkeypatch):
    cfg, _snapshot, _asked, _written = lake
    import cnequity.adapters.baostock.instruments as bs_instruments

    def _down(**_kwargs):
        raise RuntimeError("baostock down")

    monkeypatch.setattr(bs_instruments, "fetch_instrument_basics", _down)
    _run(cfg)
    assert StateStore(cfg.meta_root).get_date("instruments", "undated_checked_on") is None


def test_nothing_undated_means_no_vendor_round(lake, monkeypatch):
    cfg, snapshot, asked, _written = lake
    del snapshot["159999.SZ"]
    _run(cfg)
    assert asked == []
