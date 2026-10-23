"""Official Beijing Stock Exchange data adapters."""

from cnequity.adapters.bse.daily_quotes import (
    BseMarketDataError,
    fetch_daily_quotes,
)

__all__ = ["BseMarketDataError", "fetch_daily_quotes"]
