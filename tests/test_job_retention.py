from lake_equity.jobs import JobManager
from test_jobs import wait_finished


def test_completed_and_failed_jobs_release_cancellation_events(tmp_path):
    def dispatch(kind, params, progress, cancelled):
        if params['index'] % 2:
            raise ValueError('fixture failure')
        return {'status': 'completed'}

    manager = JobManager(tmp_path, dispatch)
    identities = [manager.submit('scan', {'index': index})['id'] for index in range(20)]
    for identity in identities:
        wait_finished(manager, identity)
    manager.close()
    assert manager.stops == {}
    assert len(manager.list()) == 20
    assert {manager.get(identity)['status'] for identity in identities} == {'succeeded', 'failed'}
