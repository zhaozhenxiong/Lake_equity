"""Reversible model archival, default-release helpers and data contracts."""
import hashlib
import json
import sqlite3

import pytest

from lake_equity.learning import archive as archive_module
from lake_equity.learning import defaults as defaults_module
from lake_equity.learning import releases as releases_module
from lake_equity.learning.registry import ModelRegistry, canonical


def _connect(path):
    conn = sqlite3.connect(path)
    return conn


def _init_indexes(root):
    learning = root / 'var/learning'
    learning.mkdir(parents=True, exist_ok=True)
    conn = _connect(learning / 'models.db')
    conn.execute('CREATE TABLE models (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
    conn.execute('CREATE TABLE series (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
    conn.commit()
    conn.close()
    conn = _connect(learning / 'releases.db')
    conn.execute('CREATE TABLE releases (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    conn.execute('CREATE TABLE activations (sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                 'release_id TEXT NOT NULL, previous_id TEXT, at TEXT NOT NULL, reason TEXT NOT NULL)')
    conn.commit()
    conn.close()
    conn = _connect(learning / 'studies.db')
    conn.execute('CREATE TABLE studies (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
    conn.commit()
    conn.close()
    conn = _connect(learning / 'runs.db')
    conn.execute('CREATE TABLE runs (run_id TEXT PRIMARY KEY, report_sha256 TEXT NOT NULL, summary TEXT NOT NULL)')
    conn.commit()
    conn.close()


def _make_model(root, name, *, created_at='2026-10-01T00:00:00+00:00',
                usage='historical_reconstruction_and_frozen_inference'):
    body = canonical(dict(engine='test:' + name))
    payload = dict(schema='frozen_factor_model_v1', run_id='learn-' + name + '0' * (32 - min(len(name), 32)),
                   model_name=name, model_type='ridge', feature_set='legacy_ma', feature_count=1,
                   columns=['ma5_bias'], factor_groups=['legacy_ma'], horizon=5, fold=0,
                   information_cutoff='2026-09-30', created_at=created_at, frozen_at=created_at,
                   usage=usage, population=['000001.SZ'], qualification=dict(research_only=True),
                   artifacts={'model.json': hashlib.sha256(body.encode()).hexdigest()})
    payload['run_id'] = 'learn-' + hashlib.sha256(name.encode()).hexdigest()[:32]
    version = hashlib.sha256(canonical(payload).encode()).hexdigest()
    model_id = 'model-' + version[:32]
    value = dict(payload, version=version, model_id=model_id)
    directory = root / 'var/learning/models' / model_id
    directory.mkdir(parents=True)
    (directory / 'model.json').write_text(body, encoding='utf-8')
    (directory / 'manifest.json').write_text(canonical(value), encoding='utf-8')
    conn = _connect(root / 'var/learning/models.db')
    conn.execute('INSERT INTO models VALUES (?,?)', (model_id, canonical(value)))
    conn.commit()
    conn.close()
    return model_id


def _insert_release(root, release_id, model_id, *, active=False):
    payload = dict(release_id=release_id, name=release_id, model_id=model_id,
                   information_cutoff='2026-09-30', created_at='2026-10-01T00:00:00+00:00',
                   risk=dict(top_k=10), protocol=dict(model_name='legacy_ma_ridge'))
    conn = _connect(root / 'var/learning/releases.db')
    conn.execute('INSERT INTO releases VALUES (?,?)', (release_id, canonical(payload)))
    if active:
        conn.execute('INSERT INTO activations(release_id, previous_id, at, reason) VALUES (?,?,?,?)',
                     (release_id, None, '2026-10-01T00:00:00+00:00', 'test'))
    conn.commit()
    conn.close()


def _fixture(root):
    _init_indexes(root)
    refit_new = _make_model(root, 'refit-new', created_at='2026-10-05T00:00:00+00:00', usage='frozen_inference_only')
    refit_old = _make_model(root, 'refit-old', created_at='2026-10-04T00:00:00+00:00', usage='frozen_inference_only')
    release_two = _make_model(root, 'release-two')
    study_model = _make_model(root, 'study-model')
    workflow_model = _make_model(root, 'workflow-model')
    run_model = _make_model(root, 'run-model')
    orphan_a = _make_model(root, 'orphan-a', created_at='2026-09-01T00:00:00+00:00')
    orphan_b = _make_model(root, 'orphan-b', created_at='2026-09-02T00:00:00+00:00')
    active_release = 'release-' + '1' * 32
    other_release = 'release-' + '2' * 32
    _insert_release(root, active_release, refit_new, active=True)
    _insert_release(root, other_release, release_two)
    conn = _connect(root / 'var/learning/studies.db')
    conn.execute('INSERT INTO studies VALUES (?,?)', ('study-' + '3' * 32,
                 canonical(dict(study_id='study-' + '3' * 32, best_model_id=study_model,
                                series_id='series-' + '4' * 32))))
    conn.commit()
    conn.close()
    conn = _connect(root / 'var/learning/runs.db')
    conn.execute('INSERT INTO runs VALUES (?,?,?)', ('learn-' + '5' * 32, '0' * 64,
                 canonical(dict(run_id='learn-' + '5' * 32, best_model_id=run_model))))
    conn.commit()
    conn.close()
    workflow = root / 'var/learning/workflows' / ('backtest-' + '6' * 32)
    workflow.mkdir(parents=True)
    (workflow / 'report.json').write_text(canonical(dict(model_id=workflow_model,
                                                         model_ids=[workflow_model])), encoding='utf-8')
    return dict(active=refit_new, refit_old=refit_old, release_two=release_two,
                study=study_model, workflow=workflow_model, run=run_model,
                orphan_a=orphan_a, orphan_b=orphan_b, active_release=active_release)


def test_plan_keeps_references_and_is_deterministic(tmp_path):
    ids = _fixture(tmp_path)
    first = archive_module.plan(tmp_path, keep_recent=1)
    assert first['schema'] == 'model_archive_plan_v1'
    assert first['total'] == 8
    assert first['archive'] == sorted([ids['orphan_a'], ids['orphan_b']])
    assert first['already_archived'] == 0
    assert first['estimated_paths'] == 4
    reasons = {row['model_id']: row['reason'] for row in first['keep']}
    assert reasons[ids['active']] == 'active_release'
    assert reasons[ids['release_two']] == 'release_reference'
    assert reasons[ids['study']] == 'study_reference'
    assert reasons[ids['workflow']] == 'workflow_reference'
    assert reasons[ids['run']] == 'run_report_reference'
    assert reasons[ids['refit_old']] == 'recent_refit'
    second = archive_module.plan(tmp_path, keep_recent=1)
    assert second['plan_id'] == first['plan_id']
    path = tmp_path / 'var/learning/archive/plans' / (first['plan_id'] + '.json')
    assert path.is_file()
    assert hashlib.sha256(canonical(first['archive']).encode()).hexdigest() == first['plan_id']


def test_execute_moves_bundles_and_registry_reads_archive(tmp_path):
    ids = _fixture(tmp_path)
    plan = archive_module.plan(tmp_path, keep_recent=1)
    result = archive_module.execute(tmp_path, plan['plan_id'])
    assert result['archived'] == 2
    assert result['model_ids'] == sorted([ids['orphan_a'], ids['orphan_b']])
    for model_id in (ids['orphan_a'], ids['orphan_b']):
        assert not (tmp_path / 'var/learning/models' / model_id).exists()
        assert (tmp_path / 'var/learning/archive/models' / model_id / 'manifest.json').is_file()
    registry = ModelRegistry(tmp_path)
    default = registry.list_models()
    assert len(default['models']) == 6
    assert all(row['archived'] is False for row in default['models'])
    everything = registry.list_models(include_archived=True)
    assert len(everything['models']) == 8
    assert sum(row['archived'] for row in everything['models']) == 2
    model = registry.read_model(ids['orphan_a'])
    assert model['model_id'] == ids['orphan_a']
    assert registry.directory_for(ids['orphan_a']) == tmp_path / 'var/learning/archive/models' / ids['orphan_a']
    assert archive_module.archived_ids(tmp_path) == {ids['orphan_a'], ids['orphan_b']}
    rows = archive_module.archived_rows(tmp_path)
    assert {row['id'] for row in rows} == {ids['orphan_a'], ids['orphan_b']}
    assert all(row['plan_id'] == plan['plan_id'] for row in rows)
    again = archive_module.execute(tmp_path, plan['plan_id'])
    assert again['archived'] == 0
    assert again['model_ids'] == []


def test_execute_refuses_active_release_model(tmp_path):
    ids = _fixture(tmp_path)
    payload = [ids['active']]
    plan_id = hashlib.sha256(canonical(payload).encode()).hexdigest()
    plan_dir = tmp_path / 'var/learning/archive/plans'
    plan_dir.mkdir(parents=True)
    (plan_dir / (plan_id + '.json')).write_text(canonical(dict(
        schema='model_archive_plan_v1', plan_id=plan_id, total=8, keep=[],
        archive=payload, already_archived=0, estimated_paths=0,
        created_at='2026-10-01T00:00:00+00:00')), encoding='utf-8')
    with pytest.raises(ValueError, match='active strategy release'):
        archive_module.execute(tmp_path, plan_id)
    assert (tmp_path / 'var/learning/models' / ids['active']).is_dir()


def test_execute_rejects_ids_outside_plan(tmp_path):
    ids = _fixture(tmp_path)
    plan = archive_module.plan(tmp_path, keep_recent=1)
    with pytest.raises(ValueError, match='not a member'):
        archive_module.execute(tmp_path, plan['plan_id'], [ids['active']])


def test_data_contract_defaults_for_legacy_release(tmp_path):
    _init_indexes(tmp_path)
    legacy = dict(release_id='release-' + '7' * 32, model_id='model-' + '8' * 32)
    contract = releases_module.data_contract(legacy)
    assert contract['schema'] == 'strategy_data_contract_v1'
    assert contract['required'] == ['daily_bars', 'trading_calendar']
    assert contract['optional'] == ['instruments', 'trading_status', 'corporate_actions',
                                    'index_bars', 'margin_trading']
    custom = dict(release_id='release-' + '7' * 32, data_contract=dict(
        schema='strategy_data_contract_v1', required=['daily_bars'], optional=[]))
    assert releases_module.data_contract(custom) == custom['data_contract']
    store = releases_module.ReleaseRegistry(tmp_path)
    assert store.data_contract(legacy)['schema'] == 'strategy_data_contract_v1'


def test_refit_due_zero_sessions_is_not_due(tmp_path):
    release = dict(release_id='release-' + '9' * 32, information_cutoff='2026-10-20',
                   created_at='2026-09-01T00:00:00+00:00')
    value = defaults_module.refit_due(tmp_path, release, '2026-10-20')
    assert value['due'] is False
    assert value['sessions_since_cutoff'] == 0
    assert value['days_since_release'] == 49
    assert value['reason'] == 'insufficient_sessions_since_information_cutoff'
