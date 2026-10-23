"""Bring a user config's schedule up to the packaged example: `cne config upgrade`.

`cne config diff` can only report that a release added a step to a schedule
group; the operator then had to find the group in their own TOML and type the
step in. This makes the edit itself, limited to the one kind of drift that
loses data — what the schedule runs:

* a step the example's group has and none of this config's groups do is
  appended to the same-named group's ``steps``;
* a daily or events group this config lacks entirely is copied from the
  example, comments included, to the end of the file;
* a step the example schedules only in a wave goes into the same-named wave.

Missing keys are left alone on purpose: they already take the built-in
default, and writing that default into the file would pin it against later
releases.

There is no TOML writer in the dependency set, so the edits are textual and
narrow — they touch only ``steps`` arrays and append whole tables — and every
result is parsed back and compared with the intended change before anything
is written. A config laid out in a way the edits do not recognise (inline
tables, dotted keys) is reported for a manual edit instead.
"""

from __future__ import annotations

import copy
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore

from cnequity.config.bootstrap import example_toml_text
from cnequity.config.drift import _group_tables, _scheduled_steps, missing_group_steps

_HEADER = re.compile(r"^\s*\[")


@dataclass
class UpgradePlan:
    """What `cne config upgrade` would change, and the text it would write."""

    added_steps: dict[str, list[str]] = field(default_factory=dict)
    added_groups: list[str] = field(default_factory=list)
    manual_steps: list[str] = field(default_factory=list)
    text: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.added_steps or self.added_groups)


def _key_pattern(path: str) -> str:
    """``job.daily.groups.core`` → a regex for that dotted key, bare or quoted parts."""
    parts = [rf'(?:{re.escape(p)}|"{re.escape(p)}"|\'{re.escape(p)}\')' for p in path.split(".")]
    return r"\s*\.\s*".join(parts)


def _table_span(lines: list[str], table: str) -> tuple[int, int] | None:
    """Line range of ``[table]`` (header included), or ``[[job.daily.waves]]`` named so."""
    if table.startswith("wave:"):
        name = table.removeprefix("wave:")
        header = re.compile(r"^\s*\[\[\s*" + _key_pattern("job.daily.waves") + r"\s*\]\]")
        name_line = re.compile(r"""^\s*name\s*=\s*["']""" + re.escape(name) + r"""["']""")
        for start, line in enumerate(lines):
            if not header.match(line):
                continue
            end = _block_end(lines, start)
            if any(name_line.match(body) for body in lines[start + 1 : end]):
                return start, end
        return None
    header = re.compile(r"^\s*\[\s*" + _key_pattern(table) + r"\s*\]\s*(?:#.*)?$")
    for start, line in enumerate(lines):
        if header.match(line):
            return start, _block_end(lines, start)
    return None


def _block_end(lines: list[str], start: int) -> int:
    return next((i for i in range(start + 1, len(lines)) if _HEADER.match(lines[i])), len(lines))


def _append_to_steps(text: str, table: str, steps: list[str]) -> str | None:
    """Append *steps* to ``steps = [...]`` in *table*; None when it cannot be found."""
    lines = text.splitlines(keepends=True)
    span = _table_span(lines, table)
    if span is None:
        return None
    start, end = span
    offset = sum(len(line) for line in lines[:start])
    body = "".join(lines[start:end])
    match = re.search(r"(?m)^\s*steps\s*=\s*\[", body)
    if match is None:
        return None
    # Walk to the closing bracket, skipping strings and comments.
    i, last_sig, quote = match.end(), None, None
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == quote:
                quote = None
                last_sig = i
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            i = body.find("\n", i)
            if i < 0:
                return None
            continue
        elif ch == "]":
            break
        elif not ch.isspace():
            last_sig = i
        i += 1
    else:
        return None
    close = i
    items = ", ".join(f'"{step}"' for step in steps)
    if last_sig is None:
        new_body = body[: match.end()] + items + body[close:]
    else:
        comma = "" if body[last_sig] == "," else ","
        if "\n" in body[last_sig + 1 : close]:
            # The closing bracket has a line of its own: add a line above it,
            # indented like the last element.
            line_start = body.rfind("\n", 0, close) + 1
            last_line = body[body.rfind("\n", 0, last_sig) + 1 :]
            indent = re.match(r"[ \t]*", last_line).group(0)
            new_body = (
                body[: last_sig + 1]
                + comma
                + body[last_sig + 1 : line_start]
                + f"{indent}{items},\n"
                + body[line_start:]
            )
        else:
            new_body = body[: last_sig + 1] + comma + " " + items + body[last_sig + 1 :]
    return text[:offset] + new_body + text[offset + len(body) :]


