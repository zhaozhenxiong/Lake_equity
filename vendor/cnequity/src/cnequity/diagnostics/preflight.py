"""A few seconds of reachability checks before a whole-market `cne init`.

Without them an unreachable TDX host (an overseas exit, a proxy that drops
TCP 7709) surfaced only after minutes of connect retries inside the first
step, and a new user read that as a hang. The probes are the ones
`cne sources probe` runs, one request each, so a passing preflight costs the
sources nothing they would not see from the init itself.

TDX is the one the init cannot do without: the security list and the daily
bars both come from it. The exchange lists and EastMoney feed steps that
degrade on their own, so they are reported and never stop the run.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from cnequity.config import Config
from cnequity.diagnostics.source_health import (
    PROBES_BY_KEY,
    ProbeResult,
    ProbeStatus,
    SourceProbe,
    run_probe,
)
from cnequity.i18n import N_, Message

#: TDX first: the only probe whose failure stops the init.
REQUIRED_PROBES = ("tdx_protocol",)
INFORMATIONAL_PROBES = ("exchange_sse", "exchange_szse", "eastmoney_datacenter")

#: Wall-clock budget for the whole check. The probes run side by side because
#: each hits a different host. A healthy mainland connection measured 3–5s for
#: TDX (host discovery, handshake, one page of bars); the margin keeps a slow
#: moment from stopping an init that would have run fine.
PREFLIGHT_BUDGET_SECONDS = 8.0

#: Set to any non-empty value to skip the check (offline machines, CI).
SKIP_ENV = "CNE_SKIP_PREFLIGHT"
# Exported by the test suite's network guard; a check that dials out would
# only trip it.
_TEST_GUARD_ENV = "CNE_TEST_NO_NETWORK"


@dataclass(frozen=True)
class PreflightResult:
    results: list[ProbeResult]
    required: tuple[str, ...]

    @property
    def failed_required(self) -> list[ProbeResult]:
        # Skipped means the config turned the source off, which is a choice.
        passing = {ProbeStatus.OK.value, ProbeStatus.SKIPPED.value}
        return [r for r in self.results if r.key in self.required and r.status not in passing]

    @property
    def ok(self) -> bool:
        return not self.failed_required


def preflight_skip_reason(config: Config, *, requested_skip: bool = False) -> str | None:
    """Why the check should not run here, or ``None`` when it should."""
    if requested_skip:
        return "--no-preflight"
    if os.environ.get(SKIP_ENV):
        return SKIP_ENV
    if os.environ.get(_TEST_GUARD_ENV):
        return "test network guard"
    if config.tdx_allow_mock:
        return "tdx_protocol.allow_mock"
    return None


def _timed_out(probe: SourceProbe, budget: float) -> ProbeResult:
    return ProbeResult(
        key=probe.key,
        label=probe.label,
        host=probe.host,
        powers=list(probe.powers),
        status=ProbeStatus.DOWN.value,
        latency_ms=None,
        detail=Message(N_("no response within %g seconds"), budget),
        note=probe.note,
        blast_radius=probe.blast_radius,
    )


def run_init_preflight(
    config: Config,
    *,
    budget: float = PREFLIGHT_BUDGET_SECONDS,
    probe_runner: Callable[[SourceProbe, Config], ProbeResult] = run_probe,
) -> PreflightResult:
    """Probe what the init needs, all at once, for at most *budget* seconds.

    A probe still running at the deadline is reported as down and left to
    finish on a daemon thread; nothing waits for it.
    """
    probes = [PROBES_BY_KEY[key] for key in (*REQUIRED_PROBES, *INFORMATIONAL_PROBES)]
    finished: dict[str, ProbeResult] = {}

    def measure(probe: SourceProbe) -> None:
        finished[probe.key] = probe_runner(probe, config)

    threads = [
        threading.Thread(target=measure, args=(probe,), name=f"preflight-{probe.key}", daemon=True)
        for probe in probes
    ]
    deadline = time.monotonic() + budget
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return PreflightResult(
        results=[finished.get(probe.key) or _timed_out(probe, budget) for probe in probes],
        required=REQUIRED_PROBES,
    )
