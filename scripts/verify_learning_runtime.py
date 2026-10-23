"""Synthetic-only import, dataset, fit, predict and artifact smoke check."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import uuid

import numpy as np
import pandas as pd

from lake_equity.learning.models import fit_predict


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--backend', choices=['local', 'qlib'], default='qlib')
    args = parser.parse_args()
    root = args.root.resolve()
    directory = root / 'var/learning/runtime_checks' / uuid.uuid4().hex
    dates = pd.bdate_range('2020-01-01', periods=100).strftime('%Y-%m-%d')
    rng = np.random.default_rng(42)
    frame = pd.DataFrame([dict(date=day, symbol=symbol, x=float(rng.normal()), z=float(rng.normal()))
                          for day in dates for symbol in ('600000.SH', '600519.SH', '000001.SZ')])
    frame['label'] = .01*frame.x - .005*frame.z
    train, valid, test = (frame.loc[frame.date.isin(window)].copy() for window in (dates[:60], dates[60:80], dates[80:]))
    test.loc[test.date.eq(dates[-1]), 'label'] = np.nan
    results = {}
    for model in ('ridge', 'lightgbm'):
        scores, details = fit_predict(train, valid, test, ['x', 'z'], model_type=model,
            backend=args.backend, seed=42, directory=directory / model, root=root)
        assert len(scores) == len(test) and np.isfinite(scores).all()
        results[model] = dict(engine=details['engine'], rows=len(scores), versions=details.get('versions'),
                              retained_unmatured_inference=int(test.label.isna().sum()))
    if args.backend == 'qlib':
        python = root / 'var/runtimes/qlib' / ('Scripts/python.exe' if __import__('os').name == 'nt' else 'bin/python')
        freeze = subprocess.run([str(python), '-m', 'pip', 'freeze'], check=True, capture_output=True, text=True).stdout
        (python.parent.parent / 'requirements.lock').write_text(freeze, encoding='utf-8')
        (python.parent.parent / 'verified.json').write_text(json.dumps(dict(backend='qlib', synthetic_only=True,
            results=results, verification_path=str(directory)), indent=2), encoding='utf-8')
    print(json.dumps(dict(status='passed', synthetic_only=True, path=str(directory), results=results), indent=2))


if __name__ == '__main__':
    main()
