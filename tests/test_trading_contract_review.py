import json
import sqlite3

from lake_equity.learning.selection import SelectionService
from test_api_integration import integrated_app  # noqa: F401
from test_learning_fusion_trading import account_fixture  # noqa: F401
from test_trading_preflight import prepare_fixture


def test_trading_gets_on_an_empty_workspace_do_not_create_trading_storage(integrated_app):
    client, app, settings = integrated_app
    before = {path.relative_to(settings.root) for path in settings.root.rglob('*') if path.is_file()}
    for path, key in [('accounts', 'accounts'), ('selections', 'selections'), ('plans', 'plans')]:
        response = client.get('/api/v1/trading/' + path)
        assert response.status_code == 200 and response.json()[key] == []
    assert not (settings.root / 'var/learning/trading.db').exists()
    assert {path.relative_to(settings.root) for path in settings.root.rglob('*') if path.is_file()} == before
    assert app.state.jobs.list(None) == []


def test_actual_preflight_route_and_job_rejection_preserve_the_exact_date_contract(
    integrated_app, account_fixture, monkeypatch
):
    client, app, settings = integrated_app
    service, account, source = prepare_fixture(account_fixture, monkeypatch)
    snapshot = service.read_snapshot(account['latest_snapshot_id'])
    state = {key: snapshot[key] for key in ['as_of', 'cash', 'positions', 'permissions', 'is_fixture']}
    state['as_of'] = '2026-10-26'
    latest = service.add_snapshot(account['account_id'], state)
    params = dict(account_id=account['account_id'], snapshot_id=latest['snapshot_id'],
                  source_id=source['run_id'], source_kind='fusion_scan_v1')
    before = service.index.read_bytes()

    def no_job(*args, **kwargs):
        raise AssertionError('blocked preflight must not submit a task')

    monkeypatch.setattr(app.state.jobs, 'submit', no_job)
    response = client.post('/api/v1/trading/plans/preflight', json=params)
    assert response.status_code == 200
    value = response.json()
    assert value['schema'] == 'plan_preflight_v1' and value['status'] == 'blocked'
    assert value['dates']['snapshot_as_of'] == '2026-10-26'
    assert value['dates']['signal_date'] == '2026-10-22'
    assert value['dates']['execution_date'] == '2026-10-23'
    assert any(issue['code'] == 'account_signal_date_mismatch' for issue in value['issues'])
    assert {key: value['identity'][key] for key in params} == params
    assert len(value['identity']['source_sha256']) == 64
    rejected = client.post('/api/v1/jobs', json={'kind': 'trade_plan', 'params': params})
    assert rejected.status_code == 409
    assert rejected.json()['detail']['schema'] == 'plan_preflight_v1'
    assert service.index.read_bytes() == before
    assert service.list_plans()['plans'] == [] and app.state.jobs.list(None) == []


def test_selection_history_preserves_both_source_kinds_beyond_one_hundred(tmp_path):
    directory = tmp_path / 'var/learning'
    directory.mkdir(parents=True)
    expected = set()
    for prefix, count, filename, kind in [('scan', 145, 'workflows.db', 'scan'),
                                         ('fusion', 140, 'fusion.db', 'fusion')]:
        with sqlite3.connect(directory / filename) as connection:
            connection.execute('CREATE TABLE runs(id TEXT PRIMARY KEY,summary TEXT NOT NULL)')
            for number in range(count):
                identity = f'{prefix}-{number:032x}'
                summary = dict(run_id=identity, kind=kind, as_of='2026-10-22', status='completed')
                connection.execute('INSERT INTO runs VALUES(?,?)', (identity, json.dumps(summary)))
                expected.add(identity)
            connection.commit()
    before = {path: path.read_bytes() for path in directory.iterdir()}
    result = SelectionService(tmp_path).list()
    assert {row['source_id'] for row in result['selections']} == expected
    assert {row['source_kind'] for row in result['selections']} == {'model_scan_v1', 'fusion_scan_v1'}
    assert {path: path.read_bytes() for path in directory.iterdir()} == before
