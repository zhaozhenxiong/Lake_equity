"""Report how a user config has fallen behind the packaged example.

``cne config create`` writes the config once. Every release after that may add a
section, a key, or — the case that actually loses data — a step to a schedule
group. Nothing tells the operator: the file is gitignored, missing sections
silently take their defaults, and ``cne config validate`` answers
``Configuration OK`` for a config whose ``core`` group is missing a step the
shipped example has been running for weeks.

A missing *key* is usually harmless (the default applies). A missing *step* is
not: a step that appears in no group and no wave is never scheduled, so the
feature it implements is installed and never runs.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore

from cnequity.config.bootstrap import example_toml_text
from cnequity.i18n import _

# Local by construction: reporting these as drift would be noise on every run.
_LOCAL_ONLY_KEYS = frozenset(
    {
        "data.root",
        "orchestrator.workers",
        "orchestrator.tdx_daily_backend",
    }
)


@dataclass
class ConfigDrift:
    """What the packaged example has that this config does not."""

    missing_sections: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    unscheduled_steps: list[str] = field(default_factory=list)
    #: ``job.daily.groups.core`` → steps the example's same-named group has and
    #: none of this config's groups do. The group schedule (`cne run daily`)
    #: never runs them, even when a wave still lists them.
    group_steps: dict[str, list[str]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (
            self.missing_sections or self.missing_keys or self.unscheduled_steps or self.group_steps
        )


def _flatten(table: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested tables to dotted paths, keeping arrays-of-tables opaque.

    An array of tables (``[[job.daily.waves]]``) is compared as a whole by the
    step check below; walking into it would report every wave index as drift
    for an operator who merely reordered them.
    """
    out: dict[str, Any] = {}
    for key, value in table.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out[path] = value
            out.update(_flatten(value, f"{path}."))
        else:
            out[path] = value
    return out


def _scheduled_steps(raw: dict) -> set[str]:
    """Every step name this config would ever run, from waves and both group maps."""
    steps: set[str] = set()
    job = raw.get("job", {})
    for wave in job.get("daily", {}).get("waves", []) or []:
        steps.update(wave.get("steps", []) or [])
    for scope in ("daily", "events"):
        for group in (job.get(scope, {}).get("groups", {}) or {}).values():
            steps.update(group.get("steps", []) or [])
    return steps


def _group_tables(raw: dict) -> dict[str, list[str]]:
    """``job.<scope>.groups.<name>`` → its steps, for both group maps."""
    out: dict[str, list[str]] = {}
    job = raw.get("job", {})
    for scope in ("daily", "events"):
        for name, group in (job.get(scope, {}).get("groups", {}) or {}).items():
            out[f"job.{scope}.groups.{name}"] = list(group.get("steps", []) or [])
    return out


def missing_group_steps(mine: dict, theirs: dict) -> dict[str, list[str]]:
    """Steps the example schedules in a group this config also has, but no group of its own runs.

    A step the operator moved to another group is not missing: only steps
    absent from every one of this config's groups are reported.
    """
    mine_groups = _group_tables(mine)
    grouped = {step for steps in mine_groups.values() for step in steps}
    out: dict[str, list[str]] = {}
    for table, steps in _group_tables(theirs).items():
        if table not in mine_groups:
            continue
        missing = [step for step in steps if step not in grouped]
        if missing:
            out[table] = missing
    return out


def config_drift(config_path: Path | str) -> ConfigDrift:
    """Compare *config_path* against the packaged example template."""
    with open(config_path, "rb") as handle:
        mine = tomllib.load(handle)
    theirs = tomllib.loads(example_toml_text())

    mine_flat = _flatten(mine)
    theirs_flat = _flatten(theirs)

    drift = ConfigDrift()
    for path, value in theirs_flat.items():
        if path in mine_flat or path in _LOCAL_ONLY_KEYS:
            continue
        if isinstance(value, dict):
            # Report the outermost missing table only; its children add nothing.
            if not any(path.startswith(f"{parent}.") for parent in drift.missing_sections):
                drift.missing_sections.append(path)
        elif not any(path.startswith(f"{parent}.") for parent in drift.missing_sections):
            drift.missing_keys.append(path)

    drift.unscheduled_steps = sorted(_scheduled_steps(theirs) - _scheduled_steps(mine))
    drift.group_steps = missing_group_steps(mine, theirs)
    drift.missing_sections.sort()
    drift.missing_keys.sort()
    return drift


def render_drift(drift: ConfigDrift, config_path: Path | str) -> list[str]:
    """Human-readable report lines, most consequential first."""
    if drift.clean:
        return [_("{path} matches the packaged example config.").format(path=config_path)]

    lines: list[str] = []
    if drift.unscheduled_steps:
        lines.append(
            _("Unscheduled steps ({count}) — installed but never run:").format(
                count=len(drift.unscheduled_steps)
            )
        )
        lines.extend(f"  {step}" for step in drift.unscheduled_steps)
        lines.append("")
    in_groups = {
        table: [step for step in steps if step not in drift.unscheduled_steps]
        for table, steps in drift.group_steps.items()
    }
    in_groups = {table: steps for table, steps in in_groups.items() if steps}
    if in_groups:
        lines.append(
            _(
                "Steps missing from schedule groups — `cne run daily` without arguments "
                "will not run them:"
            )
        )
        lines.extend(f"  [{table}] {', '.join(steps)}" for table, steps in in_groups.items())
        lines.append("")
    if drift.missing_sections:
        lines.append(
            _("Missing config sections ({count}) — built-in defaults are in use:").format(
                count=len(drift.missing_sections)
            )
        )
        lines.extend(f"  [{section}]" for section in drift.missing_sections)
        lines.append("")
    if drift.missing_keys:
        lines.append(
            _("Missing config keys ({count}) — built-in defaults are in use:").format(
                count=len(drift.missing_keys)
            )
        )
        lines.extend(f"  {key}" for key in drift.missing_keys)
        lines.append("")
    if (
        drift.unscheduled_steps
        or drift.group_steps
        or any(
            section.startswith(("job.daily.groups.", "job.events.groups."))
            for section in drift.missing_sections
        )
    ):
        lines.append(
            _(
                "→ Run `cne config upgrade` to add the missing steps and schedule groups "
                "(it backs up the config first)."
            )
        )
    if drift.missing_keys or drift.missing_sections:
        lines.append(
            _("Missing config keys use built-in defaults; there is no need to add them by hand.")
        )
    lines.append(
        _("Compared with: {path}").format(
            path=Path(__file__).parent / "templates" / "cnequity.example.toml"
        )
    )
    return lines
