"""Shared EastMoney request budgets and host-family breakers.

The ledger follows ``CNE_RATE_LIMIT_ROOT`` so lakes using one egress can
coordinate. A legacy lake-local ledger is imported once per lake and day.
Rules are enforced before a request leaves the process:

==============  ==========================  ===================================
rule            push2                       datacenter
==============  ==========================  ===================================
pause           ``push2_paused``            —
breaker trips   first refusal               3 refusals in a row (a success
                                            resets the count); timeouts are
                                            not refusals here
refusal         403/429/5xx, any transport  403/429/5xx, connection refused /
                error but a local proxy     reset / dropped, "busy" retries
                                            exhausted
daily budget    ``push2_daily_budget``      ``datacenter_daily_budget``
                                            (0 = count only)
==============  ==========================  ===================================

A tripped breaker closes that host family until midnight, across processes.
A refused request raises a :class:`EastMoneyHostBlockedError`, an
``httpx.ConnectError``, so every caller already treats it as a dead route:
fail fast, no retry, and its own fallbacks run.
"""

from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from cnequity.domain.rate_limit import _write_json
from cnequity.file_lock import exclusive_lock

if TYPE_CHECKING:
    from cnequity.config import Config

logger = logging.getLogger(__name__)

# Statuses that mean "this IP is being refused", not "this request was bad".
_TRIP_STATUSES = frozenset({403, 429})


class EastMoneyHostBlockedError(httpx.ConnectError):
    """An EastMoney request refused locally, before anything was sent."""


class Push2BlockedError(EastMoneyHostBlockedError):
    """A push2 request refused locally."""


class Push2PausedError(Push2BlockedError):
    """Refused by ``[sources.eastmoney].push2_paused``."""


class Push2BreakerOpenError(Push2BlockedError):
    """Refused because push2 already refused this IP today."""


class Push2BudgetExhaustedError(Push2BlockedError):
    """Refused because today's push2 request budget is spent."""


class DatacenterBlockedError(EastMoneyHostBlockedError):
    """A datacenter request refused locally."""


class DatacenterBreakerOpenError(DatacenterBlockedError):
    """Refused because datacenter refused this IP repeatedly today."""


class DatacenterBudgetExhaustedError(DatacenterBlockedError):
    """Refused because today's datacenter request budget is spent."""


@dataclass(frozen=True)
class _Profile:
    name: str
    hosts: tuple[str, ...]
    breaker_attr: str
    budget_attr: str
    strikes_attr: str | None
    timeouts_are_refusals: bool
    breaker_error: type[EastMoneyHostBlockedError]
    budget_error: type[EastMoneyHostBlockedError]

    def matches(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def strikes_needed(self, config: Config) -> int:
        if self.strikes_attr is None:
            return 1
        return max(1, int(getattr(config, self.strikes_attr, 3) or 3))


PUSH2 = _Profile(
    name="push2",
    hosts=(
        "push2.eastmoney.com",
        "push2delay.eastmoney.com",
        "push2his.eastmoney.com",
        "push2ex.eastmoney.com",
    ),
    breaker_attr="eastmoney_push2_breaker",
    budget_attr="eastmoney_push2_daily_budget",
    strikes_attr=None,
    timeouts_are_refusals=True,
    breaker_error=Push2BreakerOpenError,
    budget_error=Push2BudgetExhaustedError,
)
DATACENTER = _Profile(
    name="datacenter",
    hosts=("datacenter-web.eastmoney.com", "datacenter.eastmoney.com"),
    breaker_attr="eastmoney_datacenter_breaker",
    budget_attr="eastmoney_datacenter_daily_budget",
    strikes_attr="eastmoney_datacenter_breaker_strikes",
    timeouts_are_refusals=False,
    breaker_error=DatacenterBreakerOpenError,
    budget_error=DatacenterBudgetExhaustedError,
)
_PROFILES = (PUSH2, DATACENTER)


def profile_for(url: str) -> _Profile | None:
    return next((p for p in _PROFILES if p.matches(url)), None)


def is_push2_url(url: str) -> bool:
    return PUSH2.matches(url)


def is_datacenter_url(url: str) -> bool:
    return DATACENTER.matches(url)


def _today() -> date:
    """The local calendar day; breakers and budgets reset at midnight."""
    return datetime.now().astimezone().date()


def _state_path(config: Config) -> Path:
    return config.rate_limit_root / "eastmoney_guard.json"


def _legacy_state_path(config: Config) -> Path:
    return Path(config.meta_root) / "state" / "eastmoney_guard.json"


@contextmanager
def _locked(config: Config):
    path = _state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(path.with_suffix(".lock"), timeout=60.0):
        yield path


def _load(path: Path, today: date, legacy_path: Path | None = None) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict) or state.get("day") != today.isoformat():
        state = {"day": today.isoformat()}
    for profile in _PROFILES:
        section = state.setdefault(profile.name, {})
        section.setdefault("requests", 0)
        section.setdefault("breaker", None)
        section.setdefault("strikes", 0)
    if legacy_path is not None and legacy_path != path and legacy_path.exists():
        # A hash identifies the imported lake without exposing its path.
        identity = hashlib.sha256(str(legacy_path).encode()).hexdigest()[:24]
        imported = state.setdefault("imported_lakes", [])
        if identity not in imported:
            try:
                legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                legacy = {}
            if isinstance(legacy, dict) and legacy.get("day") == today.isoformat():
                for profile in _PROFILES:
                    old = legacy.get(profile.name)
                    if not isinstance(old, dict):
                        continue
                    section = state[profile.name]
                    section["requests"] = max(0, int(section["requests"])) + max(
                        0, int(old.get("requests", 0))
                    )
                    section["strikes"] = max(int(section["strikes"]), int(old.get("strikes", 0)))
                    section["breaker"] = section["breaker"] or old.get("breaker")
            imported.append(identity)
    vendor = state.setdefault("vendor", {})
    vendor["requests"] = max(
        int(vendor.get("requests", 0) or 0),
        sum(int(state[profile.name]["requests"]) for profile in _PROFILES),
    )
    return state


