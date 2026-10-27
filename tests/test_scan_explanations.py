"""Exact frozen-score explanations and immutable, explicitly built attachments."""
import hashlib
import json
import shutil
import sqlite3

import numpy as np
import pandas as pd
import polars as pl
import pytest

from lake_equity.learning import models
from lake_equity.learning.workflow import LearningWorkflow
from lake_equity.learning.registry import canonical, digest
from test_learning_workflow import workbench
from test_learning_dataset import publish
from lake_equity.market.store import MarketStore


def ridge_bundle(directory):
    directory.mkdir()
    state = dict(columns=['a', 'b'], median={'a': 2., 'b': 3.}, low={'a': 0., 'b': 1.},
                 high={'a': 4., 'b': 8.}, mean={'a': 1., 'b': 2.}, scale={'a': 2., 'b': 3.})
    raw = json.dumps(state).encode()
    (directory / 'preprocessing.json').write_bytes(raw)
    details = dict(model_type='ridge', feature_count=2, preprocessing=state,
                   preprocessing_sha256=hashlib.sha256(raw).hexdigest(), coefficients=[.5, -.6], intercept=.1)
    (directory / 'model.json').write_text(json.dumps(details), encoding='utf-8')
    return directory


def test_ridge_explanation_accounts_for_imputation_clipping_and_signed_contributions(tmp_path):
    directory = ridge_bundle(tmp_path / 'model')
    frame = pd.DataFrame([dict(symbol='600000.SH', a=np.nan, b=100.)])
    result = models.explain_saved(directory, frame, scores=np.array([-.85]))
    row = result['rows'][0]
    assert result['capability'] == 'exact_additive'
    assert row['score'] == pytest.approx(-.85)
    assert row['reconstructed_score'] == pytest.approx(-.85)
    assert row['reconstruction_error'] < 1e-12
    assert row['intercept'] == .1
    a, b = row['features']
    assert a == dict(name='a', label='a', group=None, raw_value=None, imputed=True, filled_value=2.,
                     clipped_value=2., standardized_value=.5, coefficient=.5, contribution=.25)
    assert b['raw_value'] == 100. and not b['imputed']
    assert b['clipped_value'] == 8. and b['standardized_value'] == 2.
    assert b['coefficient'] == -.6 and b['contribution'] == -1.2
    assert row['summary']['positive'][0]['name'] == 'a'
    assert row['summary']['negative'][0]['name'] == 'b'


@pytest.fixture
def scan_root(workbench, tmp_path):
    root = tmp_path / 'lake'
    shutil.copytree(workbench['root'], root)
    params = dict(release_id=workbench['release']['release_id'], scope='symbols',
                  symbols=workbench['symbols'], end=workbench['days'][290])
    return root, params, workbench['days']


def test_new_scan_saves_compact_explanations_and_frozen_quote_context(scan_root):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, days = scan_root
    report = LearningWorkflow(root).scan(params)
    assert report['explanation']['count'] == report['population']['scored'] == 3
    assert report['source_context']['next_session'] == days[291]
    assert set(report['source_context']['reviewed_symbols']) == set(params['symbols'])
    first = report['rows'][0]
    assert 'features' not in first and len(first['explanation_summary']['positive']) <= 2
    explained = ScanExplanationService(root).read(report['run_id'], first['symbol'])
    assert explained['status'] == 'saved'
    assert explained['score'] == first['score']
    assert explained['reconstruction_error'] < 1e-12
    assert explained['source']['read_ref'] == report['read_ref']
    assert len(explained['features']) == len(LearningWorkflow(root).registry.read_model(report['model_id'])['columns'])


def legacy_scan(root, params, *, alter_score=False):
    report = LearningWorkflow(root).scan(params)
    folder = root / 'var/learning/workflows' / report['run_id']
    report.pop('explanation')
    report['artifacts'].pop('explanations.json')
    (folder / 'explanations.json').unlink()
    for row in report['rows']:
        row.pop('explanation_ref', None)
        row.pop('explanation_summary', None)
    if alter_score:
        report['rows'][0]['score'] += .01
    path = folder / 'report.json'
    path.write_text(canonical(report), encoding='utf-8')
    with sqlite3.connect(root / 'var/learning/workflows.db') as conn:
        conn.execute('UPDATE runs SET report_sha256=? WHERE id=?', (digest(path), report['run_id']))
    return report, path


