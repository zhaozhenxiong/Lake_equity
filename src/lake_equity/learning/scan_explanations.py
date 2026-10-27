"""Read-only exact explanations and explicit immutable attachments for old scans."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

import numpy as np
import pandas as pd

from .registry import ModelRegistry, canonical, digest


def quote_context(frame, as_of, symbols):
    """Reuse the scan's already-read bars, with the same quote qualification as fusion."""
    from .dataset import _valid_bars
    from .fusion import _state
    result = {}
    for symbol in symbols:
        bars = frame.loc[frame.symbol.eq(symbol) & frame.date.le(as_of)].sort_values('date')
        if 'trade_date' in bars:
            bars = bars.loc[bars.trade_date.notna()]
        bars = bars.tail(21)
        exact = bars.loc[bars.date.eq(as_of)]
        quote = dict(reference_close=None, reference_date=as_of, prior_volume=None, atr_pct=None,
                     quote_usable=False, quote_reason='missing_signal_session_bar', source=None)
        if len(exact) == 1 and {'open', 'high', 'low', 'close', 'volume', 'amount', 'source'} <= set(exact):
            row = exact.iloc[0]
            observed = row.to_dict()
            flagged = any(_state(observed, key) is True for key in
                          ('corporate_action_suspected', 'wyckoff_corporate_action_suspected', 'suspended', 'is_suspended'))
            invalid = any(_state(observed, key) is False for key in ('bar_valid', 'has_trade_price'))
            valid = bool(_valid_bars(exact).iloc[0]) and not flagged and not invalid
            quote.update(quote_usable=valid, quote_reason=None if valid else 'invalid_unverified_or_corporate_action_bar',
                         source=str(row.source))
            if valid:
                prior_close = bars.close.shift(1)
                tr = pd.concat([bars.high - bars.low, (bars.high - prior_close).abs(),
                                (bars.low - prior_close).abs()], axis=1).max(axis=1)
                atr = float(tr.tail(20).mean() / row.close)
                quote.update(reference_close=float(row.close), prior_volume=float(row.volume),
                             atr_pct=atr if np.isfinite(atr) else None)
        result[symbol] = quote
    return result


def source_identity(report, model, scores_sha256):
    return dict(run_id=report['run_id'], model_id=model['model_id'], model_version=model['version'],
        release_id=report.get('release_id'), read_ref=report['read_ref'], as_of=report['as_of'],
        feature_history_start=report['feature_history_start'],
        preprocessing_sha256=model['artifacts']['preprocessing.json'],
        model_sha256=model['artifacts']['model.json'], scores_sha256=scores_sha256,
        source_evidence_sha256=hashlib.sha256(canonical(report['sources']).encode()).hexdigest())


def save_explanations(directory, report, model, rows, *, scores_sha256=None):
    supported = model['model_type'] == 'ridge'
    metadata = dict(status='saved' if supported else 'unsupported',
        capability='exact_additive' if supported else 'unsupported',
        schema='ridge_local_contributions_v1' if supported else None, count=len(rows))
    if supported:
        path = Path(directory) / 'explanations.json'
        payload = dict(schema=metadata['schema'], capability=metadata['capability'],
            source=source_identity(report, model, scores_sha256 or digest(Path(directory) / 'scores.parquet')), rows=rows)
        path.write_text(canonical(payload), encoding='utf-8')
        metadata.update(artifact=path.name, sha256=digest(path))
    else:
        metadata['reason'] = 'model_type_has_no_verified_local_contribution'
    return metadata


