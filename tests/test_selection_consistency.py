"""Research actions agree with frozen portfolio targets and calendar evidence."""
from datetime import datetime

import pytest

from lake_equity.learning.selection import project_decisions


def selection(kind='scan'):
    row = dict(symbol='600000.SH', score=.1, rank=1, status='scored')
    report = dict(run_id=('scan-' if kind == 'scan' else 'fusion-') + 'a' * 32,
        family='model_scan_v1' if kind == 'scan' else 'fusion_scan_v1', as_of='2026-10-22',
        created_at='2026-10-22T16:00:00+08:00', frozen_at='2026-10-21T16:00:00+08:00',
        final_ranking=True, scope='all', release_id='release-' + 'b' * 32,
        release_identity=dict(created_at='2026-10-21T17:00:00+08:00', risk=dict(
            top_k=1, rebalance_sessions=5, max_stock_weight=.1, max_total_weight=.8)), rows=[row])
    if kind != 'scan':
        report.update(mode=kind, baseline_rows=[dict(row)])
        row.update(factor_score=.1, factor_rank=1, baseline_status='scored', selection_eligible=True,
                   fused_rank=1, kronos_status='succeeded' if kind == 'kronos_filter' else 'deferred',
                   kronos=dict(summary=dict(terminal_return_p10=-.01, terminal_return_median=.02)))
    return report


def account(*, cash=9500., shares=100, last=None):
    return dict(account_id='account-test', latest_snapshot_id='snapshot-test', snapshot=dict(
        snapshot_id='snapshot-test', as_of='2026-10-22', cash=cash, last_rebalance_date=last,
        positions=[dict(symbol='600000.SH', total_shares=shares, sellable_shares=shares,
                        frozen_shares=0, buy_lots=[])]))


def projected(report, state=None, *, quote=None, calendar=None):
    return project_decisions(report, account=state or account(),
        quotes={'600000.SH': quote or dict(reference_close=10., quote_usable=True)},
        calendar_recent=calendar if calendar is not None else ['2026-10-22'],
        next_session='2026-10-23', now=datetime.fromisoformat('2026-10-22T18:00:00+08:00'))['rows'][0]['decision']


@pytest.mark.parametrize('kind', ['scan', 'factor_baseline', 'kronos_filter'])
def test_at_rounded_target_holds_instead_of_falsely_suggesting_an_add(kind):
    # 10,500 equity x 10% / 10 CNY = 105 shares; original plan rounds to 100.
    decision = projected(selection(kind))
    assert decision['action'] == 'hold'
    assert decision['target_shares'] == 100
    assert decision['current_weight'] == pytest.approx(1000. / 10500.)


@pytest.mark.parametrize('cash,shares,target,action', [
    (19500., 100, 200, 'add_candidate'), (8000., 200, 100, 'reduce_candidate')])
def test_nonzero_rounded_target_difference_preserves_add_or_reduce(cash, shares, target, action):
    decision = projected(selection(), account(cash=cash, shares=shares))
    assert decision['action'] == action and decision['target_shares'] == target


@pytest.mark.parametrize('reason', ['rank_beyond_cap', 'liquidity_below_threshold'])
def test_held_verified_policy_exclusion_is_a_scheduled_exit_candidate(reason):
    report = selection()
    report['rows'][0].update(status='excluded', score=None, rank=None, reason=reason)
    decision = projected(report)
    assert decision['action'] == 'sell_candidate'
    assert decision['target_shares'] == 0 and decision['target_weight'] == 0
    assert any('排除' in reason for reason in decision['reasons'])


def test_exit_target_is_separate_from_locked_sellable_inventory():
    report=selection();report['rows'][0].update(status='excluded',score=None,rank=None,reason='rank_beyond_cap')
    state=account();state['snapshot']['positions'][0]['sellable_shares']=0
    decision=projected(report,state)
    assert decision['action']=='sell_candidate' and decision['sellable_shares']==0
    assert decision['eligible'] is False
    assert 't_plus_one_or_frozen_inventory' in decision['execution_constraints']


@pytest.mark.parametrize('reason,quote', [
    ('no_valid_bar_as_of', dict(reference_close=10., quote_usable=True)),
    ('rank_beyond_cap', dict(reference_close=10., quote_usable=False)),
    ('liquidity_below_threshold', dict(reference_close=10.))])
def test_unknown_exclusion_or_unverified_quote_never_becomes_a_sell(reason, quote):
    report = selection()
    report['rows'][0].update(status='excluded', score=None, rank=None, reason=reason)
    decision = projected(report, quote=quote)
    assert decision['action'] == 'unknown' and not decision['eligible']


@pytest.mark.parametrize('days,period,reason', [
    ([], 5, 'rebalance_calendar_missing'),
    (['2026-10-20', '2026-10-21', '2026-10-22'], 100, 'rebalance_calendar_insufficient'),
    (['2026-10-20', '2026-10-21'], 5, 'rebalance_calendar_insufficient'),
    (['2026-10-22', '2026-10-22'], 2, 'rebalance_calendar_insufficient')])
def test_incomplete_calendar_cannot_turn_unknown_schedule_into_hold(days, period, reason):
    report = selection()
    report['release_identity']['risk']['rebalance_sessions'] = period
    decision = projected(report, account(last='2026-01-01'), calendar=days)
    assert decision['action'] == 'unknown' and decision['rebalance_due'] is None
    assert reason in decision['blockers']


def test_enough_known_sessions_prove_due_even_if_prior_rebalance_precedes_window():
    report = selection()
    report['release_identity']['risk']['rebalance_sessions'] = 2
    decision = projected(report, account(last='2026-01-01'),
                         calendar=['2026-10-20', '2026-10-21', '2026-10-22'])
    assert decision['action'] == 'hold' and decision['rebalance_due'] is True
    assert not decision['blockers']


def test_verified_window_since_rebalance_proves_not_due_and_policy_exit_waits():
    report = selection()
    report['rows'][0].update(status='excluded', score=None, rank=None, reason='rank_beyond_cap')
    decision = projected(report, account(last='2026-10-21'), calendar=['2026-10-20', '2026-10-21', '2026-10-22'])
    assert decision['action'] == 'hold' and decision['rebalance_due'] is False
    assert not decision['blockers']
