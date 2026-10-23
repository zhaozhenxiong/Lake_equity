"""Derivative plans must be read-only, bounded and honest about source scope."""

import json

import pytest
from click.testing import CliRunner

from cnequity.cli.main import cli
from cnequity.config.bootstrap import path_for_toml


def _config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[data]\nroot = "{path_for_toml(tmp_path / "lake")}"\n[futures]\nenabled = true\n'
    )
    return cfg


def test_plan_does_not_create_a_lake_and_limits_exchange(tmp_path):
    config = _config(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "backfill",
            "futures_bars",
            "--config",
            str(config),
            "--start",
            "2026-09-23",
            "--end",
            "2026-09-24",
            "--exchange",
            "SHF",
            "--plan",
        ],
    )
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert [r["exchange"] for r in plan["routes"]] == ["SHF"]
    assert plan["routes"][0]["cold_request_estimate"] == 2
    assert "futures_contracts" in plan["followup_steps"]
    assert plan["writes"] is False
    assert not (tmp_path / "lake").exists()


def test_2018_ine_plan_counts_separate_official_file(tmp_path):
    config = _config(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "backfill",
            "futures_bars",
            "--config",
            str(config),
            "--start",
            "2018-03-26",
            "--end",
            "2018-03-26",
            "--exchange",
            "INE",
            "--plan",
        ],
    )
    assert result.exit_code == 0, result.output
    route = json.loads(result.output)["routes"][0]
    assert route["exchange"] == "SHF"
    assert route["cold_request_estimate"] == 2
    assert not (tmp_path / "lake").exists()


@pytest.mark.usefixtures("chinese_output")
def test_minute_symbols_are_supported_but_history_dates_are_not(tmp_path):
    config = _config(tmp_path)
    args = [
        "backfill",
        "futures_minute_bars",
        "--config",
        str(config),
        "--symbols",
        "CU2611.SHF",
        "--plan",
    ]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["contracts"] == ["CU2611.SHF"]
    result = CliRunner().invoke(cli, [*args, "--start", "2020-01-01"])
    assert result.exit_code != 0 and "只能采集近期窗口" in result.output


def test_force_is_not_silently_ignored_for_derivatives(tmp_path):
    config = _config(tmp_path)
    result = CliRunner().invoke(
        cli, ["backfill", "futures_bars", "--config", str(config), "--force"]
    )
    assert result.exit_code != 0 and "--refresh" in result.output


@pytest.mark.usefixtures("chinese_output")
def test_continuous_rejects_ignored_date_bounds(tmp_path):
    config = _config(tmp_path)
    result = CliRunner().invoke(
        cli, ["derive", "futures_continuous", "--config", str(config), "--start", "2026-09-01"]
    )
    assert result.exit_code != 0 and "必须全量重算" in result.output


def test_derivative_window_empty_lake_is_incomplete_and_read_only(tmp_path):
    config = _config(tmp_path)
    result = CliRunner().invoke(
        cli,
        [
            "verify",
            "--derivatives",
            "--dataset",
            "futures_bars",
            "--start",
            "2026-09-23",
            "--end",
            "2026-09-24",
            "--config",
            str(config),
        ],
    )
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["state"] == "incomplete"
    assert report["exchange_gaps"]["CFE"] == ["2026-09-23", "2026-09-24"]
    assert not (tmp_path / "lake").exists()


def test_derivative_window_rejects_repair_and_missing_bounds(tmp_path):
    config = _config(tmp_path)
    args = ["verify", "--derivatives", "--dataset", "futures_bars", "--config", str(config)]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 2 and "--end" in result.output
    result = CliRunner().invoke(cli, [*args, "--repair"])
    assert result.exit_code == 2 and "--repair" in result.output
    assert not (tmp_path / "lake").exists()


def test_contract_plan_distinguishes_history_from_current_snapshot(tmp_path):
    config = _config(tmp_path)
    for exchange, requests in [("CFE", 2), ("GFE", 0)]:
        result = CliRunner().invoke(
            cli,
            [
                "backfill",
                "option_contracts",
                "--config",
                str(config),
                "--exchange",
                exchange,
                "--start",
                "2026-09-23",
                "--end",
                "2026-09-24",
                "--plan",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["routes"][0]["reference_requests_upper_bound"] == requests
        assert payload["routes"][0]["cold_request_estimate"] == requests
    assert not (tmp_path / "lake").exists()
