"""Rest the EastMoney fast-news lane for the day after repeated refusals.

The 15-minute news job reaches EastMoney through a handshake (``anonflow2``)
and then the list host. When that route is refused, the shared 300-second
cooldown expires before the next tick, so every tick sends one more probe to a
host that keeps answering 403 — 2026-09-27 to 10-02 was six days of that.
Unlike push2 and datacenter, the lane has no breaker, because the refusal
surfaces as a local :class:`SourceCoolingDown` rather than an HTTP outcome the
host guard sees.

After ``eastmoney_news_breaker_strikes`` consecutive refused sweeps the lane
stays closed until local midnight, across processes; one success clears the
count. A dead local proxy or a timeout is not a refusal and never counts.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from cnequity.domain.http_policy import SourceCoolingDown
from cnequity.domain.rate_limit import _read_json, _write_json
from cnequity.file_lock import exclusive_lock
from cnequity.orchestrator.outcomes import SourceUnavailableError

if TYPE_CHECKING:
    from cnequity.config import Config

logger = logging.getLogger(__name__)

_REFUSAL_STATUSES = frozenset({403, 429})


class NewsPausedError(SourceUnavailableError):
    """The news lane was refused repeatedly today; nothing is sent until tomorrow."""


def _today() -> date:
    """The local calendar day, as the other EastMoney breakers reckon it."""
    return datetime.now().astimezone().date()


def _path(config: Config) -> Path:
    return config.rate_limit_root / "eastmoney_news_pause.json"


@contextmanager
def _locked(config: Config):
    path = _path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(".lock"), timeout=60.0):
        yield path


def _load(path: Path) -> dict[str, Any]:
    state = _read_json(path)
    if state.get("day") != _today().isoformat():
        return {"day": _today().isoformat(), "strikes": 0, "paused": None}
    state.setdefault("strikes", 0)
    state.setdefault("paused", None)
    return state


def _enabled(config: Config | None) -> bool:
    return config is not None and bool(getattr(config, "eastmoney_news_breaker", True))


def is_refusal(exc: BaseException) -> bool:
    """Whether *exc* is EastMoney (or its shared cooldown) refusing this IP."""
    if isinstance(exc, SourceCoolingDown):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response is not None and exc.response.status_code in _REFUSAL_STATUSES
    return False


def ensure_open(config: Config | None) -> None:
    """Refuse the sweep before any request when today's refusals closed the lane."""
    if not _enabled(config):
        return
    path = _path(config)
    if not path.exists():
        return  # read-only fast path: no lock, no state directory
    with _locked(config) as locked:
        paused = _load(locked)["paused"]
    if paused:
        raise NewsPausedError(
            f"fast news paused since {paused.get('at')} after {paused.get('strikes')} refused "
            f"sweeps in a row ({paused.get('reason')}); nothing is sent until tomorrow"
        )


def note_outcome(config: Config | None, exc: BaseException | None) -> None:
    """Record one sweep: ``exc=None`` is a success, a refusal strikes, the rest is ignored."""
    if not _enabled(config):
        return
    if exc is not None and not is_refusal(exc):
        return
    path = _path(config)
    if exc is None and not path.exists():
        return
    needed = max(1, int(getattr(config, "eastmoney_news_breaker_strikes", 3) or 3))
    tripped = False
    with _locked(config) as locked:
        state = _load(locked)
        if state["paused"]:
            return
        if exc is None:
            if state["strikes"]:
                state["strikes"] = 0
                _write_json(locked, state)
            return
        state["strikes"] = int(state["strikes"]) + 1
        if state["strikes"] >= needed:
            tripped = True
            state["paused"] = {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "reason": type(exc).__name__,
                "strikes": state["strikes"],
            }
        _write_json(locked, state)
        strikes = state["strikes"]
    if tripped:
        logger.warning(
            "fast news paused: %d refused sweeps in a row (%s); nothing is sent until tomorrow",
            strikes,
            type(exc).__name__,
        )
    else:
        logger.warning("fast news refused: sweep %d/%d (%s)", strikes, needed, type(exc).__name__)


def status(config: Config) -> dict[str, Any]:
    """Today's state, for reporting; reads only, creating nothing."""
    state = _load(_path(config))
    return {
        "breaker_policy": _enabled(config),
        "strikes": state["strikes"],
        "strikes_needed": max(1, int(getattr(config, "eastmoney_news_breaker_strikes", 3) or 3)),
        "paused": state["paused"],
    }
