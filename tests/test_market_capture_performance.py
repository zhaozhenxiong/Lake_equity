"""Published-generation statistics reuse without weakening source validation."""
from datetime import date
import json
from pathlib import Path

import polars as pl
import pytest

from cnequity.domain.contracts import contract_fingerprint
from cnequity.domain.datasets import DATASETS
from cnequity.storage.revisions import RevisionConsistencyError, RevisionStore
from lake_equity.market import store as store_module
from lake_equity.market.store import MarketStore
from test_market_store import fixture_bars, publish


@pytest.fixture(autouse=True)
def isolated_statistics_cache():
    cache = getattr(store_module, '_generation_statistics', None)
    if cache is not None:
        cache.cache_clear()
    yield
    if cache is not None:
        cache.cache_clear()


def test_repeated_capture_reuses_footer_reads_across_store_instances(tmp_path, monkeypatch):
    store = MarketStore(tmp_path)
    publish(store, 'sector_bars', fixture_bars())
    reads = []
    original = pl.read_parquet_schema

    def observed(path, *args, **kwargs):
        reads.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pl, 'read_parquet_schema', observed)
    first = store.capture()
    count = len(reads)
    first['datasets']['sector_bars']['rows'] = 999
    second = MarketStore(tmp_path).capture()
    assert count > 0
    assert len(reads) == count
    assert second['datasets']['sector_bars']['rows'] == 1
    assert second['datasets']['sector_bars']['coverage_start'] == '2026-10-22'
    assert second['revision_map'] == first['revision_map']
    assert set(second['datasets']) == set(DATASETS)


def test_new_published_generation_recomputes_rows_and_coverage(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, 'sector_bars', fixture_bars())
    first = store.capture()
    changed = pl.DataFrame({'sector_code': ['881001', '881001'],
                            'trade_date': [date(2026, 10, 22), date(2026, 10, 23)],
                            'close': [10., 12.], 'source': ['ths', 'ths']})
    publish(store, 'sector_bars', changed, 'second-published-generation')
    second = store.capture()
    assert first['revision_map'] != second['revision_map']
    assert second['datasets']['sector_bars']['rows'] == 2
    assert second['datasets']['sector_bars']['coverage_end'] == '2026-10-23'
    assert store.capture(first['read_ref'])['datasets']['sector_bars']['rows'] == 1


@pytest.mark.parametrize('fault', ['missing_file', 'extra_file', 'changed_size', 'changed_receipt'])
def test_warm_capture_still_rejects_generation_and_receipt_changes(tmp_path, fault):
    store = MarketStore(tmp_path)
    publish(store, 'sector_bars', fixture_bars())
    snapshot = store.capture()
    store.capture()  # Warm the statistics before changing isolated source evidence.
    item = snapshot['datasets']['sector_bars']
    generation = Path(item['path'])
    path = next(generation.rglob('*.parquet'))
    if fault == 'missing_file':
        path.unlink()
    elif fault == 'extra_file':
        (generation / 'extra.parquet').write_bytes(path.read_bytes())
    elif fault == 'changed_size':
        path.write_bytes(path.read_bytes() + b'changed')
    else:
        receipt_path = store.config.meta_root / item['receipt_path']
        receipt = json.loads(receipt_path.read_text())
        receipt['dataset'] = 'index_bars'
        receipt_path.write_text(json.dumps(receipt), encoding='utf-8')
    with pytest.raises((ValueError, RevisionConsistencyError)):
        store.capture()
    with pytest.raises(ValueError):
        store.capture(snapshot['read_ref'])


def test_receipt_byte_identity_change_does_not_reuse_statistics(tmp_path, monkeypatch):
    store = MarketStore(tmp_path)
    publish(store, 'sector_bars', fixture_bars())
    reads = []
    original = pl.read_parquet_schema

    def observed(path, *args, **kwargs):
        reads.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pl, 'read_parquet_schema', observed)
    first = store.capture()
    count = len(reads)
    item = first['datasets']['sector_bars']
    receipt_path = store.config.meta_root / item['receipt_path']
    receipt_path.write_bytes(receipt_path.read_bytes() + b'\n')
    second = store.capture()
    assert second['datasets']['sector_bars']['receipt_sha256'] != item['receipt_sha256']
    assert len(reads) > count
    with pytest.raises(ValueError, match='receipt bytes'):
        store.capture(first['read_ref'])


@pytest.mark.parametrize('replace_expected', [False, True])
def test_warm_capture_rejects_expected_or_extra_symlinks(tmp_path, replace_expected):
    store = MarketStore(tmp_path)
    publish(store, 'sector_bars', fixture_bars())
    snapshot = store.capture()
    generation = Path(snapshot['datasets']['sector_bars']['path'])
    source = next(generation.rglob('*.parquet'))
    outside = tmp_path / 'outside.parquet'
    outside.write_bytes(source.read_bytes())
    if replace_expected:
        source.unlink()
        link = source
    else:
        link = generation / 'extra-link.parquet'
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f'Symlink creation unavailable: {error}')
    with pytest.raises(ValueError, match='symlink|file set'):
        store.capture()
    with pytest.raises(ValueError, match='symlink|file set'):
        store.capture(snapshot['read_ref'])


def test_new_generation_with_mixed_schema_keeps_all_columns_and_rows(tmp_path):
    store = MarketStore(tmp_path)
    publish(store, 'sector_bars', fixture_bars())
    first = store.capture()
    path = store.config.curated_root / 'sector_bars' / 'additional.parquet'
    pl.DataFrame({'sector_code': ['881001'], 'trade_date': [date(2026, 10, 23)],
                  'close': [12.], 'source': ['ths'], 'volume': [100.]}).write_parquet(path)
    spec = DATASETS['sector_bars']
    RevisionStore(store.config.meta_root, store.config.curated_root, store.config.derived_root).commit(
        'sector_bars', run_id='new-mixed-schema', changed_files=[path],
        schema_version=spec.schema_version, contract_fingerprint=contract_fingerprint('sector_bars'))
    second = store.capture()
    assert second['revision_map'] != first['revision_map']
    assert second['datasets']['sector_bars']['rows'] == 2
    assert second['datasets']['sector_bars']['coverage_end'] == '2026-10-23'
    frame = store.frame('sector_bars', second)
    assert 'volume' in frame.columns
    assert frame['volume'].null_count() == 1
    assert frame.height == 2


def test_selected_source_bytes_are_hashed_even_after_statistics_are_cached(tmp_path):
    from lake_equity.learning.dataset import DatasetSession
    store = MarketStore(tmp_path)
    publish(store, 'sector_bars', fixture_bars())
    store.capture()
    session = DatasetSession(tmp_path)
    evidence = session.source_files('sector_bars', '2026-10-22', '2026-10-22')
    path = Path(evidence['files'][0]['path'])
    content = bytearray(path.read_bytes())
    content[len(content) // 2] ^= 1
    path.write_bytes(content)
    assert path.stat().st_size == evidence['files'][0]['size_bytes']
    with pytest.raises(ValueError, match='Published source changed'):
        session.verify()
    with pytest.raises(ValueError, match='SHA256 changed'):
        DatasetSession(tmp_path, read_ref=session.snapshot['read_ref']).source_files(
            'sector_bars', '2026-10-22', '2026-10-22')
