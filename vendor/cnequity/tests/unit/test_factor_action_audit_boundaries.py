"""Full audits and scoped repair gates must inspect the same evidence window."""

from datetime import date, datetime, timezone

import polars as pl
import pytest

from cnequity.config import Config
from cnequity.quality.cross_checks import factor_action_contradictions

DAYS = [date(2024, 1, day) for day in (2, 3, 4, 5)]
FETCHED = datetime(2024, 1, 6, tzinfo=timezone.utc)


def _lake(tmp_path, series, actions=(), funds=()):
    cfg = Config(data_root=tmp_path)
    root = cfg.derived_root / "adj_factors"
    root.mkdir(parents=True)
    pl.DataFrame(
        [
            {
                "symbol": symbol,
                "trade_date": day,
                "adjust_type": "hfq",
                "factor": factor,
                "source": "sina",
                "data_version": "v1",
                "fetched_at": FETCHED,
            }
            for symbol, levels in series.items()
            for day, factor in levels
        ]
    ).write_parquet(root / "part.parquet")
    if actions:
        root = cfg.curated_root / "corporate_actions"
        root.mkdir(parents=True)
        pl.DataFrame(
            [
                {
                    "symbol": symbol,
                    "ex_date": day,
                    "action_type": "cash_dividend",
                    "source": "tdx_protocol",
                    "data_version": "v1",
                    "fetched_at": FETCHED,
                }
                for symbol, day in actions
            ]
        ).write_parquet(root / "part.parquet")
    if funds:
        root = cfg.curated_root / "instruments"
        root.mkdir(parents=True)
        pl.DataFrame({"symbol": list(funds), "asset_type": ["etf"] * len(funds)}).write_parquet(
            root / "part.parquet"
        )
    return cfg


@pytest.mark.parametrize("scoped", [False, True])
def test_flat_factor_still_exposes_an_unmatched_action(tmp_path, scoped):
    cfg = _lake(
        tmp_path,
        {"600001.SH": [(d, 1.0) for d in DAYS]},
        [("600001.SH", DAYS[1])],
    )
    frame, _ = factor_action_contradictions(cfg, symbols=["600001.SH"] if scoped else None)
    assert frame.to_dicts() == [
        {"symbol": "600001.SH", "ex_date": DAYS[1], "kind": "action_without_step"}
    ]


def test_other_symbols_first_jump_does_not_hide_an_earlier_action(tmp_path):
    cfg = _lake(
        tmp_path,
        {
            "600001.SH": [(d, 1.0) for d in DAYS],
            "600002.SH": list(zip(DAYS, [1.0, 1.0, 1.0, 1.1], strict=True)),
        },
        [("600001.SH", DAYS[1]), ("600002.SH", DAYS[3])],
    )
    full, _ = factor_action_contradictions(cfg)
    scoped, _ = factor_action_contradictions(cfg, symbols=["600001.SH"])
    assert full.equals(scoped)
    assert full.height == 1


def test_first_observation_and_older_actions_have_no_comparable_step(tmp_path):
    cfg = _lake(
        tmp_path,
        {
            "600001.SH": [(DAYS[2], 1.0), (DAYS[3], 1.0)],
            "600002.SH": list(zip(DAYS, [1.0, 1.1, 1.1, 1.1], strict=True)),
        },
        [("600001.SH", DAYS[0]), ("600001.SH", DAYS[2]), ("600002.SH", DAYS[1])],
    )
    full, _ = factor_action_contradictions(cfg)
    assert full.is_empty()


@pytest.mark.parametrize("scoped", [False, True])
def test_absent_actions_do_not_hide_a_factor_step(tmp_path, scoped):
    cfg = _lake(tmp_path, {"600001.SH": [(DAYS[0], 1.0), (DAYS[1], 1.1)]})
    frame, _ = factor_action_contradictions(cfg, symbols=["600001.SH"] if scoped else None)
    assert frame.to_dicts() == [
        {"symbol": "600001.SH", "ex_date": DAYS[1], "kind": "step_without_action"}
    ]


def test_flat_fund_payout_is_counted_as_convention(tmp_path):
    cfg = _lake(
        tmp_path,
        {"510300.SH": [(d, 1.0) for d in DAYS]},
        [("510300.SH", DAYS[1])],
        funds=["510300.SH"],
    )
    frame, payouts = factor_action_contradictions(cfg)
    assert frame.is_empty()
    assert payouts == 1
