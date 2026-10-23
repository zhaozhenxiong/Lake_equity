"""Init job phase definitions and step-completion helpers."""

from __future__ import annotations

from typing import Any

from cnequity.orchestrator.outcomes import execution_settled

INIT_PHASE_STEPS: dict[str, list[str]] = {
    "phase1_reference": ["instruments", "trading_calendar"],
    "phase2a_corporate_actions": ["corporate_actions"],
    "phase2b_daily_bars_incremental": ["daily_bars"],
    "phase2c_daily_bars_backfill": ["daily_bars"],
    "phase3_index_and_status": ["index_bars", "trading_status"],
    "phase4_finalize": ["compact", "derive_adj_factors", "derive_industry_index", "audit"],
    # Reconstructing suspensions needs the bars *committed*, which only happens
    # in phase4, so this cannot run earlier — and it needs a compact of its own
    # to publish what it stages. This is the one pass that reaches the whole
    # history; afterwards the daily `trading_status` snapshot records halts and
    # `cne backfill daily_bars` re-derives the window whose bars it changed.
    "phase5_derive_and_publish": ["trading_status_derive", "compact"],
}

#: The Sina factor prefetch runs in the background from the first of these
#: phases until the finalize phase, where the derive takes over. It is not a
#: step, so it is never part of what an init owes.
FACTOR_PREFETCH_PHASES = frozenset({"phase2c_daily_bars_backfill", "phase2a_corporate_actions"})
FACTOR_PREFETCH_STOP_PHASE = "phase4_finalize"


INIT_BACKFILL_PHASES = frozenset(
    {
        # trading_calendar must span the full bar history (2016+) or historical
        # PIT/trading-day alignment falls back to a Mon–Fri heuristic. instruments
        # ignores the date window, so backfilling phase1 is safe.
        "phase1_reference",
        "phase2a_corporate_actions",
        "phase2c_daily_bars_backfill",
        # index_bars needs the same history as daily_bars. Init trading_status
        # explicitly uses its current-snapshot path, including on legacy retry.
        "phase3_index_and_status",
        # Backfill is how the derive is told to reach past its daily tail.
        "phase5_derive_and_publish",
    }
)

#: Phases whose failure must not stop a first build. Without corporate actions
#: or index/status rows the lake is thinner but its bars still publish and
#: query; stopping here left a new user with nothing after an hour of work.
#: The failed steps stay owed, so the next ``cne init`` resumes only them.
NON_BLOCKING_INIT_PHASES = frozenset({"phase2a_corporate_actions", "phase3_index_and_status"})

#: Steps inside blocking phases that run after ``compact`` has published the
#: bars. Each derives or checks what is already in the lake, and a later run
#: redoes it; letting one of them fail a first build reported a lake with
#: published, queryable bars as unusable and skipped the phase after it.
NON_BLOCKING_INIT_STEPS = frozenset(
    {"derive_adj_factors", "derive_industry_index", "audit", "trading_status_derive"}
)

FINALIZE_STEPS = frozenset({"compact", "derive_adj_factors", "derive_industry_index", "audit"})
RESOLVED_BATCH_STATUSES = frozenset({"success", "superseded"})

#: Daily bars run before the corporate-action sweep. The sweep asks TDX for
#: every instrument's whole xdxr history whatever the window, which held a
#: first build's bars back by ~44 minutes; nothing before phase4's
#: ``derive_adj_factors`` reads corporate actions during init.
DEFAULT_INIT_PHASES = [
    "phase1_reference",
    "phase2c_daily_bars_backfill",
    "phase2a_corporate_actions",
    "phase3_index_and_status",
    "phase4_finalize",
    "phase5_derive_and_publish",
]

#: The order shipped (and written into generated configs) before 0.17.0. A
#: config still carrying it verbatim never chose it, so it gets the new order.
LEGACY_DEFAULT_INIT_PHASES = (
    "phase1_reference",
    "phase2a_corporate_actions",
    "phase2c_daily_bars_backfill",
    "phase3_index_and_status",
    "phase4_finalize",
    "phase5_derive_and_publish",
)


