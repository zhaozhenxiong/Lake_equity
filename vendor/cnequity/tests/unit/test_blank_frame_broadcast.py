"""A column-less frame must never gain a row from being stamped.

`pl.DataFrame()` is the codebase's ordinary "nothing to report" value, and
polars 1.x broadcasts a literal against a zero-**column** frame to length one
(polars 2 keeps zero rows; `polars>=1.0` still admits both). The
resulting row carries only the literals, so it has no primary key; a strict
validate rejects it, but a diagonal concat with a real day's rows fills its
keys with nulls first and launders it into the lake.

Found in `step_trading_status` on 2026-08-30: a session whose vendor universe
was empty produced a row with no symbol and no `trade_date`, which then failed
the *next* session's date validation and blamed the wrong day.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from cnequity.domain.frames import is_blank, with_columns_unless_blank
from cnequity.domain.pit import normalize_pit_storage_columns
from cnequity.domain.schemas import with_provenance


def test_a_zero_row_frame_with_a_schema_is_not_affected():
    """The distinction the guard turns on: columns, not rows."""
    typed = pl.DataFrame(schema={"symbol": pl.Utf8})
    assert typed.is_empty()
    assert not is_blank(typed)
    assert typed.with_columns(pl.lit("x").alias("s")).height == 0


def test_is_blank_separates_the_two_empties():
    assert is_blank(pl.DataFrame())
    assert not is_blank(pl.DataFrame(schema={"symbol": pl.Utf8}))
    assert not is_blank(pl.DataFrame({"symbol": ["600519.SH"]}))


def test_the_guard_is_a_no_op_on_a_blank_frame():
    out = with_columns_unless_blank(pl.DataFrame(), pl.lit("x").alias("s"))
    assert out.height == 0
    assert out.width == 0


def test_the_guard_still_stamps_a_real_frame():
    out = with_columns_unless_blank(pl.DataFrame({"symbol": ["600519.SH"]}), pl.lit("x").alias("s"))
    assert out.to_dicts() == [{"symbol": "600519.SH", "s": "x"}]


def test_the_guard_still_stamps_a_typed_empty_frame():
    """A schema-carrying empty frame must keep gaining its columns."""
    out = with_columns_unless_blank(
        pl.DataFrame(schema={"symbol": pl.Utf8}), pl.lit("x").alias("s")
    )
    assert out.height == 0
    assert out.columns == ["symbol", "s"]


# --- the guarded entry points -------------------------------------------------


def test_provenance_does_not_invent_a_row():
    """The universal write-path stamp — 28 call sites depend on this."""
    out = with_provenance(pl.DataFrame(), source="eastmoney", data_version="v1")
    assert out.height == 0
    assert out.width == 0


def test_provenance_still_stamps_a_typed_empty_frame():
    out = with_provenance(
        pl.DataFrame(schema={"symbol": pl.Utf8}), source="eastmoney", data_version="v1"
    )
    assert out.height == 0
    assert set(out.columns) == {"symbol", "source", "data_version", "fetched_at"}


def test_provenance_still_stamps_real_rows():
    out = with_provenance(
        pl.DataFrame({"symbol": ["600519.SH"]}), source="eastmoney", data_version="v1"
    )
    assert out.height == 1
    assert out["source"].to_list() == ["eastmoney"]


def test_pit_normalization_does_not_invent_a_row():
    out = normalize_pit_storage_columns(pl.DataFrame(), "financial_statement_items")
    assert out.height == 0
    assert out.width == 0


def test_mock_bars_for_an_empty_symbol_set_stay_empty():
    """The mock path is the one that can legitimately be handed no symbols."""
    from cnequity.adapters.tdx_protocol import client

    out = client._mock_bars([], date(2024, 6, 24), date(2024, 6, 28))
    assert out.height == 0


def test_a_laundered_phantom_row_is_what_the_guard_prevents():
    """Why a strict validate is not enough on its own.

    The phantom row has no primary key, so `validate_dataframe` would reject it
    alone. A diagonal concat runs first in several paths and fills the missing
    keys with nulls — after which the row looks merely incomplete rather than
    fabricated. Polars 2 no longer broadcasts onto a column-less frame, but the
    declared `polars>=1.0` still admits 1.x, so the guard must hold on both.
    """
    real = pl.DataFrame({"symbol": ["600519.SH"], "trade_date": [date(2024, 6, 28)]})
    guarded = with_columns_unless_blank(pl.DataFrame(), pl.lit("eastmoney").alias("source"))
    assert pl.concat([guarded, real], how="diagonal_relaxed").height == 1

    phantom = pl.DataFrame().with_columns(pl.lit("eastmoney").alias("source"))
    if phantom.height:
        laundered = pl.concat([phantom, real], how="diagonal_relaxed")
        assert laundered.height == 2
        assert laundered["symbol"].to_list() == [None, "600519.SH"]
