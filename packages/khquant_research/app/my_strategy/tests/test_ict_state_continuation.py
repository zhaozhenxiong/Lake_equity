"""ICT evidence state continuation: day-append resume, byte-exact outputs.

ict_evidence captures its causal machines (active/invalidated supports, origin
ages, open setups, confirmed swings) in an ict_evidence_state_v1 state; the
native evidence builder wires it into native_evidence_state_v3 so an ICT resume
replays only the appended days instead of the full observed prefix, with the
v1/v2 legacy path (full replay + slice) preserved.
"""
import json

import numpy as np
import pandas as pd
import pytest

from my_strategy.tests.test_current_native_evidence import _raw
from my_strategy.core.run_context import stable_hash
from my_strategy.services.czsc_market_features import _validate_input
import my_strategy.services.czsc_native_evidence as ne


def _inputs(bars, dates):
    return (bars, np.ones(len(bars), dtype=bool), ne._prior_metrics(bars),
            ne._settings(None), ne._VerifiedCalendar(bars, dates))


def _json_roundtrip(value):
    return json.loads(json.dumps(value))


def test_ict_resume_delta_matches_full_replay():
    bars = _raw(340, varied=True)
    dates = bars.date.dt.strftime('%Y-%m-%d').tolist()
    sink = {}
    ne.ict_evidence(*_inputs(bars.iloc[:240], dates[:240]), state_sink=sink)
    state = _json_roundtrip(sink)
    resumed = ne.ict_evidence(*_inputs(bars, dates), resume_state=state, state_sink={})
    full = ne.ict_evidence(*_inputs(bars, dates))
    assert resumed[0] == [fact for fact in full[0] if fact['date'] >= dates[240]]
    assert resumed[1] == full[1][240:]
    assert resumed[2] == full[2][240:]


def test_ict_multi_hop_resume_stays_exact():
    bars = _raw(300, varied=True)
    dates = bars.date.dt.strftime('%Y-%m-%d').tolist()
    state = None
    facts, evals, daily = [], [], []
    for end in (180, 240, 300):
        sink = {}
        out = ne.ict_evidence(*_inputs(bars.iloc[:end], dates[:end]),
                              resume_state=_json_roundtrip(state) if state else None,
                              state_sink=sink)
        facts.extend(out[0]); evals.extend(out[1]); daily.extend(out[2])
        state = sink
    full = ne.ict_evidence(*_inputs(bars, dates))
    assert facts == full[0]
    assert evals == full[1]
    assert daily == full[2]


def test_ict_state_tampering_is_rejected():
    bars = _raw(240, varied=True)
    dates = bars.date.dt.strftime('%Y-%m-%d').tolist()
    sink = {}
    ne.ict_evidence(*_inputs(bars.iloc[:200], dates[:200]), state_sink=sink)
    state = _json_roundtrip(sink)
    assert state['active'] or state['setups'] or state['swings']
    if state['active']:
        key = next(iter(state['active']))
        state['active'][key]['support_low'] = 0.01
    else:
        state['swings'][0]['price'] = 0.01
    with pytest.raises(ValueError, match='SHA'):
        ne.ict_evidence(*_inputs(bars, dates), resume_state=state)


def test_ict_state_requires_strict_append():
    bars = _raw(220, varied=True)
    dates = bars.date.dt.strftime('%Y-%m-%d').tolist()
    sink = {}
    ne.ict_evidence(*_inputs(bars, dates), state_sink=sink)
    with pytest.raises(ValueError, match='strict'):
        ne.ict_evidence(*_inputs(bars, dates), resume_state=_json_roundtrip(sink))


def test_build_level_ict_resume_telemetry_and_parity():
    bars = _raw(300, varied=True)
    dates = bars.date.dt.strftime('%Y-%m-%d').tolist()
    prefix = ne.build_native_evidence(bars.iloc[:230], market_dates=dates[:230], capture_state=True)
    state = _json_roundtrip(prefix['incremental_state'])
    assert state['format'] == 'native_evidence_state_v3'
    resumed = ne.build_native_evidence(bars, market_dates=dates, incremental_state=state, capture_state=True)
    assert resumed['incremental']['ict_mode'] == 'state_machine_resume'
    assert resumed['incremental']['ict_rows_replayed'] == 0
    assert resumed['incremental']['ict_rows_appended'] == 70
    full = ne.build_native_evidence(bars, market_dates=dates)
    pd.testing.assert_frame_equal(resumed['frame'], full['frame'].iloc[230:], check_exact=True)
    assert resumed['evaluations'] == full['evaluations'][230:]
    assert resumed['events'] == [e for e in full['events'] if e['date'] >= str(bars.date.iloc[230].date())]


def test_legacy_state_without_ict_replays_and_stays_exact():
    bars = _raw(220, varied=True)
    dates = bars.date.dt.strftime('%Y-%m-%d').tolist()
    prefix = ne.build_native_evidence(bars.iloc[:170], market_dates=dates[:170], capture_state=True)
    state = _json_roundtrip(prefix['incremental_state'])
    state.pop('ict')
    state.pop('state_sha256')
    state['state_sha256'] = stable_hash(state)
    resumed = ne.build_native_evidence(bars, market_dates=dates, incremental_state=state, capture_state=True)
    assert resumed['incremental']['ict_mode'] == 'full_prefix_replay_sliced'
    assert resumed['incremental']['ict_rows_replayed'] == len(bars)
    full = ne.build_native_evidence(bars, market_dates=dates)
    pd.testing.assert_frame_equal(resumed['frame'], full['frame'].iloc[170:], check_exact=True)
    assert resumed['evaluations'] == full['evaluations'][170:]
    assert resumed['events'] == [e for e in full['events'] if e['date'] >= str(bars.date.iloc[170].date())]
