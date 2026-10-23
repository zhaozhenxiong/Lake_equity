"""Exchange regime boundaries and research-only portfolio execution."""
import pandas as pd
import pytest

from lake_equity.learning.execution import execution_evidence, execution_profile, round_shares
from lake_equity.learning.portfolio import backtest_scores


@pytest.mark.parametrize('symbol,day,ratio', [
    ('600000.SH', '2026-07-03', .05), ('600000.SH', '2026-07-06', .10),
    ('000001.SZ', '2026-07-03', .05), ('000001.SZ', '2026-07-06', .10),
    ('688001.SH', '2025-01-02', .20), ('300001.SZ', '2020-08-24', .20),
])
def test_known_st_regime_is_dated(symbol, day, ratio):
    profile = execution_profile(symbol, day, {'is_st': True})
    assert profile['price_limit_ratio'] == ratio


def test_unknown_st_and_ipo_are_never_reported_as_verified_false():
    profile = execution_profile('600000.SH', '2025-01-02')
    assert profile['historical_st_state'] is None
    assert profile['historical_ipo_unrestricted'] is None
    assert profile['price_limit_ratio'] == .10
    assert any('st_state_unknown' in item for item in profile['assumptions'])
    assert any('ipo_or_relisting_state_unknown' in item for item in profile['assumptions'])


def test_chinext_2020_boundary_and_original_delisting_exception():
    row = dict(open=11.5, _previous_close=10.)
    assert execution_profile('300001.SZ', '2020-08-21')['price_limit_ratio'] == .10
    assert execution_profile('300001.SZ', '2020-08-24')['price_limit_ratio'] == .20
    assert execution_evidence('300001.SZ', row, 'BUY', '2020-08-21', 11.5)['reason'] == 'open_outside_daily_limits_or_unverified_price_basis'
    assert execution_evidence('300001.SZ', row, 'BUY', '2020-08-24', 11.5)['reason'] is None
    assert execution_evidence('300216.SZ', row, 'BUY', '2020-08-24', 11.5)['reason'] == 'historical_delisting_exception_requires_verified_limits'


@pytest.mark.parametrize('side,quantity,held,sellable,expected', [
    ('BUY', 199, 0, None, 0), ('BUY', 200, 0, None, 200),
    ('BUY', 201, 0, None, 201), ('BUY', 50_001, 0, None, 50_000),
    ('SELL', 199, 399, 399, 0), ('SELL', 199, 199, 199, 199),
    ('SELL', 250, 250, 250, 250), ('SELL', 200, 250, 250, 200),
    ('SELL', 250, 250, 0, 0),
])
def test_star_minimum_one_share_increments_and_complete_residual(side, quantity, held, sellable, expected):
    assert round_shares('688001.SH', side, quantity, day='2025-01-02', held=held, sellable=sellable) == expected


def test_mainboard_and_chinext_lots_preserve_conservative_existing_sizes():
    for symbol in ('600000.SH', '000001.SZ', '300001.SZ'):
        assert round_shares(symbol, 'BUY', 199, day='2025-01-02') == 100
        assert round_shares(symbol, 'SELL', 50, day='2025-01-02', held=50) == 50
        assert round_shares(symbol, 'SELL', 150, day='2025-01-02', held=250) == 100


def test_half_up_cent_limits_and_both_directions_are_validated():
    evidence = execution_evidence('600000.SH', dict(open=10.05, _previous_close=10.05), 'BUY', '2025-01-02', 10.05)
    assert evidence['limit_up'] == 11.06
    assert evidence['limit_down'] == 9.05
    assert execution_evidence('688001.SH', dict(open=5., _previous_close=10.), 'BUY', '2025-01-02', 5.)['reason'] == 'open_outside_daily_limits_or_unverified_price_basis'
    assert execution_evidence('688001.SH', dict(open=12., _previous_close=10.), 'BUY', '2025-01-02', 12.)['reason'] == 'limit_up_or_slippage_outside_limit'


