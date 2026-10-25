"""Causal coverage/liquidity universes from one immutable lake read.

The current security catalogue is incomplete historically.  These are explicitly
limited research universes, not certified historical investable populations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date
import hashlib
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd
import polars as pl

from .dataset import BAR_COLUMNS, DatasetSession, RAW_SOURCES, _check, _read, _unique, _valid_bars
from .registry import canonical, digest


@dataclass(frozen=True)
class UniversePolicy:
    version: str = 'liquidity_coverage_v1'
    history_sessions: int = 252
    recent_sessions: int = 60
    min_coverage: float = .95
    min_recent_coverage: float = .95
    min_median_amount: float = 100_000_000.
    max_members: int = 1000

    def __post_init__(self):
        if self.version != 'liquidity_coverage_v1':
            raise ValueError('Unsupported universe policy version')
        for key, lower, upper in [('history_sessions', 2, 2520), ('recent_sessions', 2, 2520),
                                  ('max_members', 1, 6000)]:
            value = getattr(self, key)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f'{key} must be an integer between {lower} and {upper}')
        if self.recent_sessions > self.history_sessions:
            raise ValueError('recent_sessions cannot exceed history_sessions')
        for key in ('min_coverage', 'min_recent_coverage'):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
                raise ValueError(f'{key} must be in (0, 1]')
        if isinstance(self.min_median_amount, bool) or not isinstance(self.min_median_amount, (int, float)) or not np.isfinite(self.min_median_amount) or self.min_median_amount < 0:
            raise ValueError('min_median_amount must be a finite nonnegative CNY amount')

    @classmethod
    def from_dict(cls, value=None):
        if isinstance(value, cls):
            return value
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - {f.name for f in fields(cls)}:
            raise ValueError('Unknown universe policy fields')
        return cls(**value)

    def to_dict(self):
        return asdict(self)


QUALIFICATIONS = {
    'usage': 'limited_research',
    'historical_population_complete': False,
    'historical_execution_status_complete': False,
    'corporate_actions_reconciled': False,
    'point_in_time_certified': False,
    'limits': [
        'Observed historical bars plus current catalogue do not establish complete delisted population.',
        'Historical listing dates are reconstructed from the captured catalogue; absent dates use first observed bar as a conservative lower bound.',
        'Historical ST, suspension, permissions and delisting evidence are not certified; missing bars remain unknown.',
        'Raw OHLCV validity does not establish corporate-action adjusted total returns or executable prices.',
        'Dates describe historical observations; original publication timestamps and revisions are not fully certified.',
    ],
}


def _symbol_metrics(bars, calendar, listing, *, symbol, policy, start, end):
    """Full-calendar windows; every result at T uses only observations <= T."""
    _unique(bars, ['symbol', 'trade_date'], 'universe daily_bars')
    bars = bars.copy()
    bars.index = pd.to_datetime(bars.trade_date).dt.strftime('%Y-%m-%d')
    panel = bars.reindex(calendar)
    for key in BAR_COLUMNS:
        panel[key] = pd.to_numeric(panel[key], errors='coerce')
    valid = _valid_bars(panel)
    positions = np.arange(len(calendar))
    observed = panel.trade_date.notna()
    first_observed = next((i for i, present in enumerate(observed) if present), len(calendar))
    list_date = listing.get('list_date') if listing else None
    delist_date = listing.get('delist_date') if listing else None
    list_date = str(list_date)[:10] if pd.notna(list_date) and list_date is not None else None
    delist_date = str(delist_date)[:10] if pd.notna(delist_date) and delist_date is not None else None
    listed_at = np.searchsorted(calendar, list_date) if list_date else first_observed
    listed = positions >= listed_at
    if delist_date:
        listed &= np.asarray(calendar) <= delist_date
    valid &= listed
    history_count = valid.astype(int).rolling(policy.history_sessions, min_periods=1).sum()
    recent_count = valid.astype(int).rolling(policy.recent_sessions, min_periods=1).sum()
    # Unknown observations remain missing in the data and audit.  The decision
    # uses the nonnegative lower bound of the complete-window median, never the
    # inflated median of a shortened window with missing sessions squeezed out.
    amounts = panel.amount.where(valid)
    median = amounts.fillna(0).rolling(policy.recent_sessions, min_periods=policy.recent_sessions).median()
    observed_median = amounts.rolling(policy.recent_sessions, min_periods=1).median()
    history_ready = listed & (positions - listed_at + 1 >= policy.history_sessions)
    coverage = history_count / policy.history_sessions
    recent_coverage = recent_count / policy.recent_sessions
    reason = pd.Series('eligible', index=calendar)
    reason.loc[median.isna() | median.lt(policy.min_median_amount)] = 'liquidity_below_threshold'
    reason.loc[recent_coverage.lt(policy.min_recent_coverage)] = 'insufficient_recent_coverage'
    reason.loc[coverage.lt(policy.min_coverage)] = 'insufficient_history_coverage'
    reason.loc[~valid] = 'latest_bar_missing_or_invalid'
    reason.loc[~history_ready] = 'insufficient_listing_history'
    reason.loc[~listed] = 'outside_observed_listing_window'
    metrics = pd.DataFrame(dict(date=calendar, symbol=symbol, valid252=history_count.astype('uint16'),
        valid60=recent_count.astype('uint16'), coverage252=coverage, coverage60=recent_coverage,
        amount60_median=median, amount60_observed_median=observed_median,
        unknown_or_invalid60=(policy.recent_sessions-recent_count).astype('uint16'),
        latest_valid=valid.to_numpy(), eligible=reason.eq('eligible').to_numpy(), reason=reason.to_numpy()))
    metrics = metrics.loc[metrics.date.between(start, end)].reset_index(drop=True)
    audit = []
    requested = np.asarray([(start <= day <= end) for day in calendar])
    for year in sorted({day[:4] for day in calendar if start <= day <= end}):
        period = np.asarray([day.startswith(year) for day in calendar]) & requested
        expected = period & listed
        seen = period & observed.to_numpy()
        positive = panel.volume.gt(0) & panel.amount.gt(0)
        zero = panel.volume.eq(0) | panel.amount.eq(0)
        audit.append(dict(year=int(year), symbol=symbol, expected_sessions=int(expected.sum()),
            observed_rows=int(seen.sum()), valid_rows=int((expected & valid.to_numpy()).sum()),
            missing_rows=int((expected & ~observed.to_numpy()).sum()),
            invalid_rows=int((expected & observed.to_numpy() & ~valid.to_numpy()).sum()),
            zero_activity_rows=int((period & observed.to_numpy() & zero.to_numpy()).sum()),
            nonpositive_activity_rows=int((period & observed.to_numpy() & ~positive.to_numpy()).sum()),
            invalid_source_rows=int((period & observed.to_numpy() & ~panel.source.isin(RAW_SOURCES).to_numpy()).sum()),
            eligible_days=int((metrics.date.str.startswith(year) & metrics.eligible).sum()),
            listing_evidence='captured_catalogue' if list_date else 'first_observed_bar_lower_bound'))
    return metrics, audit


def _eligibility_symbols(symbols):
    if not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols) or any(
            not isinstance(symbol, str) or not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', symbol) for symbol in symbols):
        raise ValueError('Provide unique explicit Shanghai/Shenzhen A-share symbols')
    return symbols


def eligibility_for_symbols(session, symbols, start, end, policy=None, check_cancel=None):
    """Qualification for only the requested symbols over one verified window.

    This reuses the frozen-universe window rules but never builds an all-market
    panel and never applies ``max_members``; callers must still call
    ``session.verify()`` before trusting the result.
    """
    policy = UniversePolicy.from_dict(policy)
    _eligibility_symbols(symbols)
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise ValueError('Universe start must not follow end')
    sources = {}
    cal = _read(session.store, session.snapshot, 'trading_calendar', '1900-01-01', end, None, sources, check_cancel, session)
    _unique(cal, ['trade_date'], 'universe trading_calendar')
    if cal.empty or 'is_trading' not in cal:
        raise ValueError('Published trading calendar is empty or invalid')
    calendar = sorted(pd.to_datetime(cal.loc[cal.is_trading, 'trade_date']).dt.strftime('%Y-%m-%d').tolist())
    requested = [day for day in calendar if start <= day <= end]
    if not requested:
        raise ValueError('No verified trading sessions in universe interval')
    first = calendar.index(requested[0])
    history_start = calendar[max(0, first - policy.history_sessions + 1)]
    bars = _read(session.store, session.snapshot, 'daily_bars', history_start, end, list(symbols), sources, check_cancel, session)
    _unique(bars, ['symbol', 'trade_date'], 'universe daily_bars')
    if bars.empty:
        bars = pd.DataFrame(columns=['symbol', 'trade_date', 'source', *BAR_COLUMNS])
    catalog = pd.DataFrame()
    if session.snapshot['datasets']['instruments'].get('available'):
        catalog = _read(session.store, session.snapshot, 'instruments', '1900-01-01', end, list(symbols),
                        sources, check_cancel, session)
        _unique(catalog, ['symbol'], 'universe instruments')
    catalogue = {row['symbol']: row for row in catalog.to_dict('records')}
    groups = dict(tuple(bars.groupby('symbol', sort=False)))
    frames = []
    for symbol in symbols:
        _check(check_cancel)
        frame, _ = _symbol_metrics(groups.get(symbol, bars.iloc[:0]), calendar, catalogue.get(symbol),
            symbol=symbol, policy=policy, start=requested[0], end=requested[-1])
        frames.append(frame)
    columns = ['date', 'symbol', 'eligible', 'reason', 'amount60_median', 'coverage252', 'coverage60']
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)[columns].sort_values(['date', 'symbol']).reset_index(drop=True)


def metrics_for_symbols(artifact, start, end, symbols):
    """Read one frozen universe's metrics for requested symbols, with daily rank.

    ``rank`` is recomputed only here, among the frozen universe's eligible
    identities on each requested date (amount descending, symbol ascending).
    """
    manifest, directory = _artifact(artifact)
    start, end = str(start)[:10], str(end)[:10]
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise ValueError('Universe start must not follow end')
    _eligibility_symbols(symbols)
    files = []
    for record in manifest['files']:
        if not record['path'].startswith('metrics/'):
            continue
        path = directory / record['path']
        if path.stat().st_size != record['size_bytes'] or digest(path) != record['sha256']:
            raise ValueError('Universe metrics bytes changed')
        files.append(str(path))
    columns = ['date', 'symbol', 'eligible', 'reason', 'amount60_median', 'coverage252', 'coverage60', 'rank']
    if not files:
        return pd.DataFrame(columns=columns)
    frame = pl.scan_parquet(files, hive_partitioning=False)
    frame = frame.filter(pl.col('date').is_between(pl.lit(start), pl.lit(end))).select(columns[:-1]).collect()
    ranked = (frame.filter(pl.col('eligible'))
              .sort(['date', 'amount60_median', 'symbol'], descending=[False, True, False])
              .with_columns(pl.col('symbol').cum_count().over('date').alias('rank'))
              .select('date', 'symbol', 'rank'))
    frame = frame.join(ranked, on=['date', 'symbol'], how='left')
    result = frame.filter(pl.col('symbol').is_in(list(symbols))).sort(['date', 'symbol']).to_pandas()
    return result[columns].reset_index(drop=True)


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def build_universe(root, start, end, directory, *, policy=None, read_ref=None, session=None,
                   progress=None, check_cancel=None):
    """Write bounded per-symbol batches and annual membership partitions.

    ``membership_path`` is a directory of annual Parquet files.  The returned
    manifest can be passed directly to load_membership/apply_membership.
    No missing data is downloaded and no published lake generation is changed.
    """
    date.fromisoformat(start)
    date.fromisoformat(end)
    if start > end:
        raise ValueError('Universe start must not follow end')
    policy = UniversePolicy.from_dict(policy)
    session = session or DatasetSession(root, read_ref, check_cancel)
    if read_ref and read_ref != session.snapshot['read_ref']:
        raise ValueError('Universe session differs from requested read_ref')
    if end > (session.snapshot['datasets']['daily_bars'].get('coverage_end') or ''):
        raise ValueError('Universe end exceeds published daily bars coverage')
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    metrics_dir, membership_dir = directory / 'metrics', directory / 'membership'
    metrics_dir.mkdir()
    membership_dir.mkdir()
    sources = {}
    cal = _read(session.store, session.snapshot, 'trading_calendar', '1900-01-01', end,
                None, sources, check_cancel, session)
    _unique(cal, ['trade_date'], 'universe trading_calendar')
    if cal.empty or start < pd.to_datetime(cal.trade_date).dt.strftime('%Y-%m-%d').min():
        raise ValueError('Universe start predates verified trading calendar coverage')
    calendar = sorted(pd.to_datetime(cal.loc[cal.is_trading, 'trade_date']).dt.strftime('%Y-%m-%d').tolist())
    requested = [day for day in calendar if start <= day <= end]
    if not requested:
        raise ValueError('No verified trading sessions in universe interval')
    first = calendar.index(requested[0])
    history_start = calendar[max(0, first - policy.history_sessions + 1)]
    # Listing age needs the entire verified calendar; bars only need one past
    # history window.  No current membership list is substituted for bar IDs.
    evidence = session.source_files('daily_bars', history_start, end)
    sources['daily_bars'] = evidence
    raw = pl.scan_parquet([item['path'] for item in evidence['files']], hive_partitioning=False)
    raw = raw.filter(pl.col('trade_date').is_between(date.fromisoformat(history_start), date.fromisoformat(end)))
    symbols = sorted(s for s in raw.select(pl.col('symbol').unique()).collect()['symbol'].to_list()
                     if isinstance(s, str) and re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', s))
    catalog = pd.DataFrame()
    if session.snapshot['datasets']['instruments'].get('available'):
        catalog = _read(session.store, session.snapshot, 'instruments', '1900-01-01', end,
                        None, sources, check_cancel, session)
        _unique(catalog, ['symbol'], 'universe instruments')
    catalogue = {row['symbol']: row for row in catalog.to_dict('records')}
    observed_symbols = list(symbols)
    # Retain catalogue identities without bars in coverage diagnostics.  Their
    # absence must be visible, rather than silently dropping them as successes.
    symbols = sorted(set(symbols) | {s for s in catalogue
        if isinstance(s, str) and re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', s)})
    audit_rows = []
    for offset in range(0, len(symbols), 200):
        _check(check_cancel)
        batch = symbols[offset:offset+200]
        bars = raw.filter(pl.col('symbol').is_in(batch)).collect().to_pandas()
        _unique(bars, ['symbol', 'trade_date'], 'universe daily_bars')
        groups = dict(tuple(bars.groupby('symbol', sort=False)))
        frames = []
        for symbol in batch:
            frame, annual = _symbol_metrics(groups.get(symbol, bars.iloc[:0]), calendar, catalogue.get(symbol),
                symbol=symbol, policy=policy, start=requested[0], end=requested[-1])
            frames.append(frame)
            audit_rows.extend(annual)
        pd.concat(frames, ignore_index=True).to_parquet(metrics_dir / f'batch-{offset//200:04d}.parquet', index=False)
        if progress:
            progress(phase='universe_batches', current=min(offset+200, len(symbols)), total=len(symbols))
    if not symbols:
        raise ValueError('No historical bar identities in the requested universe interval')
    annual = pd.DataFrame(audit_rows)
    annual_path = directory / 'stock_year_coverage.parquet'
    annual.to_parquet(annual_path, index=False)
    metrics = pl.scan_parquet(str(metrics_dir / '*.parquet'))
    daily_rows, selected_symbols, annual_membership = [], set(), []
    for year in sorted({day[:4] for day in requested}):
        _check(check_cancel)
        part = metrics.filter(pl.col('date').str.starts_with(year))
        counts = part.group_by('date').agg(pl.len().alias('examined_identities'),
            pl.col('latest_valid').sum().alias('valid_latest_bars'),
            pl.col('eligible').sum().alias('qualified_before_cap')).sort('date').collect()
        members = (part.filter(pl.col('eligible')).select('date', 'symbol', 'amount60_median',
                    'amount60_observed_median', 'coverage252', 'coverage60', 'unknown_or_invalid60')
            .sort(['date', 'amount60_median', 'symbol'], descending=[False, True, False])
            .with_columns(pl.col('symbol').cum_count().over('date').alias('rank'))
            .filter(pl.col('rank') <= policy.max_members).sort(['date', 'symbol']).collect())
        members.write_parquet(membership_dir / f'year={year}.parquet')
        selected_symbols.update(members['symbol'].to_list())
        selected_counts = members.group_by('date').len(name='selected')
        counts = counts.join(selected_counts, on='date', how='left').with_columns(pl.col('selected').fill_null(0))
        daily_rows.extend(counts.to_dicts())
        annual_membership.append(dict(year=int(year), signal_sessions=counts.height,
            selected_rows=members.height, unique_members=members['symbol'].n_unique(),
            min_members=int(counts['selected'].min()), max_members=int(counts['selected'].max()),
            empty_sessions=counts.filter(pl.col('selected') == 0).height,
            below_cap_sessions=counts.filter(pl.col('selected') < policy.max_members).height))
    daily_path = directory / 'daily_counts.parquet'
    pd.DataFrame(daily_rows).to_parquet(daily_path, index=False)
    reason_path = directory / 'reason_counts.parquet'
    metrics.group_by(pl.col('date').str.slice(0, 4).alias('year'), 'reason').len().sort(['year', 'reason']).collect().write_parquet(reason_path)
    session.verify()
    files = [{ 'path': p.relative_to(directory).as_posix(), 'sha256': digest(p), 'size_bytes': p.stat().st_size }
             for p in sorted(directory.rglob('*.parquet'))]
    identity = dict(schema_version=1, policy=policy.to_dict(), read_ref=session.snapshot['read_ref'],
        requested_start=start, requested_end=end, start=requested[0], end=requested[-1],
        history_start=history_start, symbols=sorted(selected_symbols), observed_symbols=observed_symbols,
        examined_symbols=symbols, catalogue_symbols_without_bars=sorted(set(symbols)-set(observed_symbols)),
        qualifications=QUALIFICATIONS, annual_membership=annual_membership, files=files,
        liquidity_statistic='complete_calendar_nonnegative_median_lower_bound',
        tie_break='descending_liquidity_then_ascending_symbol',
        source_generations={name: {key: session.snapshot['datasets'][name].get(key)
            for key in ['revision_id', 'coverage_start', 'coverage_end', 'rows', 'receipt_sha256']}
            for name in ['daily_bars', 'trading_calendar', 'instruments', 'adj_factors', 'corporate_actions', 'margin_trading', 'trading_status']})
    identity['universe_id'] = 'universe-' + hashlib.sha256(canonical(identity).encode()).hexdigest()[:32]
    manifest_path = directory / 'manifest.json'
    _write_json(manifest_path, identity)
    _write_json(directory / 'source_evidence.json', {'read_ref': identity['read_ref'], 'datasets': sources})
    return dict(identity, manifest_path=str(manifest_path), membership_path=str(membership_dir),
        audit_paths=dict(stock_year_coverage=str(annual_path), daily_counts=str(daily_path), reason_counts=str(reason_path)))


def _artifact(value):
    if isinstance(value, dict):
        path = Path(value['manifest_path'])
    else:
        path = Path(value)
        if path.is_dir():
            path = path / 'manifest.json'
    result = json.loads(path.read_text(encoding='utf-8'))
    identity = {k: v for k, v in result.items() if k != 'universe_id'}
    expected = 'universe-' + hashlib.sha256(canonical(identity).encode()).hexdigest()[:32]
    if result.get('universe_id') != expected:
        raise ValueError('Universe manifest identity changed')
    return result, path.parent


def read_universe(artifact_or_path, *, verify_files=True):
    """Restore a manifest with its derived paths; verify retained artifact bytes."""
    manifest, directory = _artifact(artifact_or_path)
    if verify_files:
        for record in manifest['files']:
            path = directory / record['path']
            if path.stat().st_size != record['size_bytes'] or digest(path) != record['sha256']:
                raise ValueError('Universe artifact bytes changed')
    return dict(manifest, manifest_path=str(directory/'manifest.json'),
        membership_path=str(directory/'membership'),
        audit_paths={key: str(directory/f'{key}.parquet')
                     for key in ['stock_year_coverage', 'daily_counts', 'reason_counts']})


def load_membership(artifact_or_path, start=None, end=None, dates=None):
    manifest, directory = _artifact(artifact_or_path)
    requested_dates = {str(day)[:10] for day in dates} if dates is not None else None
    files = []
    for record in manifest['files']:
        if not record['path'].startswith('membership/'):
            continue
        year = record['path'].split('year=')[-1][:4]
        if (start and year < start[:4]) or (end and year > end[:4]) or (requested_dates is not None and year not in {d[:4] for d in requested_dates}):
            continue
        path = directory / record['path']
        if path.stat().st_size != record['size_bytes'] or digest(path) != record['sha256']:
            raise ValueError('Universe membership bytes changed')
        files.append(str(path))
    columns = ['date', 'symbol', 'amount60_median', 'amount60_observed_median', 'coverage252', 'coverage60', 'unknown_or_invalid60', 'rank']
    if not files:
        return pd.DataFrame(columns=columns)
    frame = pl.scan_parquet(files, hive_partitioning=False)
    if start:
        frame = frame.filter(pl.col('date') >= start)
    if end:
        frame = frame.filter(pl.col('date') <= end)
    if requested_dates is not None:
        frame = frame.filter(pl.col('date').is_in(sorted(requested_dates)))
    return frame.sort(['date', 'symbol']).collect().to_pandas()


def apply_membership(frame, artifact_or_path):
    if frame.empty:
        return frame.copy()
    members = load_membership(artifact_or_path, start=str(frame.date.min())[:10], end=str(frame.date.max())[:10])
    keys = set(zip(members.date, members.symbol))
    return frame.loc[[(str(day)[:10], symbol) in keys for day, symbol in zip(frame.date, frame.symbol)]].copy()


def universe_on_date(artifact_or_path, date):
    manifest, directory = _artifact(artifact_or_path)
    if not manifest['start'] <= date <= manifest['end']:
        raise ValueError('Date lies outside the frozen universe interval; build a new pinned universe')
    members = load_membership(artifact_or_path, dates=[date])
    counts_path = directory / 'daily_counts.parquet'
    record = next(item for item in manifest['files'] if item['path'] == 'daily_counts.parquet')
    if digest(counts_path) != record['sha256']:
        raise ValueError('Universe daily counts changed')
    counts = pd.read_parquet(counts_path)
    selected = counts.loc[counts.date.eq(date)]
    if selected.empty:
        raise ValueError('Requested universe date is not a verified signal session')
    return dict(date=date, universe_id=manifest['universe_id'], read_ref=manifest['read_ref'],
        symbols=members.symbol.tolist(), members=members.to_dict('records'), counts=selected.iloc[0].to_dict(),
        policy=manifest['policy'], qualifications=manifest['qualifications'])
