"""Immutable research strategy releases and a transactional default pointer."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3

from .registry import ModelRegistry, canonical


DEFAULT_RISK = dict(top_k=10, max_total_weight=.8, max_stock_weight=.1, rebalance_sessions=5,
                    initial_cash=1_000_000, max_volume_fraction=.01)
DEFAULT_FUSION = dict(mode='factor_baseline', candidate_count=200, kronos_weight=0.,
                      chronology='pretraining_cutoff_unproven', calibrated_probability=False)
DEFAULT_DATA_CONTRACT = dict(
    schema='strategy_data_contract_v1',
    required=['daily_bars', 'trading_calendar'],
    optional=['instruments', 'trading_status', 'corporate_actions', 'index_bars', 'margin_trading'])


def data_contract(release):
    """Return the frozen data contract of a release, defaulting old releases."""
    if not isinstance(release, dict):
        raise ValueError('Strategy release must be an object')
    value = release.get('data_contract')
    if value is None:
        value = DEFAULT_DATA_CONTRACT
    if not isinstance(value, dict):
        raise ValueError('Strategy release data_contract must be an object')
    return {key: list(item) if isinstance(item, list) else item for key, item in value.items()}


class ReleaseRegistry:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'var/learning/releases'
        self.index = self.root / 'var/learning/releases.db'
        self.models = ModelRegistry(root)

    def _connect(self):
        if self.directory.is_symlink():
            raise ValueError('Strategy release directory cannot be a symlink')
        self.directory.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index, timeout=30)
        conn.execute('CREATE TABLE IF NOT EXISTS releases (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS activations (sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
                     'release_id TEXT NOT NULL, previous_id TEXT, at TEXT NOT NULL, reason TEXT NOT NULL)')
        conn.commit()
        return closing(conn)

    def list(self):
        if not self.index.is_file():
            return dict(releases=[], active=None, events=[])
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            rows = [json.loads(row[0]) for row in conn.execute('SELECT payload FROM releases ORDER BY rowid DESC')]
            events = [dict(sequence=r[0], release_id=r[1], previous_id=r[2], at=r[3], reason=r[4])
                      for r in conn.execute('SELECT * FROM activations ORDER BY sequence DESC LIMIT 100')]
        return dict(releases=rows, active=events[0]['release_id'] if events else None, events=events)

    def read(self, release_id):
        if not isinstance(release_id, str) or not re.fullmatch(r'release-[a-f0-9]{32}', release_id):
            raise ValueError('Invalid strategy release identity')
        if not self.index.is_file():
            raise KeyError(release_id)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT payload FROM releases WHERE id=?', (release_id,)).fetchone()
        if row is None:
            raise KeyError(release_id)
        release = json.loads(row[0])
        payload = {key: value for key, value in release.items() if key not in {'release_id', 'version'}}
        version = hashlib.sha256(canonical(payload).encode()).hexdigest()
        path = self.directory / release_id / 'manifest.json'
        if (release.get('version') != version or release_id != 'release-' + version[:32] or
                path.is_symlink() or path.parent.is_symlink() or not path.is_file() or
                json.loads(path.read_text(encoding='utf-8')) != release):
            raise ValueError('Strategy release manifest mismatch')
        model = self.models.read_model(release['model_id'])
        if model['version'] != release['model_version']:
            raise ValueError('Strategy release model identity mismatch')
        if release.get('series_id'):
            series = self.models.read_series(release['series_id'])
            if series['version'] != release['series_version']:
                raise ValueError('Strategy release historical series identity mismatch')
        return release

    def active(self):
        if not self.index.is_file():
            return None
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT release_id FROM activations ORDER BY sequence DESC LIMIT 1').fetchone()
        return self.read(row[0]) if row else None

    def data_contract(self, release_or_id):
        release = self.read(release_or_id) if isinstance(release_or_id, str) else release_or_id
        return data_contract(release)

    def _pin_universe(self, supplied):
        if not supplied.get('manifest_path'):
            return supplied
        from .universe import read_universe
        source = Path(supplied['manifest_path']).resolve().parent
        if not source.is_relative_to(self.root):
            raise ValueError('Release universes must be retained inside this workspace')
        universe = read_universe(supplied)
        target = self.root / 'var/learning/universes' / universe['universe_id']
        if target.is_symlink():
            raise ValueError('Retained universe cannot be a symlink')
        if not (target / 'manifest.json').is_file():
            target.mkdir(parents=True, exist_ok=True)
            for record in universe['files']:
                relative = Path(record['path'])
                original, copied = source / relative, target / relative
                if relative.is_absolute() or not original.resolve().is_relative_to(source) or original.is_symlink() or not copied.resolve().is_relative_to(target):
                    raise ValueError('Universe artifact escapes its retained directory')
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, copied)
            shutil.copy2(source / 'manifest.json', target / 'manifest.json')
        retained = read_universe(target / 'manifest.json')
        if retained['universe_id'] != universe['universe_id']:
            raise ValueError('Retained universe identity differs from the selected study')
        return retained

    def create(self, params):
        allowed = {'model_id', 'series_id', 'study_id', 'protocol', 'selection', 'universe', 'fusion', 'risk', 'name'}
        if set(params) - allowed:
            raise ValueError('Unknown strategy release parameters: ' + ', '.join(sorted(set(params) - allowed)))
        model = self.models.read_model(params.get('model_id'))
        series = self.models.read_series(params['series_id']) if params.get('series_id') else None
        if series:
            member = self.models.read_model(series['members'][0]['model_id'])
            if series.get('model_name') != model.get('model_name') or member.get('horizon') != model.get('horizon'):
                raise ValueError('Selected historical series must use the same model protocol and horizon')
            parent_run = model.get('run_id')
            if model.get('usage') == 'frozen_inference_only':
                from .service import LearningService
                parent_run = LearningService(self.root).read_run(model['run_id']).get('parent_run_id')
            if not parent_run or parent_run != series.get('run_id'):
                raise ValueError('Historical series must belong to this exact model experiment or immutable refit parent')
        risk = {**DEFAULT_RISK, **params.get('risk', {})}
        if set(risk) - set(DEFAULT_RISK):
            raise ValueError('Unknown risk parameters')
        for key in ['top_k', 'rebalance_sessions']:
            if isinstance(risk[key], bool) or not isinstance(risk[key], int) or not 1 <= risk[key] <= 100:
                raise ValueError(key + ' must be an integer in [1,100]')
        for key in ['max_total_weight', 'max_stock_weight', 'max_volume_fraction']:
            value = risk[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ValueError(key + ' must be in (0,1]')
        if risk['max_stock_weight'] > risk['max_total_weight']:
            raise ValueError('max_stock_weight cannot exceed max_total_weight')
        if not isinstance(risk['initial_cash'], (int, float)) or isinstance(risk['initial_cash'], bool) or not 10_000 <= risk['initial_cash'] <= 100_000_000:
            raise ValueError('initial_cash must be in [10000,100000000]')
        fusion = {**DEFAULT_FUSION, **params.get('fusion', {})}
        if set(fusion) - set(DEFAULT_FUSION) or fusion['mode'] not in {'factor_baseline', 'kronos_filter', 'rank_fusion'}:
            raise ValueError('Invalid fusion configuration')
        if fusion['mode'] == 'rank_fusion' and not params.get('selection', {}).get('fusion_validation'):
            raise ValueError('Weighted fusion requires saved out-of-sample validation evidence')
        if not 0 <= fusion['kronos_weight'] <= 1 or not 1 <= fusion['candidate_count'] <= 1000:
            raise ValueError('Invalid fusion weight or candidate count')
        payload = dict(schema='research_strategy_release_v1', created_at=datetime.now().astimezone().isoformat(),
            name=str(params.get('name') or '阶段研究主模型'), model_id=model['model_id'], model_version=model['version'],
            series_id=series['series_id'] if series else None, series_version=series['version'] if series else None,
            study_id=params.get('study_id'), protocol=params.get('protocol') or {}, selection=params.get('selection') or {},
            universe=self._pin_universe(params.get('universe') or {}), fusion=fusion, risk=risk,
            information_cutoff=model['information_cutoff'], read_ref=model.get('training_read_ref', model.get('read_ref')),
            qualification=dict(research_only=True, stage='provisional_research_active', strict_history_eligible=False,
                               paper_allocation_eligible=False, independent_forward_status='pending'),
            performance_scope='Protocol historical results do not constitute independent results for these refitted weights.')
        version = hashlib.sha256(canonical(payload).encode()).hexdigest()
        release = dict(payload, release_id='release-' + version[:32], version=version)
        with self._connect() as conn:
            folder = self.directory / release['release_id']
            if folder.is_symlink():
                raise ValueError('Strategy release folder cannot be a symlink')
            folder.mkdir(exist_ok=True)
            path = folder / 'manifest.json'
            if path.exists() and json.loads(path.read_text(encoding='utf-8')) != release:
                raise ValueError('Cannot overwrite a frozen release')
            path.write_text(canonical(release), encoding='utf-8')
            conn.execute('INSERT OR IGNORE INTO releases VALUES (?,?)', (release['release_id'], canonical(release)))
            conn.commit()
        return self.read(release['release_id'])

    def activate(self, release_id, *, reason='User selected provisional research default', expected_current=None):
        release = self.read(release_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('An activation reason is required')
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT release_id FROM activations ORDER BY sequence DESC LIMIT 1').fetchone()
            previous = row[0] if row else None
            if expected_current is not None and previous != expected_current:
                raise ValueError('Active research release changed; refresh before switching')
            if previous != release_id:
                conn.execute('INSERT INTO activations(release_id,previous_id,at,reason) VALUES (?,?,?,?)',
                             (release_id, previous, datetime.now().astimezone().isoformat(), reason.strip()))
            conn.commit()
        return release

    def resolve(self, params):
        identity = params.get('release_id')
        if identity and identity != 'active':
            release = self.read(identity)
        elif identity == 'active' or not params.get('model_id'):
            release = self.active()
            if release is None:
                raise ValueError('No active research release; train and publish a stage model first')
        else:
            return None
        if params.get('model_id') and params['model_id'] != release['model_id']:
            raise ValueError('Requested model differs from the frozen strategy release')
        for key in ['series_id', 'study_id']:
            if params.get(key) and params[key] != release.get(key):
                raise ValueError('Requested ' + key + ' differs from the frozen strategy release')
        return release

    def from_study(self, report):
        identity = report.get('study_id', report.get('run_id'))
        for row in self.list()['releases']:
            if row.get('study_id') == identity and row['model_id'] == report['best_model_id']:
                return self.read(row['release_id'])
        return self.create(dict(model_id=report['best_model_id'], series_id=report['series_id'],
            study_id=identity, protocol=report.get('selected_protocol', report.get('protocol', {})),
            selection=report.get('selection', {}), universe=report.get('universe', {}),
            risk={key: report.get('spec', {}).get(key, value) for key, value in DEFAULT_RISK.items()}))
