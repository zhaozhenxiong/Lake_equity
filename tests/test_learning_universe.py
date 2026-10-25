"""Dynamic membership uses only prior observations and never squeezes gaps."""
import json

import pandas as pd
import polars as pl
import pytest

from lake_equity.learning.dataset import DatasetSession
from lake_equity.learning.universe import (UniversePolicy, _symbol_metrics, apply_membership, build_universe,
    eligibility_for_symbols, load_membership, metrics_for_symbols, universe_on_date)
from lake_equity.market.store import MarketStore
from test_learning_dataset import publish


def fixtures():
    days = pd.bdate_range('2025-01-01', periods=14).strftime('%Y-%m-%d').tolist()
    bars = pd.DataFrame(dict(symbol='600000.SH', trade_date=pd.to_datetime(days).date,
        open=10., high=11., low=9., close=10., volume=10_000_000., amount=100_000_000., source='sina_unadjusted_v1'))
    policy = UniversePolicy(history_sessions=5, recent_sessions=3, min_coverage=.8,
                            min_recent_coverage=2/3, min_median_amount=80_000_000., max_members=2)
    return days, bars, policy


def compute(bars, days, policy):
    return _symbol_metrics(bars, days, {'list_date': days[0]}, symbol='600000.SH',
                            policy=policy, start=days[0], end=days[-1])


def test_policy_is_versioned_strict_and_immutable():
    policy = UniversePolicy.from_dict()
    assert policy.to_dict()['max_members'] == 1000
    assert UniversePolicy.from_dict(policy) is policy
    for invalid in ({'max_members': 0}, {'version': 'future'}, {'min_coverage': float('nan')},
                    {'history_sessions': True}, {'recent_sessions': 253}, {'bogus': 1}):
        with pytest.raises(ValueError):
            UniversePolicy.from_dict(invalid)
    with pytest.raises(AttributeError):
        policy.max_members = 3


def test_listing_age_gaps_and_no_future_changes():
    days, bars, policy = fixtures()
    before, _ = compute(bars, days, policy)
    changed = bars.copy()
    changed.loc[8:, 'amount'] *= 1000
    after, _ = compute(changed, days, policy)
    pd.testing.assert_frame_equal(before.iloc[:8], after.iloc[:8])
    assert not before.iloc[:4].eligible.any()
    assert before.iloc[4:].eligible.all()
    missing, audit = compute(bars.drop(index=[6, 7]), days, policy)
    assert missing.loc[6, 'reason'] == 'latest_bar_missing_or_invalid'
    assert missing.loc[8, 'coverage252'] == pytest.approx(.6)
    assert not missing.loc[8, 'eligible']
    assert audit[0]['missing_rows'] == 2
    assert audit[0]['expected_sessions'] == len(days)


def test_unknown_liquidity_is_lower_bound_not_observed_window_median():
    days, bars, policy = fixtures()
    bars.loc[4, 'amount'] = 10_000_000.
    bars = bars.drop(index=5)
    frame, _ = compute(bars, days, policy)
    # At day6: [10m, unknown, 100m].  Observed median=55m, full-window
    # nonnegative lower bound=10m; the unknown remains explicit in the audit.
    assert frame.loc[6, 'amount60_observed_median'] == 55_000_000.
    assert frame.loc[6, 'amount60_median'] == 10_000_000.
    assert frame.loc[6, 'unknown_or_invalid60'] == 1
    assert frame.loc[6, 'reason'] == 'liquidity_below_threshold'


def test_unlisted_duplicate_and_unknown_sources_are_not_valid():
    days, bars, policy = fixtures()
    frame, _ = _symbol_metrics(bars, days, {'list_date': days[6]}, symbol='600000.SH',
                                policy=policy, start=days[0], end=days[-1])
    assert not frame.iloc[:10].eligible.any()
    bars.loc[10, 'source'] = 'unknown_vendor'
    frame, audit = compute(bars, days, policy)
    assert not frame.iloc[10].latest_valid
    assert audit[0]['invalid_source_rows'] == 1
    with pytest.raises(ValueError, match='Duplicate keys'):
        compute(pd.concat([bars, bars.iloc[[0]]]), days, policy)


def test_build_frozen_dynamic_pool_and_tie_break(tmp_path):
    days, bars, policy = fixtures()
    store = MarketStore(tmp_path)
    publish(store, 'trading_calendar', pl.DataFrame({'trade_date': pd.to_datetime(days).date.tolist(), 'is_trading': True}))
    second = bars.assign(symbol='000001.SZ')
    third = bars.assign(symbol='600001.SH', amount=200_000_000.)
    # A later entrant cannot become a historical member before having 5 sessions.
    fourth = bars.iloc[9:].assign(symbol='600002.SH', amount=300_000_000.)
    publish(store, 'daily_bars', pl.from_pandas(pd.concat([bars, second, third, fourth], ignore_index=True)))
    catalog = pd.DataFrame(dict(symbol=['600000.SH', '000001.SZ', '600001.SH', '600002.SH', '000002.SZ'],
        list_date=pd.to_datetime([days[0], days[0], days[0], days[9], days[0]]).date))
    publish(store, 'instruments', pl.from_pandas(catalog))
    artifact = build_universe(tmp_path, days[0], days[-1], tmp_path/'universe', policy=policy)
    members = load_membership(artifact)
    assert members.date.min() == days[4]
    assert universe_on_date(artifact, days[4])['symbols'] == ['000001.SZ', '600001.SH']
    assert universe_on_date(artifact, days[-1])['symbols'] == ['600001.SH', '600002.SH']
    assert artifact['qualifications']['historical_population_complete'] is False
    assert artifact['catalogue_symbols_without_bars'] == ['000002.SZ']
    audit = pd.read_parquet(artifact['audit_paths']['stock_year_coverage'])
    assert audit.loc[audit.symbol.eq('000002.SZ'), 'missing_rows'].item() == len(days)
    frame = pd.DataFrame(dict(date=[days[4], days[4], days[-1]], symbol=['000001.SZ', '600000.SH', '600002.SH'], score=[1.,2.,3.]))
    assert apply_membership(frame, artifact).score.tolist() == [1., 3.]
    # Future lake revisions do not change membership of the saved read.
    publish(store, 'daily_bars', pl.from_pandas(bars))
    assert universe_on_date(artifact, days[-1])['symbols'] == ['600001.SH', '600002.SH']
    # Identity and byte validation catches edits rather than silently using them.
    path = tmp_path/'universe'/'membership'/'year=2025.parquet'
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='membership bytes changed'):
        load_membership(artifact)


