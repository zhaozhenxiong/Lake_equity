from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from cnequity.cli.main import cli
from cnequity.config import load_config
from cnequity.config.bootstrap import path_for_toml
from cnequity.orchestrator.engine import JobEngine
from cnequity.storage.layout import init_data_layout


def _write_config(tmp_path) -> str:
    cfg_path = tmp_path / "cnequity.toml"
    cfg_path.write_text(
        f"""
[data]
root = "{path_for_toml(tmp_path / "data")}"

[tdx_protocol]
allow_mock = true

[job.init.phases]
names = ["phase1_reference"]
"""
    )
    return str(cfg_path)


@pytest.mark.usefixtures("chinese_output")
def test_init_layout_only_skips_phases(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    called = {"phases": False}

    def fake_run_init_phases(self, trade_date=None, **kwargs):
        called["phases"] = True
        return {"run_id": "x", "status": "success", "phases": []}

    monkeypatch.setattr(JobEngine, "run_init_phases", fake_run_init_phases)

    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--config", cfg_path, "--layout-only"])

    assert result.exit_code == 0
    assert called["phases"] is False
    assert "建好目录结构" in result.output
    cfg = load_config(cfg_path)
    init_data_layout(cfg)
    assert cfg.curated_root.exists()


def test_init_runs_phases_by_default(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    seen: dict[str, date | None] = {"trade_date": "unset"}

    def fake_run_init_phases(self, trade_date=None, **kwargs):
        seen["trade_date"] = trade_date
        return {
            "run_id": "init-run",
            "status": "success",
            "phases": [{"phase": "phase1_reference", "status": "success"}],
        }

    monkeypatch.setattr(JobEngine, "run_init_phases", fake_run_init_phases)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["init", "--config", cfg_path, "--trade-date", "2024-06-28"],
    )

    assert result.exit_code == 0
    assert seen["trade_date"] == date(2024, 6, 28)
    assert "init-run" in result.output


def test_run_daily_passes_trade_date(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    # Minimal schedule group so --group core resolves.
    Path(cfg_path).write_text(
        Path(cfg_path).read_text()
        + """
[job.daily.groups.core]
at = "16:00"
steps = ["compact"]
"""
    )
    seen: dict = {}

    def fake_run_job(self, job_name, trade_date=None, **kwargs):
        seen["job_name"] = job_name
        seen["trade_date"] = trade_date
        return {"run_id": "d1", "status": "success", "results": []}

    monkeypatch.setattr(JobEngine, "run_job", fake_run_job)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["run", "daily", "--config", cfg_path, "--group", "core", "--trade-date", "2026-07-17"],
    )
    assert result.exit_code == 0, result.output
    assert seen["job_name"] == "daily:core"
    assert seen["trade_date"] == date(2026, 7, 17)


def test_init_exits_nonzero_when_phase_fails(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)

    def fake_run_init_phases(self, trade_date=None, **kwargs):
        return {
            "run_id": "init-run",
            "status": "failed",
            "phases": [{"phase": "phase1_reference", "status": "failed"}],
        }

    monkeypatch.setattr(JobEngine, "run_init_phases", fake_run_init_phases)

    runner = CliRunner()
    result = runner.invoke(cli, ["init", "--config", cfg_path])

    assert result.exit_code == 1


def _init_with(tmp_path, monkeypatch, outcome: dict, *extra: str):
    import json as json_mod

    cfg_path = _write_config(tmp_path)
    monkeypatch.setattr(
        JobEngine, "run_init_phases", lambda self, trade_date=None, **kwargs: dict(outcome)
    )
    result = CliRunner().invoke(cli, ["init", "--config", cfg_path, *extra])
    saved = tmp_path / "data" / "logs" / f"cne-init-{outcome['run_id']}.json"
    return result, cfg_path, json_mod.loads(saved.read_text(encoding="utf-8"))


@pytest.mark.usefixtures("chinese_output")
def test_init_ends_with_a_summary_and_saves_the_full_result(tmp_path, monkeypatch):
    outcome = {"run_id": "r1", "status": "success", "phases": [{"phase": "p", "big": "x" * 50}]}
    result, cfg_path, saved = _init_with(tmp_path, monkeypatch, outcome)

    assert result.exit_code == 0, result.output
    assert "✓ 湖已建好，可以使用（run r1）" in result.output
    assert f"cne check --config {cfg_path}" in result.output
    assert "claude mcp add cnequity" in result.output
    assert '"phases"' not in result.output
    assert saved["phases"] == outcome["phases"]


def test_init_json_flag_prints_the_machine_result(tmp_path, monkeypatch):
    outcome = {"run_id": "r2", "status": "success", "phases": []}
    result, _, _ = _init_with(tmp_path, monkeypatch, outcome, "--json")

    assert result.exit_code == 0, result.output
    assert '"run_id": "r2"' in result.output
    assert "建湖结果" not in result.output


@pytest.mark.usefixtures("chinese_output")
def test_a_usable_lake_with_owed_steps_does_not_fail_the_build(tmp_path, monkeypatch):
    outcome = {
        "run_id": "r3",
        "status": "failed",
        "phases": [],
        "owed_steps": ["corporate_actions"],
        "lake_usable": True,
    }
    result, cfg_path, _ = _init_with(tmp_path, monkeypatch, outcome)

    assert result.exit_code == 0, result.output
    assert "可以查询日线" in result.output
    assert "公司行为（corporate_actions）" in result.output
    assert f"cne init --config {cfg_path}" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_an_unusable_build_says_how_to_resume(tmp_path, monkeypatch):
    outcome = {
        "run_id": "r4",
        "status": "failed",
        "phases": [],
        "owed_steps": ["daily_bars", "compact"],
        "lake_usable": False,
    }
    result, _, _ = _init_with(tmp_path, monkeypatch, outcome)

    assert result.exit_code == 1
    assert "✗ 建湖还没完成" in result.output
    assert "日线（daily_bars）" in result.output
    assert "cne sources probe" in result.output
    assert "cne serve" not in result.output


def test_init_on_a_weekend_builds_as_of_the_last_session(tmp_path, monkeypatch):
    cfg_path = _write_config(tmp_path)
    seen = {}

    def fake_run_init_phases(self, trade_date=None, **kwargs):
        seen["trade_date"] = trade_date
        return {"run_id": "w1", "status": "success", "phases": []}

    monkeypatch.setattr(JobEngine, "run_init_phases", fake_run_init_phases)
    monkeypatch.setattr("cnequity.cli.setup_cmds.shanghai_today", lambda: date(2026, 10, 17))
    result = CliRunner().invoke(cli, ["init", "--config", cfg_path])

    assert result.exit_code == 0, result.output
    assert seen["trade_date"] == date(2026, 10, 16)  # Saturday -> Friday's session
