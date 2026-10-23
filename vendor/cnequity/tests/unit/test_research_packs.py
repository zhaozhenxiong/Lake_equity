"""Research packs, mid-init daily-bar preview, and pack-scoped daily runs."""

from __future__ import annotations

import json
from datetime import date

import click
import polars as pl
import pytest
from click.testing import CliRunner

from cnequity.cli import quality_cmds
from cnequity.cli.main import cli
from cnequity.config.bootstrap import path_for_toml
from cnequity.orchestrator.engine import JobEngine
from cnequity.query.reader import ReaderError, _preview_can_answer, _with_daily_bar_preview, load
from cnequity.research.packs import (
    assess,
    groups_for,
    next_command_for_dataset,
    normalize_packs,
    packs_path,
    readiness_exit_code,
    snapshot_steps,
)
from cnequity.research.preview import (
    preview_symbols,
    publish_daily_bar_preview,
    retire_preview_symbols,
)
from cnequity.serve.ops.scheduled import _daily_params, _stale_params
from cnequity.storage.parquet import StagingWriter


def _bars() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": ["600000.SH"],
            "trade_date": [date(2024, 6, 18)],
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "volume": [100.0],
            "amount": [1020.0],
            "source": ["test"],
            "data_version": ["v1"],
            "fetched_at": ["2024-06-18T10:00:00+00:00"],
        }
    )


def _groups_config(tmp_path) -> str:
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f"""
[data]
root = "{path_for_toml(tmp_path / "data")}"

[orchestrator]
workers = 1

[tdx_protocol]
allow_mock = true

[job.daily.groups.core]
at = "16:00"
steps = ["daily_bars", "trading_status", "compact"]

[job.daily.groups.fundamentals]
at = "17:00"
steps = ["financial_statement_items", "compact"]

[job.daily.groups.capital]
at = "17:00"
steps = ["compact"]

[job.events.groups.disclosures]
steps = ["compact"]
""",
        encoding="utf-8",
    )
    return str(path)


def test_a_missing_query_names_the_pack_command(config):
    assert next_command_for_dataset("daily_bars") == "cne init"
    assert next_command_for_dataset("financial_statement_items") == (
        "cne run daily --pack fundamentals"
    )
    assert next_command_for_dataset("adj_factors") is None
    with pytest.raises(ReaderError, match="build it with `cne init`"):
        load("daily_bars", symbols=["600000.SH"], config=config)
    with pytest.raises(ReaderError, match="cne run daily --pack fundamentals"):
        load("financial_statement_items", as_of="2024-06-18", config=config)
    with pytest.raises(ReaderError, match="cne derive adj_factors"):
        load("adj_factors", config=config)


def test_a_demo_lake_is_told_to_backfill_the_missing_dataset(config):
    config.lake_profile = "demo"
    # `cne init` and `run daily --pack` never grow a demo lake.
    with pytest.raises(ReaderError, match="build it with `cne backfill daily_bars`"):
        load("daily_bars", symbols=["600000.SH"], config=config)


@pytest.mark.usefixtures("chinese_output")
def test_pack_order_groups_and_empty_lake(config):
    assert normalize_packs(["universe", "market", "market"]) == ("market", "universe")
    assert normalize_packs(None) == ("market",)
    assert groups_for(["fundamentals", "universe"]) == ["fundamentals"]
    with pytest.raises(ValueError, match="未知研究包"):
        normalize_packs(["signals"])

    market, fundamentals, universe = assess(config, ["market", "fundamentals", "universe"])
    assert market["status"] == "missing"
    assert market["next_command"] == "cne init"
    assert fundamentals["next_command"] == "cne run daily --pack fundamentals"
    assert "估值历史" in fundamentals["detail"]
    assert universe["next_command"] == "cne init"
    assert readiness_exit_code([market, fundamentals]) == 1
    assert readiness_exit_code([{"status": "unknown"}]) == 2
    assert readiness_exit_code([{"status": "ready"}, {"status": "unknown"}]) == 2

    from types import SimpleNamespace

    stand_in = SimpleNamespace(
        schedule_groups={
            "core": SimpleNamespace(steps=["daily_bars", "trading_status"]),
            "fundamentals": SimpleNamespace(steps=["financial_statement_items"]),
        }
    )
    assert snapshot_steps(stand_in, ["core", "fundamentals"]) == ["trading_status"]


def test_sealed_daily_bars_load_by_symbol_before_publish(config):
    writer = StagingWriter(config.staging_root)
    writer.write_batch("daily_bars", "run-1", "batch-1", _bars())
    assert publish_daily_bar_preview(config, "run-1", "batch-1") == ["600000.SH"]
    assert preview_symbols(config) == ["600000.SH"]

    loaded = load("daily_bars", symbols=["600000.SH"], config=config)
    assert loaded.height == 1
    assert loaded["symbol"][0] == "600000.SH"
    with pytest.raises(ReaderError):
        load("daily_bars", config=config)

    assert _preview_can_answer("daily_bars", ["600000.SH"], object(), None) is False
    hidden = _with_daily_bar_preview(
        config,
        pl.DataFrame(),
        dataset="daily_bars",
        symbols=["600000.SH"],
        start=None,
        end=None,
        revision=object(),
        read_context=None,
    )
    assert hidden.is_empty()

    loose = config.staging_root / "daily_bars" / "run_id=run-2" / "part-open.parquet"
    loose.parent.mkdir(parents=True, exist_ok=True)
    _bars().write_parquet(loose)
    assert publish_daily_bar_preview(config, "run-2", "open") == []

    retire_preview_symbols(config, ["600000.SH"])
    assert preview_symbols(config) == []
    with pytest.raises(ReaderError):
        load("daily_bars", symbols=["600000.SH"], config=config)