def _save(path: Path, state: dict[str, Any]) -> None:
    _write_json(path, state)


def _section_policy(section: dict[str, Any], profile: _Profile, config: Config) -> None:
    """Use the strictest positive budget and strike threshold seen today."""
    requested = int(getattr(config, profile.budget_attr, 0) or 0)
    current = int(section.get("budget", 0) or 0)
    section["budget"] = min(current, requested) if current and requested else current or requested
    section["breaker_policy"] = bool(section.get("breaker_policy")) or bool(
        getattr(config, profile.breaker_attr, True)
    )
    needed = profile.strikes_needed(config)
    previous = int(section.get("strikes_needed", needed) or needed)
    section["strikes_needed"] = min(previous, needed)


def _vendor_policy(state: dict[str, Any], config: Config) -> None:
    section = state["vendor"]
    requested = int(getattr(config, "eastmoney_daily_budget", 0) or 0)
    if requested < 0:
        raise ValueError("eastmoney daily_budget must be >= 0")
    current = int(section.get("budget", 0) or 0)
    section["budget"] = min(current, requested) if current and requested else current or requested


def _host(url: str) -> str:
    return urlparse(url).netloc


def _check_policy(config: Config, url: str, profile: _Profile, state: dict) -> None:
    section = state[profile.name]
    vendor = state["vendor"]
    if vendor["budget"] and int(vendor["requests"]) >= int(vendor["budget"]):
        raise EastMoneyHostBlockedError(
            f"EastMoney shared daily budget spent ({vendor['requests']}/{vendor['budget']}); "
            f"not sent: {_host(url)}"
        )
    if profile is PUSH2 and getattr(config, "eastmoney_push2_paused", False):
        raise Push2PausedError(
            f"push2 paused by [sources.eastmoney].push2_paused; not sent: {_host(url)}"
        )
    tripped = section.get("breaker")
    if tripped:
        raise profile.breaker_error(
            f"{profile.name} breaker open since {tripped.get('at')} "
            f"({tripped.get('reason')} on {tripped.get('host')}); "
            f"not sent until tomorrow: {_host(url)}"
        )
    budget = int(section["budget"])
    if budget > 0 and int(section["requests"]) >= budget:
        raise profile.budget_error(
            f"{profile.name} daily budget spent ({section['requests']}/{budget}); "
            f"not sent: {_host(url)}"
        )


def ensure_open(config: Config | None, url: str) -> None:
    """Early rejection and legacy import before auth headers or handshakes."""
    if config is None or (profile := profile_for(url)) is None:
        return
    with _locked(config) as path:
        state = _load(path, _today(), _legacy_state_path(config))
        section = state[profile.name]
        _section_policy(section, profile, config)
        _vendor_policy(state, config)
        _save(path, state)
        _check_policy(config, url, profile, state)


def admit(config: Config | None, url: str) -> None:
    """Refuse *url* locally, or count it against today's ledger and let it go.

    URLs outside the guarded hosts and bare (config-less) clients pass untouched.
    """
    if config is None:
        return
    profile = profile_for(url)
    if profile is None:
        return
    with _locked(config) as path:
        state = _load(path, _today(), _legacy_state_path(config))
        section = state[profile.name]
        _section_policy(section, profile, config)
        _vendor_policy(state, config)
        # Persist a newly imported legacy ledger before refusing the request.
        _save(path, state)
        _check_policy(config, url, profile, state)
        section["requests"] = int(section["requests"]) + 1
        state["vendor"]["requests"] = int(state["vendor"]["requests"]) + 1
        _save(path, state)


