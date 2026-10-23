"""Shared small market situations for unit checks."""

from datetime import date

import polars as pl
import pytest


@pytest.fixture
def halted_ex_event():
    """An ex-date without a trade, followed by the first effective session."""

    def make(symbol: str, prior: date, ex_date: date, resumed: date) -> dict:
        return {
            "symbol": symbol,
            "ex_date": ex_date,
            "resumed": resumed,
            "actions": pl.DataFrame({"symbol": [symbol], "ex_date": [ex_date]}),
            "sessions": pl.DataFrame({"symbol": [symbol, symbol], "trade_date": [prior, resumed]}),
        }

    return make
