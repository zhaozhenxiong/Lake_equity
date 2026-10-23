"""Explicit inference domains and frozen current-population manifests."""
from __future__ import annotations

import hashlib
import re

from .dataset import _read
from .registry import canonical


def population(session, symbols=None, *, scope='symbols', end):
    if scope not in {'all', 'symbols'}:
        raise ValueError('scope must be all or symbols')
    if scope == 'all':
        sources = {}
        catalog = _read(session.store, session.snapshot, 'instruments', '1900-01-01', end,
                        None, sources, session.check_cancel, session)
        if catalog.empty or 'symbol' not in catalog:
            raise ValueError('A published instrument population is required for all-market research')
        symbols = sorted({s for s in catalog.symbol.tolist() if isinstance(s, str) and
                          re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', s)})
    if not isinstance(symbols, list) or not symbols or len(symbols) > 6000 or len(set(symbols)) != len(symbols) or any(
            not isinstance(s, str) or not re.fullmatch(r'(?:6\d{5}\.SH|[03]\d{5}\.SZ)', s) for s in symbols):
        raise ValueError('Provide unique explicit Shanghai/Shenzhen A-share symbols, or a published all-market population')
    manifest = dict(scope=scope, symbols=list(symbols), total=len(symbols), as_of=end,
                    read_ref=session.snapshot['read_ref'], historical_population_complete=False,
                    policy='current_published_instruments' if scope == 'all' else 'explicit_frozen_cohort')
    manifest['sha256'] = hashlib.sha256(canonical(manifest).encode()).hexdigest()
    return manifest