def configured_init_phases(configured: list[str] | None) -> list[str]:
    """Phase list for a new init: the config's own, or the default.

    An unedited legacy default is upgraded; any other list is the operator's
    choice and is kept as written. Existing runs resume from the phases they
    recorded, never from this.
    """
    if not configured or tuple(configured) == LEGACY_DEFAULT_INIT_PHASES:
        return list(DEFAULT_INIT_PHASES)
    return list(configured)


def scheduled_later(step: str, dep: str, phases: list[str]) -> bool:
    """Whether *phases* deliberately run *dep* after *step*.

    The phase list is the init's own ordering contract. A declared dependency
    it schedules later (``daily_bars`` on ``corporate_actions``, which exists
    for daily-wave ordering) cannot gate *step*, or a retry would wait for a
    phase that only starts once *step* is done.
    """

    def first(name: str) -> int | None:
        for index, phase in enumerate(phases):
            if name in INIT_PHASE_STEPS.get(phase, []):
                return index
        return None

    step_at, dep_at = first(step), first(dep)
    return step_at is not None and dep_at is not None and dep_at > step_at


def expected_steps(phases: list[str]) -> list[str]:
    """Ordered unique step names for the given init phase list."""
    out: list[str] = []
    seen: set[str] = set()
    for phase in phases:
        for step in INIT_PHASE_STEPS.get(phase, []):
            if step not in seen:
                seen.add(step)
                out.append(step)
    return out


def phase_backfill(phase: str) -> bool:
    return phase in INIT_BACKFILL_PHASES


def step_backfill(step: str, phases: list[str]) -> bool:
    if step == "corporate_actions":
        return "phase2a_corporate_actions" in phases
    if step == "daily_bars":
        return "phase2c_daily_bars_backfill" in phases
    if step in ("instruments", "trading_calendar"):
        return "phase1_reference" in phases
    if step == "index_bars":
        return "phase3_index_and_status" in phases
    if step == "trading_status":
        return False
    if step == "trading_status_derive":
        return "phase5_derive_and_publish" in phases
    return False


def datasets_in_run(batches: list[Any]) -> set[str]:
    return {str(b["dataset"]) for b in batches}


def step_batches(batches: list[Any], step: str) -> list[Any]:
    return [b for b in batches if b["dataset"] == step]


def step_started(batches: list[Any], step: str) -> bool:
    return bool(step_batches(batches, step))


def step_succeeded(batches: list[Any], step: str) -> bool:
    rows = step_batches(batches, step)
    return bool(rows) and all(r["status"] in RESOLVED_BATCH_STATUSES for r in rows)


def step_completed(batches: list[Any], step: str) -> bool:
    """Execution can close with a persisted coverage limitation."""
    rows = step_batches(batches, step)
    if any(row["status"] in {"queued", "running", "stale"} for row in rows):
        return False
    logical = [row for row in rows if dict(row).get("logical_step")]
    if logical:
        return execution_settled(logical[-1])
    return bool(rows) and all(execution_settled(row) for row in rows)


def step_incomplete(batches: list[Any], step: str) -> bool:
    rows = step_batches(batches, step)
    return bool(rows) and any(r["status"] not in RESOLVED_BATCH_STATUSES for r in rows)


def current_phase_statuses(phases: list[str], batches: list[Any]) -> dict[str, str]:
    """Recompute phase state from the current ledger, not old run output."""
    out: dict[str, str] = {}
    for phase in phases:
        steps = INIT_PHASE_STEPS.get(phase, [])
        if not steps:
            continue
        if all(step_completed(batches, step) for step in steps):
            out[phase] = (
                "success" if all(step_succeeded(batches, step) for step in steps) else "degraded"
            )
        elif any(step_incomplete(batches, step) for step in steps):
            out[phase] = "incomplete"
        else:
            out[phase] = "pending"
    return out


