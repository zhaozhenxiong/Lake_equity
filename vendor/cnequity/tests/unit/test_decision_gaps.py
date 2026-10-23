from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest
from click.testing import CliRunner

from cnequity.cli.main import cli
from cnequity.quality import decision_gaps


def _holdout_config(tmp_path) -> str:
    path = tmp_path / "cnequity.toml"
    path.write_text(
        f'[data]\nroot = "{(tmp_path / "lake").as_posix()}"\n\n[research]\nholdout_start = 2025-01-01\n'
    )
    return str(path)


def _forbid_reads(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("holdout data was read")

    monkeypatch.setattr(decision_gaps, "load", forbidden)
    monkeypatch.setattr(decision_gaps, "committed_revision", forbidden)


def test_holdout_window_rejected_before_lake_read(monkeypatch, tmp_path):
    _forbid_reads(monkeypatch)
    with pytest.raises(ValueError, match="research holdout"):
        decision_gaps.inventory(
            data_root=tmp_path,
            start=date(2024, 1, 1),
            end=date(2025, 1, 1),
            holdout_start=date(2025, 1, 1),
        )
    with pytest.raises(ValueError, match="ends before it starts"):
        decision_gaps.check_research_window(date(2024, 1, 2), date(2024, 1, 1))
    # No holdout configured: a public lake may inventory any window.
    decision_gaps.check_research_window(date(2016, 1, 1), date(2026, 9, 25))


def test_inventory_is_pinned_and_splits_gaps_by_tradable_span(monkeypatch, tmp_path):
    rows = pl.DataFrame(
        {
            "symbol": ["600025.SH", "519001.SH", "920001.BJ", "600519.SH", "920002.BJ"],
            "ex_date": [date(2020, 1, 2)] * 4 + [date(2019, 5, 6)],
            "action_type": ["cash_dividend"] * 5,
            "cash_dividend": [0.1, 0.2, 0.3, 0.4, 0.5],
            "payment_date": [None, None, None, date(2020, 1, 3), None],
            "source": ["vendor"] * 5,
            "data_version": ["v1"] * 5,
        }
    )
    bars = pl.DataFrame(
        {
            "symbol": [
                "600025.SH",
                "600025.SH",
                "519001.SH",
                "519001.SH",
                "920001.BJ",
                "920002.BJ",
            ],
            "trade_date": [
                date(2019, 1, 2),
                date(2020, 12, 31),
                date(2019, 1, 2),
                date(2020, 12, 31),
                date(2021, 11, 15),
                date(2021, 11, 15),
            ],
        }
    )

    def fake_load(dataset, **kwargs):
        if dataset == "daily_bars":
            assert kwargs["revision"] == "bars-id"
            return bars.filter(pl.col("symbol").is_in(kwargs["symbols"]))
        assert dataset == "corporate_actions"
        assert kwargs["revision"] == "revision-id"
        return rows

    monkeypatch.setattr(decision_gaps, "load", fake_load)
    monkeypatch.setattr(decision_gaps, "committed_revision", lambda *a, **kw: (9, "bars-id"))
    kwargs = {
        "data_root": tmp_path,
        "start": date(2016, 1, 1),
        "end": date(2024, 12, 31),
        "revision": (214, "revision-id"),
    }
    first = decision_gaps.inventory(**kwargs)
    second = decision_gaps.inventory(**kwargs)
    assert first == second
    assert first["count"] == 4
    assert first["source_route_counts"] == {
        "identity_unresolved": 1,
        "sh_sz_equity_candidate": 1,
        "bse_equity_candidate": 2,
    }
    assert first["bar_relation_counts"] == {
        "before_first_observed_bar": 2,
        "within_observed_bar_span": 2,
    }
    # Two gaps a holding could have been paid in; only the fund-like code
    # cannot fall back on the ex-date rule.
    assert (first["within_span_count"], first["within_span_rule_ineligible_count"]) == (2, 1)
    assert all(event["evidence_status"] == "unreviewed" for event in first["events"])
    saved = decision_gaps.save_immutable(first, tmp_path)
    assert decision_gaps.save_immutable(second, tmp_path) == saved
    assert json.loads(saved.read_text()) == first
    saved.write_text("corrupt")
    with pytest.raises(ValueError, match="different content"):
        decision_gaps.save_immutable(first, tmp_path)


@pytest.mark.parametrize("command", ["payment-gaps", "stock-terms", "cash-rights"])
def test_cli_refuses_a_window_reaching_the_configured_holdout(monkeypatch, tmp_path, command):
    _forbid_reads(monkeypatch)
    config = _holdout_config(tmp_path)
    result = CliRunner().invoke(
        cli, ["decision-data", command, "--end", "2025-01-01", "--config", config]
    )
    assert result.exit_code != 0
    assert "research holdout" in result.output


def test_stock_term_diagnostic_flags_coexistence_without_claiming_error(monkeypatch, tmp_path):
    rows = pl.DataFrame(
        {
            "symbol": ["920799.BJ", "920799.BJ", "920799.BJ", "600000.SH"],
            "ex_date": [date(2022, 6, 1)] * 3 + [date(2022, 6, 2)],
            "action_type": ["bonus", "transfer", "cash_dividend", "bonus"],
            "bonus_ratio": [0.5, 0.0, 0.0, 0.2],
            "transfer_ratio": [0.0, 0.5, 0.0, 0.0],
            "source": ["tdx_protocol"] * 4,
        }
    )
    monkeypatch.setattr(decision_gaps, "committed_revision", lambda *a, **kw: (221, "rev"))

    def fake_load(dataset, **kwargs):
        assert kwargs["revision"] == "rev"
        return rows

    monkeypatch.setattr(decision_gaps, "load", fake_load)
    result = decision_gaps.stock_term_diagnostic(
        data_root=tmp_path, start=date(2016, 1, 1), end=date(2024, 12, 31)
    )
    assert result["count"] == 1
    assert result["events"][0]["symbol"] == "920799.BJ"
    assert result["events"][0]["evidence_status"] == "requires_issuer_reconciliation"
    assert result["coexistence_is_not_error_proof"] is True


def test_zeroed_bonus_is_not_an_unresolved_dual_distribution():
    rows = pl.DataFrame(
        {
            "symbol": ["920405.BJ", "920405.BJ"],
            "ex_date": [date(2022, 5, 23)] * 2,
            "action_type": ["bonus", "transfer"],
            "bonus_ratio": [0.0, 0.0],
            "transfer_ratio": [0.0, 1.0],
            "source": ["eastmoney", "ths"],
        }
    )
    assert decision_gaps.dual_stock_action_events(rows) == []