@pytest.mark.usefixtures("chinese_output")
def test_init_pack_is_saved_and_does_not_fetch_a_different_spine(config, monkeypatch):
    calls = {"n": 0}

    def fake_run_init_phases(self, **kwargs):
        calls["n"] += 1
        return {"run_id": "r", "status": "success", "phases": []}

    monkeypatch.setattr(JobEngine, "run_init_phases", fake_run_init_phases)
    runner = CliRunner()
    first = runner.invoke(
        cli,
        [
            "init",
            "--config",
            str(config.config_path),
            "--pack",
            "universe",
            "--pack",
            "fundamentals",
        ],
    )
    assert first.exit_code == 0, first.output
    assert calls["n"] == 1
    assert json.loads(packs_path(config).read_text(encoding="utf-8"))["packs"] == [
        "fundamentals",
        "universe",
    ]
    assert "研究包" in first.output

    second = runner.invoke(cli, ["init", "--config", str(config.config_path)])
    assert second.exit_code == 0, second.output
    assert json.loads(packs_path(config).read_text(encoding="utf-8"))["packs"] == [
        "fundamentals",
        "universe",
    ]

    demo = runner.invoke(cli, ["init", "--profile", "demo", "--pack", "market"])
    assert demo.exit_code != 0
    assert "--pack" in demo.output
    assert calls["n"] == 2


class _Timer:
    kind = "isolated timer"

    def __init__(self, *_args, **kwargs):
        self.working_directory = kwargs.get("working_directory") or "."
        self.enabled = False

    def observe(self):
        return {
            "available": True,
            "installed": self.enabled,
            "active": self.enabled,
            "matches": self.enabled,
            "fingerprint": "isolated",
            "error": None,
        }

    def render(self):
        return "ISOLATED TIMER"

    def apply(self, enabled, artifact):
        self.enabled = enabled
        self.artifact = artifact


@pytest.mark.usefixtures("chinese_output")
def test_init_schedule_installs_only_the_pack_groups(config, monkeypatch):
    monkeypatch.setattr("cnequity.serve.ops.scheduler.SchedulerBackend", _Timer)
    monkeypatch.setattr(
        JobEngine,
        "run_init_phases",
        lambda self, **kwargs: {"run_id": "r", "status": "success", "phases": []},
    )
    result = CliRunner().invoke(
        cli,
        ["init", "--config", str(config.config_path), "--pack", "market", "--schedule"],
    )
    assert result.exit_code == 0, result.output
    assert "已安装定时日更" in result.output
    from cnequity.serve.ops.scheduler import settings

    saved = settings(config)
    assert saved["daily"] is True
    assert saved["stale"] is True
    assert saved["daily_packs"] == ["market"]
    assert saved.get("events") is not True


def test_run_daily_pack_runs_those_groups_without_events(tmp_path, monkeypatch):
    cfg = _groups_config(tmp_path)
    seen: list[str] = []

    def fake_run_job(self, job_name, *args, **kwargs):
        seen.append(job_name)
        return {"run_id": job_name, "status": "success", "results": []}

    monkeypatch.setattr(JobEngine, "run_job", fake_run_job)
    result = CliRunner().invoke(
        cli, ["run", "daily", "--config", cfg, "--pack", "market", "--pack", "fundamentals"]
    )
    assert result.exit_code == 0, result.output
    assert seen == ["daily:core", "daily:fundamentals", "daily:audit"]
    assert "事件流" not in result.output
    assert "trading_status" in result.output

    seen.clear()
    universe = CliRunner().invoke(cli, ["run", "daily", "--config", cfg, "--pack", "universe"])
    assert universe.exit_code == 0, universe.output
    assert seen == []
    assert "cne backfill trading_status" in universe.output

    clash = CliRunner().invoke(
        cli, ["run", "daily", "--config", cfg, "--pack", "market", "--group", "core"]
    )
    assert clash.exit_code != 0
    assert "--pack" in clash.output


@pytest.mark.usefixtures("chinese_output")
def test_check_pack_fails_an_empty_lake(config, monkeypatch):
    @click.command()
    @click.pass_context
    def fake_status(ctx, **kwargs):
        click.echo("dataset table")

    monkeypatch.setattr(quality_cmds, "status", fake_status)
    result = CliRunner().invoke(
        cli, ["check", "--config", str(config.config_path), "--pack", "fundamentals"]
    )
    assert result.exit_code == 1, result.output
    assert "研究包" in result.output
    assert "cne run daily --pack fundamentals" in result.output

    plain = CliRunner().invoke(cli, ["check", "--config", str(config.config_path)])
    assert "研究包" not in plain.output


@pytest.mark.usefixtures("chinese_output")
def test_readiness_route_names_the_gap(config):
    from fastapi.testclient import TestClient

    from cnequity.serve.app import create_app

    client = TestClient(create_app(config), base_url="http://127.0.0.1")
    response = client.get("/api/ops/readiness")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"] == ["market"]
    assert body["groups"] == ["core"]
    assert body["packs"][0]["status"] == "missing"
    assert "快照" in body["note"]


def test_scheduler_pack_limits_daily_and_skips_empty_stale():
    session = date(2026, 10, 2)
    assert _daily_params({}, session) == {"trade_date": "2026-10-02"}
    assert _daily_params({"daily_packs": ["market", "fundamentals"]}, session)["pack"] == [
        "market",
        "fundamentals",
    ]
    assert _stale_params({}) == {"snapshots_only": True}
    assert _stale_params({"daily_packs": ["market"]}) == {
        "snapshots_only": True,
        "groups": ["core"],
    }
    assert _stale_params({"daily_packs": ["universe"]}) is None