def test_old_report_read_is_read_only_then_explicit_build_preserves_source_bytes(scan_root):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report, path = legacy_scan(root, params)
    before = path.read_bytes()
    service = ScanExplanationService(root)
    symbol = report['rows'][0]['symbol']
    assert service.read(report['run_id'], symbol)['status'] == 'not_saved'
    assert not service.directory.exists() and not service.index.exists()
    built = service.build(report['run_id'])
    assert built['status'] == 'completed' and built['explanation']['count'] == 3
    read = service.read(report['run_id'], symbol)
    assert read['status'] == 'saved' and read['score'] == report['rows'][0]['score']
    assert path.read_bytes() == before
    assert service.build(report['run_id'])['run_id'] == built['run_id']


def test_rebuilt_scores_must_match_every_original_observation_before_publication(scan_root):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report, path = legacy_scan(root, params, alter_score=True)
    before = path.read_bytes()
    service = ScanExplanationService(root)
    with pytest.raises(ValueError, match='original frozen score'):
        service.build(report['run_id'])
    assert path.read_bytes() == before
    assert not service.directory.exists() and not service.index.exists()


def test_missing_end_uses_last_closed_session_contract(scan_root, monkeypatch):
    from lake_equity import pipeline
    root, params, days = scan_root
    monkeypatch.setattr(pipeline, '_default_target_date', lambda root: (params['end'], days, 'fixture_calendar', None))
    report = LearningWorkflow(root).scan({key: value for key, value in params.items() if key != 'end'})
    assert report['requested_cutoff'] == params['end']
    assert report['spec']['end'] == params['end']


def test_default_date_preserves_published_calendar_fallback_warning(scan_root, monkeypatch):
    from lake_equity import pipeline
    root, params, days = scan_root
    monkeypatch.setattr(pipeline, '_default_target_date', lambda root: (
        params['end'], days, 'published_trading_calendar', 'verified calendar unavailable'))
    report = LearningWorkflow(root).scan({key: value for key, value in params.items() if key != 'end'})
    assert report['date_resolution']['source'] == 'published_trading_calendar'
    assert report['date_resolution']['warning'] == 'verified calendar unavailable'


def test_explanation_rejects_wrong_score_and_invalid_preprocessing(tmp_path):
    directory = ridge_bundle(tmp_path / 'model')
    frame = pd.DataFrame([dict(a=2., b=8.)])
    with pytest.raises(ValueError, match='original frozen score'):
        models.explain_saved(directory, frame, scores=[99.])
    state = json.loads((directory / 'preprocessing.json').read_text())
    state['columns'].reverse()
    (directory / 'preprocessing.json').write_text(json.dumps(state))
    with pytest.raises(ValueError, match='identity mismatch'):
        models.explain_saved(directory, frame)


def test_scan_names_come_from_the_pinned_generation(scan_root):
    root, params, _ = scan_root
    store = MarketStore(root)
    publish(store, 'instruments', pl.DataFrame({'symbol': params['symbols'], 'name': ['原名称'] * len(params['symbols'])}))
    pinned = store.capture()['read_ref']
    publish(store, 'instruments', pl.DataFrame({'symbol': params['symbols'], 'name': ['新名称'] * len(params['symbols'])}))
    report = LearningWorkflow(root).scan(dict(params, read_ref=pinned))
    assert {row['name'] for row in report['rows']} == {'原名称'}


def test_cancelled_old_explanation_build_publishes_nothing(scan_root):
    from lake_equity.jobs import JobCancelled
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report, path = legacy_scan(root, params)
    before = path.read_bytes()
    service = ScanExplanationService(root)
    with pytest.raises(JobCancelled):
        service.build(report['run_id'], check_cancel=lambda: True)
    assert not service.index.exists() and not service.directory.exists()
    assert path.read_bytes() == before


def test_embedded_and_derived_artifact_tampering_fail_closed(scan_root):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report, _ = legacy_scan(root, params)
    service = ScanExplanationService(root)
    built = service.build(report['run_id'])
    (service.directory / built['run_id'] / 'explanations.json').write_text('{}')
    with pytest.raises(ValueError, match='artifact SHA256'):
        service.read(report['run_id'], report['rows'][0]['symbol'])
    current = LearningWorkflow(root).scan(params)
    (LearningWorkflow(root).directory / current['run_id'] / 'explanations.json').write_text('{}')
    with pytest.raises(ValueError, match='artifact SHA256'):
        service.read(current['run_id'], current['rows'][0]['symbol'])


