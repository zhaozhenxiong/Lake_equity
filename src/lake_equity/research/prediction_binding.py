"""Bind a forecast to the saved analysis and the same verified input generation."""
from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing

from lake_equity.data import InputContract


def _chart_window(rows, lookback):
    if len(rows) < lookback:
        raise ValueError('Analysis history is shorter than the prediction lookback')
    values = []
    for row in rows[-lookback:]:
        values.append([str(row.get('time') or row.get('date'))[:10],
                       *[float(row.get(key, row.get('vol') if key == 'volume' else None))
                         for key in ('open', 'high', 'low', 'close', 'volume')]])
    if any(not all(math.isfinite(number) for number in row[1:]) for row in values):
        raise ValueError('Analysis history contains invalid numerical inputs')
    return values


def bind_analysis(bridge, analysis_run_id, request, manifest, engine):
    from my_strategy.adapters.czsc_adapter import load_bars
    from my_strategy.services.kronos_prediction import prepare_input

    analysis = bridge.read_report(analysis_run_id)
    if not bridge.results_db.is_file():
        raise ValueError('A saved analysis report with verified identity is required')
    with closing(bridge._read_connection(bridge.results_db)) as connection:
        if not bridge._table(connection, 'lake_research_runs'):
            raise ValueError('A saved analysis report with verified identity is required')
        row = connection.execute('SELECT kind FROM lake_research_runs WHERE run_id=?', (analysis_run_id,)).fetchone()
    if row is None or row['kind'] != 'analysis':
        raise ValueError('A saved analysis report is required for prediction overlay')
    current = InputContract(bridge.raw_db).validate()
    if not manifest or manifest != current.manifest_id or analysis.get('input_manifest_id') != manifest:
        raise ValueError('Analysis input manifest differs from the current frozen input; reanalyze before predicting')
    if analysis.get('symbol') != request['symbol']:
        raise ValueError('Analysis prediction symbol mismatch')
    cutoff = analysis.get('as_of') or analysis.get('data_end')
    if not cutoff or request.get('end') != cutoff or analysis.get('data_end', cutoff) != cutoff:
        raise ValueError('Analysis prediction cutoff mismatch')
    lookback, horizon = request.get('lookback', 128), request.get('horizon', 5)
    if type(lookback) is not int or not 16 <= lookback <= 512:
        raise ValueError('Prediction lookback must be an integer in 16..512')
    if type(horizon) is not int or horizon not in (1, 5):
        raise ValueError('Analysis prediction horizon must be 1 or 5 trading days')
    raw = load_bars(request['symbol'], end=cutoff, db_path=bridge.raw_db)
    if analysis.get('price_basis') != raw.attrs.get('price_basis'):
        raise ValueError('Analysis prediction price basis mismatch')
    dates, _ = engine.calendar()
    future = [day for day in dates if day > cutoff][:horizon]
    prepared, _, info = prepare_input(raw, future, symbol=request['symbol'], as_of=cutoff,
                                      lookback=lookback, horizon=horizon)
    saved_rows = (analysis.get('frequencies') or {}).get('日线', {}).get('bars') or analysis.get('bars') or []
    if saved_rows and saved_rows[-1].get('is_closed') is False:
        raise ValueError('Analysis prediction cutoff requires a closed daily bar')
    saved = _chart_window(saved_rows, lookback)
    actual_rows = [{**row, 'date': str(day)[:10]} for day, row in prepared.to_dict('index').items()]
    actual = _chart_window(actual_rows, lookback)
    if saved != actual or saved[-1][0] != cutoff:
        raise ValueError('Analysis prediction history window mismatch')
    window_sha = hashlib.sha256(json.dumps(saved, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    binding = dict(schema='analysis_prediction_binding_v1', analysis_run_id=analysis_run_id,
                   symbol=request['symbol'], as_of=cutoff, input_manifest_id=manifest,
                   input_sqlite_sha256=current.sqlite_sha256, price_basis=info['price_basis'],
                   lookback=lookback, input_window_sha256=window_sha,
                   history_input_sha256=info['data_sha256'], compatible=True)
    return binding, raw, info, current
