"""Explicit final refit of an already fixed experiment protocol.

This creates new weights and a new identity. It never rewrites evaluation
predictions, promotes a candidate, or attributes an OOS return to the refit.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import shutil
import uuid

import pandas as pd

from .dataset import build_dataset
from .models import check_stop, fit_predict
from .registry import ModelRegistry, canonical, digest
from .service import LearningService


def refit_model(root, request, progress=None, check_cancel=None):
    if not isinstance(request, dict) or set(request) - {'run_id', 'model_name', 'end'}:
        raise ValueError('Refit accepts run_id, model_name and an optional end date')
    service = LearningService(root)
    parent = service.read_run(request.get('run_id'))
    if parent.get('purpose') == 'final_refit':
        raise ValueError('Use the original evaluation protocol for a new final refit')
    choices = [row for row in parent['models'] if row['name'] == request.get('model_name')]
    if len(choices) != 1:
        raise ValueError('Choose one evaluated model protocol explicitly')
    choice = choices[0]
    end = request.get('end', parent['spec']['end'])
    if not isinstance(end, str) or date.fromisoformat(end).isoformat() != end or end < parent['spec']['end']:
        raise ValueError('Refit end must be an ISO date at or after the original experiment end')
    check_stop(check_cancel)
    run_id = 'learn-' + uuid.uuid4().hex
    working = service.runs_root / ('.' + run_id + '.building')
    working.mkdir(parents=True)
    source = Path(__file__).parent
    implementation = {path.name: digest(path) for path in source.glob('*.py')}
    (working / 'code').mkdir()
    for path in source.glob('*.py'):
        shutil.copyfile(path, working / 'code' / path.name)
    spec = dict(parent['spec'], end=end)
    if end == parent['spec']['end']:
        original = service.runs_root / parent['run_id'] / 'dataset.parquet'
        if digest(original) != parent['artifacts']['dataset.parquet']['sha256']:
            raise ValueError('Original frozen dataset SHA256 mismatch')
        frame = pd.read_parquet(original)
        dataset, sources = parent['dataset'], parent['sources']
        groups = parent['feature_groups']
    else:
        # A new capture is explicit in this refit job; the original evaluation
        # data remains immutable and is never replaced by the later snapshot.
        spec.pop('read_ref', None)
        if len(spec['symbols']) > 500:
            from .partitioned import build_partitioned
            data = build_partitioned(root, spec, working / 'partitions',
                                     progress=lambda **value: progress(value) if progress else None, check_cancel=check_cancel)
        else:
            data = build_dataset(root, spec, progress=lambda **value: progress(value) if progress else None, check_cancel=check_cancel)
        frame, dataset, sources, groups = data['frame'], data['report'], data['sources'], data['feature_groups']
    eligible = frame.feature_eligible.eq(True) & frame.legacy_ma_eligible.eq(True)
    mature = frame.loc[eligible & frame.label.notna() & frame.label_end.lt(end).fillna(False)]
    days = sorted(frame.date.unique())
    if mature.empty:
        raise ValueError('No mature pre-cutoff labels for final refit')
    last = days.index(mature.date.max())
    valid_start = last - spec['valid_sessions'] + 1
    train_start = valid_start - spec['train_sessions']
    if train_start < 0:
        raise ValueError('Insufficient mature sessions for the fixed refit protocol')
    valid = mature.loc[mature.date.between(days[valid_start], days[last])]
    train = mature.loc[mature.date.between(days[train_start], days[valid_start - 1]) & mature.label_end.lt(days[valid_start])]
    if len(train) < 20 or len(valid) < 10:
        raise ValueError('Insufficient eligible final refit rows')
    features = groups['legacy_ma'] if choice['feature_set'] == 'legacy_ma' else groups['price_volume'] if choice['feature_set'] == 'price_volume' else list(dict.fromkeys(c for group in spec['factor_groups'] for c in groups[group]))
    for group in spec['factor_groups'] if choice['feature_set'] == 'all_factors' else [choice['feature_set']]:
        if not train[groups[group]].notna().any().any():
            raise ValueError('Required factor group is empty in final refit: ' + group)
    prediction = frame.loc[eligible & frame.date.eq(end)].copy()
    if prediction.empty:
        prediction = frame.loc[eligible & frame.date.eq(frame.loc[eligible, 'date'].max())].copy()
    if progress:
        progress(dict(phase='final_refit', model=choice['name'], train_rows=len(train), valid_rows=len(valid)))
    _, details = fit_predict(train, valid, prediction, features, model_type=choice['model_type'],
        backend=spec['backend'], seed=spec['seed'], directory=working / 'models/fold-0' / choice['name'],
        root=root, model_options=spec.get('model_options') if choice['model_type'] == 'mlp' else None, check_cancel=check_cancel)
    if implementation != {path.name: digest(path) for path in source.glob('*.py')}:
        raise RuntimeError('Learning implementation changed during final refit')
    frame.to_parquet(working / 'dataset.parquet', index=False)
    config = dict(spec=spec, purpose='final_refit', model_name=choice['name'], parent_run_id=parent['run_id'])
    for name, value in [('config.json', config), ('sources.json', sources)]:
        (working / name).write_text(canonical(value), encoding='utf-8')
    cutoff = max(train.label_end.max(), valid.label_end.max())
    if cutoff >= end:
        raise ValueError('Final refit information cutoff must precede inference date')
    fold = dict(fold=0, train_start=days[train_start], train_end=days[valid_start - 1], valid_start=days[valid_start],
                valid_end=days[last], test_start=None, test_end=None, fit_end=cutoff,
                train_rows=len(train), valid_rows=len(valid), test_rows=0)
    fitted = dict(choice, feature_count=details['feature_count'], feature_count_range=[details['feature_count']] * 2,
                  metrics={}, portfolio={}, fold_models=[], cost_stress_2x={}, excess_return_vs_equal_weight=None)
    from my_strategy.core.run_context import RunContext
    context = RunContext(run_id=run_id, as_of_date=end, git_commit='NO_GIT',
        config_hash=hashlib.sha256(canonical(config).encode()).hexdigest(),
        data_version=hashlib.sha256(canonical(sources).encode()).hexdigest(), seed=spec['seed'],
        artifact_root=str(service.runs_root / run_id), scope='train', source='api', task='final_refit',
        stocks=spec['symbols'], start_date=spec['start'], end_date=end)
    report = dict(family='frozen_factor_refit_v1', purpose='final_refit', run_id=run_id, lake_run_id=run_id,
        status='completed', created_at=datetime.now().astimezone().isoformat(), spec=spec,
        parent_run_id=parent['run_id'], fixed_protocol_model=choice['name'],
        protocol_sha256=hashlib.sha256(canonical({'spec': parent['spec'], 'model_name': choice['name']}).encode()).hexdigest(),
        dataset=dataset, sources=sources, feature_groups=groups, folds=[fold],
        selected_factor_groups=spec['factor_groups'], models=[fitted], benchmark={},
        implementation_sha256=implementation, qualification=parent['qualification'],
        run_context=asdict(context),
        limitations=['Final refit has no attributed out-of-sample performance. Evaluate the original saved rolling series.',
                     'Hyperparameters were inherited from an explicitly selected study; no auto selection or paper promotion.'] + parent.get('limitations', []))
    report['artifacts'] = {p.relative_to(working).as_posix(): dict(path=p.relative_to(working).as_posix(), sha256=digest(p), size_bytes=p.stat().st_size)
                           for p in working.rglob('*') if p.is_file()}
    check_stop(check_cancel)
    (working / 'report.json').write_text(canonical(report), encoding='utf-8')
    working.rename(service.runs_root / run_id)
    ModelRegistry(root).publish_run(report)
    return report
