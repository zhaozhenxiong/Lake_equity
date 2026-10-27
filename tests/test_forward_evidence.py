from copy import deepcopy

from lake_equity.learning.forward import ForwardLedger
from lake_equity.learning.forward_evidence import annotate, qualify


def record(origin='live', horizon=1):
    return dict(day='2026-10-23', origin=origin, information_cutoff='2026-10-21',
                created_at='2026-10-24T16:00:00+08:00', following_sessions=['2026-10-26'],
                release_id='release-a', model_id='model-a', model_version='a', model_horizon=5)


def test_live_timing_uses_target_open_and_distinguishes_backfills():
    row = dict(n=5)
    assert qualify(record(), row)['live_eligible'] is True  # weekend freeze is valid
    assert qualify(record('backfill'), row)['live_eligible'] is False
    late = record()
    late['created_at'] = '2026-10-26T09:30:00+08:00'
    assert 'record_not_before_target_open' in qualify(late, row)['reasons']
    late['created_at'] = '2026-10-24T16:00:00'  # ambiguous timezone
    assert qualify(late, row)['live_eligible'] is False
    late = record()
    late['information_cutoff'] = late['day']
    assert qualify(late, row)['live_eligible'] is False


def test_target_period_origin_and_payoff_are_explicit():
    one = annotate(dict(day='2026-10-23', horizon=1, n=5, scope='symbols'), record())
    assert one['target']['a_share_new_inventory_executable'] is False
    assert one['target']['agreement_reason'] == 'horizon_mismatch'
    assert one['target']['factor_direction'] == 'relative_to_cross_section_median'
    five = annotate(dict(day='2026-10-23', horizon=5, n=5, scope='symbols'), record())
    assert five['target']['agreement_reason'] == 'label_target_mismatch'
    assert one['group'] != five['group']
    backfill = annotate(dict(day='2026-10-23', horizon=1, n=5, scope='symbols'), record('backfill'))
    assert backfill['group'] != one['group']
    changed = record()
    changed['kronos'] = {'params': {'seed': 99}}
    protocol = annotate(dict(day='2026-10-23', horizon=1, n=5, scope='symbols'), changed)
    assert protocol['group'] != one['group']


def test_summary_gate_uses_all_qualified_days_not_display_limit(tmp_path, monkeypatch):
    ledger = ForwardLedger(tmp_path)
    with ledger._connect():
        pass
    rows = []
    for i in range(45):
        row = annotate(dict(day=f'fixture-{i:03}', horizon=1, n=5, scope='symbols',
                            rank_ic=.1, sign_agree=.2, coverage=1., details={}), record())
        rows.append(row)
    monkeypatch.setattr(ledger, '_metric_rows', lambda: deepcopy(rows))
    monkeypatch.setattr(ledger, 'list_days', lambda **kw: {'days': []})
    assert len(ledger.summary({'limit': 20})['metrics']) == 20
    assert ledger.summary({'limit': 20})['sample']['verified_days'] == 45
    assert ledger.summary({'limit': 1})['sample']['status'] == 'sufficient'
    rows[-1]['group'][0] = 'release-b'
    assert ledger.summary()['sample']['verified_days'] == 1
    for row in rows:
        row['origin'] = 'backfill'
        row['qualification']['live_eligible'] = False
    result = ledger.summary()
    assert result['sample']['verified_days'] == 0
    assert result['sample']['historical_verified_days'] == 45


def test_new_signal_identity_is_not_reconciled_using_previous_signal_metrics(tmp_path, monkeypatch):
    from lake_equity.learning import forward as module
    from test_learning_forward import DAY, FUTURES, FakeSession, make_payload, synthetic_bars

    ledger = ForwardLedger(tmp_path)
    bars = synthetic_bars()
    monkeypatch.setattr(module, '_read', lambda *args, **kwargs: bars)
    first = make_payload(ledger, bars)
    ledger._persist(first)
    one = ledger._verify_day(DAY, first, FUTURES, FakeSession('same-evaluation-ref'), None)
    second = make_payload(ledger, bars, read_ref='ref-new-signal')
    ledger._persist(second)
    two = ledger._verify_day(DAY, second, FUTURES, FakeSession('same-evaluation-ref'), None)
    assert two['status'] == 'verified'
    assert one['verification_id'] != two['verification_id']


def test_day_detail_never_splices_previous_horizon_and_latest_group_is_current(tmp_path, monkeypatch):
    from lake_equity.learning import forward as module
    from test_learning_forward import DAY, FUTURES, FakeSession, make_payload, synthetic_bars

    ledger = ForwardLedger(tmp_path)
    bars = synthetic_bars()
    monkeypatch.setattr(module, '_read', lambda *args, **kwargs: bars)
    first = make_payload(ledger, bars)
    first.update(horizon=1, following_sessions=FUTURES[:1])
    ledger._persist(first)
    ledger._verify_day(DAY, first, FUTURES[:1], FakeSession('eval-one'), None)
    second = make_payload(ledger, bars, read_ref='ref-five')
    ledger._persist(second)
    assert ledger.read(DAY)['metrics'] is None
    assert ledger.read(DAY)['verification'] is None
    ledger._verify_day(DAY, second, FUTURES, FakeSession('eval-five'), None)
    metrics = ledger.metrics()
    assert len(metrics['days']) == 2
    assert metrics['days'][-1]['horizon'] == 5
    assert metrics['summary']['rolling']['group'] == metrics['days'][-1]['group']


def test_ambiguous_legacy_timestamps_remain_readable_but_unqualified(tmp_path, monkeypatch):
    import json
    from lake_equity.learning import forward as module
    from test_learning_forward import DAY, FUTURES, FakeSession, make_payload, synthetic_bars

    ledger = ForwardLedger(tmp_path)
    bars = synthetic_bars()
    monkeypatch.setattr(module, '_read', lambda *args, **kwargs: bars)
    payload = make_payload(ledger, bars)
    ledger._persist(payload)
    ledger._verify_day(DAY, payload, FUTURES, FakeSession('legacy'), None)
    with ledger._connect() as conn:
        for identity, text in conn.execute('SELECT verification_id,payload_json FROM verifications'):
            document = json.loads(text)
            document.pop('record_identity')
            conn.execute('UPDATE verifications SET payload_json=? WHERE verification_id=?', (json.dumps(document), identity))
        payload.pop('created_at')
        conn.execute('UPDATE attempts SET payload_json=?', (json.dumps(payload),))
        conn.commit()
    result = ledger.summary()
    assert result['sample']['verified_days'] == 0
    assert result['metrics'][0]['qualification']['live_eligible'] is False
