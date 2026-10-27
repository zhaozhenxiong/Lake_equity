import json
import os
from pathlib import Path
import shutil
import subprocess
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from lake_equity.jobs import JobManager
import lake_equity.storage_usage as usage


def put(root, relative, value=b'data'):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    return path


def content(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*')
            if path.is_file() and not path.is_relative_to(root / 'var/cache/storage_usage')}


def test_cache_miss_and_get_do_not_enumerate_or_create_a_workspace(tmp_path, monkeypatch):
    service = usage.StorageUsageService(tmp_path)
    monkeypatch.setattr(usage.os, 'scandir', lambda *a, **k: pytest.fail('GET must not enumerate'))
    monkeypatch.setattr(service, '_roots', lambda *a: pytest.fail('GET must not read source configuration'))
    app = FastAPI()
    app.include_router(usage.create_storage_usage_router(service, lambda *a: pytest.fail('GET submitted a task')))
    with TestClient(app) as client:
        value = client.get('/api/v1/data/storage/usage').json()
        assert value['status'] == 'not_measured' and value['snapshot'] is None
        assert value['limits'] == {'max_files': 500_000, 'max_seconds': 180}
        assert client.get('/api/v1/data/storage/usage?scope=elsewhere').status_code == 422
        assert client.post('/api/v1/data/storage/usage/refresh', json={'scope': 'lake', 'root': 'D:/'}).status_code == 422
    assert not (tmp_path / 'var').exists()


def test_logical_and_unique_file_counts_distinguish_copies_from_cross_category_hardlinks(tmp_path):
    first = put(tmp_path, 'var/lake/curated/daily_bars/a.parquet', b'x' * 8193)
    copy = first.with_name('copy.parquet')
    shutil.copy2(first, copy)
    link = tmp_path / 'var/learning/studies/shared/dataset.parquet'
    link.parent.mkdir(parents=True)
    os.link(first, link)
    before = content(tmp_path)
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'runtime'})
    assert result['status'] == 'completed', result
    totals = result['totals']
    assert totals['file_count'] == 3 and totals['logical_path_bytes'] == 3 * first.stat().st_size
    assert totals['unique_file_count'] == 2 and totals['unique_logical_bytes'] == 2 * first.stat().st_size
    assert totals['allocated_bytes'] is not None
    assert totals['shared_allocated_bytes'] > 0
    rows = {row['category']: row for row in result['categories']}
    assert rows['curated_working']['shared_allocated_reference_bytes'] == rows['learning_datasets']['shared_allocated_reference_bytes']
    assert sum(row['exclusive_allocated_bytes'] for row in rows.values()) + totals['shared_allocated_bytes'] == totals['allocated_bytes']
    assert content(tmp_path) == before


def test_generation_raw_working_staging_and_metadata_are_mutually_exclusive(tmp_path):
    rid, old = 'a' * 32, 'b' * 32
    values = {
        'var/lake/meta/raw/daily_bars/source=gzip/wire.gz': b'gzip-saved-bytes',
        'var/lake/curated/daily_bars/a.parquet': b'curated',
        'var/lake/derived/industry/a.parquet': b'derived',
        f'var/lake/meta/revisions/data/daily_bars/{rid}/a.parquet': b'published',
        f'var/lake/meta/revisions/data/daily_bars/{old}/a.parquet': b'historical',
        'var/lake/staging/daily_bars/run1/a.parquet': b'staging',
        'var/lake/meta/manifest.db-wal': b'wal',
        'var/lake/meta/stats/partitions.parquet': b'stats',
    }
    pointer = f'var/lake/meta/revisions/daily_bars/current.json'
    values[pointer] = json.dumps({'revision_id': rid}).encode()
    for name, value in values.items():
        put(tmp_path, name, value)
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'lake'})
    assert result['status'] == 'completed', result
    assert result['totals']['logical_path_bytes'] == sum(map(len, values.values()))
    rows = {row['category']: row for row in result['categories']}
    assert set(rows) == {'raw_archive', 'curated_working', 'derived_working', 'current_generation',
                         'historical_generations', 'staging', 'lake_metadata', 'cache_metadata', 'revision_metadata'}
    dataset = next(row for row in result['datasets'] if row['dataset'] == 'daily_bars')
    assert dataset['current_published_logical_bytes'] == len(b'published')
    assert sum(row['logical_path_bytes'] for row in rows.values()) == result['totals']['logical_path_bytes']


