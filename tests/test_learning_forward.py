from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from lake_equity.learning import forward as forward_module
from lake_equity.learning import verification
from lake_equity.learning.forward import ForwardLedger, _day, _fusion_contract, _symbols, _validate_origin

DAY = '2026-10-23'
FUTURES = ['2026-10-26', '2026-10-27', '2026-10-28', '2026-10-29', '2026-10-30']
SYMBOLS = ['600000.SH', '600001.SH', '000001.SZ', '300001.SZ', '002436.SZ']
NOW = '2026-10-23T16:00:00+08:00'


def synthetic_bars(symbols=None, close_shift=0.0, day_shift=0.0, suspended=None):
    symbols = symbols or SYMBOLS
    rows = []
    for index, symbol in enumerate(symbols):
        for offset, day in enumerate([DAY, *FUTURES]):
            if offset == 0:
                open_price, close_price = 10.0, 10.2 + day_shift
            else:
                open_price = 10.0 + .1 * index
                close_price = open_price * (1 + .002 * (index + 1)) + .001
            if offset == len(FUTURES) and close_shift:
                close_price += close_shift
            rows.append(dict(symbol=symbol, date=day, trade_date=day, open=open_price,
                             high=max(open_price, close_price) + .05, low=min(open_price, close_price) - .05,
                             close=close_price, volume=100_000., amount=1_000_000.,
                             source='sina_unadjusted_v1',
                             suspended=bool(suspended and symbol in suspended and day == FUTURES[0])))
    return pd.DataFrame(rows)


def make_payload(ledger, bars, symbols=None, read_ref='ref-0001', close_shift=0.0):
    symbols = symbols or SYMBOLS
    scores = {symbol: (float(index), index + 1) for index, symbol in enumerate(symbols)}
    kronos_rows = {symbol: dict(status='succeeded', prediction_id='prediction-' + symbol, summary={
        'terminal_return_median': .01 * (index - 2)}, quality={'prediction_usable': True}, resource={}, reused=False)
        for index, symbol in enumerate(symbols)}
    signals = [dict(symbol=symbol, source='factor', model_id='model-fixture',
                    score=scores[symbol][0], rank=scores[symbol][1],
                    direction='BUY' if index > 2 else 'WAIT', expected_return=None,
                    quality={'reason': None}, payload_sha256='sha-' + symbol, created_at=NOW)
               for index, symbol in enumerate(symbols)]
    payload = dict(schema='forward_record_v1', rule_version='forward_ledger_v1', code_sha='code-sha',
                   origin='backfill', data_cutoff=DAY, model_horizon=5,
                   day=DAY, read_ref=read_ref, release_id='release-' + 'a' * 32,
                   model_id='model-' + 'b' * 32, model_version='v', series_id=None,
                   information_cutoff='2026-10-21', horizon=5, scope='symbols', symbols=symbols,
                   top_k=3, bars_sha256=ledger._bars_identity(bars, DAY, symbols), device={},
                   calendar_recent=[DAY], following_sessions=FUTURES,
                   factor=dict(model_name='legacy_ma_ridge', model_type='ridge', factor_groups=['legacy_ma'],
                               scores={symbol: dict(score=value[0], rank=value[1]) for symbol, value in scores.items()},
                               unavailable={}),
                   kronos=dict(params={'device': 'cuda', 'seed': 42}, coverage={'succeeded': len(symbols)},
                               rows=kronos_rows),
                   quotes={}, signals=signals,
                   agreement={'n': len(symbols), 'sign_agree': 1., 'rank_agree': 1., 'topk_overlap': 1.},
                   gates=dict(data=dict(status='complete', reason=None),
                              agreement=dict(status='pass', reason='agreement_thresholds_met')),
                   limitations=[], created_at=NOW, elapsed_seconds=1.)
    payload['identity'] = 'identity-' + read_ref + ('-' + str(close_shift) if close_shift else '')
    return payload


class FakeSession:
    def __init__(self, read_ref):
        self.snapshot = {'read_ref': read_ref}
        self.store = object()
        self.check_cancel = None

    def verify(self):
        return None