class ScanExplanationService:
    def __init__(self, root):
        from .workflow import LearningWorkflow
        self.root = Path(root).resolve()
        self.workflow = LearningWorkflow(root)
        self.registry = ModelRegistry(root)
        self.directory = self.root / 'var/learning/scan_explanations'
        self.index = self.root / 'var/learning/scan_explanations.db'

    def _report(self, run_id):
        report = self.workflow.read_run(run_id)
        if report.get('family') != 'model_scan_v1' or report.get('kind') != 'scan':
            raise ValueError('Explanations require a saved model scan')
        return report

    def _attachment(self, run_id):
        if not self.index.is_file():
            return None
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT id,report_sha256 FROM attachments WHERE source_run_id=? ORDER BY rowid DESC LIMIT 1',
                               (run_id,)).fetchone()
        if row is None:
            return None
        identity, sha = row
        return self._attachment_metadata(identity, sha, run_id)

    def _attachment_metadata(self, identity, sha, source_run_id):
        if not re.fullmatch(r'explanation-[0-9a-f]{32}', identity):
            raise ValueError('Invalid explanation attachment identity')
        folder = self.directory / identity
        path = folder / 'report.json'
        if self.directory.is_symlink() or folder.is_symlink() or path.is_symlink() or digest(path) != sha:
            raise ValueError('Explanation attachment report SHA256 mismatch')
        result = json.loads(path.read_text(encoding='utf-8'))
        if result.get('run_id') != identity or result.get('source_run_id') != source_run_id:
            raise ValueError('Explanation attachment source identity mismatch')
        return folder, result

    def read_run(self, attachment_id):
        """Reopen a task result without rebuilding or mutating the original scan."""
        if not isinstance(attachment_id, str) or not re.fullmatch(r'explanation-[0-9a-f]{32}', attachment_id):
            raise ValueError('Invalid explanation attachment identity')
        if not self.index.is_file():
            raise KeyError(attachment_id)
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT source_run_id,report_sha256 FROM attachments WHERE id=?',
                               (attachment_id,)).fetchone()
        if row is None:
            raise KeyError(attachment_id)
        folder, result = self._attachment_metadata(attachment_id, row[1], row[0])
        original = self._report(row[0])
        if digest(self.workflow.directory / row[0] / 'report.json') != result['source_report_sha256']:
            raise ValueError('Explanation original report SHA256 mismatch')
        metadata = result['explanation']
        if metadata.get('artifact') != 'explanations.json':
            raise ValueError('Invalid explanation artifact path')
        path = folder / metadata['artifact']
        if path.is_symlink() or digest(path) != metadata['sha256']:
            raise ValueError('Explanation artifact SHA256 mismatch')
        model = self.registry.read_model(original['model_id'])
        payload = json.loads(path.read_text(encoding='utf-8'))
        if (payload.get('source') != source_identity(original, model, original['artifacts']['scores.parquet'])
                or payload.get('schema') != 'ridge_local_contributions_v1'
                or len(payload.get('rows', {})) != metadata['count']):
            raise ValueError('Explanation source/model/input identity mismatch')
        return result

    def list_runs(self, limit=100):
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError('Explanation list limit must be positive or None')
        if not self.index.is_file():
            return {'reports': []}
        with closing(sqlite3.connect(self.index.as_uri() + '?mode=ro', uri=True)) as conn:
            rows = conn.execute('SELECT id,source_run_id,report_sha256 FROM attachments ORDER BY rowid DESC LIMIT ?',
                                (-1 if limit is None else limit,)).fetchall()
        summaries = []
        for identity, source_id, sha in rows:
            _, result = self._attachment_metadata(identity, sha, source_id)
            summary = {key: result.get(key) for key in ('run_id', 'family', 'kind', 'status', 'created_at',
                       'source_run_id', 'model_id', 'release_id', 'read_ref', 'as_of')}
            summary.update(id=identity, title='模型评分解释附件', explanation_count=result['explanation']['count'])
            summaries.append(summary)
        return {'reports': summaries}

    def build(self, run_id, progress=None, check_cancel=None):
        """Explicitly rebuild at most 6,000 observations, publishing only a complete match."""
        from . import dataset, features
        from .models import check_stop, explain_saved
        report = self._report(run_id)
        source_path = self.workflow.directory / run_id / 'report.json'
        source_sha = digest(source_path)
        model = self.registry.read_model(report['model_id'])
        if model['version'] != report['model_version']:
            raise ValueError('Explanation frozen model version mismatch')
        if model['model_type'] != 'ridge':
            return dict(kind='scan_explanation', status='unsupported', source_run_id=run_id,
                        reason='model_type_has_no_verified_local_contribution')
        records = [row for row in report['rows'] if row['status'] == 'scored']
        if not records or len(records) > 6000 or len({row['symbol'] for row in records}) != len(records):
            raise ValueError('Explicit explanation build requires 1..6000 unique scored observations')
        check_stop(check_cancel)
        if report.get('explanation'):
            self.read(run_id, records[0]['symbol'])
            return dict(kind='scan_explanation', status='completed', run_id=run_id,
                        source_run_id=run_id, explanation=report['explanation'])
        previous = self._attachment(run_id)
        if previous:
            self.read(run_id, records[0]['symbol'])
            return previous[1]
        implementation = {path.name: digest(path) for path in (Path(dataset.__file__), Path(features.__file__))}
        if any(report.get('inference_implementation_sha256', {}).get(name) != sha
               for name, sha in implementation.items()):
            raise ValueError('Original feature implementation evidence is missing or changed')
        session = dataset.DatasetSession(self.root, report['read_ref'], check_cancel)
        if session.snapshot['read_ref'] != report['read_ref']:
            raise ValueError('Explanation pinned source read_ref mismatch')
        explained = {}
        for offset in range(0, len(records), 200):
            check_stop(check_cancel)
            batch = records[offset:offset + 200]
            symbols = [row['symbol'] for row in batch]
            data = dataset.build_dataset(self.root, dict(symbols=symbols, start=report['feature_history_start'],
                end=report['as_of'], horizon=model['horizon'], factor_groups=model['factor_groups']),
                session=session, check_cancel=check_cancel)
            frame = data['frame']
            selected = frame.loc[frame.date.eq(report['as_of']) & frame.feature_eligible.eq(True)
                                 & frame.legacy_ma_eligible.eq(True)].copy()
            if selected.symbol.duplicated().any() or set(selected.symbol) != set(symbols):
                raise ValueError('Original scored observation inputs are no longer reproducible')
            selected = selected.set_index('symbol', drop=False).loc[symbols].reset_index(drop=True)
            result = explain_saved(self.registry.directory_for(model['model_id']), selected,
                scores=[row['score'] for row in batch], feature_groups=data.get('feature_groups'))
            explained.update({row['symbol']: row for row in result['rows']})
            if progress:
                progress(dict(phase='scan_explanation', current=min(offset + 200, len(records)), total=len(records)))
        session.verify()
        for key, evidence in session.evidence.items():
            if report['sources'].get(str(key)) != evidence:
                raise ValueError('Explanation original input source evidence mismatch')
        check_stop(check_cancel)
        if digest(source_path) != source_sha or self.registry.read_model(model['model_id']) != model:
            raise ValueError('Explanation source changed during build')
        identity = 'explanation-' + uuid.uuid4().hex
        self.directory.mkdir(parents=True, exist_ok=True)
        working = self.directory / ('.' + identity + '.building')
        working.mkdir()
        metadata = save_explanations(working, report, model, explained,
                                     scores_sha256=report['artifacts']['scores.parquet'])
        result = dict(run_id=identity, kind='scan_explanation', family='scan_explanation_attachment_v1',
            status='completed', created_at=datetime.now().astimezone().isoformat(), source_run_id=run_id,
            model_id=model['model_id'], release_id=report.get('release_id'), read_ref=report['read_ref'], as_of=report['as_of'],
            source_report_sha256=source_sha, explanation=metadata, implementation_sha256=implementation)
        path = working / 'report.json'
        path.write_text(canonical(result), encoding='utf-8')
        sha = digest(path)
        working.rename(self.directory / identity)
        with closing(sqlite3.connect(self.index, timeout=30)) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,source_run_id TEXT NOT NULL,report_sha256 TEXT NOT NULL)')
            conn.execute('INSERT INTO attachments VALUES(?,?,?)', (identity, run_id, sha))
            conn.commit()
        return result

    def read(self, run_id, symbol):
        report = self._report(run_id)
        record = next((row for row in report['rows'] if row['symbol'] == symbol), None)
        if record is None:
            raise KeyError(symbol)
        source = dict(run_id=run_id, symbol=symbol, model_id=report['model_id'],
            model_version=report['model_version'], release_id=report.get('release_id'),
            read_ref=report['read_ref'], as_of=report['as_of'],
            source_report_sha256=digest(self.workflow.directory / run_id / 'report.json'))
        metadata = report.get('explanation')
        folder = self.workflow.directory / run_id
        attachment = None if metadata else self._attachment(run_id)
        if attachment:
            folder, attached = attachment
            if attached['source_report_sha256'] != source['source_report_sha256']:
                raise ValueError('Explanation original report SHA256 mismatch')
            metadata = attached['explanation']
        if not metadata or record['status'] != 'scored':
            return dict(status='not_saved', capability='not_saved', source=source,
                reason='original_report_has_no_saved_explanation' if record['status'] == 'scored' else 'row_was_not_scored')
        model = self.registry.read_model(report['model_id'])
        if model['version'] != report['model_version']:
            raise ValueError('Explanation frozen model version mismatch')
        source['preprocessing_sha256'] = model['artifacts']['preprocessing.json']
        if metadata['status'] == 'unsupported':
            return dict(status='unsupported', capability='unsupported', source=source, reason=metadata['reason'])
        if metadata.get('artifact') != 'explanations.json':
            raise ValueError('Invalid explanation artifact path')
        path = folder / metadata['artifact']
        if path.is_symlink() or digest(path) != metadata['sha256']:
            raise ValueError('Explanation artifact SHA256 mismatch')
        payload = json.loads(path.read_text(encoding='utf-8'))
        expected = source_identity(report, model, report['artifacts']['scores.parquet'])
        if payload.get('source') != expected or payload.get('schema') != 'ridge_local_contributions_v1':
            raise ValueError('Explanation source/model/input identity mismatch')
        row = payload['rows'].get(symbol)
        if row is None or row.get('symbol') != symbol or row.get('score') != record['score']:
            raise ValueError('Explanation original score mismatch')
        return dict(status='saved', capability='exact_additive', schema=payload['schema'], source=source, **row)
