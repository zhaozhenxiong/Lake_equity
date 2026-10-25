import pandas as pd
import pytest

from lake_equity.learning.portfolio import _sellable_shares, backtest_scores


DATES = pd.bdate_range("2025-03-03", periods=8).strftime("%Y-%m-%d").tolist()


def prices(symbols=("600000.SH", "000001.SZ")):
    return pd.DataFrame([
        dict(date=day, symbol=symbol, open=10., high=10.3, low=9.8,
             close=10., volume=1_000_000., amount=10_000_000.)
        for day in DATES for symbol in symbols
    ])


def scores(days=None):
    return pd.DataFrame([
        dict(date=day, symbol=symbol, score=score, model_name="ridge", fold="fold-1",
             train_end="2025-02-28", available_at=day + "T15:00:00+08:00")
        for day in (days or [DATES[0]])
        for symbol, score in (("600000.SH", 2.), ("000001.SZ", 1.))
    ])


def run(bars=None, predictions=None, **config):
    return backtest_scores(prices() if bars is None else bars,
                           scores() if predictions is None else predictions, DATES,
                           dict(initial_cash=100_000., top_k=1, **config), run_id="unit-oos")


def test_next_open_uses_real_broker_cash_ledger_and_preserves_inventory():
    bars = prices()
    bars.loc[(bars.date == DATES[1]) & (bars.symbol == "600000.SH"), "open"] = 10.2
    result = run(bars)
    trade = result["trades"][0]
    assert trade["date"] == DATES[1]
    assert trade["signal_date"] == DATES[0]
    assert trade["price"] == pytest.approx(10.2 * 1.0005)
    assert trade["shares"] % 100 == 0
    assert trade["cash_after"] == pytest.approx(100_000. + trade["cash_flow"])
    assert result["daily"][0]["cash"] == 100_000.
    assert result["terminal_state"]["positions"]["600000.SH"] == trade["shares"]
    assert result["terminal_state"]["forced_liquidation"] is False
    assert result["strict_history_eligible"] is False
    assert result["paper_allocation_eligible"] is False
    assert "position_simulation.py" in result["implementation_sha256"]


def test_training_period_replay_is_explicit_and_keeps_true_cutoff():
    predictions = scores()
    predictions['train_end'] = DATES[-1]
    with pytest.raises(ValueError, match='train_end'):
        backtest_scores(prices(), predictions, DATES, run_id='forbidden')
    replay = backtest_scores(prices(), predictions, DATES, run_id='diagnostic', research_replay=True)
    assert replay['evaluation_mode'] == 'in_sample_research_replay'
    assert replay['future_training_information_rows'] == len(predictions)
    assert replay['oos_ranking_eligible'] is False
    assert predictions.train_end.eq(DATES[-1]).all()


def test_cached_time_parsing_preserves_complete_account_and_rejections(monkeypatch):
    from lake_equity.learning import portfolio
    cached = portfolio._stamp
    portfolio._cached_stamp.cache_clear()
    predictions = scores(DATES[:5])
    monkeypatch.setattr(portfolio, '_stamp', portfolio._parse_stamp)
    original = run(predictions=predictions, rebalance_sessions=1)
    monkeypatch.setattr(portfolio, '_stamp', cached)
    replay = run(predictions=predictions, rebalance_sessions=1)
    assert original == replay
    assert portfolio._cached_stamp.cache_info().currsize <= 20000
    invalid = predictions.copy()
    invalid.loc[0, 'available_at'] = DATES[0] + 'T14:59:59+08:00'
    with pytest.raises(ValueError, match='decision close'):
        run(predictions=invalid)
    invalid.loc[0, 'available_at'] = DATES[1] + 'T09:30:00+08:00'
    with pytest.raises(ValueError, match='next executable open'):
        run(predictions=invalid)
    assert cached('2025-03-03T07:00:00Z') == cached('2025-03-03T15:00:00+08:00')
    size = portfolio._cached_stamp.cache_info().currsize
    cached('now')
    assert portfolio._cached_stamp.cache_info().currsize == size


