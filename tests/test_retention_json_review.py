import hashlib
import json

import pytest

from lake_equity.learning.retention_json import extract


def test_streamed_metadata_after_escaped_chunk_boundaries_matches_the_original_bytes(tmp_path):
    path = tmp_path / 'report.json'
    value = {'large': {'rows': ['中文 { [ \\" quoted ' * 90000]},
             'artifacts': {'dataset.parquet': {'sha256': 'a' * 64, 'size_bytes': 12}},
             'run_id': 'learn-' + 'b' * 32, 'status': 'completed'}
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    assert len(raw) > 1024 * 1024
    path.write_bytes(raw)
    metadata, checksum = extract(path, {'artifacts', 'run_id', 'status'}, complete=True)
    assert metadata == {key: value[key] for key in ('artifacts', 'run_id', 'status')}
    assert checksum == hashlib.sha256(raw).hexdigest()


def test_complete_verification_rejects_duplicate_wanted_metadata_after_the_preview_prefix(tmp_path):
    path = tmp_path / 'report.json'
    path.write_text('{"artifacts":{},"large":["escaped \\\" } ]"],"artifacts":{"changed":true}}')
    assert extract(path, {'artifacts'}) == ({'artifacts': {}}, None)
    with pytest.raises(ValueError, match='Duplicate'):
        extract(path, {'artifacts'}, complete=True)


def test_complete_benchmark_contract_rejects_an_unknown_tail_key(tmp_path):
    path = tmp_path / 'benchmark.json'
    path.write_text('{"schema":"known","account_scope":"shared_cash_account","external_reference":"learn-x"}')
    with pytest.raises(ValueError):
        extract(path, {'schema', 'account_scope'}, complete=True,
                allowed_keys={'schema', 'account_scope'})


def test_truncated_metadata_never_looks_like_a_completed_delivery(tmp_path):
    path = tmp_path / 'report.json'
    path.write_text('{"artifacts":{"one":{"sha256":"not-finished')
    with pytest.raises(ValueError, match='Truncated'):
        extract(path, {'artifacts'}, complete=True)