def refusal_reason(
    *,
    status_code: int | None = None,
    exc: BaseException | None = None,
    url: str | None = None,
) -> str | None:
    """Why an outcome counts as the host refusing this IP, or None."""
    profile = profile_for(url) if url else PUSH2
    if exc is not None:
        if isinstance(exc, EastMoneyHostBlockedError | httpx.ProxyError):
            # Our own refusal, or a dead local proxy: neither is EastMoney talking.
            return None
        if isinstance(exc, httpx.TimeoutException) and not (
            profile is None or profile.timeouts_are_refusals
        ):
            return None
        if isinstance(exc, httpx.TransportError):
            return type(exc).__name__
        return None
    if status_code is not None and (status_code in _TRIP_STATUSES or status_code >= 500):
        return f"HTTP {status_code}"
    return None


def note_outcome(
    config: Config | None,
    url: str,
    *,
    status_code: int | None = None,
    exc: BaseException | None = None,
) -> None:
    """Record one request's outcome: a refusal strikes, a success clears strikes."""
    if config is None:
        return
    profile = profile_for(url)
    if profile is None:
        return
    reason = refusal_reason(status_code=status_code, exc=exc, url=url)
    if reason is None:
        if exc is None and profile.strikes_attr is not None:
            _clear_strikes(config, profile)
        return
    strike(config, url, reason)


def _clear_strikes(config: Config, profile: _Profile) -> None:
    with _locked(config) as path:
        state = _load(path, _today(), _legacy_state_path(config))
        if state[profile.name]["strikes"]:
            state[profile.name]["strikes"] = 0
            _save(path, state)


def strike(config: Config | None, url: str, reason: str) -> None:
    """Count one refusal; trip the host's breaker once enough have landed in a row."""
    if config is None:
        return
    profile = profile_for(url)
    if profile is None:
        return
    with _locked(config) as path:
        state = _load(path, _today(), _legacy_state_path(config))
        section = state[profile.name]
        _section_policy(section, profile, config)
        if not section["breaker_policy"]:
            _save(path, state)
            return
        needed = int(section["strikes_needed"])
        if section.get("breaker"):
            return
        section["strikes"] = int(section["strikes"]) + 1
        tripped = section["strikes"] >= needed
        if tripped:
            section["breaker"] = {
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "reason": reason,
                "host": _host(url),
                "strikes": section["strikes"],
            }
        _save(path, state)
    if tripped:
        logger.warning(
            "%s breaker tripped: %s on %s; no %s request until tomorrow",
            profile.name,
            reason,
            _host(url),
            profile.name,
        )
    else:
        logger.warning(
            "%s refusal %d/%d: %s on %s",
            profile.name,
            section["strikes"],
            needed,
            reason,
            _host(url),
        )


def trip(config: Config | None, url: str, reason: str) -> None:
    """Close *url*'s host family for the rest of the local day, now."""
    if config is None:
        return
    profile = profile_for(url)
    if profile is None:
        return
    with _locked(config) as path:
        state = _load(path, _today(), _legacy_state_path(config))
        section = state[profile.name]
        _section_policy(section, profile, config)
        if not section["breaker_policy"]:
            _save(path, state)
            return
        if section.get("breaker"):
            return
        section["breaker"] = {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reason": reason,
            "host": _host(url),
        }
        _save(path, state)
    logger.warning(
        "%s breaker tripped: %s on %s; no %s request until tomorrow",
        profile.name,
        reason,
        _host(url),
        profile.name,
    )


def breaker_enabled(config: Config | None) -> bool:
    """Whether the push2 breaker is on (the clist failover policy reads it)."""
    return config is not None and bool(getattr(config, PUSH2.breaker_attr, True))


def status(config: Config) -> dict[str, Any]:
    """Today's ledger, for reporting."""
    # Atomic writes allow a read-only snapshot: status/--plan must not create
    # a lake or an egress directory merely to inspect protection.
    state = _load(_state_path(config), _today(), _legacy_state_path(config))
    for profile in _PROFILES:
        section = state[profile.name]
        _section_policy(section, profile, config)
        section["budget_remaining"] = (
            max(0, section["budget"] - section["requests"]) if section["budget"] else None
        )
    state["push2"]["paused"] = bool(getattr(config, "eastmoney_push2_paused", False))
    _vendor_policy(state, config)
    vendor = state["vendor"]
    vendor["budget_remaining"] = (
        max(0, vendor["budget"] - vendor["requests"]) if vendor["budget"] else None
    )
    return state
