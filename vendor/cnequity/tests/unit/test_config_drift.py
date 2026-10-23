"""`cne config diff` — what a config written once has fallen behind.

The user config is gitignored and written a single time by `cne config create`.
A release that adds a step to a schedule group is therefore invisible: the
feature ships, the config never schedules it, and `cne config validate` still
answers `Configuration OK`.
"""

from __future__ import annotations

import pytest

from cnequity.config.bootstrap import example_toml_text
from cnequity.config.drift import config_drift, render_drift


def _write(tmp_path, text: str):
    path = tmp_path / "cnequity.toml"
    path.write_text(text, encoding="utf-8")
    return path


# `ths_official_snapshot` is scheduled twice in the example: in the core group
# and in the finalize wave.
CORE_WITH, CORE_WITHOUT = (
    '  "compact", "derive_adj_factors", "derive_industry_index",\n  "ths_official_snapshot",\n]',
    '  "compact", "derive_adj_factors", "derive_industry_index",\n]',
)
WAVE_WITH, WAVE_WITHOUT = '    "ths_official_snapshot",\n    "audit",', '    "audit",'


def _without_snapshot_step() -> str:
    """The example with `ths_official_snapshot` removed from its wave and its group."""
    text = example_toml_text()
    for old, new in ((CORE_WITH, CORE_WITHOUT), (WAVE_WITH, WAVE_WITHOUT)):
        assert old in text
        text = text.replace(old, new)
    return text


def test_the_packaged_example_has_no_drift_against_itself(tmp_path):
    drift = config_drift(_write(tmp_path, example_toml_text()))

    assert drift.clean
    assert drift.unscheduled_steps == []


def test_a_step_missing_from_every_group_is_reported(tmp_path):
    """The case that actually loses data: installed, configured nowhere, never runs."""
    text = _without_snapshot_step()
    drift = config_drift(_write(tmp_path, text))

    assert "ths_official_snapshot" in drift.unscheduled_steps
    assert not drift.clean


def test_missing_sections_and_keys_are_separated(tmp_path):
    text = example_toml_text().replace('audit_gate = "shadow"', "")
    drift = config_drift(_write(tmp_path, text))

    assert "quality.audit_gate" in drift.missing_keys
    assert "quality" not in drift.missing_sections


def test_a_whole_missing_section_is_reported_once_not_per_key(tmp_path):
    lines = example_toml_text().splitlines()
    start = lines.index("[incremental]")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("["))
    drift = config_drift(_write(tmp_path, "\n".join(lines[:start] + lines[end:])))

    assert "incremental" in drift.missing_sections
    assert not [key for key in drift.missing_keys if key.startswith("incremental.")]


def test_local_values_are_not_drift(tmp_path):
    """data.root and the platform worker count are the operator's, always."""
    text = (
        example_toml_text()
        .replace('root = "./data/cnequity"', 'root = "/srv/lake"')
        .replace("workers = 1", "workers = 8")
    )
    drift = config_drift(_write(tmp_path, text))

    assert drift.clean


def test_extra_local_settings_are_not_reported(tmp_path):
    """A config may carry more than the example; only what it lacks matters."""
    drift = config_drift(_write(tmp_path, example_toml_text() + "\n[my_own]\nthing = 1\n"))

    assert drift.clean


@pytest.mark.usefixtures("chinese_output")
def test_render_names_the_unscheduled_steps_first(tmp_path):
    text = _without_snapshot_step()
    path = _write(tmp_path, text)

    lines = render_drift(config_drift(path), path)

    assert "ths_official_snapshot" in lines[1]
    assert "未被调度" in lines[0]


def test_render_says_so_when_there_is_nothing_to_report(tmp_path):
    path = _write(tmp_path, example_toml_text())

    assert len(render_drift(config_drift(path), path)) == 1


def test_a_step_only_a_wave_still_runs_is_missing_from_its_group(tmp_path):
    """`cne run daily` runs the groups; a wave-only step there never runs."""
    text = example_toml_text().replace(CORE_WITH, CORE_WITHOUT)
    path = _write(tmp_path, text)
    drift = config_drift(path)

    assert drift.unscheduled_steps == []
    assert drift.group_steps == {"job.daily.groups.core": ["ths_official_snapshot"]}
    lines = render_drift(drift, path)
    assert any("ths_official_snapshot" in line for line in lines)
    assert any("cne config upgrade" in line for line in lines)


def test_a_step_moved_to_another_group_is_not_missing(tmp_path):
    text = (
        example_toml_text()
        .replace(CORE_WITH, CORE_WITHOUT)
        .replace(
            'steps = ["dragon_tiger", "block_trades", "compact"]',
            'steps = ["dragon_tiger", "block_trades", "ths_official_snapshot", "compact"]',
        )
    )
    assert config_drift(_write(tmp_path, text)).group_steps == {}
