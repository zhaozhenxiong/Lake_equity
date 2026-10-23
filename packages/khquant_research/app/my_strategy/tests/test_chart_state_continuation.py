"""Chart session state continuation: day-append resume, byte-exact payloads.

dump_chart_session/restore_chart_session carry the engine truth (geometry
SHA re-verified from retained bars) plus confirmation bookkeeping; the chart
cache resumes the newest verified strict-prefix state and recomputes
payload(None) wholesale, so resumed entries are complete payloads identical
to a full advance.
"""
import json

import pytest

from my_strategy.tests.test_chart_features_cache import _synthetic_frame, _advance
from my_strategy.services.czsc_analysis import CzscSession, strategy_config
from my_strategy.services.czsc_incremental_state import (
    StateNotRestorable, dump_chart_session, restore_chart_session)
from my_strategy.services.native_pack_cache import (
    _pack_path, chart_payload_identity, get_or_build_chart, put_pack)
from my_strategy.core.run_context import stable_hash
from my_strategy.web_dashboard.trend_strategy import _advance_chart_payload as _build_chart


def _json_roundtrip(value):
    return json.loads(json.dumps(value))


def _build(frame, resume_state=None):
    return _build_chart(frame, strategy_config(None), resume_state)


def test_restore_chart_session_payload_matches_fresh_advance():
    frame = _synthetic_frame(days=320)
    settings = strategy_config(None)
    prefix = _advance(frame.iloc[:260], settings)
    state = _json_roundtrip(dump_chart_session(prefix))
    session = restore_chart_session(state)
    for row in frame.iloc[260:].itertuples(index=False):
        session.advance_structure(row)
    fresh = _advance(frame, settings)
    assert session.payload(None) == fresh.payload(None)


def test_multi_hop_chart_restart_stays_exact():
    frame = _synthetic_frame(days=320)
    settings = strategy_config(None)
    session = None
    start = 0
    for end in (200, 260, 320):
        state = None if session is None else _json_roundtrip(dump_chart_session(session))
        session = restore_chart_session(state) if state else CzscSession(
            "000001.SZ", settings, end, create_position=False)
        for row in frame.iloc[start:end].itertuples(index=False):
            session.advance_structure(row)
        start = end
    assert session.payload(None) == _advance(frame, settings).payload(None)


def test_chart_state_tampering_is_rejected():
    frame = _synthetic_frame(days=140)
    state = dump_chart_session(_advance(frame, strategy_config(None)))
    state = _json_roundtrip(state)
    state["confirmations"]["日线"]["pens"][0][1] = "2000-01-01T00:00:00+08:00"
    with pytest.raises(StateNotRestorable, match="SHA"):
        restore_chart_session(state)


def test_chart_cache_day_append_resume_matches_full(tmp_path):
    frame = _synthetic_frame(days=320)
    settings = strategy_config(None)
    payload, origin = get_or_build_chart(frame.iloc[:260], settings, _build, root=tmp_path)
    assert origin == "compute"
    resumed, origin = get_or_build_chart(frame, settings, _build, root=tmp_path)
    assert origin == "compute_incremental"
    assert resumed == _advance(frame, settings).payload(None)
    again, origin = get_or_build_chart(frame, settings, _build, root=tmp_path)
    assert origin == "cache"
    assert again == resumed


def test_chart_cache_changed_prefix_rebuilds(tmp_path):
    frame = _synthetic_frame(days=320)
    settings = strategy_config(None)
    get_or_build_chart(frame.iloc[:260], settings, _build, root=tmp_path)
    changed = frame.copy()
    changed.loc[changed.index[40], "close"] = changed.close.iloc[40] * 1.01
    payload, origin = get_or_build_chart(changed, settings, _build, root=tmp_path)
    assert origin == "compute"
    assert payload == _advance(changed, settings).payload(None)


def test_chart_cache_missing_candidate_falls_back(tmp_path):
    frame = _synthetic_frame(days=320)
    settings = strategy_config(None)
    get_or_build_chart(frame.iloc[:260], settings, _build, root=tmp_path)
    identity = chart_payload_identity(frame.iloc[:260], settings)
    _pack_path(tmp_path, stable_hash(identity)).unlink()
    payload, origin = get_or_build_chart(frame, settings, _build, root=tmp_path)
    assert origin == "compute"
    assert payload == _advance(frame, settings).payload(None)


def test_chart_cache_reads_legacy_bare_payload_entries(tmp_path):
    frame = _synthetic_frame(days=140)
    settings = strategy_config(None)
    bare = _advance(frame, settings).payload(None)
    put_pack(chart_payload_identity(frame, settings), bare, root=tmp_path)

    def _must_not_build(frame, resume_state=None):
        raise AssertionError("legacy entry must be served from cache")

    payload, origin = get_or_build_chart(frame, settings, _must_not_build, root=tmp_path)
    assert origin == "cache"
    assert payload == bare