def test_future_prices_cannot_change_past_fills_or_equity():
    original = prices()
    changed = original.copy()
    changed.loc[changed.date >= DATES[5], ["open", "high", "low", "close"]] *= 1.7
    left, right = run(original), run(changed)
    assert left["trades"] == right["trades"]
    assert left["daily"][:5] == right["daily"][:5]
    assert left["daily"][-1]["equity"] != right["daily"][-1]["equity"]


def test_missing_next_open_expires_without_later_fill():
    bars = prices()
    bars = bars.loc[~((bars.date == DATES[1]) & (bars.symbol == "600000.SH"))]
    result = run(bars)
    assert result["trades"] == []
    assert result["rejections"][0]["reason"] == "missing_open"
    assert result["rejections"][0]["date"] == DATES[1]
    assert result["rejections"][0]["requested_shares"] is None
    assert all(row["equity"] == 100_000. for row in result["daily"])
    assert result["metrics"]["performance_status"] == "unavailable"
    assert result["metrics"]["performance_reason"] == "no_executable_next_open"
    assert result["metrics"]["missing_open_rejections"] == 1


def test_cost_stress_uses_existing_dated_fee_helper():
    low = run(commission=.001, min_commission=0., slippage=.001, transfer_fee=0.)
    high = run(commission=.001, min_commission=0., slippage=.001, transfer_fee=0., cost_stress=2.)
    a, b = low["trades"][0], high["trades"][0]
    assert a["price"] == pytest.approx(10.01)
    assert a["fee"] == pytest.approx(a["shares"] * a["price"] * .001)
    assert b["price"] == pytest.approx(10.02)
    assert b["fee"] == pytest.approx(b["shares"] * b["price"] * .002)
    assert high["metrics"]["ending_equity"] < low["metrics"]["ending_equity"]


def test_shared_cash_sells_before_buys_and_lots_respect_t_plus_one():
    predictions = scores(DATES[:2])
    predictions.loc[(predictions.date == DATES[1]) & (predictions.symbol == "000001.SZ"), "score"] = 3.
    result = run(predictions=predictions, rebalance_sessions=1, max_stock_weight=.9)
    trades = result["trades"]
    on_second_open = [row for row in trades if row["date"] == DATES[2]]
    assert [row["action"] for row in on_second_open] == ["SELL", "BUY"]
    assert trades[0]["date"] < on_second_open[0]["date"]
    assert result["terminal_state"]["positions"].keys() == {"000001.SZ"}
    assert result["terminal_state"]["cash"] == pytest.approx(100_000. + sum(row["cash_flow"] for row in trades))
    for daily in result["daily"]:
        assert daily["cash"] >= 0
        assert daily["equity"] == pytest.approx(daily["cash"] + sum(daily["positions"].values()) * 10.)
    lots = [dict(date=DATES[1], shares=100), dict(date=DATES[2], shares=200)]
    assert _sellable_shares(lots, DATES[2]) == 100


def test_prior_session_volume_caps_fill_without_using_execution_day_volume():
    bars = prices()
    bars.loc[(bars.date == DATES[0]) & (bars.symbol == "600000.SH"), "volume"] = 10_000.
    baseline = run(bars)
    bars.loc[(bars.date == DATES[1]) & (bars.symbol == "600000.SH"), "volume"] = 0.
    altered = run(bars)
    assert baseline["trades"] == altered["trades"]
    assert baseline["trades"][0]["shares"] == 100
    assert baseline["orders"][0]["status"] == "partial"


def test_missing_mark_carries_only_last_known_close_and_records_staleness():
    bars = prices()
    bars.loc[(bars.date == DATES[2]) & (bars.symbol == "600000.SH"), "close"] = float("nan")
    result = run(bars)
    assert result["daily"][2]["stale_symbols"] == ["600000.SH"]
    assert result["daily"][2]["equity"] == result["daily"][1]["equity"]
    assert result["metrics"]["stale_valuation_days"] == 1


