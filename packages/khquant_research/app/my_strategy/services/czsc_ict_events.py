"""Causal daily ICT liquidity/structure events (third native evidence family).

Rules and default thresholds are ported from the MIT-licensed reference
implementation at https://github.com/carreypro/ict-trading-model (``ict_scan.py``).
The original scanner evaluates a whole candle array with future visibility
(lookahead and forward retest scans). This module keeps only its rule
definitions and re-implements them as a strictly causal daily state machine:
every swing, FVG, sweep, displacement, MSS, retest, invalidation and no-trade
transition becomes visible on the day its last required closed bar arrives, and
a first observation is immutable. Daily bars carry no intraday session window,
so the original ``America/New_York`` session labels are not ported.

ICT/SMC labels are interpretive price-action hypotheses, not proof of
institutional activity or a guarantee of performance. The output is educational
analysis and not investment advice.

MIT License attribution: portions of the rule definitions are derived from
carreypro/ict-trading-model, Copyright (c) 2025 carreypro, released under the
MIT License (permission to use, copy, modify and distribute, with the above
copyright notice; the software is provided "as is", without warranty).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from my_strategy.core.run_context import stable_hash
from my_strategy.services.czsc_trend_support import VERIFIED_SOURCES, copy_json

ICT_RULE_VERSION = "causal_ict_events_v1"
ICT_DEFAULT_CONFIG = {
    "swing_width": 2, "sweep_lookback": 80, "setup_lookahead": 12,
    "atr_period": 14, "displacement_body_multiple": 1.5,
    "displacement_atr_multiple": 1.0, "minimum_rr": 2.0,
}
ICT_SEMANTICS = {
    "version": ICT_RULE_VERSION,
    "origin": "carreypro/ict-trading-model_MIT_rules_ported_to_strictly_causal_daily_state_machine",
    "swing": "strict_pivot_width;visible_at_pivot+width_close;confirmed_set_only",
    "fvg": "strict_three_bar_gap_no_wick_overlap;visible_at_third_bar_close;ce_midpoint",
    "sweep": "penetration_and_close_back_within_lookback_of_latest_confirmed_swing;visible_at_sweep_close",
    "displacement": "middle_body>=median_prior20_nonzero_body*body_multiple;"
                    "middle_span>=ATR_at_middle*atr_multiple;direction_aligned",
    "mss": "first_close_beyond_latest_confirmed_opposing_swing_within_setup_lookahead",
    "location": "entry_ce_vs_dealing_range_equilibrium;range_finalizes_at_mss_or_window_expiry",
    "target": "nearest_confirmed_opposing_swing_beyond_range_extreme;gross_rr_excludes_costs",
    "invalidation": "wick_beyond_sweep_extreme_from_start_check;"
                    "post_confirmation_close_beyond_extreme_emits_directional_fact",
    "no_trade": "target_before_retrace;wrong_equilibrium_side;gross_rr_below_minimum",
    "pending_expiry": "confirmation_window_closed_without_fvg_or_mss;or_no_trade_terminal",
    "sessions": "daily_bars_no_intraday_session_windows",
    "interpretation": "ict_labels_are_price_action_hypotheses_not_institutional_proof_not_profit_probability",
    "risk_notice": "educational_analysis_not_investment_advice",
}
ICT_SCHEMA_HASH = stable_hash(ICT_SEMANTICS)
_ICT_BUY = "BUY"
_ICT_SELL = "SELL"


def ict_config(config=None):
    value = dict(ICT_DEFAULT_CONFIG)
    incoming = (config or {}).get("ict", config) or {}
    value.update({key: incoming[key] for key in value if key in incoming})
    for key in ("swing_width", "sweep_lookback", "setup_lookahead", "atr_period"):
        if int(value[key]) != value[key] or value[key] < 1:
            raise ValueError("invalid ICT event window: " + key)
        value[key] = int(value[key])
    for key in ("displacement_body_multiple", "displacement_atr_multiple", "minimum_rr"):
        if not np.isfinite(value[key]) or value[key] <= 0:
            raise ValueError("invalid ICT event threshold: " + key)
        value[key] = float(value[key])
    return value


def _day(value):
    return pd.Timestamp(value).date().isoformat()


def _number(value):
    try:
        result = float(value)
        return result if np.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _fact(symbol, date, side, event_type, pattern, anchor, stop, boundary,
          atr, settings, *, candidate, confirmed, eligible, reference, extra=None):
    """Same shape as the structure/wyckoff native facts so every consumer is uniform."""
    available = date + "T15:00:00+08:00"
    pattern_id = "ict:native:" + stable_hash({"symbol": symbol, **pattern})
    risk = None if stop is None or atr is None else stop + (-1 if side == _ICT_BUY else 1) * settings["risk_buffer_atr"] * atr
    return {"symbol": symbol, "date": date, "source": "ict", "side": side, "event_type": event_type,
            "event": event_type, "event_id": pattern_id + ":confirmed:" + available,
            "original_event_id": pattern_id + ":confirmed:" + available,
            "pattern_id": pattern_id, "first_confirmed_at": available if confirmed else None,
            "first_available_at": available, "origin_observed_at": available,
            "observed_at": available, "available_at": available, "confirmed_at": available if confirmed else None,
            "anchor_at": anchor, "time": date, "support_low": boundary if side == _ICT_BUY else None,
            "resistance_high": boundary if side == _ICT_SELL else None, "invalidation_price": boundary,
            "stop_price": stop, "risk_stop_price": risk, "risk_atr_at_origin": atr,
            "confirmed": confirmed, "is_confirmed": confirmed, "candidate_eligible": candidate and eligible,
            "input_eligible": eligible, "evidence_visible": eligible,
            "candidate_direction_valid": candidate and eligible,
            "reference_price": reference, "reference_price_date": date,
            "reference_price_source": "signal_day_close", "dependencies": [],
            "state": "confirmed" if confirmed else "pending", **(extra or {})}


def _invalidate(active, invalidated, ids, available, reason):
    result = []
    for identity in ids:
        item = active.pop(identity)
        record = {"event_id": identity, "pattern_id": item["pattern_id"], "side": item["side"],
                  "invalidated_at": available, "reason": reason, "invalidation_price": item.get("invalidation_price")}
        invalidated[identity] = record
        result.append(record)
    return result


def _check_cancel(cancel_check):
    if cancel_check is not None and hasattr(cancel_check, 'check'):
        cancel_check.check()
        return
    if cancel_check and (cancel_check.is_set() if hasattr(cancel_check, 'is_set') else cancel_check()):
        raise InterruptedError('原生ICT事件计算已取消')


ICT_STATE_FORMAT = "ict_evidence_state_v1"


def ict_evidence(bars, raw_quality, metrics, settings, calendar=None, *, config=None,
                 cancel_check=None, compact=False, resume_state=None, state_sink=None):
    """Run the causal ICT state machine over a complete observed single-stock prefix.

    ``bars`` is the validated raw frame; ``raw_quality`` the same per-day full
    eligibility used by the structure family; ``metrics`` carries the prior-ATR
    column for risk buffers; ``calendar`` is the native verified-calendar span
    checker (no calendar means no eligibility, never an invented schedule).
    Returns ``(facts, evaluations, daily_events)`` aligned with the other
    native families.

    With ``resume_state`` (captured by an earlier ``state_sink`` call on a
    strict prefix of ``bars``) the state machines — active/invalidated
    supports, origin ages, open setups, confirmed swings — are restored and
    only the appended days are advanced; the returned triple then covers just
    those delta days. Format, schema, config, self-SHA or strict-append
    violations raise ValueError and callers fall back to a full replay, so a
    wrong continuation is never served.
    """
    cfg = ict_config(config)
    width, lookback, lookahead, atr_period = (cfg[key] for key in
        ("swing_width", "sweep_lookback", "setup_lookahead", "atr_period"))
    body_multiple, atr_multiple, minimum_rr = (cfg[key] for key in
        ("displacement_body_multiple", "displacement_atr_multiple", "minimum_rr"))
    n = len(bars)
    dates = [_day(value) for value in bars.date]
    symbol = str(bars.iloc[0].symbol)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    open_ = bars["open"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    previous = np.r_[np.nan, close[:-1]]
    with np.errstate(invalid="ignore"):
        tr = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    tr[0] = np.nan  # the reference ATR never includes the first bar's synthetic range
    atr = pd.Series(tr).rolling(atr_period, min_periods=1).mean().to_numpy()
    body = np.abs(close - open_)

    # Input-quality prefix mirroring the structure family's source/quantity checks.
    source_bad = ~bars["source"].fillna("").astype(str).isin(VERIFIED_SOURCES).to_numpy() if "source" in bars else np.ones(n, bool)
    trade_bad = ~pd.to_numeric(bars["has_trade_price"], errors="coerce").eq(1).to_numpy() if "has_trade_price" in bars else np.ones(n, bool)
    quantity = bars[["volume", "amount"]].to_numpy(dtype=float)
    bad = source_bad | trade_bad | ~np.isfinite(quantity).all(axis=1) | (quantity < 0).any(axis=1) | ((quantity[:, 0] == 0) != (quantity[:, 1] == 0))
    bad_prefix = np.r_[0, np.cumsum(bad)]
    raw_quality = np.asarray(raw_quality, dtype=bool)

    def eligible_at(index, dependency_start):
        if not raw_quality[index]:
            return False
        if bad_prefix[index + 1] != bad_prefix[dependency_start]:
            return False
        if calendar is None:
            return False
        return bool(calendar.span(dependency_start, index)["dependency_calendar_complete"])

    def span_of(start, index):
        if calendar is None:
            return {"dependency_start": dates[start], "dependency_end": dates[index],
                    "dependency_expected_sessions": None, "dependency_observed_sessions": index - start + 1,
                    "dependency_calendar_complete": False}
        return calendar.span(start, index)

    facts: list[dict] = []
    evaluations: list[dict] = []
    daily_events: list[list] = []
    if resume_state is not None:
        if (not isinstance(resume_state, dict) or resume_state.get("format") != ICT_STATE_FORMAT
                or resume_state.get("schema_hash") != ICT_SCHEMA_HASH
                or resume_state.get("config_hash") != stable_hash(cfg)
                or resume_state.get("sha256") != stable_hash({key: value for key, value in resume_state.items() if key != "sha256"})):
            raise ValueError("ICT continuation state SHA or schema mismatch")
        start_index = int(resume_state.get("rows", 0))
        if not 0 < start_index < n:
            raise ValueError("ICT continuation requires a nonempty strict observed prefix")
        active = {str(key): copy_json(value) for key, value in resume_state["active"].items()}
        invalidated = {str(key): copy_json(value) for key, value in resume_state["invalidated"].items()}
        origin_index = {str(key): int(value) for key, value in resume_state["origin_index"].items()}
        setups = copy_json(resume_state["setups"])
        swings = copy_json(resume_state["swings"])
        last_low = copy_json(resume_state["last_low"])
        last_high = copy_json(resume_state["last_high"])
    else:
        start_index = 0
        active: dict[str, dict] = {}        # pending sweeps and confirmed setups acting as supports
        invalidated: dict[str, dict] = {}
        origin_index: dict[str, int] = {}
        setups: list[dict] = []             # open setups, in sweep order
        swings: list[dict] = []             # confirmed swings in pivot order
        last_low = None                     # most recent confirmed swing low (O(1) sweep reference)
        last_high = None
    weights = {"ict_sweep": .5, "ict_setup": 1.}

    for i in range(start_index, n):
        if i % 32 == 0:
            _check_cancel(cancel_check)
        detail = not compact or i == n - 1
        date = dates[i]
        available = date + "T15:00:00+08:00"
        new: list[dict] = []

        # 1. Confirm the swing pivot whose right side closes today.
        pivot = i - width
        if pivot >= width:
            left = slice(pivot - width, pivot)
            right = slice(pivot + 1, i + 1)
            if high[pivot] > np.max(np.r_[high[left], high[right]]):
                swings.append({"kind": "high", "index": pivot, "price": float(high[pivot]), "time": dates[pivot]})
                last_high = swings[-1]
            if low[pivot] < np.min(np.r_[low[left], low[right]]):
                swings.append({"kind": "low", "index": pivot, "price": float(low[pivot]), "time": dates[pivot]})
                last_low = swings[-1]

        # 2. Confirm today's strict three-bar FVG and freeze its displacement metrics.
        fvg_today = None
        if i >= 2:
            first, middle, third = i - 2, i - 1, i
            if high[first] < low[third]:
                zone, direction = [float(high[first]), float(low[third])], "bullish"
            elif low[first] > high[third]:
                zone, direction = [float(high[third]), float(low[first])], "bearish"
            else:
                zone, direction = None, None
            if zone is not None:
                window = body[max(0, middle - 20):middle]
                nonzero = window[window > 0]
                median_body = float(np.median(nonzero)) if len(nonzero) else 0.
                atr_middle = _number(atr[middle])
                direction_ok = close[middle] > open_[middle] if direction == "bullish" else close[middle] < open_[middle]
                body_ok = median_body > 0 and body[middle] >= median_body * body_multiple
                atr_ok = atr_middle is not None and (high[middle] - low[middle]) >= atr_middle * atr_multiple
                fvg_today = {"direction": direction, "start_index": first, "middle_index": middle,
                             "end_index": third, "time": dates[middle], "zone": zone,
                             "ce": (zone[0] + zone[1]) / 2., "size": zone[1] - zone[0],
                             "displacement": bool(body_ok and atr_ok and direction_ok),
                             "body_multiple": None if median_body <= 0 else float(body[middle] / median_body),
                             "range_atr": None if not atr_middle else float((high[middle] - low[middle]) / atr_middle)}

        # 3. Detect sweeps at today's close against the latest confirmed swings.
        if last_low is not None and 0 < i - last_low["index"] <= lookback:
            reference = last_low
            dependency_start = max(0, min(reference["index"] - width, i - lookback))
            eligible = eligible_at(i, dependency_start)
            if eligible and low[i] < reference["price"] and close[i] > reference["price"]:
                pattern = {"version": ICT_RULE_VERSION, "event_type": "ict_sweep", "direction": "long",
                           "session": "daily", "reference": dict(reference), "sweep_index": i,
                           "sweep_day": date, "extreme": float(low[i])}
                fact = _fact(symbol, date, _ICT_BUY, "ict_sweep", pattern, date, float(low[i]), float(low[i]),
                             _number(metrics.iloc[i].atr), settings, candidate=True, confirmed=False,
                             eligible=eligible, reference=float(close[i]),
                             extra={"version": ICT_RULE_VERSION, "family": "ict", "rule_name": "sellside_liquidity_sweep",
                                    "ict_direction": "long", "ict_sweep": {"kind": "sellside_sweep", "extreme": float(low[i]),
                                        "reference": dict(reference), "session": "daily"},
                                    "dependency_start": dates[dependency_start], **span_of(dependency_start, i),
                                    "reason_codes": [] if eligible else ["native_ict_input_ineligible"]})
                facts.append(fact); new.append(fact)
                active[fact["event_id"]] = fact
                origin_index[fact["event_id"]] = i
                mss_reference = last_high if last_high is not None and last_high["index"] < i else None
                setups.append({"sweep_index": i, "direction": "long", "stop": float(low[i]),
                               "reference": dict(reference), "mss_reference": mss_reference,
                               "pending_id": fact["event_id"], "fvg": None, "mss_index": None,
                               "range_final": None, "target": None, "rr": None, "terminal": None,
                               "dependency_start": dependency_start})
        if last_high is not None and 0 < i - last_high["index"] <= lookback:
            reference = last_high
            dependency_start = max(0, min(reference["index"] - width, i - lookback))
            eligible = eligible_at(i, dependency_start)
            if eligible and high[i] > reference["price"] and close[i] < reference["price"]:
                pattern = {"version": ICT_RULE_VERSION, "event_type": "ict_sweep", "direction": "short",
                           "session": "daily", "reference": dict(reference), "sweep_index": i,
                           "sweep_day": date, "extreme": float(high[i])}
                fact = _fact(symbol, date, _ICT_SELL, "ict_sweep", pattern, date, float(high[i]), float(high[i]),
                             _number(metrics.iloc[i].atr), settings, candidate=True, confirmed=False,
                             eligible=eligible, reference=float(close[i]),
                             extra={"version": ICT_RULE_VERSION, "family": "ict", "rule_name": "buyside_liquidity_sweep",
                                    "ict_direction": "short", "ict_sweep": {"kind": "buyside_sweep", "extreme": float(high[i]),
                                        "reference": dict(reference), "session": "daily"},
                                    "dependency_start": dates[dependency_start], **span_of(dependency_start, i),
                                    "reason_codes": [] if eligible else ["native_ict_input_ineligible"]})
                facts.append(fact); new.append(fact)
                active[fact["event_id"]] = fact
                origin_index[fact["event_id"]] = i
                mss_reference = last_low if last_low is not None and last_low["index"] < i else None
                setups.append({"sweep_index": i, "direction": "short", "stop": float(high[i]),
                               "reference": dict(reference), "mss_reference": mss_reference,
                               "pending_id": fact["event_id"], "fvg": None, "mss_index": None,
                               "range_final": None, "target": None, "rr": None, "terminal": None,
                               "dependency_start": dependency_start})

        # 4. Advance open setups: FVG match, MSS, range finalization, terminal scans.
        day_invalidations: list[dict] = []
        for setup in setups:
            if setup["terminal"] is not None or not raw_quality[i]:
                continue
            sweep_index = setup["sweep_index"]
            if i <= sweep_index:
                continue
            direction = setup["direction"]
            long = direction == "long"
            desired = "bullish" if long else "bearish"

            # FVG: the first displaced matching gap confirmed inside the window is frozen.
            if (setup["fvg"] is None and fvg_today is not None and fvg_today["displacement"]
                    and fvg_today["direction"] == desired and fvg_today["middle_index"] >= sweep_index
                    and fvg_today["end_index"] <= sweep_index + lookahead):
                setup["fvg"] = dict(fvg_today)
                setup["dependency_start"] = min(setup["dependency_start"], max(0, fvg_today["middle_index"] - 20))

            # MSS: first close beyond the latest confirmed opposing swing inside the window.
            mss_reference = setup["mss_reference"]
            if setup["mss_index"] is None and mss_reference is not None and i <= sweep_index + lookahead:
                crossed = close[i] > mss_reference["price"] if long else close[i] < mss_reference["price"]
                if crossed:
                    setup["mss_index"] = i

            # Range finalizes once the MSS is seen or its window has closed.
            window_closed = i >= sweep_index + lookahead
            if setup["range_final"] is None and (setup["mss_index"] is not None or window_closed):
                range_end = setup["mss_index"] if setup["mss_index"] is not None else (
                    setup["fvg"]["end_index"] if setup["fvg"] else sweep_index)
                segment = slice(sweep_index, range_end + 1)
                range_low, range_high = float(np.min(low[segment])), float(np.max(high[segment]))
                equilibrium = (range_low + range_high) / 2.
                if long:
                    ote = [range_high - 0.79 * (range_high - range_low), range_high - 0.62 * (range_high - range_low)]
                else:
                    ote = [range_low + 0.62 * (range_high - range_low), range_low + 0.79 * (range_high - range_low)]
                threshold = range_high if long else range_low
                prior = [s for s in swings if s["index"] < sweep_index]
                if long:
                    choices = [s for s in prior if s["kind"] == "high" and s["price"] > threshold]
                    target = min(choices, key=lambda item: item["price"]) if choices else None
                else:
                    choices = [s for s in prior if s["kind"] == "low" and s["price"] < threshold]
                    target = max(choices, key=lambda item: item["price"]) if choices else None
                fvg = setup["fvg"]
                entry = fvg["ce"] if fvg else None
                location_ok = None if entry is None else (entry <= equilibrium if long else entry >= equilibrium)
                rr = None
                if entry is not None and target is not None:
                    risk = abs(entry - setup["stop"])
                    reward = target["price"] - entry if long else entry - target["price"]
                    if risk > 0:
                        rr = reward / risk
                setup.update(range_final={"low": range_low, "high": range_high, "equilibrium": equilibrium,
                             "ote_zone": ote, "range_end": range_end,
                             "fvg_ote_overlap": None if fvg is None else
                             max(fvg["zone"][0], ote[0]) <= min(fvg["zone"][1], ote[1])},
                             target=target, rr=rr)
                if fvg is not None:
                    if location_ok is False:
                        setup["terminal"] = "no-trade"
                        setup["terminal_reason"] = "entry FVG is on the wrong side of equilibrium"
                    elif rr is not None and rr < minimum_rr:
                        setup["terminal"] = "no-trade"
                        setup["terminal_reason"] = f"candidate R:R {rr:.2f} is below {minimum_rr:.2f}"
                    if setup["terminal"] == "no-trade" and setup["pending_id"] in active:
                        day_invalidations.extend(_invalidate(active, invalidated, [setup["pending_id"]],
                                                             available, "ict_setup_no_trade"))

            # Retrace / invalidation / target-first scan, exactly as the reference contract.
            fvg = setup["fvg"]
            if setup["terminal"] is None and fvg is not None:
                start_check = max(sweep_index + 1,
                                  (setup["mss_index"] + 1) if setup["mss_index"] is not None else sweep_index + 1,
                                  fvg["end_index"] + 1)
                if i >= start_check:
                    stop = setup["stop"]
                    broken = low[i] < stop if long else high[i] > stop
                    target = setup["target"]
                    target_hit = bool(target) and (high[i] >= target["price"] if long else low[i] <= target["price"])
                    touched = low[i] <= fvg["zone"][1] and high[i] >= fvg["zone"][0]
                    if broken:
                        setup["terminal"] = "invalidated"
                        setup["terminal_reason"] = "structural sweep extreme was crossed before a valid retest"
                        if setup["pending_id"] in active:
                            day_invalidations.extend(_invalidate(active, invalidated, [setup["pending_id"]],
                                                                 available, "ict_structural_stop_crossed"))
                    elif target_hit and not touched:
                        setup["terminal"] = "no-trade"
                        setup["terminal_reason"] = "candidate target was reached before the retrace; entry is late"
                        if setup["pending_id"] in active:
                            day_invalidations.extend(_invalidate(active, invalidated, [setup["pending_id"]],
                                                                 available, "ict_setup_no_trade"))
                    elif touched:
                        checks = {"meaningful_sweep": True, "displacement_and_strict_fvg": True,
                                  "closed_candle_mss": setup["mss_index"] is not None,
                                  "correct_premium_discount_side": (setup["range_final"] or {}).get("equilibrium") is not None
                                      and (fvg["ce"] <= setup["range_final"]["equilibrium"] if long
                                           else fvg["ce"] >= setup["range_final"]["equilibrium"]),
                                  "retrace_observed": True,
                                  "minimum_rr": setup["rr"] is not None and setup["rr"] >= minimum_rr}
                        if all(checks.values()):
                            setup["terminal"] = "confirmed"
                            setup["terminal_reason"] = "all mechanical ICT setup gates passed on closed candles"
                            dependency_start = setup["dependency_start"]
                            eligible = eligible_at(i, dependency_start)
                            pattern = {"version": ICT_RULE_VERSION, "event_type": "ict_setup",
                                       "direction": direction, "origin_sweep_day": dates[sweep_index],
                                       "fvg": {"zone": fvg["zone"], "ce": fvg["ce"], "end_day": dates[fvg["end_index"]]},
                                       "mss": {"reference": mss_reference, "break_day": dates[setup["mss_index"]],
                                               "break_close": float(close[setup["mss_index"]])},
                                       "range": {"low": setup["range_final"]["low"], "high": setup["range_final"]["high"]},
                                       "entry_ce": fvg["ce"]}
                            origin_id = setup["pending_id"]
                            fact = _fact(symbol, date, _ICT_BUY if long else _ICT_SELL, "ict_setup", pattern,
                                         dates[sweep_index], setup["stop"], setup["stop"],
                                         _number(metrics.iloc[i].atr), settings, candidate=True, confirmed=True,
                                         eligible=eligible, reference=float(close[i]),
                                         extra={"version": ICT_RULE_VERSION, "family": "ict",
                                                "rule_name": "strict_ict_setup_confirmed",
                                                "ict_direction": direction, "ict_mechanical_status": "confirmed",
                                                "ict_checks": checks, "ict_fvg": copy_json(fvg),
                                                "ict_mss": {"reference": mss_reference, "break_day": dates[setup["mss_index"]],
                                                            "break_close": float(close[setup["mss_index"]])},
                                                "ict_dealing_range": copy_json(setup["range_final"]),
                                                "ict_entry": {"zone": list(fvg["zone"]), "ce": fvg["ce"], "retest_day": date},
                                                "ict_target": copy_json(target),
                                                "ict_gross_rr": setup["rr"], "ict_minimum_rr": minimum_rr,
                                                "origin_event_id": origin_id,
                                                "dependency_start": dates[dependency_start], **span_of(dependency_start, i),
                                                "reason_codes": [] if eligible else ["native_ict_input_ineligible"]})
                            facts.append(fact); new.append(fact)
                            origin_index[fact["event_id"]] = i
                            if origin_id in active:
                                day_invalidations.extend(_invalidate(active, invalidated, [origin_id],
                                                                     available, "later_event_confirmed"))
                            if (close[i] >= setup["stop"]) if long else (close[i] <= setup["stop"]):
                                active[fact["event_id"]] = fact
                        else:
                            setup["terminal"] = "developing"
                            setup["terminal_reason"] = "waiting for: " + ", ".join(
                                name for name, value in checks.items() if value is not True)

            # Confirmation is impossible once the displacement/MSS window closed incomplete.
            if (setup["terminal"] is None and i >= sweep_index + lookahead
                    and (setup["fvg"] is None or setup["mss_index"] is None)):
                setup["terminal"] = "developing"
                setup["terminal_reason"] = "confirmation window closed without a displaced FVG or MSS"
                if setup["pending_id"] in active:
                    day_invalidations.extend(_invalidate(active, invalidated, [setup["pending_id"]],
                                                         available, "ict_confirmation_window_expired"))

        # 5. Close-based invalidation of confirmed ICT supports emits a directional fact.
        broken_confirmed = [key for key, value in active.items()
                            if value["event_type"] == "ict_setup" and value.get("invalidation_price") is not None
                            and (close[i] < value["invalidation_price"] if value["side"] == _ICT_BUY
                                 else close[i] > value["invalidation_price"])]
        for key in broken_confirmed:
            item = active[key]
            side = _ICT_SELL if item["side"] == _ICT_BUY else _ICT_BUY
            stop = item["invalidation_price"]
            pattern = {"version": ICT_RULE_VERSION, "invalidated_event_id": key}
            fact = _fact(symbol, date, side, "ict_setup_invalidated", pattern, date, stop, stop,
                         _number(metrics.iloc[i].atr), settings, candidate=True, confirmed=True,
                         eligible=bool(raw_quality[i]), reference=float(close[i]),
                         extra={"version": ICT_RULE_VERSION, "family": "ict",
                                "rule_name": "confirmed_ict_support_close_break",
                                "ict_direction": "short" if side == _ICT_SELL else "long",
                                "invalidates_event_id": key,
                                "reason_codes": ["close_broke_frozen_ict_boundary"]})
            facts.append(fact); new.append(fact)
        day_invalidations.extend(_invalidate(active, invalidated, broken_confirmed,
                                             available, "close_broke_frozen_ict_boundary"))
        if not raw_quality[i]:
            day_invalidations.extend(_invalidate(active, invalidated, list(active), available, "input_quality_gap"))
            for setup in setups:
                if setup["terminal"] is None:
                    setup["terminal"] = "input_quality_gap"
                    setup["terminal_reason"] = "input quality gap ended the pending setup"

        # 6. Daily snapshot with wyckoff-parity strength decay.
        supports = []
        for item in active.values():
            snapshot = copy_json(item) if detail else dict(item)
            age = i - origin_index[item["event_id"]]
            strength = weights.get(item["event_type"], .5) * max(0., 1. - age / settings["event_score_decay_bars"])
            snapshot.update(snapshot_at=available, current_available_at=available, age_bars=age,
                            event_strength=strength, event_expired=strength == 0,
                            lifecycle_state="strength_expired" if strength == 0 else
                            "valid_confirmed" if item["confirmed"] else "pending")
            supports.append(snapshot)
        buys = [item for item in supports if item["side"] == _ICT_BUY]
        sells = [item for item in supports if item["side"] == _ICT_SELL]
        buy_strength = max([item["event_strength"] for item in buys], default=0.)
        sell_strength = max([item["event_strength"] for item in sells], default=0.)
        open_setups = [s for s in setups if s["terminal"] is None]
        latest = next(iter(reversed([s for s in setups if s["terminal"] is not None])), None)
        dependency_start = min([i - lookback - width - 21] +
                               [s["dependency_start"] for s in open_setups] +
                               [origin_index[key] for key in active])
        dependency_start = max(0, dependency_start)
        eligible = eligible_at(i, dependency_start)
        span = span_of(dependency_start, i)
        state = {"close": float(close[i]), "buy_score": float(buy_strength) if eligible else None,
                 "sell_score": float(sell_strength) if eligible else None,
                 "event_demand_support": float(buy_strength) if eligible else None,
                 "event_supply_support": float(sell_strength) if eligible else None,
                 "snapshot_at": available,
                 "support_valid": any(item["event_strength"] > 0 for item in buys),
                 "resistance_valid": any(item["event_strength"] > 0 for item in sells),
                 "invalidated": bool(day_invalidations),
                 "support_floor": max([item["support_low"] for item in buys if item["support_low"] is not None], default=None),
                 "resistance_ceiling": min([item["resistance_high"] for item in sells if item["resistance_high"] is not None], default=None),
                 "origin_observed_at": max([item["origin_observed_at"] for item in supports], default=None),
                 "setups_open": len(open_setups),
                 "setups_confirmed": sum(1 for s in setups if s["terminal"] == "confirmed"),
                 "latest_terminal": None if latest is None else {
                     "status": latest["terminal"], "direction": latest["direction"],
                     "reason": latest["terminal_reason"], "gross_rr": latest["rr"],
                     "sweep_day": dates[latest["sweep_index"]],
                     "dealing_range": copy_json(latest["range_final"]),
                     "target": copy_json(latest["target"])}}
        evaluations.append({"source_id": "ict", "family": "ict", "version": ICT_RULE_VERSION,
            "date": date, "available_at": available, "snapshot_at": available, "input_eligible": eligible,
            "buy_valid": bool(eligible and buy_strength > 0), "sell_valid": bool(eligible and sell_strength > 0),
            "direction_state": "CONFLICT" if eligible and buy_strength > 0 and sell_strength > 0 else
                "BUY" if eligible and buy_strength > 0 else "SELL" if eligible and sell_strength > 0 else "ABSTAIN",
            "current_state": state, "support_facts": supports if detail else [],
            "active_buy_event_ids": [item["event_id"] for item in buys],
            "active_sell_event_ids": [item["event_id"] for item in sells],
            "invalidated_event_ids": list(invalidated) if detail else [],
            "invalidations_at_day": day_invalidations,
            "reason_codes": [] if eligible else ["native_ict_input_ineligible",
                *([] if span["dependency_calendar_complete"] else ["native_ict_dependency_calendar_incomplete"])]})
        daily_events.append(copy_json(new) if detail else [])
    _check_cancel(cancel_check)
    if state_sink is not None:
        state_sink.update(format=ICT_STATE_FORMAT, rows=n, schema_hash=ICT_SCHEMA_HASH,
                          config_hash=stable_hash(cfg),
                          active=copy_json(active), invalidated=copy_json(invalidated),
                          origin_index=copy_json(origin_index), setups=copy_json(setups),
                          swings=copy_json(swings),
                          last_low=copy_json(last_low), last_high=copy_json(last_high))
        state_sink["sha256"] = stable_hash(state_sink)
    return facts, evaluations, daily_events
