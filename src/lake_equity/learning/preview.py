"""Read-only effective-cohort preview for watchlist/backtest submissions.

This endpoint never creates a workflow run, never scores and never trades.  It
only resolves the frozen method identity, the verified session window and the
per-symbol eligibility so a user can see the effective pool *before* submitting.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import json
import re

from .dataset import DatasetSession
from .models import check_stop
from .registry import ModelRegistry
from .releases import ReleaseRegistry

ALLOWED = {'mode', 'release_id', 'expected_release_id', 'scope', 'symbols', 'start', 'end',
           'universe_scope', 'read_ref', 'model_id', 'series_id'}
MODES = {'frozen', 'research_replay', 'watchlist_replay'}
SCOPES = {'symbols', 'all'}
UNIVERSE_SCOPES = {'release', 'explicit_symbols'}
SYMBOL = re.compile(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)')
MAX_PREVIEW_SYMBOLS = 200


def _day(value, name):
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise ValueError(name + ' must be an ISO date')
    return value


def _next_day(value):
    parsed = date.fromisoformat(value)
    return date.fromordinal(parsed.toordinal() + 1).isoformat()


def _symbols(value):
    if not isinstance(value, list):
        raise ValueError('symbols must be a list of Shanghai/Shenzhen A-share codes')
    codes = []
    for item in value:
        code = str(item or '').strip().upper()
        if not code:
            continue
        if not SYMBOL.fullmatch(code):
            raise ValueError('Unsupported symbol in preview: ' + code)
        if code not in codes:
            codes.append(code)
    if not codes:
        raise ValueError('Provide at least one symbol to preview')
    if len(codes) > MAX_PREVIEW_SYMBOLS:
        raise ValueError('Preview accepts at most %d symbols' % MAX_PREVIEW_SYMBOLS)
    return codes


class BacktestPreview:
    def __init__(self, root):
        self.root = Path(root).resolve()

    # -- helpers -----------------------------------------------------------
    def _sessions(self, session, end):
        from .workflow import LearningWorkflow
        return LearningWorkflow(self.root)._sessions(session, end, {})

    def _release(self, body):
        registry = ReleaseRegistry(self.root)
        requested = body.get('release_id') or ''
        if body.get('expected_release_id'):
            requested = body.get('expected_release_id')
        release = registry.resolve({'release_id': requested or 'active'})
        if body.get('expected_release_id') and release['release_id'] != body['expected_release_id']:
            raise ValueError('默认方法已变更，请刷新后重试')
        if body.get('release_id') and body['release_id'] != 'active' and release['release_id'] != body['release_id']:
            raise ValueError('Requested release differs from the frozen strategy release')
        for key in ('model_id', 'series_id'):
            if body.get(key) and release.get(key) and body[key] != release[key]:
                raise ValueError('Requested ' + key + ' differs from the frozen strategy release')
        return release

    def _pinned_universe(self, read_ref, policy, start, end, last_session=None):
        directory = self.root / 'var/learning/universes'
        if not directory.is_dir():
            return None
        accepted_ends = {end}
        if last_session:
            accepted_ends.add(last_session)
        best = None
        for manifest in sorted(directory.glob('*/manifest.json')):
            try:
                payload = json.loads(manifest.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            if payload.get('read_ref') != read_ref or payload.get('requested_start') != start:
                continue
            if payload.get('requested_end') not in accepted_ends:
                continue
            if policy and payload.get('policy') != policy:
                continue
            best = payload.get('manifest_path') or str(manifest)
        return best

    # -- public ------------------------------------------------------------
    def preview(self, params):
        body = dict(params or {})
        unknown = set(body) - ALLOWED
        if unknown:
            raise ValueError('Unknown preview parameters: ' + ', '.join(sorted(unknown)))
        mode = body.get('mode') or 'frozen'
        if mode not in MODES:
            raise ValueError('Unknown preview mode: ' + str(mode))
        scope = body.get('scope') or 'symbols'
        if scope not in SCOPES:
            raise ValueError('scope must be all or symbols')
        universe_scope = body.get('universe_scope') or 'release'
        if universe_scope not in UNIVERSE_SCOPES:
            raise ValueError('universe_scope must be release or explicit_symbols')
        start, end = _day(body.get('start'), 'start'), _day(body.get('end'), 'end')
        if start > end:
            raise ValueError('start must not exceed end')
        symbols = _symbols(body.get('symbols')) if scope == 'symbols' else []
        if universe_scope == 'explicit_symbols':
            if scope != 'symbols' or not symbols:
                raise ValueError('explicit_symbols universe_scope requires an explicit non-empty symbol pool')
            if mode not in MODES:
                raise ValueError('explicit_symbols is not supported for this mode')

        release = self._release(body)
        registry = ModelRegistry(self.root)
        model_id = release['model_id'] if release else body.get('model_id')
        if not model_id:
            raise ValueError('A frozen model identity is required for preview')
        information_cutoff = release.get('information_cutoff') if release else registry.read_model(model_id)['information_cutoff']
        policy = (release or {}).get('universe', {}).get('policy') or None

        if mode == 'watchlist_replay':
            return self._replay(body, release, registry, symbols, start, end)

        model = registry.read_model(model_id)
        session = DatasetSession(self.root, body.get('read_ref'))
        calendar = self._sessions(session, end)
        earliest = start
        if mode == 'frozen':
            earliest = max(start, _next_day(str(information_cutoff)[:10]))
        sessions = [day for day in calendar if earliest <= day <= end]
        blockers, warnings = [], []
        if not sessions:
            blockers.append('no verified trading session inside the requested window after the information cutoff')
        response = dict(status='blocked' if blockers else 'ready',
                        read_ref=session.snapshot['read_ref'],
                        release_identity=self._identity(release),
                        information_cutoff=str(information_cutoff)[:10] if information_cutoff else None,
                        window=dict(requested_start=start, requested_end=end, earliest_start=earliest,
                                    first_session=sessions[0] if sessions else None,
                                    last_session=sessions[-1] if sessions else None,
                                    available_sessions=len(sessions)),
                        universe_scope=universe_scope, release_universe_policy=policy,
                        cap=(policy or {}).get('max_members') if universe_scope == 'release' else None,
                        per_symbol=[], counts=dict(requested=len(symbols), eligible=0, excluded=0),
                        warnings=warnings, blockers=blockers)
        if blockers:
            return response

        if universe_scope == 'explicit_symbols':
            from .universe import eligibility_for_symbols
            frame = eligibility_for_symbols(session, symbols, sessions[0], sessions[-1],
                                            policy=policy, check_cancel=None)
            rows = self._eligibility_rows(frame, symbols, len(sessions))
            response['cap_applied'] = False
            response['warnings'].append('explicit_symbols: qualification rules applied, max_members cap skipped')
        else:
            artifact = self._pinned_universe(session.snapshot['read_ref'], policy, start, end,
                                             sessions[-1] if sessions else None)
            if artifact is None:
                response['status'] = 'partial'
                response['warnings'].append('universe_build_required')
                rows = [dict(symbol=code, status='unknown', reason='universe_build_required',
                             rank=None, amount60_median=None, coverage252=None, coverage60=None,
                             eligible_sessions=0, sessions_checked=len(sessions)) for code in symbols]
            else:
                from .universe import metrics_for_symbols
                frame = metrics_for_symbols(artifact, sessions[0], sessions[-1], symbols)
                rows = self._eligibility_rows(frame, symbols, len(sessions), ranked=True,
                                              cap=(policy or {}).get('max_members'))
        response['per_symbol'] = rows
        response['counts'] = dict(requested=len(symbols),
                                  eligible=sum(1 for row in rows if row['status'] == 'eligible'),
                                  excluded=sum(1 for row in rows if row['status'] != 'eligible'))
        if response['counts']['eligible'] == 0 and not blockers:
            response['warnings'].append('no requested symbol is eligible inside the effective window')
        session.verify()
        return response

    def _replay(self, body, release, registry, symbols, start, end):
        if not release or not release.get('series_id'):
            raise ValueError('watchlist_replay requires the active release to expose a saved rolling series')
        series = registry.read_series(release['series_id'])
        members = sorted(series.get('members') or [], key=lambda row: row['start'])
        if not members:
            raise ValueError('Saved rolling series has no members')
        series_start, series_end = members[0]['start'], members[-1]['end']
        session = DatasetSession(self.root, body.get('read_ref'))
        calendar = self._sessions(session, end)
        earliest = max(start, series_start)
        latest = min(end, series_end)
        sessions = [day for day in calendar if earliest <= day <= latest]
        warnings, blockers = [], []
        if not sessions:
            blockers.append('requested window does not overlap the saved rolling-series coverage')
        from .service import LearningService
        try:
            saved = LearningService(self.root).read_run(series['run_id'])
        except (KeyError, ValueError, OSError) as error:
            raise ValueError('Saved rolling-series parent run is unavailable: ' + str(error))
        original = set(saved.get('spec', {}).get('symbols') or [])
        response = dict(status='ready' if sessions else 'blocked', read_ref=session.snapshot['read_ref'],
                        release_identity=self._identity(release),
                        information_cutoff=str(release.get('information_cutoff') or '')[:10] or None,
                        window=dict(requested_start=start, requested_end=end, earliest_start=earliest,
                                    series_start=series_start, series_end=series_end,
                                    first_session=sessions[0] if sessions else None,
                                    last_session=sessions[-1] if sessions else None,
                                    available_sessions=len(sessions)),
                        universe_scope='release', release_universe_policy=(release.get('universe') or {}).get('policy'),
                        cap=None,
                        per_symbol=[dict(symbol=code,
                                         status='available' if code in original else 'unavailable',
                                         reason=None if code in original else 'not_in_saved_evaluation_data',
                                         rank=None, amount60_median=None, coverage252=None, coverage60=None,
                                         eligible_sessions=len(sessions) if code in original else 0,
                                         sessions_checked=len(sessions)) for code in symbols],
                        warnings=warnings, blockers=blockers,
                        series=dict(series_id=series['series_id'], members=len(members),
                                    original_cohort_size=len(original)))
        response['counts'] = dict(requested=len(symbols),
                                  eligible=sum(1 for row in response['per_symbol'] if row['status'] == 'available'),
                                  excluded=sum(1 for row in response['per_symbol'] if row['status'] != 'available'))
        if response['counts']['eligible'] == 0 and not blockers:
            response['warnings'].append('no requested symbol exists in the saved evaluation data')
        session.verify()
        return response

    @staticmethod
    def _identity(release):
        if not release:
            return None
        return {key: release.get(key) for key in
                ('release_id', 'name', 'model_id', 'information_cutoff', 'series_id', 'protocol', 'risk', 'qualification')}

    @staticmethod
    def _eligibility_rows(frame, symbols, sessions_checked, ranked=False, cap=None):
        import pandas as pd
        rows = []
        if frame is None or getattr(frame, 'empty', True):
            return [dict(symbol=code, status='unavailable', reason='no_qualification_evidence', rank=None,
                         amount60_median=None, coverage252=None, coverage60=None,
                         eligible_sessions=0, sessions_checked=sessions_checked) for code in symbols]
        frame = pd.DataFrame(frame)
        for code in symbols:
            subset = frame.loc[frame.symbol.eq(code)]
            eligible = subset.loc[subset.eligible.fillna(False).astype(bool)] if len(subset) else subset
            latest = subset.iloc[-1] if len(subset) else {}
            rank = None
            if ranked and len(eligible) and 'rank' in eligible:
                rank = int(eligible.rank.iloc[-1])
            if len(eligible):
                if cap is not None and rank is not None and rank > int(cap):
                    status, reason = 'excluded', 'rank_beyond_cap'
                else:
                    status, reason = 'eligible', None
            else:
                status = 'unavailable' if not len(subset) else 'excluded'
                reason = str(subset.reason.iloc[-1]) if len(subset) else 'no_qualification_evidence'
            rows.append(dict(symbol=code, status=status, reason=reason, rank=rank,
                             amount60_median=_number(latest.get('amount60_median')),
                             coverage252=_number(latest.get('coverage252')),
                             coverage60=_number(latest.get('coverage60')),
                             eligible_sessions=int(len(eligible)), sessions_checked=sessions_checked))
        return rows


def _number(value):
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None
