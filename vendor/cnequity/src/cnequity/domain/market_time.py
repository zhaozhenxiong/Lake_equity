"""Exchange-local clock helpers used by ingestion defaults and gates."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# Mainland China has used UTC+08:00 without daylight-saving changes for the
# exchange dates handled by this project. A fixed offset also works on Windows
# without requiring an IANA timezone database or an extra runtime dependency.
SHANGHAI_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
# Leave a small settlement buffer after the 15:00 closing auction before a
# daily dataset is expected to publish the session.
A_SHARE_FINAL_AT = time(15, 5)

# The Beijing Stock Exchange's first trading session, re-exported for callers
# that read it here. Market eras are declared in ``market_profile``.
from cnequity.domain.market_profile import BSE_FIRST_SESSION  # noqa: E402, F401


def shanghai_now(now: datetime | None = None) -> datetime:
    """Return an aware current timestamp represented in exchange time."""
    anchor = now or datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        raise ValueError("market clock must be timezone-aware")
    return anchor.astimezone(SHANGHAI_TZ)


def shanghai_today(now: datetime | None = None) -> date:
    """Return the exchange-local calendar date, independent of host timezone."""
    return shanghai_now(now).date()


def is_session_final(as_of: date, now: datetime | None = None) -> bool:
    """Whether *as_of* is a completed exchange session at the current clock.

    Historical/future dates are treated as final. Only the current Shanghai
    date is provisional before the settlement buffer, which prevents a Monday
    pre-open health check from calling Friday's complete data stale.
    """
    local_now = shanghai_now(now)
    return as_of != local_now.date() or local_now.time() >= A_SHARE_FINAL_AT


def last_closed_session(now: datetime | None = None) -> date | None:
    """The session live (undated) market data describes at *now*, or None while open.

    From a session's 15:30 Beijing close until the next session's 09:15 open,
    a live snapshot — TDX batch quotes, 同花顺's money-flow ranking — can only
    describe that session. Walks the bundled exchange calendar, so a weekend
    or holiday points at the last session before it.
    """
    from cnequity.domain.datasets import _is_exchange_session

    local = shanghai_now(now)
    day, clock = local.date(), local.time()
    if _is_exchange_session(day):
        if clock >= time(15, 30):
            return day
        if clock >= time(9, 15):
            return None
    day -= timedelta(days=1)
    for _ in range(40):
        if _is_exchange_session(day):
            return day
        day -= timedelta(days=1)
    return None