def test_eligibility_for_symbols_is_request_only_and_skips_the_cap(tmp_path):
    days, bars, policy = fixtures()
    store = MarketStore(tmp_path)
    publish(store, 'trading_calendar', pl.DataFrame({'trade_date': pd.to_datetime(days).date.tolist(), 'is_trading': True}))
    second = bars.assign(symbol='000001.SZ', volume=1_000., amount=10_000.)
    third = bars.assign(symbol='600001.SH', amount=200_000_000.)
    publish(store, 'daily_bars', pl.from_pandas(pd.concat([bars, second, third], ignore_index=True)))
    capped = UniversePolicy(history_sessions=5, recent_sessions=3, min_coverage=.8,
                            min_recent_coverage=2/3, min_median_amount=80_000_000., max_members=1)
    session = DatasetSession(tmp_path)
    frame = eligibility_for_symbols(session, ['600000.SH', '600001.SH'], days[0], days[-1], policy=capped)
    session.verify()
    assert list(frame.columns) == ['date', 'symbol', 'eligible', 'reason', 'amount60_median', 'coverage252', 'coverage60']
    assert set(frame.symbol) == {'600000.SH', '600001.SH'}  # the third identity is never examined
    assert frame.loc[frame.date.eq(days[-1]) & frame.symbol.eq('600000.SH'), 'eligible'].item()
    assert frame.loc[frame.date.eq(days[-1]) & frame.symbol.eq('600001.SH'), 'eligible'].item()
    # max_members=1 would cap membership; qualification for the requested pool does not rank.
    assert len(frame.loc[frame.eligible.astype(bool), 'symbol'].unique()) == 2
    illiquid = eligibility_for_symbols(DatasetSession(tmp_path), ['000001.SZ'], days[0], days[-1],
                                       policy=UniversePolicy(history_sessions=5, recent_sessions=3, min_coverage=.8,
                                       min_recent_coverage=2/3, min_median_amount=80_000_000.))
    row = illiquid.loc[illiquid.date.eq(days[-1])].iloc[0]
    assert not row['eligible'] and row['reason'] == 'liquidity_below_threshold'


def test_metrics_for_symbols_recomputes_rank_from_frozen_metrics(tmp_path):
    days, bars, policy = fixtures()
    store = MarketStore(tmp_path)
    publish(store, 'trading_calendar', pl.DataFrame({'trade_date': pd.to_datetime(days).date.tolist(), 'is_trading': True}))
    second = bars.assign(symbol='000001.SZ')
    third = bars.assign(symbol='600001.SH', amount=200_000_000.)
    publish(store, 'daily_bars', pl.from_pandas(pd.concat([bars, second, third], ignore_index=True)))
    artifact = build_universe(tmp_path, days[0], days[-1], tmp_path/'universe', policy=policy)
    assert set(universe_on_date(artifact, days[-1])['symbols']) == {'000001.SZ', '600001.SH'}
    ranked = metrics_for_symbols(artifact, days[-1], days[-1], ['600000.SH', '000001.SZ', '600001.SH'])
    assert list(ranked.columns) == ['date', 'symbol', 'eligible', 'reason', 'amount60_median',
                                    'coverage252', 'coverage60', 'rank']
    ranks = dict(zip(ranked.symbol, ranked['rank']))
    assert ranks == {'600001.SH': 1, '000001.SZ': 2, '600000.SH': 3}  # capped-out identity still ranked
    subset = metrics_for_symbols(artifact, days[-1], days[-1], ['000001.SZ'])
    assert subset.symbol.tolist() == ['000001.SZ'] and subset['rank'].item() == 2
    metrics_path = tmp_path/'universe'/'metrics'/'batch-0000.parquet'
    metrics_path.write_bytes(metrics_path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='metrics bytes changed'):
        metrics_for_symbols(artifact, days[-1], days[-1], ['000001.SZ'])


def test_policy_manifest_changes_and_requested_dates_rejected(tmp_path):
    days, bars, policy = fixtures()
    store = MarketStore(tmp_path)
    publish(store, 'trading_calendar', pl.DataFrame({'trade_date': pd.to_datetime(days).date.tolist(), 'is_trading': True}))
    publish(store, 'daily_bars', pl.from_pandas(bars))
    artifact = build_universe(tmp_path, days[0], days[-1], tmp_path/'universe', policy=policy)
    with pytest.raises(ValueError, match='outside the frozen'):
        universe_on_date(artifact, '2026-01-01')
    manifest = tmp_path/'universe'/'manifest.json'
    value = json.loads(manifest.read_text())
    value['policy']['max_members'] = 99
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='manifest identity changed'):
        load_membership(artifact)
