from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from cnequity.config import Config
from cnequity.domain.http_policy import check_source_cooldown, source_probe_slot
from cnequity.domain.http_policy import source_family as _source_family
from cnequity.domain.rate_limit import (
    DEFAULT_LOCK_TIMEOUT_SECONDS,
    DEFAULT_SOURCE_CONCURRENCY,
    DEFAULT_SOURCE_INTERVALS,
    DEFAULT_UNKNOWN_SOURCE_INTERVAL_SECONDS,
    RateLimiter,
    SourceConcurrencyLimiter,
    record_metered_attempt,
)


@dataclass
class SourceRateLimiters:
    config: Config
    _limiters: dict[str, RateLimiter] = field(default_factory=dict, init=False, repr=False)
    _concurrency: dict[str, SourceConcurrencyLimiter] = field(
        default_factory=dict, init=False, repr=False
    )
    _request_local: threading.local = field(default_factory=threading.local, init=False, repr=False)

    def __post_init__(self) -> None:
        state_dir = self.config.rate_limit_root
        intervals = {**DEFAULT_SOURCE_INTERVALS, **self.config.source_intervals}
        if self.config.tdx_enabled:
            interval = self.config.tdx_rate_limit_spec().min_interval
            intervals["tdx_protocol"] = interval
            self._limiters["tdx_protocol"] = RateLimiter(
                "tdx_protocol",
                interval,
                state_dir,
                lock_timeout=self.config.tdx_lock_timeout_sec,
            )
        for source, interval in intervals.items():
            if source == "tdx_protocol" and self.config.tdx_enabled:
                continue  # Keep the wire lane's stricter interval and lock timeout.
            self._limiters[source] = RateLimiter(source, interval, state_dir)
        # Alias lanes also need one family-wide start spacing. An explicit
        # family interval is the aggregate floor; keep slower endpoint lanes
        # separate so a catalog page does not slow unrelated K-line requests.
        # Without a family setting, use the strictest alias as a safe default.
        family_intervals: dict[str, float] = {}
        for name, limiter in self._limiters.items():
            family = _source_family(name)
            family_intervals[family] = max(family_intervals.get(family, 0.0), limiter.min_interval)
        for family, interval in family_intervals.items():
            family_interval = intervals.get(family, interval)
            self._limiters[family] = RateLimiter(
                family,
                family_interval,
                state_dir,
                lock_timeout=(
                    self.config.tdx_lock_timeout_sec
                    if family == "tdx_protocol"
                    else DEFAULT_LOCK_TIMEOUT_SECONDS
                ),
            )

        # Every source with an interval gets a default cap as well.  The
        # default follows the legacy scheduler budget, while an explicit
        # source_concurrency/http_workers/source_workers value narrows it.
        # Endpoint aliases (ths_pages/ths_bonus, for example) share one
        # vendor-wide ledger so independent DAG steps cannot exceed the cap in
        # aggregate.
        sources = set(self._limiters) | set(self.config.source_concurrency)
        sources |= set(self.config.http_workers) | set(self.config.source_workers)
        sources.add("tdx_protocol")
        for source in sources:
            family = _source_family(source)
            if family in self._concurrency:
                continue
            limit = self._configured_limit(family, sources)
            self._concurrency[family] = SourceConcurrencyLimiter(
                family,
                max(1, int(limit)),
                state_dir,
                lock_timeout=getattr(self.config, "tdx_lock_timeout_sec", 15.0),
            )

    def _configured_limit(self, family: str, sources: set[str] | None = None) -> int:
        """Resolve one deterministic vendor-wide cap from all aliases.

        ``ths``, ``ths_pages`` and ``ths_bonus`` are separate pacing lanes but
        one upstream service.  Taking the narrowest explicitly configured
        value guarantees that whichever alias initializes the family first
        cannot accidentally discard a stricter sibling setting.
        """
        names = {family}
        names.update(source for source in (sources or set()) if _source_family(source) == family)
        values: list[int] = []
        for mapping in (
            self.config.source_concurrency,
            self.config.http_workers,
            self.config.source_workers,
        ):
            values.extend(int(mapping[name]) for name in names if name in mapping)
        if values:
            limit = max(1, min(values))
        elif family == "tdx_protocol":
            # TDX's wire adapter uses the daily lane width, including on
            # macOS where the unrelated process-pool budget is one worker.
            limit = self.config.tdx_daily_worker_count()
        elif family.startswith("futures_exchange_"):
            limit = DEFAULT_SOURCE_CONCURRENCY["futures_exchange"]
        else:
            limit = DEFAULT_SOURCE_CONCURRENCY.get(family, max(1, int(self.config.workers)))
        if family == "baostock":
            # The vendor allows one connection. A config value above that is
            # ignored here so a request cannot open a second session.
            from cnequity.adapters.baostock.access import MAX_CONCURRENT_CONNECTIONS

            limit = min(limit, MAX_CONCURRENT_CONNECTIONS)
        return limit

    def wait(self, source: str) -> None:
        limiter = self._limiters.get(source)
        family = _source_family(source)
        if limiter is None:
            base = self._limiters.get(
                "futures_exchange" if source.startswith("futures_exchange_") else family
            )
            state_dir = self.config.rate_limit_root
            limiter = RateLimiter(
                source,
                base.min_interval if base else DEFAULT_UNKNOWN_SOURCE_INTERVAL_SECONDS,
                state_dir,
            )
            self._limiters[source] = limiter
        if limiter is not None:
            limiter.wait()
        if family != source and (family_limiter := self._limiters.get(family)) is not None:
            family_limiter.wait()

    def defer(self, source: str, seconds: float) -> None:
        """Apply a vendor-wide cooling-off deadline to every pacing alias."""
        family = _source_family(source)
        matching = [
            limiter for name, limiter in self._limiters.items() if _source_family(name) == family
        ]
        for limiter in matching:
            limiter.defer(seconds)

    def _get_concurrency(self, source: str) -> SourceConcurrencyLimiter:
        family = _source_family(source)
        limiter = self._concurrency.get(family)
        if limiter is None:
            # A source can be used by a lazy adapter without a min-interval
            # entry.  Materialize its cap on demand so it still participates
            # in the global in-flight contract.
            state_dir = self.config.rate_limit_root
            limit = self._configured_limit(family, {source})
            limiter = SourceConcurrencyLimiter(
                family,
                limit,
                state_dir,
                lock_timeout=getattr(self.config, "tdx_lock_timeout_sec", 15.0),
            )
            self._concurrency[family] = limiter
        return limiter

    @contextmanager
    def slot(
        self,
        source: str,
        *,
        metrics: dict | None = None,
        timeout: float | None = None,
    ) -> Iterator[None]:
        with self._get_concurrency(source).slot(metrics=metrics, timeout=timeout):
            yield

    @contextmanager
    def request(
        self,
        source: str,
        *,
        metrics: dict | None = None,
        timeout: float | None = None,
    ) -> Iterator[None]:
        """Acquire capacity, then pace immediately before the request."""
        family = _source_family(source)
        switches = {source, family}
        if family.startswith("eastmoney"):
            switches.add("eastmoney")
        if source.startswith("futures_exchange_"):
            switches.add("futures_exchange")
        if any(self.config.sources.get(name, True) is False for name in switches):
            raise RuntimeError(f"{source}: source disabled in config")
        active = getattr(self._request_local, "active", None)
        if active is None:
            active = set()
            self._request_local.active = active
        # ``SourceConcurrencyLimiter.slot`` is itself re-entrant, but skip the
        # second pacing reservation as well: a nested helper is part of the
        # same caller operation, not a new request start.
        if family in active:
            check_source_cooldown(self.config.rate_limit_root, source)
            with self.slot(source, metrics=metrics, timeout=timeout):
                check_source_cooldown(self.config.rate_limit_root, source)
                yield
            return
        check_source_cooldown(self.config.rate_limit_root, source)
        active.add(family)
        try:
            with self.slot(source, metrics=metrics, timeout=timeout):
                check_source_cooldown(self.config.rate_limit_root, source)
                self.wait(source)
                check_source_cooldown(self.config.rate_limit_root, source)
                with source_probe_slot(self.config.rate_limit_root, source):
                    if family == "baostock":
                        from cnequity.adapters.baostock.access import admit_request

                        admit_request(self.config)
                    record_metered_attempt(self.config.rate_limit_root, source, family)
                    yield
        finally:
            active.discard(family)
