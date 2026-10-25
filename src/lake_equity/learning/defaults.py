"""Single-default-method helpers: resolve the active release, decide refits and
derive a default backtest window.

Nothing here activates a release, overwrites a release or runs a backtest; the
caller owns promotion and scheduling decisions.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from .registry import ModelRegistry
from .releases import ReleaseRegistry

SESSION_THRESHOLD = 20
DAY_THRESHOLD = 30


def _root(root) -> Path:
    return Path(root).resolve()


def _iso_date(value, name: str) -> date:
    if not isinstance(value, str) or date.fromisoformat(value[:10]).isoformat() != value[:10]:
        raise ValueError(name + ' must be an ISO date')
    return date.fromisoformat(value[:10])


def resolve_default(root) -> dict | None:
    """Return the active strategy release, or None when nothing is active."""
    return ReleaseRegistry(root).active()


def _calendar_days() -> list[str] | None:
    try:
        from my_strategy.services.czsc_trend_support import _calendar
        value = _calendar()
        days = value[0] if isinstance(value, tuple) else value
        result = []
        for day in days:
            text = day.strftime('%Y-%m-%d') if hasattr(day, 'strftime') else str(day)
            try:
                result.append(date.fromisoformat(text[:10]).isoformat())
            except (TypeError, ValueError):
                continue
        return sorted(set(result))
    except Exception:
        return None


def _weekdays_after(start: date, end: date) -> int:
    if end <= start:
        return 0
    count = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            count += 1
        current += timedelta(days=1)
    return count


def _sessions_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    calendar = _calendar_days()
    if calendar is None:
        return _weekdays_after(start, end)
    return sum(1 for day in calendar if start.isoformat() < day <= end.isoformat())


def refit_due(root, release, target_date) -> dict:
    """Return whether the active protocol is due for a scheduled weight refit."""
    if not isinstance(release, dict):
        raise ValueError('A strategy release is required')
    target = _iso_date(target_date, 'target_date')
    cutoff = _iso_date(release.get('information_cutoff'), 'release information_cutoff')
    created_raw = str(release.get('created_at') or '')[:10]
    try:
        created = date.fromisoformat(created_raw) if created_raw else None
    except ValueError:
        created = None
    sessions = _sessions_between(cutoff, target)
    days = max((target - created).days, 0) if created else 0
    due = sessions >= SESSION_THRESHOLD and days >= DAY_THRESHOLD
    if due:
        reason = 'refit_window_reached'
    elif sessions < SESSION_THRESHOLD:
        reason = 'insufficient_sessions_since_information_cutoff'
    else:
        reason = 'insufficient_days_since_release'
    return dict(due=due, sessions_since_cutoff=sessions, days_since_release=days, reason=reason)


def refit_and_candidate(root, release, target_date, progress=None, check_cancel=None) -> dict:
    """Refit the active protocol into new weights and publish a candidate release.

    The candidate is never activated automatically; the caller decides whether
    to activate it. An existing immutable release/model identity is never
    overwritten because both registries refuse collisions.
    """
    result = dict(report_run_id=None, model_id=None, release_id=None, error=None)
    try:
        from .refit import refit_model
        from .service import LearningService

        root = _root(root)
        registry = ModelRegistry(root)
        model = registry.read_model(release['model_id'])
        source_run_id = model['run_id']
        if model.get('usage') == 'frozen_inference_only':
            parent = LearningService(root).read_run(model['run_id'])
            if parent.get('purpose') == 'final_refit':
                source_run_id = parent.get('parent_run_id') or source_run_id
        model_name = (release.get('protocol') or {}).get('model_name')
        report = refit_model(root, dict(run_id=source_run_id, model_name=model_name, end=target_date),
                             progress=progress, check_cancel=check_cancel)
        result['report_run_id'] = report['run_id']
        candidates = [row for row in registry.list_models(include_archived=True)['models']
                      if row.get('run_id') == report['run_id']]
        if len(candidates) != 1:
            raise ValueError('Published refit run must contain exactly one frozen model')
        result['model_id'] = candidates[0]['model_id']
        try:
            created = ReleaseRegistry(root).create(dict(
                model_id=result['model_id'], series_id=release.get('series_id'),
                study_id=release.get('study_id'), protocol=release.get('protocol') or {},
                selection=dict(purpose='pipeline_refit'), universe=release.get('universe') or {},
                risk=release['risk'], name='默认方法权重刷新'))
            result['release_id'] = created['release_id']
        except Exception as error:  # Candidate weights stay usable even if publication fails.
            result['error'] = str(error)
    except Exception as error:
        from lake_equity.jobs import JobCancelled
        if isinstance(error, JobCancelled):
            raise
        result['error'] = str(error)
    return result


def default_window(root, release, target_date) -> dict:
    """Return the default backtest window implied by the active release."""
    if not isinstance(release, dict):
        raise ValueError('A strategy release is required')
    target = _iso_date(target_date, 'target_date')
    series_id = release.get('series_id')
    if series_id:
        series = ModelRegistry(root).read_series(series_id)
        members = [row for row in series.get('members') or [] if row.get('start') and row.get('end')]
        if members:
            start = min(row['start'] for row in members)
            end = max(row['end'] for row in members)
            if end > target.isoformat():
                end = target.isoformat()
            return dict(mode='historical', start=start, end=end, reason='saved_rolling_series')
    cutoff = _iso_date(release.get('information_cutoff'), 'release information_cutoff')
    start = (cutoff + timedelta(days=1)).isoformat()
    return dict(mode='frozen', start=start, end=target.isoformat(), reason='no_saved_rolling_series')