def missing_steps(phases: list[str], batches: list[Any]) -> list[str]:
    """Steps in *phases* that have no batch records yet."""
    present = datasets_in_run(batches)
    return [s for s in expected_steps(phases) if s not in present]


def missing_steps_within_phase_order(phases: list[str], batches: list[Any]) -> list[str]:
    """Never-started steps, stopping after the first phase that still owes work.

    Init phases are ordered because each reads what the one before it
    published: `daily_bars` needs the instrument list, `trading_status` needs
    the calendar. Starting a later phase while an earlier one is unresolved
    builds on input already known to be incomplete.

    `cne init` has refused to do that since it learned to resume, but
    `cne run retry --run-id <init run>` reached the same run through a
    different door and used only the dependency graph, which is a weaker
    claim: nothing *declares* that `index_bars` depends on
    `corporate_actions`, so retry happily advanced past a phase that resume
    would have stopped at. One ordering rule, both doors.

    Never-started steps of the blocking phase are returned: running them moves
    that phase toward done. It is the phases after it that must wait.
    """
    present = datasets_in_run(batches)
    out: list[str] = []
    seen: set[str] = set()
    for phase in phases:
        steps = INIT_PHASE_STEPS.get(phase, [])
        if not steps:
            continue
        for step in steps:
            if step not in present and step not in seen:
                seen.add(step)
                out.append(step)
        if not all(step_completed(batches, step) for step in steps):
            break
    return out


def pending_phases(phases: list[str], batches: list[Any]) -> list[str]:
    """Phases that are not fully successful yet."""
    out: list[str] = []
    for phase in phases:
        steps = INIT_PHASE_STEPS.get(phase, [])
        if not steps:
            continue
        if all(step_completed(batches, step) for step in steps):
            continue
        out.append(phase)
    return out


def phases_never_started(phases: list[str], batches: list[Any]) -> list[str]:
    """Phases with no batch records yet (partial phases are left to retry)."""
    out: list[str] = []
    for phase in phases:
        steps = INIT_PHASE_STEPS.get(phase, [])
        if not steps:
            continue
        if all(step_succeeded(batches, step) for step in steps):
            continue
        if any(step_started(batches, step) for step in steps):
            continue
        out.append(phase)
    return out


def init_run_complete(phases: list[str], batches: list[Any]) -> bool:
    return all(step_completed(batches, step) for step in expected_steps(phases))


def needs_finalize(phases: list[str], batches: list[Any]) -> bool:
    if "phase4_finalize" not in phases:
        return False
    if not init_run_complete(
        [p for p in phases if p != "phase4_finalize"],
        batches,
    ):
        return False
    return not all(step_completed(batches, step) for step in FINALIZE_STEPS)


def blocking_phase(phase: str) -> bool:
    return phase not in NON_BLOCKING_INIT_PHASES


def blocking_step(phase: str, step: str) -> bool:
    """Whether *step* failing in *phase* must stop a first build."""
    return blocking_phase(phase) and step not in NON_BLOCKING_INIT_STEPS


def blocks_init(phase: str, steps: set[str] | list[str]) -> bool:
    """Whether any of *steps*, left unfinished in *phase*, must stop the init.

    No step to blame (a phase that failed without per-step results) blocks
    exactly as its phase does.
    """
    if not steps:
        return blocking_phase(phase)
    return any(blocking_step(phase, step) for step in steps)


def owed_steps(phases: list[str], batches: list[Any]) -> list[str]:
    """Expected steps this init has not completed, in phase order."""
    return [step for step in expected_steps(phases) if not step_completed(batches, step)]


def only_non_blocking_owed(phases: list[str], batches: list[Any]) -> bool:
    """True when every owed step is one a first build may leave owed.

    The finalize steps must have completed too, so the lake actually holds
    the published result rather than staged rows.
    """
    owed = set(owed_steps(phases, batches))
    if not owed:
        return False
    blocking = {
        step
        for phase in phases
        for step in INIT_PHASE_STEPS.get(phase, [])
        if blocking_step(phase, step)
    }
    return not (owed & blocking)
