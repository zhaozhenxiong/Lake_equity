"""Futures 1m bars: night sessions belong to the next trading day."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import polars as pl

from cnequity.adapters.sina.futures_minute import parse_minutes, session_of
from cnequity.config import Config
from cnequity.domain.schemas import validate_dataframe, with_provenance
from cnequity.steps import derivatives

SESSIONS = [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24),
            date(2026, 10, 8)]  # fmt: skip


def test_day_bars_keep_their_date_and_night_bars_move_to_the_next_session():
    assert session_of(datetime(2026, 9, 23, 14, 59), SESSIONS) == date(2026, 9, 23)
    assert session_of(datetime(2026, 9, 22, 21, 1), SESSIONS) == date(2026, 9, 23)
    # After midnight still belongs to the evening that started it.
    assert session_of(datetime(2026, 9, 23, 0, 30), SESSIONS) == date(2026, 9, 23)
    # Across a long holiday the next session is the one after it.
    assert session_of(datetime(2026, 9, 24, 21, 5), SESSIONS) == date(2026, 10, 8)
    # A weekday that is not a session is not a day bar of anything.
    assert session_of(datetime(2026, 9, 25, 10, 0), SESSIONS) is None


def test_real_window_parses_and_validates():
    text = (
        Path(__file__).parents[1] / "fixtures" / "futures" / "sina_minute_CU2611.txt"
    ).read_bytes()
    payload = json.loads(re.search(r"\[.*\]", text.decode("gbk"), re.S).group(0))
    frame = parse_minutes(payload, symbol="CU2611.SHF", sessions=SESSIONS)
    assert frame.height > 0
    night = frame.filter(pl.col("bar_time").dt.hour() >= 21)
    assert (night["trade_date"] > night["bar_time"].dt.date()).all()
    stamped = with_provenance(frame, source="sina", data_version="v1")
    assert validate_dataframe(stamped, "futures_minute_bars").height == frame.height


def test_minute_capture_is_off_unless_named(tmp_path):
    cfg = Config(data_root=tmp_path / "data")
    cfg.futures_enabled = True
    result = derivatives.step_futures_minute_bars(cfg, date(2026, 9, 24), "run", {})
    assert "disabled" in result["note"]
    cfg.futures_minute_enabled = True
    cfg.futures_minute_contracts = ["CU2611.SHF", "CU2611.SHF", "SC2612.INE"]
    assert derivatives.minute_scope(cfg) == ["CU2611.SHF", "SC2612.INE"]
