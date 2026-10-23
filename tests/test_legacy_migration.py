"""Synthetic historical assets stay readable without retired app runtimes."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
import stat
import time

import numpy as np
import pandas as pd
import pytest

from test_api_integration import integrated_app  # noqa: F401


def dump(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        return '\n'.join(connection.iterdump())


def test_historical_journals_and_accounts_read_without_runtime_or_mutation(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    def forbidden(*args, **kwargs):
        raise AssertionError('A historical GET started a retired runtime')
    from my_strategy.storage.task_journal import TaskJournal
    monkeypatch.setattr(TaskJournal, '__init__', forbidden)
    from my_strategy.services.light_research import LightResearch
    monkeypatch.setattr(LightResearch, '__init__', forbidden)
    root = settings.metadata_root / 'light_research'
    root.mkdir(parents=True, exist_ok=True)
    journal = root / 'task_journal.db'
    payload = {'job_id': 'synthetic-old', 'status': 'running', 'kind': 'score_training',
               'created_at': '2025-01-01T10:00:00+08:00', 'params': {'symbols': ['600000.SH']}}
    with sqlite3.connect(journal) as connection:
        connection.execute('CREATE TABLE jobs(job_id TEXT PRIMARY KEY,payload TEXT)')
        connection.execute('INSERT INTO jobs VALUES(?,?)', (payload['job_id'], json.dumps(payload)))
    cache = root / 'research.db'
    account = {'immutable-account': {'last_result_id': 'a' * 64, 'enabled': True, 'model': 'original-model'}}
    with sqlite3.connect(cache) as connection:
        connection.execute('CREATE TABLE settings(name TEXT PRIMARY KEY,value_json TEXT)')
        connection.execute('INSERT INTO settings VALUES(?,?)', ('enabled_accounts', json.dumps(account)))
    before = (dump(journal), dump(cache), settings.raw_db.read_bytes())
    listed = client.get('/api/v1/jobs').json()['jobs']
    assert listed[0]['id'] == 'light:synthetic-old'
    assert listed[0]['status'] == 'historical' and listed[0]['recorded_status'] == 'running'
    assert listed[0]['historical_read_only'] and listed[0]['automatic_resume'] is False
    assert client.get('/api/v1/jobs/light:synthetic-old').json()['params'] == payload['params']
    assert client.post('/api/v1/jobs/light:synthetic-old/cancel').status_code == 409
    result = client.get('/api/v1/history/accounts').json()['accounts'][0]
    assert result['model'] == 'original-model' and result['automatic_continuation'] is False
    assert (dump(journal), dump(cache), settings.raw_db.read_bytes()) == before
    assert app.state.research.calls == []


def make_prediction(settings):
    from my_strategy.adapters.czsc_adapter import load_bars
    from my_strategy.services import kronos_prediction as kronos
    from my_strategy.storage.research_store import ResearchStore
    days = pd.bdate_range('2025-01-02', periods=140).strftime('%Y-%m-%d').tolist()
    with sqlite3.connect(settings.raw_db) as connection:
        connection.execute('CREATE TABLE stock_daily_normalized(stock TEXT,date TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,amount REAL,source TEXT,has_trade_price INTEGER)')
        connection.executemany('INSERT INTO stock_daily_normalized VALUES(?,?,?,?,?,?,?,?,?,?)',
            [('600000.SH', day, 10+i/100, 11+i/100, 9+i/100, 10.5+i/100, 10000+i, 100000+i*10,
              'sina_unadjusted_v1', 1) for i, day in enumerate(days)])
    raw = load_bars('600000.SH', db_path=settings.raw_db)
    future = pd.bdate_range(pd.Timestamp(days[-1])+pd.Timedelta(days=1), periods=5).strftime('%Y-%m-%d').tolist()
    _, _, info = kronos.prepare_input(raw, future, symbol='600000.SH', as_of=days[-1], lookback=128, horizon=5)
    parameters = dict(lookback=128,horizon=5,samples=8,seed=42,temperature=.8,top_p=.9,device='cpu',dtype='float32')
    calendar = {'run_id': 'synthetic-calendar', 'sha256': 'a'*64}
    identity = dict(schema=kronos.SCHEMA,input=info,parameters=parameters,future_dates=future,
                    calendar={'verified':True,'identity':calendar},assets=[{'fixture':True}],source={'fixture':True})
    paths = np.ones((8,5,6)) * info['last_close']
    value = kronos.summarize_samples(paths, future, info['last_close'])
    value.update(schema=kronos.SCHEMA,status='completed',symbol='600000.SH',as_of=days[-1],identity=identity,
                 input=deepcopy(info),cache_key=kronos._hash(identity))
    outer = dict(kind='prediction-v1',symbol='600000.SH',as_of=days[-1],parameters=parameters,
                 calendar=calendar,data=raw.attrs['data_version'],code='synthetic-request')
    root = settings.metadata_root / 'light_research'
    with ResearchStore(root) as store:
        reference = store.put(outer,value,namespace='predictions',pin='synthetic-protected-history')
    return root, reference['cache_key'], value


def test_v1_prediction_chart_uses_pure_reader_and_checks_cas_bytes(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    root, reference, original = make_prediction(settings)
    from my_strategy.storage.research_store import ResearchStore
    from my_strategy.services import kronos_prediction as kronos
    from my_strategy.services.light_research import LightResearch
    def forbidden(*args, **kwargs):
        raise AssertionError('Saved chart GET started a model, store migration or old manager')
    monkeypatch.setattr(ResearchStore, '__init__', forbidden)
    monkeypatch.setattr(LightResearch, '__init__', forbidden)
    monkeypatch.setattr(kronos, 'predict', forbidden)
    before = dump(root / 'research.db')
    raw_before = settings.raw_db.read_bytes()
    response = client.get('/api/v1/predictions/' + reference + '/chart')
    assert response.status_code == 200, response.text
    chart = response.json()
    assert chart['forecast'] == original['forecast']
    assert chart['history_verification']['status'] == 'verified'
    assert len(chart['history']) == 128
    assert dump(root / 'research.db') == before and settings.raw_db.read_bytes() == raw_before
    path = next((root / 'objects').rglob('*.json.gz'))
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.write_bytes(path.read_bytes()[:-1] + b'x')
    assert client.get('/api/v1/predictions/' + reference + '/chart').status_code == 400
    assert client.get('/api/v1/predictions/' + '0'*64 + '/chart').status_code == 404
    assert app.state.research.calls == []


def test_saved_account_chart_keeps_ledger_and_cutoff_without_continuation(integrated_app, monkeypatch):
    client, app, settings = integrated_app
    from my_strategy.storage.research_store import ResearchStore
    root = settings.metadata_root / 'light_research'
    identity = {'kind':'synthetic-account','start':'2026-10-13','end':'2026-10-16',
                'sources':[{'symbol':'600000.SH','price_basis':'unadjusted'}],
                'configs':{'structure_rules':{}}}
    key = ResearchStore.cache_key(identity, 'simulations')
    value = {'result_id':key,'symbols':['600000.SH'],'start':'2026-10-13','end':'2026-10-16',
             'status':'completed','identity':identity,
             'chart_bars':{'600000.SH':[dict(date=day,open=10.,high=12.,low=9.,close=11.)
                                       for day in ('2026-10-13','2026-10-14','2026-10-15','2026-10-16')]},
             'comparisons':{'structure_rules':{
                 'lineage':{'account_scope':'shared_cash_account','execution':'next_verified_open_with_declared_slippage'},
                 'trades':[dict(date='2026-10-14',symbol='600000.SH',action='BUY',price=10.005,shares=100,
                                fee=5.01,ledger_version=3,order_id='synthetic-order',signal_date='2026-10-13')]}}}
    with ResearchStore(root) as store:
        store.put(identity,value,namespace='simulations',pin='synthetic-saved-account')
        store.set_setting('enabled_accounts',{'account-original':{'last_result_id':key,'model':'model-original','enabled':True}})
        store.set_setting('last_daily',{'end':'2026-10-16','status':'completed'})
    before = dump(root / 'research.db')
    def forbidden(*args, **kwargs):
        raise AssertionError('Account view initialized a mutable CAS store')
    monkeypatch.setattr(ResearchStore,'__init__',forbidden)
    response = client.get(f'/api/v1/accounts/{key}/chart?symbol=600000.SH&strategy=structure_rules&as_of=2026-10-14')
    assert response.status_code == 200, response.text
    chart = response.json()
    assert chart['display_end'] == '2026-10-14' and len(chart['bars']) == 2
    assert chart['trades'][0]['price'] == 10.005 and chart['trades'][0]['fee'] == 5.01
    assert chart['account_mutated'] is False and chart['prediction_started'] is False
    assert client.get('/api/v1/accounts').json()['last_daily']['end'] == '2026-10-16'
    listed = client.get('/api/v1/reports?kind=simulation').json()['reports']
    assert listed[0]['run_id'] == key and listed[0]['historical_read_only'] is True
    assert client.get('/api/v1/accounts/chart-options?symbol=600000.SH&as_of=2026-10-14').json()['results'][0]['result_id'] == key
    assert client.post('/api/v1/accounts/' + key,json={'enabled':True}).status_code == 404
    assert dump(root / 'research.db') == before


def test_data_operations_whitelist_has_no_arbitrary_handler_or_shell_path(integrated_app, monkeypatch):
    client, app, _ = integrated_app
    def forbidden(*args, **kwargs):
        raise AssertionError('An operation preview spawned a process')
    monkeypatch.setattr('cnequity.serve.ops.runner.subprocess.Popen', forbidden)
    result = client.get('/api/v1/data/operations')
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['capabilities']['automatic_scheduler_started'] is False
    assert len(body['operations']) == 21
    assert all(row['available'] is False for row in body['operations'] if row['id'] in {'init.start','init.resume'})
    assert set(body['retired_operations']) == {'init.start','init.resume'}
    for op, params in [('arbitrary.shell',{'command':'echo bad'}), ('init.start',{}),
                       ('backfill.run',{'dataset':'trading_calendar'}), ('check.status',{'shell':'echo bad'})]:
        response = client.post('/api/v1/data/operations/preview', json={'op':op,'params':params})
        assert response.status_code == 400, response.text
    assert app.state.jobs.list() == []


def test_settings_preview_apply_is_explicit_and_keeps_original_backup(integrated_app, monkeypatch):
    client, _, settings = integrated_app
    def forbidden(*args, **kwargs):
        raise AssertionError('Changing settings started collection')
    monkeypatch.setattr('cnequity.serve.ops.runner.subprocess.Popen', forbidden)
    response = client.get('/api/v1/data/settings')
    assert response.status_code == 200, response.text
    desired = {item['id']:item['value'] for section in response.json()['sections'] for item in section['settings']}
    desired['push2_paused'] = True
    path = settings.root / 'configs/cnequity.toml'
    before = path.read_bytes()
    preview = client.post('/api/v1/data/settings/preview',json={'values':desired})
    assert preview.status_code == 200, preview.text
    token = preview.json()['token']
    assert token and path.read_bytes() == before
    assert client.post('/api/v1/data/settings/apply',json={'token':token,'acknowledged':False}).status_code == 400
    response = client.post('/api/v1/data/settings/apply',json={'token':token,'acknowledged':True})
    assert response.status_code == 200 and response.json()['saved']
    assert (path.parent / response.json()['backup_name']).read_bytes() == before
    assert 'push2_paused = true' in path.read_text()
    assert client.post('/api/v1/data/settings/apply',json={'token':token,'acknowledged':True}).status_code == 400


def test_schedule_apply_controls_the_same_os_timer_without_web_scheduler_start(integrated_app):
    client, app, settings = integrated_app
    calls = []
    class FakeOS:
        kind = 'synthetic-os-timer'
        working_directory = str(settings.root)
        enabled = False
        def observe(self):
            return dict(active=self.enabled,matches=self.enabled,installed=self.enabled,owned=True,error=None,
                        fingerprint='enabled' if self.enabled else 'paused')
        def render(self):
            return 'synthetic-one-minute-timer-entry'
        def apply(self, enabled, artifact):
            calls.append((enabled,artifact))
            self.enabled = enabled
    app.state.operations.schedules().backend = FakeOS()
    assert client.get('/api/v1/data/schedules').json()['enabled'] is False
    assert calls == []
    request = dict(daily=True,stale=False,daily_run_at='18:30',stale_run_at='15:30')
    preview = client.post('/api/v1/data/schedules/preview',json=request)
    assert preview.status_code == 200,preview.text
    assert calls == []
    token=preview.json()['token']
    response=client.post('/api/v1/data/schedules/apply',json={'token':token,'acknowledged':True})
    assert response.status_code == 200,response.text
    assert response.json()['daily'] is True and response.json()['enabled'] is True
    assert calls == [(True,'synthetic-one-minute-timer-entry')]
    assert client.get('/api/v1/data/operations').json()['capabilities']['automatic_scheduler_started'] is False
    backups = client.get('/api/v1/data/backups')
    assert backups.status_code == 200 and backups.json()['snapshots'] == []


def test_storage_migration_exposes_guarded_preview_and_confirmation(integrated_app):
    client, app, _ = integrated_app
    response = client.get('/api/v1/data/storage')
    assert response.status_code == 200, response.text
    assert response.json()['confirm_enabled'] is True
    assert app.state.operations.storage().jobs == {}
    assert client.post('/api/v1/data/storage/confirm',json={}).status_code == 422
    assert client.post('/api/v1/data/storage/reviews',json={'kind':'unknown'}).status_code == 422


def storage_ready(client, job_id):
    for _ in range(400):
        result = client.get('/api/v1/data/storage/jobs/' + job_id).json()
        if result['status'] not in {'checking','executing'}:
            return result
        time.sleep(.01)
    pytest.fail('Storage check did not finish')


def test_learning_references_block_purge_and_new_reference_invalidates_review(integrated_app):
    from cnequity.domain.contracts import contract_fingerprint
    from cnequity.domain.datasets import DATASETS
    from cnequity.storage.revisions import RevisionStore
    from lake_equity.market.store import MarketStore
    import polars as pl
    from datetime import date
    client, app, settings = integrated_app
    store = MarketStore(settings.root)
    revisions = RevisionStore(store.config.meta_root,store.config.curated_root,store.config.derived_root)
    ids = []
    spec = DATASETS['daily_bars']
    for number in range(7):
        path = store.config.curated_root / 'daily_bars' / 'synthetic.parquet'
        path.parent.mkdir(parents=True,exist_ok=True)
        pl.DataFrame({'symbol':['600000.SH'],'trade_date':[date(2025,1,2)],'close':[10.+number]}).write_parquet(path)
        revision = revisions.commit('daily_bars',run_id='synthetic-storage-'+str(number),changed_files=[path],
                                    schema_version=spec.schema_version,contract_fingerprint=contract_fingerprint('daily_bars'))
        ids.append(revision.revision_id)
        if number == 0:
            snapshot = store.capture()
    learning = settings.root / 'var/learning/models/synthetic-model'
    learning.mkdir(parents=True)
    (learning/'manifest.json').write_text(json.dumps({'training_read_ref':snapshot['read_ref']}))
    generation = lambda index: store.config.meta_root/'revisions/data/daily_bars'/ids[index]
    before = settings.raw_db.read_bytes()
    def review(index):
        response=client.post('/api/v1/data/storage/reviews',json={
            'kind':'revisions','phase':'purge','object_ids':['revision/daily_bars/'+ids[index]]})
        assert response.status_code == 202,response.text
        return storage_ready(client,response.json()['job_id'])
    blocked = review(0)
    assert blocked['status'] == 'error' and generation(0).is_dir()
    held = app.state.operations.storage().store.registry()['holds']
    assert 'revision/daily_bars/'+ids[0] in held
    ready = review(1)
    assert ready['status'] == 'ready',ready
    assert ready['objects'][0]['object_id'] == 'revision/daily_bars/'+ids[1]
    (learning/'new-reference.json').write_text(json.dumps({'revision_map':{'daily_bars':ids[1]}}))
    response=client.post('/api/v1/data/storage/confirm',json={'review_id':ready['job_id'],
        'confirmation_token':ready['confirmation_token'],'confirmed':True,'maintenance_confirmed':True})
    assert response.status_code == 409 and 'changed after preview' in response.json()['detail']
    assert generation(1).is_dir() and not app.state.operations.gate.closed
    assert review(1)['status'] == 'error'  # Newly referenced version is now held.
    ready = review(2)
    assert ready['status'] == 'ready',ready
    denied=client.post('/api/v1/data/storage/confirm',json={'review_id':ready['job_id'],
        'confirmation_token':ready['confirmation_token'],'confirmed':True,'maintenance_confirmed':False})
    assert denied.status_code == 409
    confirmed=client.post('/api/v1/data/storage/confirm',json={'review_id':ready['job_id'],
        'confirmation_token':ready['confirmation_token'],'confirmed':True,'maintenance_confirmed':True})
    assert confirmed.status_code == 202,confirmed.text
    complete=storage_ready(client,confirmed.json()['job_id'])
    assert complete['status'] == 'complete',complete
    assert not generation(2).exists() and generation(0).is_dir() and generation(1).is_dir()
    assert settings.raw_db.read_bytes() == before


def test_unified_maintenance_gate_blocks_api_and_background_dispatch(integrated_app):
    client, app, _ = integrated_app
    app.state.operations.gate.close()
    try:
        assert client.get('/api/v1/jobs').status_code == 409
        assert client.post('/api/v1/jobs',json={'kind':'scan','params':{}}).status_code == 409
        assert app.state.jobs.list() == []
        assert client.get('/api/v1/data/storage/jobs/missing').status_code == 404
        duplicate = client.post('/api/v1/data/storage/confirm', json={
            'review_id':'missing', 'confirmation_token':'missing', 'confirmed':True,
            'maintenance_confirmed':True})
        assert duplicate.status_code == 409
        assert app.state.operations.gate.closed  # A repeat confirmation cannot reopen an active maintenance gate.
    finally:
        app.state.operations.gate.reopen()


def test_retired_paths_remain_404_with_authentication_enabled(integrated_app, monkeypatch):
    client, _, _ = integrated_app
    monkeypatch.setenv('LAKE_EQUITY_API_TOKEN','synthetic-secret')
    assert client.get('/api/khquant/health').status_code == 404
    assert client.post('/api/lake/ops/start',json={}).status_code == 404
    assert client.get('/api/v1/jobs').status_code == 401


def test_lake_inspection_is_read_only_without_old_app_or_background_refresh(integrated_app, monkeypatch):
    from cnequity.serve.lake import LakeView
    client, app, settings = integrated_app
    def forbidden(*args, **kwargs):
        raise AssertionError('Read-only inspection started a background rebuild')
    monkeypatch.setattr(LakeView, 'refresh_stats_in_background', forbidden)
    config = app.state.lake.config
    run_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    quality = config.meta_root / 'quality/findings'
    quality.mkdir(parents=True)
    (quality / (run_id + '.json')).write_text(json.dumps({'run_id':run_id, 'findings':[]}))
    before = {str(path):path.read_bytes() for path in settings.root.rglob('*') if path.is_file()}
    for suffix in ['', '/partitions', '/dates', '/provenance', '/provenance/series']:
        response = client.get('/api/v1/data/datasets/daily_bars' + suffix)
        assert response.status_code == 200,response.text
    assert client.get('/api/v1/data/quality/runs/' + run_id).status_code == 200
    result = client.get('/api/v1/data/runs?limit=20&offset=0&open=true').json()
    assert result['runs'] == [] and result['total'] == 0 and result['failed_daily_groups'] == 0
    assert client.get('/api/v1/data/heatmap?days=10').status_code == 200
    assert client.get('/api/v1/data/runs/missing').status_code == 404
    assert client.get('/api/v1/data/datasets/missing').status_code == 404
    assert client.get('/api/v1/data/runs?status=invalid').status_code == 422
    assert client.get('/api/v1/data/heatmap?days=751').status_code == 422
    after = {str(path):path.read_bytes() for path in settings.root.rglob('*') if path.is_file()}
    assert {path:after[path] for path in before} == before
    assert all(path.endswith('.lock') and not after[path] for path in after.keys() - before.keys())
    assert app.state.jobs.list() == []


def test_lake_read_exposes_explicit_adjustment_and_pit_mode(integrated_app):
    client, app, _ = integrated_app
    calls = []
    def read(dataset, **kwargs):
        calls.append((dataset, kwargs))
        return {'rows':[], 'receipt':{'pit_mode':kwargs['pit_mode']}}
    app.state.lake.read = read
    response = client.get('/api/v1/datasets/daily_bars/read?symbol=600000.SH&adjust=qfq')
    assert response.status_code == 200
    assert calls[-1][1]['adjust'] == 'qfq' and calls[-1][1]['pit_mode'] == 'strict'
    response = client.get('/api/v1/datasets/financial_statement_indicators/read?symbol=600000.SH&as_of=2025-01-01&pit_mode=best_effort')
    assert response.status_code == 200 and calls[-1][1]['pit_mode'] == 'best_effort'
    assert client.get('/api/v1/datasets/daily_bars/read?adjust=invalid').status_code == 422
    assert client.get('/api/v1/datasets/daily_bars/read?pit_mode=invalid').status_code == 422


def test_storage_overview_defers_full_evidence_verification_to_review(integrated_app, monkeypatch):
    from lake_equity.market.store import MarketStore
    from lake_equity.storage_protection import LearningReferences
    client, app, settings = integrated_app
    snapshot = MarketStore(settings.root).capture()
    report = settings.root / 'var/learning/runs/synthetic/report.json'
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({'training_read_ref':'invalid-retained-reference'}))
    checked = []
    original = LearningReferences.collect
    def full_check(self, **kwargs):
        checked.append(True)
        return original(self, **kwargs)
    monkeypatch.setattr(LearningReferences, 'collect', full_check)
    response = client.get('/api/v1/data/storage')
    assert response.status_code == 200,response.text
    assert checked == []
    proof = response.json()['learning_protection']
    assert proof['read_ref_count'] == 1 and snapshot['read_ref']
    assert proof['reference_fingerprint'] is None
    assert proof['full_reference_inventory'] == 'required_at_review' and proof['deletion_approved'] is False
    reviewed = client.post('/api/v1/data/storage/reviews',json={'kind':'revisions'})
    assert reviewed.status_code == 409 and 'Invalid retained learning read reference' in reviewed.json()['detail']
    assert checked == [True] and not app.state.operations.gate.closed