def test_read_saved_explanation_never_runs_inference_or_reads_new_market_inputs(scan_root, monkeypatch):
    from lake_equity.learning import dataset
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report = LearningWorkflow(root).scan(params)
    monkeypatch.setattr(models, 'load_saved', lambda *args, **kwargs: pytest.fail('GET inferred'))
    monkeypatch.setattr(dataset, 'DatasetSession', lambda *args, **kwargs: pytest.fail('GET opened market inputs'))
    before = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    result = ScanExplanationService(root).read(report['run_id'], report['rows'][0]['symbol'])
    assert result['status'] == 'saved'
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == before


def test_saved_quote_context_matches_existing_fusion_qualification(scan_root):
    from lake_equity.learning.fusion import FusionService
    root, params, _ = scan_root
    report = LearningWorkflow(root).scan(params)
    saved = json.loads((LearningWorkflow(root).directory / report['run_id'] / 'quotes.json').read_text())
    _, expected, following, evidence = FusionService(root)._market_inputs(report, list(saved), 128)
    assert saved == expected
    assert report['source_context']['next_session'] == following[0]
    assert report['source_context']['calendar_recent'] == evidence['calendar_recent']


def test_non_ridge_models_explicitly_declare_unsupported_instead_of_fabricating_attribution(tmp_path):
    directory = ridge_bundle(tmp_path / 'model')
    details = json.loads((directory / 'model.json').read_text())
    details['model_type'] = 'mlp'
    (directory / 'model.json').write_text(json.dumps(details))
    result = models.explain_saved(directory, pd.DataFrame([dict(a=2., b=8.)]))
    assert result['status'] == 'unsupported' and result['rows'] == []
    assert result['reason'] == 'model_type_has_no_verified_local_contribution'


def test_changed_original_feature_implementation_blocks_legacy_rebuild(scan_root):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report, path = legacy_scan(root, params)
    report['inference_implementation_sha256']['features.py'] = '0' * 64
    path.write_text(canonical(report), encoding='utf-8')
    with sqlite3.connect(root / 'var/learning/workflows.db') as conn:
        conn.execute('UPDATE runs SET report_sha256=? WHERE id=?', (digest(path), report['run_id']))
    service = ScanExplanationService(root)
    with pytest.raises(ValueError, match='feature implementation'):
        service.build(report['run_id'])
    assert not service.index.exists()


def test_changed_frozen_model_is_rejected_even_when_explanation_is_saved(scan_root):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report = LearningWorkflow(root).scan(params)
    (LearningWorkflow(root).registry.directory_for(report['model_id']) / 'model.json').write_text('{}')
    with pytest.raises(ValueError, match='Frozen model artifact SHA256'):
        ScanExplanationService(root).read(report['run_id'], report['rows'][0]['symbol'])


def test_attachment_report_reopens_by_its_id_and_lists_metadata_without_writing(scan_root):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report, _ = legacy_scan(root, params)
    service = ScanExplanationService(root)
    built = service.build(report['run_id'])
    before = service.index.read_bytes()
    assert service.read_run(built['run_id']) == built
    listed = service.list_runs(limit=None)['reports']
    assert listed[0]['run_id'] == built['run_id'] and listed[0]['kind'] == 'scan_explanation'
    assert listed[0]['source_run_id'] == report['run_id'] and listed[0]['explanation_count'] == 3
    assert service.index.read_bytes() == before


@pytest.mark.parametrize('target', ['original', 'artifact'])
def test_attachment_reopen_checks_original_source_and_explanation_sha(scan_root, target):
    from lake_equity.learning.scan_explanations import ScanExplanationService
    root, params, _ = scan_root
    report, path = legacy_scan(root, params)
    service = ScanExplanationService(root)
    built = service.build(report['run_id'])
    if target == 'original':
        report['created_at'] = '2026-10-26T16:00:00+08:00'
        path.write_text(canonical(report), encoding='utf-8')
        with sqlite3.connect(root / 'var/learning/workflows.db') as conn:
            conn.execute('UPDATE runs SET report_sha256=? WHERE id=?', (digest(path), report['run_id']))
    else:
        (service.directory / built['run_id'] / 'explanations.json').write_text('{}')
    with pytest.raises(ValueError, match='SHA256'):
        service.read_run(built['run_id'])
