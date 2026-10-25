"""Local job activity must not masquerade as lake storage maintenance."""
from types import SimpleNamespace

from test_api_integration import integrated_app  # noqa: F401


def preview(client, op="daily.full"):
    response = client.post('/api/v1/data/operations/preview', json={'op': op, 'params': {}})
    assert response.status_code == 200, response.text
    return response.json()


def fake_launch_using_real_token_guard(service, monkeypatch):
    def start(preview_id, token, acknowledged, *, requested_by):
        selected = service.ops._take(preview_id, token, acknowledged)
        return {'job_id': 'synthetic-no-process', 'state': 'running', 'op': selected.op}
    monkeypatch.setattr(service.ops, 'start', start)


def test_daily_lock_and_local_job_do_not_report_storage_maintenance(integrated_app, monkeypatch):
    client, app, _ = integrated_app
    service = app.state.operations
    monkeypatch.setattr(service, 'external_busy', lambda: True)
    monkeypatch.setattr('cnequity.serve.ops.preflight.is_run_locked',
                        lambda root, name: name == 'daily_ingestion')
    state = client.get('/api/v1/data/operations').json()['occupancy']
    assert state['local_job_busy'] is True and state['storage_busy'] is False
    assert state['engine_locks'] == ['daily_ingestion']
    result = preview(client)
    assert len(result['blockers']) == 2
    assert any('日更' in reason for reason in result['blockers'])
    assert any('本地数据或研究写入任务' in reason for reason in result['blockers'])
    assert all('维护' not in reason and 'Storage maintenance' not in reason for reason in result['blockers'])
    assert result['launch_token'] is None and result['preview_id'] is None
    assert app.state.jobs.list() == []


def test_start_rechecks_local_busy_without_consuming_preview_token(integrated_app, monkeypatch):
    client, app, _ = integrated_app
    service = app.state.operations
    activity = {'busy': False}
    monkeypatch.setattr(service, 'external_busy', lambda: activity['busy'])
    fake_launch_using_real_token_guard(service, monkeypatch)
    result = preview(client)
    body = {'preview_id': result['preview_id'], 'launch_token': result['launch_token'],
            'acknowledged': [row['id'] for row in result['acknowledgements']]}
    assert body['preview_id'] and body['launch_token']
    activity['busy'] = True
    rejected = client.post('/api/v1/data/operations/start', json=body)
    assert rejected.status_code == 400, rejected.text
    assert '本地数据或研究写入任务' in rejected.json()['detail']
    assert '维护' not in rejected.json()['detail']
    activity['busy'] = False
    accepted = client.post('/api/v1/data/operations/start', json=body)
    assert accepted.status_code == 202 and accepted.json()['op'] == 'daily.full'
    assert client.post('/api/v1/data/operations/start', json=body).status_code == 400
    assert app.state.jobs.list() == []


def test_true_storage_maintenance_is_still_reported_and_blocks_writes(integrated_app, monkeypatch):
    client, app, _ = integrated_app
    service = app.state.operations
    monkeypatch.setattr(service, 'external_busy', lambda: True)
    monkeypatch.setattr(service, '_storage', SimpleNamespace(busy=True))
    state = client.get('/api/v1/data/operations').json()['occupancy']
    assert state['storage_busy'] is True and state['local_job_busy'] is True
    result = preview(client)
    assert any('维护' in reason for reason in result['blockers'])
    assert all('本地数据或研究写入任务' not in reason for reason in result['blockers'])
    assert result['launch_token'] is None
    service.gate.close()
    try:
        assert service.home()['occupancy']['storage_busy'] is True
        assert client.post('/api/v1/jobs', json={'kind': 'scan', 'params': {}}).status_code == 409
    finally:
        service.gate.reopen()


def test_read_only_operation_remains_available_during_local_write(integrated_app, monkeypatch):
    client, app, _ = integrated_app
    service = app.state.operations
    monkeypatch.setattr(service, 'external_busy', lambda: True)
    fake_launch_using_real_token_guard(service, monkeypatch)
    result = preview(client, 'check.status')
    assert result['blockers'] == [] and result['launch_token']
    accepted = client.post('/api/v1/data/operations/start', json={
        'preview_id': result['preview_id'], 'launch_token': result['launch_token'],
        'acknowledged': [row['id'] for row in result['acknowledgements']]})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()['op'] == 'check.status' and app.state.jobs.list() == []
