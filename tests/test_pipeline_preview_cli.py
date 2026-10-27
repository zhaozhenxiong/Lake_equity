"""Read-only CLI preview and compatibility with persisted historical preflights."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from filelock import FileLock

from lake_equity import cli, pipeline
from lake_equity.pipeline import PipelineService
from lake_equity.settings import Settings
from test_api_integration import integrated_app
from test_lake_data import lake
from test_pipeline import TARGET, build_lake, patched, root


@pytest.mark.parametrize('blocked', [False, True])
def test_cli_dry_run_uses_preview_without_compute_lease_or_report(
        root, patched, monkeypatch, capsys, blocked):
    import lake_equity.data
    lake = build_lake(root)
    if blocked:
        monkeypatch.setattr(pipeline, '_resolve_release', lambda root: (None, 'No active release'))
    monkeypatch.setattr(Settings, 'configure', lambda self, **kwargs: None)
    monkeypatch.setattr(lake_equity.data, 'LakeService', lambda *args, **kwargs: lake)

    def forbidden(*args, **kwargs):
        pytest.fail('A CLI dry-run must not start a pipeline or acquire a compute lease')

    monkeypatch.setattr(cli, '_compute', forbidden)
    monkeypatch.setattr(PipelineService, 'run', forbidden)
    monkeypatch.setattr(sys, 'argv', ['lake-equity', '--root', str(root), 'pipeline',
                                    '--dry-run', '--end', TARGET, '--refit', 'never'])
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    with pytest.raises(SystemExit) as exited:
        cli.main()
    result = json.loads(capsys.readouterr().out)
    after = {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    assert before == after
    assert not (root / 'var/pipeline').exists()
    assert not (root / 'var/learning/compute.lock').exists()
    assert result['schema'] == 'lake_pipeline_preview_v1'
    assert result['kind'] == 'preview'
    assert result['status'] == ('blocked' if blocked else 'ready')
    assert exited.value.code == result['exit_code'] == (4 if blocked else 0)
    assert result['config']['dry_run'] is True
    assert result['config']['refit'] == 'never'
    assert lake.materialize_calls == 0


def test_cli_dry_run_is_available_while_compute_lease_is_held(root, patched, monkeypatch, capsys):
    import lake_equity.data
    lake = build_lake(root)
    monkeypatch.setattr(Settings, 'configure', lambda self, **kwargs: None)
    monkeypatch.setattr(lake_equity.data, 'LakeService', lambda *args, **kwargs: lake)
    monkeypatch.setattr(sys, 'argv', ['lake-equity', '--root', str(root), 'pipeline',
                                    '--dry-run', '--end', TARGET])
    lock_path = root / 'var/learning/compute.lock'
    lock_path.parent.mkdir(parents=True)
    with FileLock(str(lock_path)):
        with pytest.raises(SystemExit) as exited:
            cli.main()
    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)['kind'] == 'preview'
    assert not (root / 'var/pipeline').exists()


def test_cli_dry_run_on_empty_workspace_reports_blockers_without_creating_var(tmp_path):
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).parents[1] / 'run.py'),
                             '--root', str(tmp_path), 'pipeline', '--dry-run'],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 4, result.stderr
    preview = json.loads(result.stdout)
    assert preview['schema'] == 'lake_pipeline_preview_v1'
    assert preview['status'] == 'blocked'
    assert 'config_missing' in {item['code'] for item in preview['blockers']}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('preview_marker', [
    {'params': {'dry_run': True}},
    {'config': {'dry_run': True}},
    {'kind': 'preview'},
])
@pytest.mark.parametrize('filename', ['report.json', 'pipeline.json'])
def test_latest_ignores_historical_preflights_but_read_keeps_their_identity(
        root, patched, preview_marker, filename):
    lake = build_lake(root)
    service = PipelineService(root, lake)
    formal_id, preview_id = 'pipe-' + '1' * 16, 'pipe-' + '2' * 16
    for identity, record in [
        (formal_id, {'finished_at': '2026-10-23T19:00:00+08:00'}),
        (preview_id, {'finished_at': '2026-10-24T19:00:00+08:00', **preview_marker}),
    ]:
        folder = root / 'var/pipeline' / identity
        folder.mkdir(parents=True)
        payload = {'pipeline_id': identity, 'status': 'completed', 'schema': 'lake_pipeline_v1', **record}
        (folder / filename).write_text(json.dumps(payload), encoding='utf-8')
    before = (root / 'var/pipeline' / preview_id / filename).read_bytes()
    assert service.latest()['pipeline_id'] == formal_id
    historical = service.read(preview_id)
    assert historical['pipeline_id'] == preview_id
    assert historical['status'] == 'completed'
    assert historical['kind'] == 'preview'
    assert historical['status_label'] == '只读预检（历史记录）'
    assert (root / 'var/pipeline' / preview_id / filename).read_bytes() == before


def test_latest_is_empty_when_only_preflights_exist(root, patched):
    lake = build_lake(root)
    service = PipelineService(root, lake)
    historical = service.run({'end': TARGET, 'dry_run': True})
    assert historical['kind'] == 'preview'
    assert service.latest() is None
    assert service.read(historical['pipeline_id'])['kind'] == 'preview'


def test_readonly_coverage_catalog_preserves_committed_bounds_without_locks(lake):
    lake.import_legacy()
    before = {path.relative_to(lake.project_root): path.read_bytes()
              for path in lake.project_root.rglob('*') if path.is_file()}
    readonly = {row['name']: row for row in pipeline._coverage_catalog(lake)['datasets']}
    after = {path.relative_to(lake.project_root): path.read_bytes()
             for path in lake.project_root.rglob('*') if path.is_file()}
    assert before == after
    original = {row['name']: row for row in lake.catalog()['datasets']}
    fields = ['name', 'has_data', 'revision_id', 'coverage_start', 'coverage_end', 'watermark', 'snapshot_date']
    assert [{key: row.get(key) for key in fields} for row in readonly.values()] == [
        {key: original[name].get(key) for key in fields} for name in readonly]
    assert readonly['daily_bars']['coverage_start'] == '2025-01-02'
    assert readonly['daily_bars']['coverage_end'] == '2026-10-22'


def test_overview_and_latest_keep_the_formal_run_when_a_newer_preflight_exists(
        integrated_app, monkeypatch):
    from my_strategy.adapters import czsc_adapter
    client, app, settings = integrated_app
    monkeypatch.setattr(czsc_adapter, 'data_status', lambda **kwargs: {'available': False})
    app.state.research.methods = lambda: []
    formal_id, preview_id = 'pipe-' + '3' * 16, 'pipe-' + '4' * 16
    for identity, stamp, params in [
        (formal_id, '2026-10-23T19:00:00+08:00', {'dry_run': False}),
        (preview_id, '2026-10-24T19:00:00+08:00', {'dry_run': True}),
    ]:
        folder = settings.root / 'var/pipeline' / identity
        folder.mkdir(parents=True)
        (folder / 'report.json').write_text(json.dumps({
            'pipeline_id': identity, 'status': 'completed', 'finished_at': stamp, 'params': params,
        }), encoding='utf-8')
    assert client.get('/api/v1/pipeline/latest').json()['pipeline']['pipeline_id'] == formal_id
    overview = client.get('/api/v1/overview')
    assert overview.status_code == 200, overview.text
    assert overview.json()['pipeline']['pipeline_id'] == formal_id
    historical = client.get('/api/v1/pipeline/' + preview_id).json()
    assert historical['kind'] == 'preview'
    assert historical['status_label'] == '只读预检（历史记录）'
    assert app.state.jobs.list() == []