def test_symbol_and_day_validation():
    assert _symbols(SYMBOLS) == SYMBOLS
    with pytest.raises(ValueError):
        _symbols([])
    with pytest.raises(ValueError):
        _symbols(['600000.SH', '600000.SH'])
    with pytest.raises(ValueError):
        _symbols(['830000.BJ'])
    with pytest.raises(ValueError):
        _symbols([f'6000{index:02d}.SH' for index in range(201)])
    assert _day('2026-10-23', 'day') == '2026-10-23'
    with pytest.raises(ValueError):
        _day('2026-1-3', 'day')


def test_origin_guard_separates_live_from_backfill():
    _validate_origin(DAY, DAY, 'live')
    _validate_origin('2026-10-21', DAY, 'backfill')
    with pytest.raises(ValueError):
        _validate_origin('2026-10-22', DAY, 'live')
    with pytest.raises(ValueError):
        _validate_origin(DAY, DAY, 'backfill')
    with pytest.raises(ValueError):
        _validate_origin('2026-10-21', DAY, 'replay')
    with pytest.raises(ValueError):
        _validate_origin(DAY, None, 'live')


def test_fusion_contract_validation_and_candidate_extraction():
    run = dict(schema='frozen_fusion_scan_v1', kind='fusion', status='completed', as_of='2026-10-22',
               release_id='release-' + 'a' * 32, model_id='model-' + 'b' * 32, read_ref='ref-1',
               release_identity={'information_cutoff': '2026-10-21', 'model_version': 'v'},
               request={'kronos': {'horizon': 1}},
               rows=[dict(symbol='600000.SH', is_candidate=True, factor_score=.1, factor_rank=1,
                          kronos_status='succeeded'),
                     dict(symbol='600001.SH', is_candidate=True, factor_score=None, factor_rank=None),
                     dict(symbol='600002.SH', is_candidate=False, factor_score=.2, factor_rank=2)])
    contract = _fusion_contract(run)
    assert contract['day'] == '2026-10-22' and contract['horizon'] == 1
    assert contract['information_cutoff'] == '2026-10-21' and contract['read_ref'] == 'ref-1'
    assert [row['symbol'] for row in contract['rows']] == ['600000.SH']
    for broken in ({**run, 'schema': 'other'}, {**run, 'status': 'failed'},
                   {**run, 'request': {'kronos': {'horizon': 0}}},
                   {**run, 'release_identity': {}}, {**run, 'rows': []}):
        with pytest.raises(ValueError):
            _fusion_contract(broken)


def test_realized_return_executable_pending_suspended_and_duplicates():
    bars = synthetic_bars()
    executed = verification.realized_return(bars, '600000.SH', DAY, FUTURES, 5)
    assert executed['executable'] is True and executed['status'] == 'ok'
    expected = executed['exit_close'] / executed['exec_open'] - 1
    assert executed['ret_gross'] == pytest.approx(expected)
    assert executed['ret_net_2x'] == pytest.approx(expected - verification.round_trip_cost(2.))
    pending = verification.realized_return(bars.loc[bars.date <= FUTURES[1]], '600000.SH', DAY, FUTURES, 5)
    assert pending['status'] == 'pending' and pending['reason'] == 'future_session_not_published'
    broken = bars.copy()
    broken.loc[(broken.symbol == '600000.SH') & (broken.date == FUTURES[0]), 'volume'] = np.nan
    invalid = verification.realized_return(broken, '600000.SH', DAY, FUTURES, 5)
    assert invalid['status'] == 'unexecutable' and invalid['reason'] == 'execution_bar_invalid'
    halted = synthetic_bars(suspended={'600000.SH'})
    suspended = verification.realized_return(halted, '600000.SH', DAY, FUTURES, 5)
    assert suspended['status'] == 'unexecutable'
    assert suspended['reason'] == 'suspended_on_execution_session'
    duplicate = pd.concat([bars, bars.loc[bars.symbol.eq('600000.SH')]], ignore_index=True)
    with pytest.raises(ValueError):
        verification.realized_return(duplicate, '600000.SH', DAY, FUTURES, 5)