def test_unknown_allocation_and_identity_remain_unknown_without_substituting_logical_bytes(tmp_path, monkeypatch):
    put(tmp_path, 'var/lake/file.bin', b'abc')
    monkeypatch.setattr(usage, 'file_measurement', lambda path, info: (None, None, 'unsupported'))
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'lake'})
    assert result['status'] == 'completed'
    assert result['totals']['logical_path_bytes'] == 3
    assert result['totals']['allocated_bytes'] is None and result['totals']['unique_file_count'] is None
    assert result['totals']['allocation_status'] == 'unknown'


@pytest.mark.parametrize('failure', ['cancel', 'budget', 'permission', 'changed', 'bad_config'])
def test_incomplete_refresh_preserves_previous_complete_snapshot(tmp_path, monkeypatch, failure):
    file = put(tmp_path, 'var/lake/a.bin', b'complete')
    service = usage.StorageUsageService(tmp_path)
    good = service.refresh({'scope': 'lake'})
    measure = usage.file_measurement
    check_cancel = None
    if failure == 'cancel':
        check_cancel = lambda: True
    elif failure == 'budget':
        service.max_files = 0
    elif failure == 'permission':
        def denied(*args):
            raise PermissionError('fixture denial')
        monkeypatch.setattr(usage, 'file_measurement', denied)
    elif failure == 'changed':
        def changed(path, info):
            result = measure(path, info)
            path.write_bytes(b'changed-size')
            return result
        monkeypatch.setattr(usage, 'file_measurement', changed)
    else:
        put(tmp_path, 'configs/cnequity.toml', b'[data]\nroot="D:/outside-usage-scope"\n')
    bad = service.refresh({'scope': 'lake'}, check_cancel=check_cancel)
    assert bad['status'] in {'partial', 'cancelled', 'failed'}
    summary = service.summary()
    assert summary['stale'] is True
    assert summary['snapshot']['scan_id'] == good['scan_id']
    assert summary['last_attempt']['scan_id'] == bad['scan_id']
    assert summary['snapshot']['totals']['logical_path_bytes'] == len(b'complete')


def test_scope_exclusions_are_not_walked_and_unregistered_current_size_is_unknown(tmp_path, monkeypatch):
    put(tmp_path, 'var/lake/curated/daily_bars/real.bin', b'yes')
    for name in ['releases', 'validation', 'runtimes', 'tools']:
        put(tmp_path, f'var/{name}/huge.bin', b'not-included')
    scan = usage.os.scandir
    visited = []
    def guarded(path):
        visited.append(Path(path))
        assert all(not Path(path).is_relative_to(tmp_path / 'var' / name) for name in ['releases', 'validation', 'runtimes', 'tools'])
        return scan(path)
    monkeypatch.setattr(usage.os, 'scandir', guarded)
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'runtime'})
    assert result['totals']['logical_path_bytes'] == 3
    assert result['datasets'][0]['current_published_logical_bytes'] is None
    assert visited


def test_static_reparse_points_are_not_followed_or_counted(tmp_path, monkeypatch):
    target = put(tmp_path, 'outside/secret.bin', b'outside')
    path = put(tmp_path, 'var/lake/link-placeholder', b'not-a-real-link')
    original = Path.lstat
    def as_reparse(self):
        result = original(self)
        if self == path:
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=usage.REPARSE)
        return result
    monkeypatch.setattr(Path, 'lstat', as_reparse)
    monkeypatch.setattr(usage, 'file_measurement', lambda *a: pytest.fail('reparse entry was opened'))
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'lake'})
    assert result['status'] == 'partial' and result['totals']['logical_path_bytes'] == 0
    assert result['errors'][0]['reason'] == 'excluded_reparse_point'
    assert target.read_bytes() == b'outside'


def test_symlinked_root_and_cache_cannot_escape_whitelist(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    lake = tmp_path / 'var/lake'
    lake.parent.mkdir()
    try:
        lake.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('Directory symlink creation unavailable for this test account')
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'lake'})
    assert result['status'] == 'failed'
    assert not list(outside.iterdir())