def _example_section(example: str, table: str) -> str:
    """``[table]`` from the example with its leading comment block, trailing comments trimmed."""
    lines = example.splitlines()
    span = _table_span([line + "\n" for line in lines], table)
    assert span is not None, table
    start, end = span
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    while end > start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return "\n".join(lines[start:end]) + "\n"


def _expected(mine: dict, theirs: dict, plan: UpgradePlan) -> dict:
    """*mine* with the plan applied as data, to check the edited text against."""
    out = copy.deepcopy(mine)
    for table, steps in plan.added_steps.items():
        if table.startswith("wave:"):
            name = table.removeprefix("wave:")
            wave = next(w for w in out["job"]["daily"]["waves"] if w.get("name") == name)
            wave["steps"] = [*wave.get("steps", []), *steps]
        else:
            _, scope, _, name = table.split(".")
            group = out["job"][scope]["groups"][name]
            group["steps"] = [*group.get("steps", []), *steps]
    for table in plan.added_groups:
        _, scope, _, name = table.split(".")
        groups = out.setdefault("job", {}).setdefault(scope, {}).setdefault("groups", {})
        groups[name] = copy.deepcopy(theirs["job"][scope]["groups"][name])
    return out


def plan_upgrade(config_path: Path | str) -> UpgradePlan:
    """Work out the schedule edits for *config_path* and the text that carries them."""
    text = Path(config_path).read_text(encoding="utf-8")
    mine = tomllib.loads(text)
    example = example_toml_text()
    theirs = tomllib.loads(example)
    plan = UpgradePlan(text=text)

    mine_groups = _group_tables(mine)
    for table in _group_tables(theirs):
        if table not in mine_groups:
            plan.added_groups.append(table)
    for table, steps in missing_group_steps(mine, theirs).items():
        plan.added_steps[table] = list(steps)

    # Wave-only steps: scheduled by no group of the example either.
    placed = {step for steps in _group_tables(theirs).values() for step in steps}
    unscheduled = sorted(_scheduled_steps(theirs) - _scheduled_steps(mine) - placed)
    mine_waves = {w.get("name") for w in mine.get("job", {}).get("daily", {}).get("waves", [])}
    for step in unscheduled:
        wave = next(
            (
                w.get("name")
                for w in theirs["job"]["daily"].get("waves", [])
                if step in (w.get("steps") or [])
            ),
            None,
        )
        if wave in mine_waves:
            plan.added_steps.setdefault(f"wave:{wave}", []).append(step)
        else:
            plan.manual_steps.append(step)

    for table, steps in list(plan.added_steps.items()):
        edited = _append_to_steps(plan.text, table, steps)
        if edited is None:
            del plan.added_steps[table]
            plan.manual_steps.extend(steps)
        else:
            plan.text = edited
    if plan.added_groups:
        sections = "\n".join(_example_section(example, table) for table in plan.added_groups)
        plan.text = (
            plan.text.rstrip("\n")
            + "\n\n# Schedule groups below were added by `cne config upgrade`"
            + " from the packaged example config.\n"
            + sections
        )

    if plan.changed and tomllib.loads(plan.text) != _expected(mine, theirs, plan):
        # The text edit did not mean what the plan says; never write it.
        plan.manual_steps.extend(step for steps in plan.added_steps.values() for step in steps)
        plan.manual_steps.extend(plan.added_groups)
        plan.added_steps, plan.added_groups, plan.text = {}, [], text
    plan.manual_steps = sorted(set(plan.manual_steps))
    return plan
