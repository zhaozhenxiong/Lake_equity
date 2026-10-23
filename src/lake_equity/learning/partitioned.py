"""Bounded symbol batches for feature preparation, with one pinned read."""
from __future__ import annotations

from pathlib import Path
import pandas as pd

from .dataset import DatasetSession, build_dataset


def build_partitioned(root, spec, directory, *, session=None, progress=None, check_cancel=None):
    session = session or DatasetSession(root, spec.get('read_ref'), check_cancel)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    symbols = spec['symbols']
    parts, reference, reports = [], None, []
    for offset in range(0, len(symbols), 200):
        batch = dict(spec, symbols=symbols[offset:offset + 200], read_ref=session.snapshot['read_ref'])
        data = build_dataset(root, batch, session=session, progress=progress, check_cancel=check_cancel)
        path = directory / f'batch-{offset // 200:04d}.parquet'
        data['frame'].to_parquet(path, index=False)
        parts.append(path)
        reports.append(data['report'])
        if reference is None:
            reference = {key: value for key, value in data.items() if key != 'frame'}
        if progress:
            progress(phase='dataset_partition', current=min(offset + 200, len(symbols)), total=len(symbols))
    session.verify()
    # Feature construction and raw lake scans are bounded. Numerical estimators
    # still consume the selected folds in memory; the preflight exposes this.
    frame = pd.concat([pd.read_parquet(path) for path in parts], ignore_index=True)
    report = reference['report']
    report.update(rows=len(frame), symbols_requested=symbols,
                  symbols_with_bars=sum(row['symbols_with_bars'] for row in reports),
                  valid_bar_rows=int(frame.bar_valid.sum()), feature_eligible_rows=int(frame.feature_eligible.sum()),
                  label_reason_counts={str(k): int(v) for k, v in frame.label_reason.value_counts().items()},
                  partition_count=len(parts), feature_batch_size=200,
                  estimator_memory_policy='in_memory_selected_folds_after_partitioned_features')
    for group, coverage in report['group_coverage'].items():
        values = [row['group_coverage'][group] for row in reports]
        for key in ['nonempty_rows', 'complete_rows', 'nonempty_values']:
            coverage[key] = sum(item[key] for item in values)
        coverage['row_coverage'] = coverage['nonempty_rows'] / len(frame)
        coverage['reason'] = 'observed_nonempty' if coverage['nonempty_rows'] else 'no_published_available_values_in_window'
    return dict(reference, frame=frame)
