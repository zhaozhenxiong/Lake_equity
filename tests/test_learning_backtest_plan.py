"""Controlled next-session research plan built from a saved model backtest."""
from __future__ import annotations

import types

from filelock import FileLock, Timeout
import pytest

from lake_equity.learning.backtest_plan import BacktestPlanService
from lake_equity.learning.workflow import LearningWorkflow
from test_learning_workflow import frozen  # noqa: F401  (pytest fixture reuse)


def _rolling_backtest(root, registry):
    series = next(row for row in registry.list_models()['series'] if row['model_name'] == 'factor_mlp')
    params = dict(mode='rolling', series_id=series['series_id'], start=series['members'][0]['start'],
                  end=series['members'][-1]['end'], top_k=2, initial_cash=100_000.)
    return LearningWorkflow(root).backtest(params)


def test_plan_requires_explicit_confirm(frozen):
    root, data, report, registry = frozen
    backtest = _rolling_backtest(root, registry)
    service = BacktestPlanService(root)
    with pytest.raises(ValueError, match='显式确认'):
        service.create(backtest['run_id'], {})
    with pytest.raises(ValueError, match='显式确认'):
        service.create(backtest['run_id'], {'confirm': False})
    with pytest.raises(ValueError, match='Unknown backtest plan parameters'):
        service.create(backtest['run_id'], {'confirm': True, 'account_id': 'account-x'})
    assert not (root / 'var/learning/trading.db').exists()


def test_plan_uses_saved_artifacts_terminal_state_and_verified_next_session(frozen, monkeypatch):
    root, data, report, registry = frozen
    backtest = _rolling_backtest(root, registry)
    service = BacktestPlanService(root)
    monkeypatch.setattr(service, '_session', lambda read_ref=None: types.SimpleNamespace(verify=lambda: None))
    monkeypatch.setattr(service, '_next_session', lambda session, signal_date: '2099-01-02')
    plan = service.create(backtest['run_id'], {'confirm': True})
    assert plan['family'] == 'backtest_plan_v1' and plan['kind'] == 'bplan'
    assert plan['plan_id'].startswith('bplan-') and plan['run_id'] == plan['plan_id']
    assert plan['source_backtest_run_id'] == backtest['run_id']
    assert plan['signal_date'] == backtest['actual_end']
    assert plan['execution_date'] == '2099-01-02'
    assert plan['account_scope'] == 'backtest_terminal_research_account'
    assert plan['qualification'] == dict(research_only=True, broker_connected=False,
                                         automatic_orders=False, executable_order_certified=False)
    assert plan['source_artifacts']['scores_sha256']
    assert plan['source_artifacts']['prices_sha256']
    assert isinstance(plan['orders'], list)
    assert all(order['valid_session'] == '2099-01-02' for order in plan['orders'])
    assert plan['account']['cash'] == pytest.approx(backtest['portfolio']['terminal_state']['cash'], abs=.01)
    assert not (root / 'var/learning/trading.db').exists()
    saved = LearningWorkflow(root).read_run(plan['run_id'])
    assert saved['source_backtest_run_id'] == backtest['run_id']
    assert saved['schema'] == 'backtest_conditional_plan_v1'
    assert saved['next_session_source'] == 'verified_trading_calendar'
    assert saved['source_artifacts']['scores_sha256'] and saved['source_artifacts']['prices_sha256']
    assert saved['source_artifacts']['report_sha256']
    assert saved['plan']['schema'] == 'backtest_conditional_plan_v1'
    listed = LearningWorkflow(root).list_runs(kind='bplan')['reports']
    assert [row['run_id'] for row in listed] == [plan['run_id']]
    with pytest.raises(ValueError, match='Only a saved model backtest'):
        service.create(plan['run_id'], {'confirm': True})


def test_plan_never_uses_an_unverified_or_non_backtest_run(frozen):
    root, data, report, registry = frozen
    service = BacktestPlanService(root)
    with pytest.raises(KeyError):
        service.create('backtest-' + 'd' * 32, {'confirm': True})
    with pytest.raises(ValueError, match='Invalid model workflow identity'):
        service.create('learn-' + 'e' * 32, {'confirm': True})


def test_plan_creation_and_quarantine_share_the_compute_lease(frozen, monkeypatch):
    from lake_equity.learning.quarantine import QuarantineService

    root, data, report, registry = frozen
    backtest = _rolling_backtest(root, registry)
    service = BacktestPlanService(root)
    monkeypatch.setattr(service, '_session', lambda read_ref=None: types.SimpleNamespace(verify=lambda: None))
    monkeypatch.setattr(service, '_next_session', lambda session, signal_date: '2099-01-02')
    lease = root / 'var/learning/compute.lock'
    with FileLock(str(lease), timeout=0):
        with pytest.raises(Timeout):
            service.create(backtest['run_id'], {'confirm': True})
    assert not list(service.workflow.directory.glob('.bplan-*.building'))

    start = service.workflow._start
    protected = []

    def start_during_cleanup(kind):
        identity, working = start(kind)
        quarantine = QuarantineService(root)
        preview = quarantine.listing()
        with pytest.raises(Timeout):
            quarantine.confirm(preview['plan_id'], [identity.split('-', 1)[1]], True)
        assert working.is_dir()
        protected.append(identity)
        return identity, working

    monkeypatch.setattr(service.workflow, '_start', start_during_cleanup)
    plan = service.create(backtest['run_id'], {'confirm': True})
    assert protected == [plan['run_id']]
    assert service.workflow.read_run(plan['run_id'])['family'] == 'backtest_plan_v1'
