"""Causal ICT native events: ported reference rules with immutable first visibility."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from my_strategy.services.czsc_ict_events import (
    ICT_RULE_VERSION, ICT_SCHEMA_HASH, ict_config, ict_evidence,
)

ROWS = [  # the bullish reference fixture from carreypro/ict-trading-model, as daily bars
    (101, 103, 99, 102),
    (102, 115, 100, 110),
    (110, 111, 98, 100),
    (100, 103, 99, 102),
    (102, 105, 100, 104),
    (104, 104.5, 99, 100),
    (100, 101, 97, 99),
    (99, 104, 98.8, 103.8),
    (103.8, 110, 103, 106),
    (106, 107, 101.5, 103),
]
CONFIG = {"swing_width": 1, "setup_lookahead": 5, "displacement_body_multiple": 1.2,
          "displacement_atr_multiple": 0.5, "minimum_rr": 2.0}
SETTINGS = {"risk_buffer_atr": .25, "event_score_decay_bars": 20}


class StubCalendar:
    provided = True

    def __init__(self, dates):
        self.dates = list(dates)

    def span(self, start, end):
        return {"dependency_start": self.dates[start], "dependency_end": self.dates[end],
                "dependency_expected_sessions": end - start + 1,
                "dependency_observed_sessions": end - start + 1,
                "dependency_calendar_complete": True}


def frame(rows=ROWS):
    n = len(rows)
    close = pd.Series([row[3] for row in rows], dtype=float)
    return pd.DataFrame({"symbol": "000001.SZ", "date": pd.bdate_range("2026-09-01", periods=n),
        "open": [row[0] for row in rows], "high": [row[1] for row in rows],
        "low": [row[2] for row in rows], "close": close, "volume": 1_000_000.,
        "amount": close * 1_000_000., "source": "sina_unadjusted_v1", "has_trade_price": 1})


def run(rows=ROWS, *, quality=None, config=None):
    bars = frame(rows)
    n = len(bars)
    metrics = pd.DataFrame({"atr": np.full(n, 1.0)})
    raw_quality = np.ones(n, bool) if quality is None else np.asarray(quality, dtype=bool)
    calendar = StubCalendar(bars.date.dt.strftime("%Y-%m-%d").tolist())
    return ict_evidence(bars, raw_quality, metrics, SETTINGS, calendar,
                        config=config or CONFIG), bars


def test_reference_bullish_sequence_confirms_with_frozen_levels():
    (facts, evaluations, daily), bars = run()
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    sweep = next(item for item in facts if item["event_type"] == "ict_sweep")
    assert sweep["side"] == "BUY" and sweep["confirmed"] is False
    assert sweep["date"] == dates[6] and sweep["ict_sweep"]["reference"]["price"] == 98.
    assert sweep["stop_price"] == 97. and sweep["support_low"] == 97.
    setup = next(item for item in facts if item["event_type"] == "ict_setup")
    assert setup["date"] == dates[9] and setup["confirmed"] is True
    assert setup["origin_event_id"] == sweep["event_id"]
    assert setup["ict_entry"]["zone"] == [101., 103.] and setup["ict_entry"]["ce"] == 102.
    assert setup["ict_mss"]["reference"]["price"] == 105.
    assert setup["ict_mss"]["break_day"] == dates[8]
    assert setup["ict_target"]["price"] == 115.
    assert setup["ict_gross_rr"] == pytest.approx(2.6)
    assert all(setup["ict_checks"].values())
    assert setup["ict_dealing_range"]["equilibrium"] == pytest.approx(103.5)
    assert setup["family"] == "ict" and setup["version"] == ICT_RULE_VERSION
    last = evaluations[-1]
    assert last["input_eligible"] is True and last["buy_valid"] is True
    assert last["direction_state"] == "BUY"
    assert last["current_state"]["latest_terminal"]["status"] == "confirmed"
    assert setup["event_id"] in last["active_buy_event_ids"]
    assert sweep["event_id"] in last["invalidated_event_ids"]  # origin resolved by later confirmation
    assert any(setup["event_id"] == event["event_id"] for event in daily[-1])


def test_wick_without_close_never_confirms_mss_or_setup():
    rows = [row for row in ROWS]
    rows[8] = (103.8, 110, 103, 104)  # close stays below the 105 MSS reference
    (facts, evaluations, _), bars = run(rows)
    # The modified close also creates a legitimate buyside (SELL) sweep at day 8;
    # neither it nor the original sellside sweep may ever produce a setup.
    assert [item["event_type"] for item in facts] == ["ict_sweep", "ict_sweep"]
    assert {item["side"] for item in facts} == {"BUY", "SELL"}
    last = evaluations[-1]["current_state"]["latest_terminal"]
    assert last["status"] == "developing"
    assert "closed_candle_mss" in last["reason"]
    sweep = next(item for item in facts if item["side"] == "BUY")
    assert sweep["event_id"] in evaluations[-1]["active_buy_event_ids"]  # still pending, decaying


def test_low_rr_setup_is_no_trade_and_pending_sweep_expires():
    rows = [row for row in ROWS]
    rows[1] = (102, 111.9, 100, 110)  # target 111.9 gives reward 9.9 over risk 5 -> R:R 1.98 < 2
    (facts, evaluations, _), bars = run(rows)
    assert "ict_setup" not in [item["event_type"] for item in facts]
    last = evaluations[-1]
    assert last["current_state"]["latest_terminal"]["status"] == "no-trade"
    sweep = facts[0]
    assert sweep["event_id"] in last["invalidated_event_ids"]
    record = [item for evaluation in evaluations for item in evaluation["invalidations_at_day"]
              if item["event_id"] == sweep["event_id"]]
    assert record and record[0]["reason"] == "ict_setup_no_trade"


def test_close_beyond_confirmed_stop_emits_directional_invalidation():
    rows = [row for row in ROWS] + [(103, 104, 95.5, 96)]  # close below the 97 structural stop
    (facts, evaluations, _), bars = run(rows)
    dates = bars.date.dt.strftime("%Y-%m-%d").tolist()
    setup = next(item for item in facts if item["event_type"] == "ict_setup")
    broken = next(item for item in facts if item["event_type"] == "ict_setup_invalidated")
    assert broken["date"] == dates[10] and broken["side"] == "SELL"
    assert broken["invalidates_event_id"] == setup["event_id"]
    assert broken["rule_name"] == "confirmed_ict_support_close_break"
    last = evaluations[-1]
    assert setup["event_id"] not in last["active_buy_event_ids"]
    assert setup["event_id"] in last["invalidated_event_ids"]


def test_first_observation_is_immutable_and_prefix_causal():
    (facts_full, evals_full, _), bars_full = run()
    cut = 7  # before the FVG/MSS/retest chain completes
    (facts_prefix, evals_prefix, _), _ = run(ROWS[:cut])
    assert facts_prefix == [item for item in facts_full if item["date"] <= evals_prefix[-1]["date"]]
    assert evals_prefix == evals_full[:cut]
    # Mutating the future cannot change any earlier evaluation or fact.
    mutated = [row for row in ROWS]
    for i in range(cut, len(mutated)):
        row = mutated[i]
        mutated[i] = (row[0] * 5, row[1] * 5, row[2] * 5, row[3] * 5)
    (facts_mut, evals_mut, _), _ = run(mutated)
    assert evals_mut[:cut] == evals_full[:cut]
    assert [item for item in facts_mut if item["date"] <= evals_full[cut - 1]["date"]] == \
           [item for item in facts_full if item["date"] <= evals_full[cut - 1]["date"]]


def test_ineligible_input_day_emits_no_facts_and_clears_active_supports():
    quality = np.ones(len(ROWS), bool)
    quality[6] = False  # sweep day has an input quality gap
    (facts, evaluations, _), _ = run(quality=quality)
    # Wyckoff convention: a quality-gap day creates no ICT evidence at all, any
    # active support is invalidated, and the pending setup ends without revival.
    assert facts == []
    assert evaluations[-1]["active_buy_event_ids"] == []
    assert evaluations[-1]["active_sell_event_ids"] == []
    assert not any(item["buy_valid"] or item["sell_valid"] for item in evaluations)


def test_no_calendar_means_no_eligibility():
    bars = frame()
    n = len(bars)
    metrics = pd.DataFrame({"atr": np.full(n, 1.0)})
    facts, evaluations, _ = ict_evidence(bars, np.ones(n, bool), metrics, SETTINGS, None, config=CONFIG)
    assert not any(item["input_eligible"] for item in evaluations)
    assert all("native_ict_dependency_calendar_incomplete" in item["reason_codes"] for item in evaluations)


def test_ict_config_validation_and_schema_hash_stable():
    assert ict_config(None) == {"swing_width": 2, "sweep_lookback": 80, "setup_lookahead": 12,
                                "atr_period": 14, "displacement_body_multiple": 1.5,
                                "displacement_atr_multiple": 1.0, "minimum_rr": 2.0}
    assert ict_config({"ict": {"minimum_rr": 3}})["minimum_rr"] == 3.
    for bad in ({"swing_width": 0}, {"sweep_lookback": 1.5}, {"minimum_rr": 0}, {"displacement_body_multiple": -1}):
        with pytest.raises(ValueError, match="ICT"):
            ict_config({"ict": bad})
    assert ICT_SCHEMA_HASH == ICT_SCHEMA_HASH and len(ICT_SCHEMA_HASH) == 64