def test_repeated_refresh_submission_is_single_flight_and_cache_get_starts_no_work(tmp_path):
    service = usage.StorageUsageService(tmp_path)
    started, release = threading.Event(), threading.Event()
    def dispatch(kind, params, progress, cancelled):
        started.set()
        assert release.wait(3)
        return service.refresh(params, check_cancel=cancelled)
    manager = JobManager(tmp_path / 'job-audit', dispatch)
    app = FastAPI()
    app.include_router(usage.create_storage_usage_router(service, manager.submit))
    try:
        with TestClient(app) as client:
            assert client.get('/api/v1/data/storage/usage').json()['status'] == 'not_measured'
            assert manager.list() == []
            first = client.post('/api/v1/data/storage/usage/refresh', json={'scope': 'lake'}).json()
            assert started.wait(3)
            second = client.post('/api/v1/data/storage/usage/refresh', json={'scope': 'lake'}).json()
            assert first['id'] == second['id'] and len(manager.list()) == 1
            release.set()
    finally:
        release.set()
        manager.close()


def test_generation_pointer_change_marks_inventory_partial(tmp_path, monkeypatch):
    rid = 'a' * 32
    file = put(tmp_path, f'var/lake/meta/revisions/data/daily_bars/{rid}/x.bin', b'x')
    pointer = put(tmp_path, 'var/lake/meta/revisions/daily_bars/current.json', json.dumps({'revision_id': rid}).encode())
    measure = usage.file_measurement
    def replace_pointer(path, info):
        result = measure(path, info)
        if path == file:
            pointer.write_text(json.dumps({'revision_id': 'b' * 32}))
        return result
    monkeypatch.setattr(usage, 'file_measurement', replace_pointer)
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'lake'})
    assert result['status'] == 'partial'
    assert any(error['reason'] == 'generation_changed' for error in result['errors'])


@pytest.mark.skipif(os.name != 'nt', reason='Windows junction boundary')
def test_windows_junction_root_is_never_enumerated(tmp_path, monkeypatch):
    outside = tmp_path / 'outside'
    outside.mkdir()
    put(outside, 'secret.bin', b'secret')
    link = tmp_path / 'var/lake'
    link.parent.mkdir()
    made = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
    if made.returncode:
        pytest.skip('Junction creation unsupported on the test filesystem')
    scan = usage.os.scandir
    def guarded(path):
        assert not Path(path).is_relative_to(link)
        return scan(path)
    monkeypatch.setattr(usage.os, 'scandir', guarded)
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'lake'})
    assert result['status'] == 'failed' and result['totals']['file_count'] == 0
    assert (outside / 'secret.bin').read_bytes() == b'secret'


@pytest.mark.skipif(os.name != 'nt', reason='Windows sparse allocation capability')
def test_windows_sparse_file_uses_handle_compression_information(tmp_path):
    import ctypes
    from ctypes import wintypes
    import msvcrt

    path = tmp_path / 'sparse.bin'
    with path.open('w+b') as handle:
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        api.DeviceIoControl.restype = wintypes.BOOL
        returned = wintypes.DWORD()
        if not api.DeviceIoControl(msvcrt.get_osfhandle(handle.fileno()), 0x900C4, None, 0, None, 0, ctypes.byref(returned), None):
            pytest.skip('Sparse files unsupported on the test filesystem')
        handle.seek(8 * 1024 * 1024)
        handle.write(b'x')
    identity, allocated, method = usage.file_measurement(path, path.lstat())
    assert identity is not None and method == 'windows_compression_info'
    assert allocated is not None and allocated < path.stat().st_size


def test_configuration_cannot_include_the_usage_cache_itself(tmp_path):
    put(tmp_path, 'configs/cnequity.toml', b'[data]\nroot="var/cache"\n')
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'lake'})
    assert result['status'] == 'failed'
    assert 'whitelist' in result['error']


def test_task_audit_enumeration_defers_its_own_progress_writes(tmp_path):
    put(tmp_path, 'var/jobs/existing.json', b'{}')
    calls = []
    def progress(value):
        calls.append(value)
        put(tmp_path, 'var/jobs/new-progress-receipt.json', b'{}')
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'runtime'}, progress=progress)
    assert result['status'] == 'completed', result
    assert result['totals']['file_count'] == 1
    assert calls == []


def test_external_changes_in_task_audit_still_make_the_scan_partial(tmp_path, monkeypatch):
    file = put(tmp_path, 'var/jobs/existing.json', b'{}')
    measure = usage.file_measurement
    def external_write(path, info):
        value = measure(path, info)
        if path == file:
            put(tmp_path, 'var/jobs/external.json', b'{}')
        return value
    monkeypatch.setattr(usage, 'file_measurement', external_write)
    result = usage.StorageUsageService(tmp_path).refresh({'scope': 'runtime'}, progress=lambda value: None)
    assert result['status'] == 'partial'
    assert any(row['reason'] == 'directory_changed' for row in result['errors'])
