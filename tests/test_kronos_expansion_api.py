import time

from filelock import FileLock

from lake_equity.learning.kronos_replay import KronosReplayService
from lake_equity.storage_usage import StorageUsageService
from test_api_integration import integrated_app  # noqa: F401


def wait(client, identity):
    for _ in range(150):
        value = client.get('/api/v1/jobs/' + identity).json()
        if value['status'] not in {'queued', 'running', 'cancelling'}:
            return value
        time.sleep(.02)
    raise AssertionError('Job did not terminate')


def test_replay_preview_history_and_resume_job_routing(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    calls = []
    identity = 'replay-' + 'a' * 32
    result = dict(run_id=identity, experiment_id=identity, kind='kronos_replay',
                  status='partial', family='kronos_replay_v1', created_at='2026-10-25T00:00:00Z')
    monkeypatch.setattr(KronosReplayService, 'preview', lambda self, body: calls.append(('preview', body)) or {'request': body})
    monkeypatch.setattr(KronosReplayService, 'list_runs', lambda self, limit=100, offset=0: {'reports': [result]})
    monkeypatch.setattr(KronosReplayService, 'read', lambda self, run_id: result)
    monkeypatch.setattr(KronosReplayService, 'resume', lambda self, run_id, params, **kw: calls.append(('resume', run_id, params)) or result)
    assert client.post('/api/v1/kronos/replays/preview', json={'days': 20}).json()['request']['days'] == 20
    assert client.get('/api/v1/kronos/replays').json()['reports'][0]['run_id'] == identity
    assert client.get('/api/v1/reports?kind=kronos_replay').json()['total'] == 1
    assert client.get('/api/v1/reports/' + identity).json()['family'] == 'kronos_replay_v1'
    submitted = client.post('/api/v1/jobs', json={'kind': 'kronos_replay', 'params': {'resume_id': identity, 'max_calls': 2}})
    assert submitted.status_code == 202
    value = submitted.json()
    job = wait(client, value.get('id', value.get('job', {}).get('id')))
    assert job['status'] == 'partial'
    assert calls[-1] == ('resume', identity, {'max_calls': 2})


def test_storage_refresh_uses_job_gate_without_waiting_for_compute(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    calls = []
    monkeypatch.setattr(StorageUsageService, 'refresh', lambda self, body, **kw: calls.append(body) or {'status': 'completed'})
    assert client.get('/api/v1/data/storage/usage?scope=invalid').status_code == 422
    lease_path = settings.root / 'var/learning/compute.lock'
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lease_path)):
        response = client.post('/api/v1/data/storage/usage/refresh', json={'scope': 'lake'})
        assert response.status_code == 202
        value = response.json()
        job = wait(client, value.get('id', value.get('job', {}).get('id')))
        assert job['status'] == 'succeeded'
    assert calls == [{'scope': 'lake'}]