def test_explicit_special_events_and_invalid_quote_never_use_regular_limits():
    for field in ('ipo_unrestricted', 'is_relisting', 'is_delisting_period'):
        row = dict(open=10., _previous_close=10., **{field: True})
        assert execution_evidence('688001.SH', row, 'BUY', '2025-01-02', 10.)['reason'] == 'unsupported_ipo_relisting_or_delisting_execution'
    assert execution_evidence('688001.SH', dict(open=10., _previous_close=10., bar_valid=False), 'BUY', '2025-01-02', 10.)['reason'] == 'invalid_or_unverified_bar'
    assert execution_evidence('688001.SH', dict(open=10., _previous_close=10., corporate_action_suspected=True), 'BUY', '2025-01-02', 10.)['reason'] == 'unverified_corporate_action'
    assert execution_evidence('688001.SH', dict(open=10., _previous_close=10.), 'BUY', '2019-07-19', 10.)['reason'] == 'star_before_first_trading_session'


def test_verified_rules_require_evidence_but_still_do_not_certify_a_portfolio():
    row = dict(open=10., _previous_close=10.)
    assert execution_evidence('688001.SH', row, 'BUY', '2025-01-02', 10., require_verified_rules=True)['reason'] == 'historical_execution_evidence_unverified'
    row.update(rules_verified=True, limit_up=12., limit_down=8., is_st=False,
               ipo_unrestricted=False, company_actions_verified=True)
    evidence = execution_evidence('688001.SH', row, 'BUY', '2025-01-02', 10., require_verified_rules=True)
    assert evidence['reason'] is None
    assert evidence['limit_basis'] == 'verified_dated_limits'
    assert evidence['strict_history_eligible'] is False


def fixture(symbol):
    dates = pd.bdate_range('2025-03-03', periods=5).strftime('%Y-%m-%d').tolist()
    bars = pd.DataFrame([dict(date=day, symbol=symbol, open=10., high=10.1, low=9.9,
                              close=10., volume=1_000_000., amount=10_000_000.) for day in dates])
    predictions = pd.DataFrame([dict(date=dates[0], symbol=symbol, score=1., model_name='fixture',
                                    fold=0, train_end='2025-02-28', available_at=dates[0] + 'T15:00:00+08:00')])
    return dates, bars, predictions


@pytest.mark.parametrize('symbol', ['688001.SH', '300001.SZ'])
def test_real_broker_research_fills_star_and_chinext(symbol):
    dates, bars, predictions = fixture(symbol)
    result = backtest_scores(bars, predictions, dates, {'initial_cash': 100_000., 'top_k': 1}, run_id='boards')
    fill = result['trades'][0]
    assert fill['date'] == dates[1]
    assert fill['fee'] > 0 and fill['ledger_version'] == 3
    if symbol.startswith('688'):
        assert fill['shares'] >= 200 and fill['shares'] % 100 != 0
    else:
        assert fill['shares'] % 100 == 0
    assert result['orders'][0]['execution_evidence']['historical_ipo_unrestricted'] is None
    assert result['strict_history_eligible'] is False
    assert result['paper_allocation_eligible'] is False
    assert 'execution.py' in result['implementation_sha256']
    assert result['terminal_state']['cash'] == pytest.approx(100_000. + sum(row['cash_flow'] for row in result['trades']))


def test_strict_rule_option_rejects_unknown_metadata_without_silent_coercion():
    dates, bars, predictions = fixture('688001.SH')
    result = backtest_scores(bars, predictions, dates, {'require_verified_rules': True}, run_id='strict')
    assert result['trades'] == []
    assert result['rejections'][0]['reason'] == 'historical_execution_evidence_unverified'
    with pytest.raises(ValueError, match='must be boolean'):
        backtest_scores(bars, predictions, dates, {'require_verified_rules': 'true'}, run_id='invalid')


def test_bad_bar_does_not_trade_and_invalid_close_is_never_borrowed_from_future():
    dates, bars, predictions = fixture('688001.SH')
    bars['bar_valid'] = True
    bars.loc[1, 'bar_valid'] = False
    result = backtest_scores(bars, predictions, dates, run_id='bad-bar')
    assert result['trades'] == []
    assert result['rejections'][0]['reason'] == 'invalid_or_unverified_bar'
    bars.loc[1, 'bar_valid'] = True
    bars.loc[2, 'close'] = float('nan')
    bars.loc[3, 'close'] = 11.
    result = backtest_scores(bars, predictions, dates, run_id='bad-mark')
    assert result['daily'][2]['equity'] == result['daily'][1]['equity']
    assert result['daily'][2]['stale_symbols'] == ['688001.SH']
    assert result['daily'][3]['equity'] > result['daily'][2]['equity']