@pytest.mark.parametrize("change,error", [
    ({"train_end": DATES[0]}, "In-sample"),
    ({"train_end": "unknown"}, None),
    ({"available_at": DATES[1] + "T09:30:00+08:00"}, "unavailable before"),
    ({"available_at": DATES[0] + "T14:00:00+08:00"}, "before the decision close"),
])
def test_invalid_prediction_provenance_is_rejected(change, error):
    prediction = scores()
    for key, value in change.items():
        prediction.loc[0, key] = value
    with pytest.raises(ValueError, match=error):
        run(predictions=prediction)


def test_foreign_model_and_unsupported_market_are_not_silently_used():
    prediction = scores()
    prediction.loc[0, "model_name"] = "lightgbm"
    with pytest.raises(ValueError, match="exactly one model"):
        run(predictions=prediction)
    bars, prediction = prices(), scores()
    bars.loc[bars.symbol == "600000.SH", "symbol"] = "920001.BJ"
    prediction.loc[prediction.symbol == "600000.SH", "symbol"] = "920001.BJ"
    result = run(bars, prediction)
    assert not result["trades"]
    assert result["rejections"][0]["reason"] == "unsupported_market"


def test_limit_locked_open_is_rejected():
    bars = prices()
    bars.loc[(bars.date == DATES[1]) & (bars.symbol == "600000.SH"), "open"] = 11.
    result = run(bars)
    assert result["trades"] == []
    assert result["rejections"][0]["reason"] == "limit_up_or_slippage_outside_limit"


def test_missing_scores_keep_inventory_and_terminal_decision_cannot_execute():
    prediction = scores([DATES[0], DATES[-1]])
    result = run(predictions=prediction, rebalance_sessions=1)
    assert result["decisions"][1]["reason"] == "no_current_oos_scores_inventory_unchanged"
    assert result["decisions"][-1]["reason"] == "no_next_verified_session"
    assert len(result["trades"]) == 1
    assert result["terminal_state"]["positions"]
    assert result["evaluation_end"] == DATES[-1]


def test_cancellation_propagates_and_inputs_remain_unchanged():
    bars, prediction = prices(), scores()
    expected_bars, expected_scores = bars.copy(deep=True), prediction.copy(deep=True)
    calls = []

    def cancel():
        calls.append(True)
        if len(calls) == 4:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        backtest_scores(bars, prediction, DATES, run_id="cancel", check_cancel=cancel)
    pd.testing.assert_frame_equal(bars, expected_bars)
    pd.testing.assert_frame_equal(prediction, expected_scores)


def test_boolean_cancellation_uses_job_cancelled_contract():
    from lake_equity.jobs import JobCancelled
    with pytest.raises(JobCancelled, match="Portfolio backtest cancelled"):
        backtest_scores(prices(), scores(), DATES, run_id="cancel", check_cancel=lambda: True)


def test_price_materialization_keeps_only_execution_prefix_and_evidence():
    dates = pd.bdate_range('2025-01-02', periods=100).strftime('%Y-%m-%d').tolist()
    bars = pd.DataFrame([dict(date=day, symbol='600000.SH', open=10., high=10.1, low=9.9,
                              close=10., volume=1_000_000., amount=10_000_000.,
                              bar_valid=True, source='sina_unadjusted_v1') for day in dates])
    for number in range(80):
        bars[f'unused_feature_{number}'] = float(number)
    prediction = pd.DataFrame([dict(date=dates[60], symbol='600000.SH', score=1., model_name='ridge',
                                    fold=0, train_end=dates[50], available_at=dates[60] + 'T15:00:00+08:00')])
    result = backtest_scores(bars, prediction, dates, run_id='bounded-prices')
    assert result['price_materialization']['start'] == dates[59]
    assert result['price_materialization']['rows'] == 41
    assert not any(column.startswith('unused_feature_') for column in result['price_materialization']['columns'])
    assert {'bar_valid', 'source'} <= set(result['price_materialization']['columns'])
    assert result['trades'][0]['date'] == dates[61]
