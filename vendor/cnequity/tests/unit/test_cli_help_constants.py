"""Help text that spells out a constant has to keep saying the constant's value.

The catalog needs help as literal text, so `--profile` and `verify --days` write
their numbers out instead of formatting them in. These fail when a constant
changes and its help was not updated with it.
"""

from __future__ import annotations

import click

from cnequity.cli.main import cli
from cnequity.cli.quality_cmds import DEFAULT_STABILITY_DAYS
from cnequity.cli.setup_cmds import QUICK_PROFILE_YEARS
from cnequity.steps.common import BACKFILL_START


def _help(path: list[str], option: str) -> str:
    command: click.Command = cli
    for name in path:
        command = command.get_command(click.Context(command), name)
    param = next(p for p in command.params if option in getattr(p, "opts", ()))
    return param.__dict__["help"]


def test_profile_help_states_the_quick_years_and_full_start():
    text = _help(["init"], "--profile")
    assert f"last {QUICK_PROFILE_YEARS} years" in text
    assert f"since {BACKFILL_START.isoformat()}" in text


def test_verify_days_help_states_the_default():
    assert f"(default {DEFAULT_STABILITY_DAYS})" in _help(["verify"], "--days")
