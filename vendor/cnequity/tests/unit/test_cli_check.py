"""`cne check` — one command to accept a lake: freshness, quality, size."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from cnequity.cli import quality_cmds
from cnequity.cli.main import cli
from cnequity.config import load_config
from cnequity.config.bootstrap import path_for_toml


@pytest.fixture
def lake(tmp_path, monkeypatch):
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f'[data]\nroot = "{path_for_toml(tmp_path / "data")}"\n\n[orchestrator]\nworkers = 1\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "cnequity.storage.stats.stats_freshness",
        lambda cfg: type("F", (), {"stale": False})(),
    )
    monkeypatch.setattr(
        "cnequity.storage.stats.load_summary",
        lambda cfg: {"datasets": 3, "rows": 1200, "bytes": 2_500_000_000, "generated_at": "t"},
    )
    return str(path)


def _status_exits(monkeypatch, code: int):
    @click.command()
    @click.pass_context
    def fake_status(ctx, **kwargs):
        click.echo("dataset table")
        if code:
            raise SystemExit(code)

    monkeypatch.setattr(quality_cmds, "status", fake_status)


def _findings(lake: str, findings: list[dict]):
    root = load_config(lake).meta_root / "quality" / "findings"
    root.mkdir(parents=True, exist_ok=True)
    (root / "r1.json").write_text(
        json.dumps({"run_id": "r1", "trade_date": "2026-09-30", "findings": findings}),
        encoding="utf-8",
    )


@pytest.mark.usefixtures("chinese_output")
def test_a_healthy_lake_passes_in_one_command(lake, monkeypatch):
    _status_exits(monkeypatch, 0)
    _findings(lake, [{"severity": "warning", "dataset": "daily_bars", "message": "w"}])

    result = CliRunner().invoke(cli, ["check", "--config", lake])

    assert result.exit_code == 0, result.output
    for section in ("新鲜度与覆盖", "数据质量", "规模", "结论"):
        assert section in result.output
    assert "0 error、1 warning" in result.output
    assert "3 个数据集、1,200 行、2.5 GB" in result.output
    assert "✓ 湖可用" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_audit_errors_fail_the_check(lake, monkeypatch):
    _status_exits(monkeypatch, 0)
    _findings(lake, [{"severity": "error", "dataset": "adj_factors", "message": "step"}])

    result = CliRunner().invoke(cli, ["check", "--config", lake])

    assert result.exit_code == 1
    assert "[error] adj_factors" in result.output
    assert "最近一次审计有 1 条 error" in result.output


def _health(lake: str, payload: dict):
    root = load_config(lake).meta_root / "quality"
    root.mkdir(parents=True, exist_ok=True)
    (root / "health-latest.json").write_text(
        json.dumps({"trade_date": "2026-09-30", **payload}), encoding="utf-8"
    )


@pytest.mark.usefixtures("chinese_output")
def test_errors_in_the_whole_lake_snapshot_fail_the_check(lake, monkeypatch):
    """A clean latest run does not vouch for history the snapshot found broken."""
    _status_exits(monkeypatch, 0)
    _findings(lake, [])
    _health(
        lake,
        {
            "findings_by_severity": {"error": 1},
            "error_findings": [
                {"severity": "error", "dataset": "corporate_actions", "message": "gap"}
            ],
        },
    )

    result = CliRunner().invoke(cli, ["check", "--config", lake])

    assert result.exit_code == 1
    assert "[error] corporate_actions" in result.output
    assert "全湖审计快照有 1 条 error" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_a_count_only_snapshot_still_fails_on_errors(lake, monkeypatch):
    _status_exits(monkeypatch, 0)
    _findings(lake, [])
    _health(lake, {"findings_by_severity": {"error": 2}})

    result = CliRunner().invoke(cli, ["check", "--config", lake])

    assert result.exit_code == 1
    assert "全湖审计快照有 2 条 error" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_full_audit_errors_fail_the_check(lake, monkeypatch):
    _status_exits(monkeypatch, 0)
    _findings(lake, [])
    monkeypatch.setattr(
        "cnequity.quality.audit.lake_health",
        lambda cfg, day: {
            "findings_by_severity": {"error": 1},
            "error_findings": [{"severity": "error", "dataset": "daily_bars", "message": "x"}],
        },
    )

    result = CliRunner().invoke(cli, ["check", "--full", "--config", lake])

    assert result.exit_code == 1
    assert "全湖审计有 1 条 error" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_stale_data_fails_even_when_quality_is_clean(lake, monkeypatch):
    _status_exits(monkeypatch, 1)
    _findings(lake, [])

    result = CliRunner().invoke(cli, ["check", "--config", lake])

    assert result.exit_code == 1
    assert "新鲜度或覆盖不合格" in result.output


def test_no_audit_evidence_cannot_prove_quality(lake, monkeypatch):
    _status_exits(monkeypatch, 0)

    result = CliRunner().invoke(cli, ["check", "--config", lake])

    assert result.exit_code == 2
    assert "--full" in result.output


def test_sample_lake_needs_no_audit_to_pass(lake, monkeypatch):
    path = lake
    text = (
        open(path, encoding="utf-8").read().replace("[data]\n", '[data]\nprofile = "sample"\n', 1)
    )
    open(path, "w", encoding="utf-8").write(text)
    _status_exits(monkeypatch, 0)

    result = CliRunner().invoke(cli, ["check", "--config", lake])

    assert result.exit_code == 0, result.output
    assert "Install verified" in result.output
    assert "not for research" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_full_reruns_the_whole_lake_audit(lake, monkeypatch):
    _status_exits(monkeypatch, 0)
    calls = []

    def health(cfg, day):
        calls.append(day)
        return {"findings_by_severity": {"error": 0}, "error_findings": []}

    monkeypatch.setattr("cnequity.quality.audit.lake_health", health)

    result = CliRunner().invoke(cli, ["check", "--full", "--config", lake])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "全湖审计（刚刚）：0 error" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_hide_empty_folds_datasets_without_data(lake):
    import polars as pl

    root = load_config(lake).curated_root / "daily_bars" / "trade_date=2026-09-30"
    root.mkdir(parents=True)
    pl.DataFrame(
        {"symbol": ["600519.SH"], "close": [1.0], "volume": [1], "source": ["tdx_protocol"]}
    ).write_parquet(root / "part-0.parquet")

    full = CliRunner().invoke(cli, ["status", "--datasets", "--no-scope", "--config", lake])
    folded = CliRunner().invoke(
        cli, ["status", "--datasets", "--no-scope", "--hide-empty", "--config", lake]
    )

    assert full.exit_code == 0, full.output
    assert folded.exit_code == 0, folded.output
    assert "valuation_metrics" in full.output
    assert "valuation_metrics" not in folded.output
    assert "daily_bars" in folded.output
    assert "个数据集还没有数据" in folded.output
