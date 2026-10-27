import json

from lake_equity.learning.releases import ReleaseRegistry  # noqa: F401
from test_api_integration import integrated_app  # noqa: F401


def test_jobs_search_and_pagination_include_records_older_than_100(integrated_app):
    client, app, settings = integrated_app
    with app.state.jobs.connection() as connection:
        for index in range(305):
            body = dict(id=f'lake-fixture-{index:04}', kind='scan', status='succeeded',
                        created_at=index, params={'symbol': '600000.SH' if index == 0 else '600519.SH'})
            connection.execute('INSERT INTO jobs VALUES (?,?,?)', (body['id'], str(index), json.dumps(body)))
        connection.commit()
    first = client.get('/api/v1/jobs?limit=50').json()
    assert first['total'] == 305 and first['has_more'] is True
    assert first['jobs'][0]['id'] == 'lake-fixture-0304'
    last = client.get('/api/v1/jobs?limit=50&offset=300').json()
    assert len(last['jobs']) == 5 and last['has_more'] is False
    found = client.get('/api/v1/jobs?q=600000.SH&status=succeeded&kind=scan').json()
    assert found['total'] == 1 and found['jobs'][0]['id'] == 'lake-fixture-0000'
    assert client.get('/api/v1/jobs?offset=-1').status_code == 422


def test_report_search_filters_before_pagination(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    rows = [dict(run_id=f'backtest-{index:032x}', kind='backtest', status='completed',
                 created_at=index, title='oldest needle' if index == 0 else 'report') for index in range(305)]
    monkeypatch.setattr(app.state.history, 'reports', lambda kind=None, limit=100: {'reports': rows[:limit]})
    result = client.get('/api/v1/reports?limit=50&offset=300&kind=backtest').json()
    assert result['total'] == 305 and len(result['reports']) == 5 and result['has_more'] is False
    result = client.get('/api/v1/reports?q=oldest%20needle&status=completed&kind=backtest').json()
    assert result['total'] == 1 and result['reports'][0]['run_id'] == rows[0]['run_id']


def test_overview_and_maintenance_occupancy_include_older_active_job(integrated_app, monkeypatch):
    from my_strategy.adapters import czsc_adapter

    client, app, settings = integrated_app
    with app.state.jobs.connection() as connection:
        for index in range(101):
            body = dict(id=f'lake-old-{index}', kind='scan', status='queued' if index == 0 else 'succeeded',
                        created_at=index)
            connection.execute('INSERT INTO jobs VALUES(?,?,?)', (body['id'], str(index), json.dumps(body)))
        connection.commit()
    assert app.state.operations.active_jobs() is True
    monkeypatch.setattr(czsc_adapter, 'data_status', lambda **kwargs: {})
    monkeypatch.setattr(app.state.research, 'methods', lambda: {}, raising=False)
    assert client.get('/api/v1/overview').json()['jobs'] == dict(active=1, total=101)
