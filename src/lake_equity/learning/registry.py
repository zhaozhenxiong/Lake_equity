"""Immutable, hash-checked model bundles and chronological model series.

The registry never fits a model or changes a champion pointer. SQLite indexes
bundles; every prediction revalidates the original manifest and weight bytes.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import uuid


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


class ModelRegistry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'var/learning/models'
        self.index = self.root / 'var/learning/models.db'

    def list_models(self):
        if not self.index.is_file():
            return {'models': [], 'series': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            models = [json.loads(row[0]) for row in conn.execute('SELECT summary FROM models ORDER BY rowid DESC')]
            series = [json.loads(row[0]) for row in conn.execute('SELECT summary FROM series ORDER BY rowid DESC')]
        memberships = {member['model_id']: row['series_id'] for row in series for member in row['members']}
        models = [dict(row, series_id=memberships.get(row['model_id'])) for row in models]
        return {'models': models, 'series': series}

    def _read(self, identity, table):
        prefix = 'model' if table == 'models' else 'series'
        if not isinstance(identity, str) or not re.fullmatch(prefix + r'-[0-9a-f]{32}', identity):
            raise ValueError('Invalid ' + prefix + ' identity')
        if not self.index.is_file():
            raise KeyError(identity)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute(f'SELECT summary FROM {table} WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        result = json.loads(row[0])
        payload = {key: value for key, value in result.items() if key not in {'model_id', 'series_id', 'version'}}
        version = hashlib.sha256(canonical(payload).encode()).hexdigest()
        if result['version'] != version or identity != prefix + '-' + version[:32]:
            raise ValueError('Registry identity or manifest SHA256 mismatch')
        return result

    def read_model(self, model_id):
        result = self._read(model_id, 'models')
        directory = self.directory / model_id
        if directory.is_symlink() or self.directory.is_symlink():
            raise ValueError('Model directories cannot be symlinks')
        manifest = directory / 'manifest.json'
        if not manifest.is_file() or json.loads(manifest.read_text(encoding='utf-8')) != result:
            raise ValueError('Frozen model manifest mismatch')
        for name, sha in result['artifacts'].items():
            path = directory / name
            if Path(name).name != name or path.is_symlink() or not path.is_file() or digest(path) != sha:
                raise ValueError('Frozen model artifact SHA256 mismatch: ' + name)
        return result

    def read_series(self, series_id):
        return self._read(series_id, 'series')

    def predict(self, model_id, frame):
        from .models import predict_saved
        model = self.read_model(model_id)
        return predict_saved(self.directory / model_id, frame), model

    def register_run(self, run_id):
        from .service import LearningService
        service = LearningService(self.root)
        report = service.read_run(run_id)
        return self._register_report(report)

    def publish_run(self, report):
        """Publish experiment and model indexes in one attached-DB transaction."""
        path = self.root / 'var/learning/runs' / report['run_id'] / 'report.json'
        if json.loads(path.read_text(encoding='utf-8')) != report:
            raise ValueError('Pending experiment report differs from frozen bytes')
        return self._register_report(report, publish_experiment=True)

    def _register_report(self, report, *, publish_experiment=False):
        run_id = report['run_id']
        run_root = self.root / 'var/learning/runs' / run_id
        now = report['created_at']
        bundles, series_rows = [], []
        self.directory.mkdir(parents=True, exist_ok=True)
        for experiment in report['models']:
            members = []
            for fold in report['folds']:
                folder = run_root / 'models' / f"fold-{fold['fold']}" / experiment['name']
                files = sorted(path for path in folder.iterdir() if path.is_file() and path.suffix in {'.json', '.txt', '.npz', '.safetensors'} and path.name != 'request.json')
                if not {'model.json', 'preprocessing.json'} <= {path.name for path in files}:
                    raise ValueError('Incomplete fitted model bundle')
                hashes = {}
                for path in files:
                    key = path.relative_to(run_root).as_posix()
                    sha = digest(path)
                    if path.is_symlink() or report['artifacts'].get(key, {}).get('sha256') != sha:
                        raise ValueError('Training artifact SHA256 mismatch: ' + key)
                    hashes[path.name] = sha
                details = json.loads((folder / 'model.json').read_text(encoding='utf-8'))
                payload = dict(schema='frozen_factor_model_v1', run_id=run_id,
                    model_name=experiment['name'], model_type=experiment['model_type'],
                    feature_set=experiment['feature_set'], feature_count=details['feature_count'],
                    columns=details['preprocessing']['columns'], factor_groups=report['selected_factor_groups'] if experiment['feature_set'] == 'all_factors' else [experiment['feature_set']],
                    horizon=report['spec']['horizon'], label_formula=report['dataset'].get('label_formula'),
                    feature_history_start=report['spec']['start'],
                    fold=fold['fold'], fit_train_end=fold['train_end'], information_cutoff=fold['fit_end'],
                    evaluation_start=fold['test_start'], evaluation_end=fold['test_end'],
                    frozen_at=now, created_at=now, usage='frozen_inference_only' if report.get('purpose') == 'final_refit' else 'historical_reconstruction_and_frozen_inference',
                    population=report['spec']['symbols'], inference_domain='sh_sz_a_v1',
                    training_read_ref=report['sources']['read_ref'], engine=details['engine'],
                    device=details.get('actual_device', 'cpu'), seed=report['spec']['seed'],
                    preprocessing_sha256=hashes['preprocessing.json'], artifacts=hashes,
                    implementation_sha256=report['implementation_sha256'],
                    qualification=report['qualification'])
                version = hashlib.sha256(canonical(payload).encode()).hexdigest()
                model_id = 'model-' + version[:32]
                model = dict(payload, version=version, model_id=model_id)
                destination = self.directory / model_id
                if not destination.exists():
                    working = self.directory / ('.' + uuid.uuid4().hex + '.building')
                    working.mkdir()
                    for path in files:
                        shutil.copyfile(path, working / path.name)
                    (working / 'manifest.json').write_text(canonical(model), encoding='utf-8')
                    working.rename(destination)
                elif json.loads((destination / 'manifest.json').read_text(encoding='utf-8')) != model:
                    raise ValueError('Existing immutable model identity collision')
                for name, sha in hashes.items():
                    if (destination / name).is_symlink() or digest(destination / name) != sha:
                        raise ValueError('Copied model artifact SHA256 mismatch')
                bundles.append(model)
                if fold['test_start'] is not None:
                    members.append(dict(model_id=model_id, fold=fold['fold'], start=fold['test_start'],
                                        end=fold['test_end'], information_cutoff=fold['fit_end']))
            if not members:
                continue
            payload = dict(schema='rolling_model_series_v1', run_id=run_id,
                model_name=experiment['name'], model_type=experiment['model_type'],
                feature_count=experiment['feature_count'], members=members,
                frozen_at=now, qualification=report['qualification'])
            version = hashlib.sha256(canonical(payload).encode()).hexdigest()
            series_rows.append(dict(payload, version=version, series_id='series-' + version[:32]))
        self.index.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.index, timeout=30)) as conn:
            if publish_experiment:
                conn.execute('ATTACH DATABASE ? AS experiments', (str(self.root / 'var/learning/runs.db'),))
                conn.execute('CREATE TABLE IF NOT EXISTS experiments.runs (run_id TEXT PRIMARY KEY, report_sha256 TEXT NOT NULL, summary TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS models (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS series (id TEXT PRIMARY KEY, summary TEXT NOT NULL)')
            for table, rows, key in [('models', bundles, 'model_id'), ('series', series_rows, 'series_id')]:
                for row in rows:
                    old = conn.execute(f'SELECT summary FROM {table} WHERE id=?', (row[key],)).fetchone()
                    if old and json.loads(old[0]) != row:
                        raise ValueError('Immutable registry identity collision')
                    conn.execute(f'INSERT OR IGNORE INTO {table} VALUES(?,?)', (row[key], canonical(row)))
            if publish_experiment:
                summary = {key: report[key] for key in ['run_id', 'family', 'status', 'created_at', 'spec', 'qualification']}
                conn.execute('INSERT INTO experiments.runs VALUES(?,?,?)',
                    (run_id, digest(run_root / 'report.json'), canonical(summary)))
            conn.commit()
        for row in bundles:
            self.read_model(row['model_id'])
        return {'models': bundles, 'series': series_rows}
