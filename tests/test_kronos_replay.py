"""Causal replay observations over an isolated published lake, without model inference."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pandas as pd
import polars as pl
import pytest

from lake_equity.jobs import JobCancelled
from lake_equity.learning.kronos_replay import KronosReplayService, _factor_reason
from test_learning_dataset import lake, publish


@pytest.fixture
def replay(lake, monkeypatch):
    from my_strategy.services import kronos_assets, kronos_prediction
    root, store, days, bars = lake
    calendar = pd.bdate_range(days[0], periods=len(days) + 10).date.tolist()
    publish(store, 'trading_calendar', pl.DataFrame({'trade_date': calendar, 'is_trading': True}))
    snapshot = store.capture()
    monkeypatch.setattr(kronos_assets, 'asset_status', lambda: {'ready': True, 'files': [{'sha256': 'fixture'}]})
    monkeypatch.setattr(kronos_prediction, '_source_identity', lambda: {'fixture': 'source'})
    calls, behavior = [], {}

    def predict(frame, future, **kwargs):
        day = kwargs['as_of']
        calls.append((day, frame.copy()))
        assert frame.date.max() == day
        assert all(frame.date <= day)
        if behavior.get('fail') == day:
            raise RuntimeError('synthetic inference failure')
        close = float(frame.close.iloc[-1])
        usable = behavior.get('unusable') != day
        return dict(cache_key=hashlib.sha256((day + str(kwargs)).encode()).hexdigest(),
                    symbol=kwargs['symbol'], as_of=day,
                    forecast=[dict(date=value, close=close * 1.01, close_p10=close * .98,
                                   close_p90=close * 1.03) for value in future],
                    summary={'terminal_return_median': .01}, quality={'prediction_usable': usable},
                    identity={'assets': 'fixture'}, input={'last_close': close},
                    resource={'actual_device': 'cpu', 'elapsed_seconds': .01, 'timings': {'inference_seconds': .01}})

    monkeypatch.setattr(kronos_prediction, 'predict', predict)
    request = dict(days=2, symbols=['600000.SH'], lookback=16, device='cpu', read_ref=snapshot['read_ref'])
    return root, store, days, bars, KronosReplayService(root), request, calls, behavior


def test_preview_is_not_inference_and_has_exact_mature_calendar_windows(replay):
    root, _, days, _, service, request, calls, _ = replay
    for count in (20, 30):
        preview = service.preview({**request, 'days': count})
        assert preview['status'] == 'ready'
        assert preview['signal_days'] == [str(day) for day in days[-count - 1:-1]]
        assert preview['coverage']['requested'] == count
        assert preview['rows'][-1]['target_day'] == str(days[-1])
        assert preview['request']['read_ref'] == request['read_ref']
        assert preview['qualification']['live_eligible'] is False
    assert not calls
    assert not (root / 'var/learning/kronos_replays.db').exists()
    assert not service.directory.exists()


def test_preview_request_pins_the_exact_execution_contract(replay):
    _, _, _, _, service, request, _, _ = replay
    preview = service.preview(request)
    report = service.run(preview['request'])
    assert report['run_id'] == preview['experiment_id']
    assert report['contract'] == preview['contract']
    assert service.list_runs(limit=None)['reports'][0]['days'] == 2
    assert service.list_runs(limit=None)['reports'][0]['horizon'] == 1


def test_budget_resume_preserves_attempts_and_close_to_close_evaluation(replay):
    root, _, days, bars, service, request, calls, _ = replay
    sentinel = root / 'var/learning/forward.db'
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b'untouched live ledger')
    first = service.run({**request, 'max_calls': 1})
    assert first['status'] == 'partial'
    assert first['coverage']['succeeded'] == first['coverage']['deferred'] == 1
    original = {path: path.read_bytes() for path in service.directory.rglob('attempt-*.json')}
    final = service.resume(first['run_id'], {'max_calls': 5})
    assert final['status'] == 'completed' and len(calls) == 2
    assert final['coverage']['requested'] == final['coverage']['succeeded'] == 2
    row = final['rows'][-1]
    previous_close = float(bars.close.iloc[-2])
    actual_close = float(bars.close.iloc[-1])
    assert row['realized']['return'] == pytest.approx(actual_close / previous_close - 1)
    assert row['realized']['price_error'] == pytest.approx(previous_close * 1.01 - actual_close)
    assert row['realized']['status'] == 'verified'
    assert all(path.read_bytes() == content for path, content in original.items())
    assert sentinel.read_bytes() == b'untouched live ledger'
    assert service.run(request)['run_id'] == final['run_id']
    assert len(calls) == 2
    assert service.read(final['run_id']) == final
    assert service.list_runs()['total'] == 1


def test_unusable_and_failed_observations_remain_visible_and_only_failed_is_retried(replay):
    _, _, days, _, service, request, calls, behavior = replay
    behavior.update(unusable=str(days[-3]), fail=str(days[-2]))
    first = service.run(request)
    assert first['coverage']['unusable'] == first['coverage']['failed'] == 1
    behavior.clear()
    final = service.resume(first['run_id'])
    assert final['coverage']['unusable'] == final['coverage']['succeeded'] == 1
    assert len(calls) == 3  # The original unusable observation is immutable.
    assert final['rows'][0]['realized']['status'] == 'not_evaluated'


def test_latest_signal_without_next_session_bar_is_pending(replay):
    _, _, days, _, service, request, _, _ = replay
    report = service.run({**request, 'include_latest': True})
    assert report['rows'][-1]['signal_day'] == str(days[-1])
    assert report['rows'][-1]['realized']['status'] == 'pending'
    assert report['coverage']['pending_realized'] == 1


def test_cancelled_replay_saves_partial_observation_and_resumes(replay):
    _, _, _, _, service, request, calls, _ = replay
    cancelled = service.run(request, check_cancel=lambda: bool(calls))
    assert cancelled['status'] == 'cancelled'
    assert cancelled['coverage']['deferred'] == 1
    assert service.resume(cancelled['run_id'])['coverage']['succeeded'] == 2


def test_resume_rejects_changed_contract_and_source_bytes(replay):
    _, store, _, _, service, request, calls, _ = replay
    first = service.run({**request, 'max_calls': 1})
    with pytest.raises(ValueError, match='contract|frozen'):
        service.resume(first['run_id'], {'seed': 7})
    generation = store.revisions.current_root('daily_bars')
    path = next(generation.rglob('*.parquet'))
    content = bytearray(path.read_bytes())
    content[len(content)//2] ^= 1
    path.write_bytes(content)
    with pytest.raises(ValueError, match='SHA256|source'):
        service.resume(first['run_id'])
    assert len(calls) == 1


def test_same_pinned_twenty_thirty_intersection_uses_observation_cache(replay):
    _, _, _, _, service, request, calls, _ = replay
    thirty = service.run({**request, 'days': 30})
    assert len(calls) == 30
    twenty = service.run({**request, 'days': 20})
    assert len(calls) == 30
    assert thirty['run_id'] != twenty['run_id']
    assert twenty['coverage']['cache_hits'] == 20


def test_read_rejects_observation_or_report_tampering(replay):
    _, _, _, _, service, request, _, _ = replay
    report = service.run(request)
    observation = service.directory / report['run_id'] / report['rows'][0]['observation']['path']
    observation.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='SHA256'):
        service.read(report['run_id'])


def test_failed_final_label_hash_check_does_not_publish_verified_metrics(replay, monkeypatch):
    from lake_equity.learning import kronos_replay as module
    _, store, days, _, service, request, _, _ = replay
    original = module._read

    def corrupt_after_label_read(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[2] == 'daily_bars' and args[4] == str(days[-1]):
            path = next(store.revisions.current_root('daily_bars').rglob('*.parquet'))
            content = bytearray(path.read_bytes())
            content[len(content)//2] ^= 1
            path.write_bytes(content)
        return result

    monkeypatch.setattr(module, '_read', corrupt_after_label_read)
    result = service.run(request)
    assert result['status'] == 'failed'
    assert result['coverage']['verified'] == 0
    assert not any(row.get('evaluation') for row in result['rows'])


def test_new_label_revision_does_not_change_or_repeat_original_predictions(replay):
    _, store, _, bars, service, request, calls, _ = replay
    original = service.run(request)
    artifacts = [row['observation'] for row in original['rows']]
    changed = bars.copy()
    changed.loc[changed.index[-1], 'close'] += .03
    publish(store, 'daily_bars', pl.from_pandas(changed))
    revised = service.resume(original['run_id'])
    assert len(calls) == 2
    assert [row['observation'] for row in revised['rows']] == artifacts
    assert revised['rows'][-1]['realized']['close'] != original['rows'][-1]['realized']['close']
    assert revised['rows'][-1]['evaluation_revision'] == 1
    assert revised['contract']['read_ref'] == original['contract']['read_ref']
    assert revised['evaluation_read_ref'] != original['evaluation_read_ref']


def test_assets_or_source_change_cannot_resume_the_original_contract(replay, monkeypatch):
    from my_strategy.services import kronos_prediction
    _, _, _, _, service, request, calls, _ = replay
    original = service.run({**request, 'max_calls': 1})
    monkeypatch.setattr(kronos_prediction, '_source_identity', lambda: {'fixture': 'changed'})
    with pytest.raises(ValueError, match='contract changed'):
        service.resume(original['run_id'])
    assert len(calls) == 1


def test_cli_preview_and_empty_history_are_read_only(replay, monkeypatch, capsys, tmp_path):
    from lake_equity.cli import main
    from lake_equity.settings import Settings
    root, _, _, _, _, request, calls, _ = replay
    spec = root / 'request.json'
    spec.write_text(json.dumps(request), encoding='utf-8')
    monkeypatch.setattr(Settings, 'configure', lambda self, **kwargs: None)
    monkeypatch.setattr(sys, 'argv', ['lake-equity', '--root', str(root), 'kronos-replay', 'preview', '--spec', str(spec)])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ready'
    assert not calls
    empty = root / 'empty-workspace'
    empty.mkdir()
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).parents[1] / 'run.py'),
                             '--root', str(empty), 'kronos-replay', 'list'],
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['total'] == 0
    assert not list(empty.iterdir())


@pytest.mark.parametrize('changes,reason', [
    ({'information_cutoff': '2026-10-22'}, 'information_cutoff_not_before_signal'),
    ({'horizon': 5}, 'horizon_mismatch'),
    ({'label_formula': 'close[T+horizon]/open[T+1]-1'}, 'label_target_mismatch'),
    ({'training_read_ref': 'different'}, 'data_snapshot_mismatch'),
])
def test_factor_pairs_require_cutoff_horizon_target_and_data_contract(changes, reason):
    model = dict(information_cutoff='2026-10-21', horizon=1,
                 label_formula='close[T+horizon]/close[T]-1', training_read_ref='same')
    member = dict(start='2026-10-22', end='2026-10-23')
    assert _factor_reason({**model, **changes}, member, '2026-10-22', 1, 'same') == reason
    assert _factor_reason(model, member, '2026-10-22', 1, 'same') is None


@pytest.mark.parametrize('params', [{'days': 5}, {'horizon': 20}, {'symbols': []}, {'samples': True},
                                  {'symbols': ['830000.BJ']}, {'budget_seconds': float('inf')}])
def test_invalid_replay_requests_fail_before_data_or_index_creation(tmp_path, params):
    service = KronosReplayService(tmp_path)
    with pytest.raises(ValueError):
        service.preview({**dict(days=20, symbols=['600000.SH'], device='cpu'), **params})
    assert not list(tmp_path.iterdir())
