"""`cne config upgrade` — schedule drift fixed by the command that reports it."""

from __future__ import annotations

import re
import sys

import pytest
from click.testing import CliRunner

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10
    import tomli as tomllib

from cnequity.cli.main import cli
from cnequity.config.bootstrap import example_toml_text
from cnequity.config.drift import config_drift
from cnequity.config.upgrade import _append_to_steps, plan_upgrade


def _old_config(tmp_path, text: str | None = None):
    """The example as an older release shipped it: core without `ths_official_snapshot`."""
    text = text if text is not None else example_toml_text()
    text = text.replace(
        '  "compact", "derive_adj_factors", "derive_industry_index",\n  "ths_official_snapshot",\n]',
        '  "compact", "derive_adj_factors", "derive_industry_index",\n]',
    )
    # `cne config create` writes workers = 1 on macOS; the raw example does not.
    text = re.sub(r"(?m)^workers = \d+", "workers = 1", text)
    path = tmp_path / "cnequity.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _core_steps(path) -> list[str]:
    return tomllib.loads(path.read_text(encoding="utf-8"))["job"]["daily"]["groups"]["core"][
        "steps"
    ]


def test_upgrade_restores_the_group_steps_and_keeps_a_backup(tmp_path):
    path = _old_config(tmp_path)
    before = path.read_text(encoding="utf-8")

    result = CliRunner().invoke(cli, ["config", "upgrade", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert "ths_official_snapshot" in _core_steps(path)
    assert config_drift(path).group_steps == {}
    backups = list(tmp_path.glob("cnequity.toml.bak-*"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == before


def test_dry_run_writes_nothing(tmp_path):
    path = _old_config(tmp_path)
    before = path.read_text(encoding="utf-8")

    result = CliRunner().invoke(cli, ["config", "upgrade", "--dry-run", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert "ths_official_snapshot" in result.output
    assert path.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.bak-*"))


@pytest.mark.usefixtures("chinese_output")
def test_an_up_to_date_config_is_left_alone(tmp_path):
    path = tmp_path / "cnequity.toml"
    path.write_text(example_toml_text(), encoding="utf-8")

    result = CliRunner().invoke(cli, ["config", "upgrade", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert "无需升级" in result.output
    assert not list(tmp_path.glob("*.bak-*"))


def test_a_missing_group_is_copied_with_its_comments(tmp_path):
    text = example_toml_text()
    start = text.index("# Futures and options.")
    end = text.index("# Not scheduled by anything")
    path = _old_config(tmp_path, text[:start] + text[end:])

    plan = plan_upgrade(path)

    assert plan.added_groups == ["job.daily.groups.derivatives"]
    assert "# Futures and options." in plan.text
    groups = tomllib.loads(plan.text)["job"]["daily"]["groups"]
    assert list(groups)[-1] == "derivatives", "appended after the existing groups"


def test_operator_settings_survive(tmp_path):
    text = example_toml_text().replace('at = "17:00"', 'at = "17:05"  # my slot')
    path = _old_config(tmp_path, text)

    plan = plan_upgrade(path)

    assert 'at = "17:05"  # my slot' in plan.text
    assert tomllib.loads(plan.text)["job"]["daily"]["groups"]["capital"]["at"] == "17:05"


@pytest.mark.usefixtures("chinese_output")
def test_a_layout_the_edit_cannot_read_is_left_for_a_manual_edit(tmp_path):
    """An inline core table is not edited; the run says which steps need a hand."""
    path = tmp_path / "cnequity.toml"
    path.write_text(
        'job.daily.groups.core = { at = "16:00", steps = ["daily_bars", "compact"] }\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli, ["config", "upgrade", "--config", str(path)])

    assert result.exit_code == 1
    assert "手动" in result.output and "ths_official_snapshot" in result.output
    assert _core_steps(path) == ["daily_bars", "compact"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('steps = ["a", "b"]\n', ["a", "b", "x", "y"]),
        ('steps = ["a", "b",]\n', ["a", "b", "x", "y"]),
        ("steps = []\n", ["x", "y"]),
        ('steps = [\n  "a",\n  "b"\n]\n', ["a", "b", "x", "y"]),
        ('steps = [\n  "a",  # keep\n  "b",  # keep too\n]\n', ["a", "b", "x", "y"]),
        ('steps = ["a",\n  "b"]  # trailing\n', ["a", "b", "x", "y"]),
    ],
)
def test_append_handles_array_layouts(body, expected):
    text = (
        f'[job.daily.groups.core]\nat = "16:00"\n{body}\n[job.daily.groups.next]\nsteps = ["z"]\n'
    )

    edited = _append_to_steps(text, "job.daily.groups.core", ["x", "y"])

    parsed = tomllib.loads(edited)
    assert parsed["job"]["daily"]["groups"]["core"]["steps"] == expected
    assert parsed["job"]["daily"]["groups"]["next"]["steps"] == ["z"]
    assert "# keep" in edited or "# keep" not in body


def test_explicit_dry_run_is_only_for_upgrade(tmp_path):
    path = tmp_path / "cnequity.toml"
    path.write_text(example_toml_text(), encoding="utf-8")
    result = CliRunner().invoke(cli, ["config", "validate", "--dry-run", "--config", str(path)])
    assert result.exit_code != 0