def test_cross_section_metrics_agreement_and_buckets():
    bars = synthetic_bars()
    realized = {symbol: verification.realized_return(bars, symbol, DAY, FUTURES, 5) for symbol in SYMBOLS}
    predictions = [dict(symbol=symbol, factor_score=index, factor_rank=index + 1,
                        kronos_median=.01 * (index - 2), expected_return=None)
                   for index, symbol in enumerate(SYMBOLS)]
    metrics = verification.cross_section(predictions, realized, top_k=3)
    assert metrics['n'] == len(SYMBOLS) and metrics['coverage'] == 1.
    assert metrics['rank_ic'] == pytest.approx(1.)
    assert metrics['topk_precision'] == pytest.approx(1.)
    assert metrics['sign_agree'] == pytest.approx(1.)
    assert metrics['buckets']['A']['count'] == 2
    assert metrics['buckets']['B']['count'] == 0
    assert metrics['pending'] == [] and metrics['unexecutable'] == []
    agreement = verification.prediction_agreement(predictions, top_k=3)
    assert agreement['sign_agree'] == pytest.approx(1.)
    assert agreement['topk_overlap'] == pytest.approx(1.)
    assert verification.prediction_agreement([dict(symbol='600000.SH', factor_score=1., kronos_median=None)])['n'] == 0


def test_ledger_persist_is_immutable_and_idempotent(tmp_path):
    ledger = ForwardLedger(tmp_path)
    bars = synthetic_bars()
    payload = make_payload(ledger, bars)
    first = ledger._persist(payload)
    assert first['status'] == 'recorded' and first['reused'] is False
    assert Path(tmp_path / first['artifact_path']).is_file()
    second = ledger._persist(payload)
    assert second['reused'] is True and second['identity'] == first['identity']
    with sqlite3.connect(tmp_path / 'var/learning/forward.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM days').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM signals').fetchone()[0] == len(SYMBOLS)
    revised = make_payload(ledger, bars, read_ref='ref-0002')
    third = ledger._persist(revised)
    assert third['identity'] != first['identity']
    with sqlite3.connect(tmp_path / 'var/learning/forward.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2
        identity = conn.execute('SELECT identity FROM days WHERE day=?', (DAY,)).fetchone()[0]
    assert identity == third['identity']
    assert Path(tmp_path / first['artifact_path']).is_file()
    document = ledger.read(DAY)
    assert document['status'] == 'recorded'
    assert document['origin'] == 'backfill'
    assert document['record']['read_ref'] == 'ref-0002'
    assert len(ledger.list_days()['days']) == 1
    assert ledger.list_days()['days'][0]['origin'] == 'backfill'


def test_verify_day_reuses_and_records_revisions(tmp_path, monkeypatch):
    ledger = ForwardLedger(tmp_path)
    bars = synthetic_bars()
    payload = make_payload(ledger, bars)
    ledger._persist(payload)
    monkeypatch.setattr(forward_module, '_read', lambda *args, **kwargs: bars)
    session = FakeSession('ref-verify')
    first = ledger._verify_day(DAY, payload, FUTURES, session, None)
    assert first['status'] == 'verified' and first['revision'] == 0
    assert first['metrics']['n'] == len(SYMBOLS)
    reused = ledger._verify_day(DAY, payload, FUTURES, session, None)
    assert reused['status'] == 'reused' and reused['verification_id'] == first['verification_id']
    changed = synthetic_bars(day_shift=.03)
    monkeypatch.setattr(forward_module, '_read', lambda *args, **kwargs: changed)
    revised = ledger._verify_day(DAY, payload, FUTURES, session, None)
    assert revised['status'] == 'verified' and revised['revision'] == 1
    assert revised['verification_id'] != first['verification_id']
    with sqlite3.connect(tmp_path / 'var/learning/forward.db') as conn:
        assert conn.execute('SELECT COUNT(*) FROM verifications').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM metrics').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM realized').fetchone()[0] == 2 * len(SYMBOLS)
        assert conn.execute('SELECT status FROM days WHERE day=?', (DAY,)).fetchone()[0] == 'verified'
    aggregate = ledger.metrics({'limit': 5})
    assert aggregate['summary']['days'] == 1
    assert aggregate['summary']['rank_ic_mean'] == pytest.approx(1.)
    summary = ledger.summary({'limit': 5})
    assert summary['days'] == dict(total=1, recorded=0, verified=1, partial=0, live=0, backfill=1)
    assert summary['latest']['n'] == len(SYMBOLS)
    assert summary['rolling']['last5']['days'] == 1
    assert summary['sample']['status'] == 'insufficient'
    assert summary['kronos']['requested'] == len(SYMBOLS)
    assert {gate['gate'] for gate in summary['gates']} == {'data', 'agreement', 'realized'}
