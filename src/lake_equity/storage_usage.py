"""Bounded metadata-only storage inventory; independent of lifecycle approvals."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time
import tomllib
import uuid
from typing import Literal

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict

from lake_equity.jobs import JobCancelled


LABELS = dict(raw_archive='原始来源档案（保存文件）', curated_working='Curated 工作树',
              derived_working='Derived 工作树', current_generation='当前发布代际',
              historical_generations='历史保留代际', unclassified_generations='身份未明的代际',
              revision_candidates='未发布代际候选', revision_metadata='代际凭证与指针', staging='暂存批次',
              cache_metadata='来源与统计缓存', lake_metadata='数据湖元数据', lake_indexes='数据湖索引',
              lake_other='数据湖其他文件', model_assets='模型资产', registered_models='注册及归档模型',
              experiment_models='实验模型', learning_datasets='学习共享数据', learning_reports='学习报告',
              learning_artifacts='学习与研究产物', research_inputs='物化研究输入', legacy_inputs='迁移保留输入',
              research_artifacts='结构研究产物', research_store='结构研究存储', job_reports='任务报告',
              job_index='任务索引', pipeline_reports='流水线报告', market_views='行情视图与读取凭证', logs='日志')
EXCLUDED = ('releases', 'release-work', 'release-validation', 'validation', 'runtimes', 'tools',
            'build-assets', 'test_runtime', 'fill_lake', '_quarantine', 'cache/storage_usage')
RUNTIME_ROOTS = ('learning', 'models', 'research_inputs', 'research', 'jobs', 'pipeline', 'market_views', 'legacy', 'logs')
REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


class StorageUsageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    scope: Literal['lake', 'runtime'] = 'lake'


def _now():
    return datetime.now(timezone.utc).isoformat()


def _linked(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & REPARSE)


def _safe(path, root):
    """Check lexical containment and every existing ancestor before following it."""
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    relative = path.relative_to(root)
    cursor = root
    for part in ('', *relative.parts):
        if part:
            cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            break
        if _linked(info):
            raise ValueError('Storage path contains a symlink, junction or reparse point: ' + str(cursor))
    return path


@lru_cache(maxsize=1)
def _windows_api():
    import ctypes as c
    from ctypes import wintypes as w

    class Standard(c.Structure):
        _fields_ = [('allocation', c.c_longlong), ('size', c.c_longlong), ('links', w.DWORD),
                    ('deleting', w.BOOLEAN), ('directory', w.BOOLEAN)]

    class Identity(c.Structure):
        _fields_ = [('volume', c.c_ulonglong), ('file_id', c.c_ubyte * 16)]

    class Attributes(c.Structure):
        _fields_ = [('attributes', w.DWORD), ('tag', w.DWORD)]

    class Compression(c.Structure):
        _fields_ = [('size', c.c_longlong), ('format', w.WORD), ('unit', c.c_ubyte),
                    ('chunk', c.c_ubyte), ('cluster', c.c_ubyte), ('reserved', c.c_ubyte * 3)]

    api = c.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
    api.CreateFileW.restype = w.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    api.GetFileInformationByHandleEx.restype = w.BOOL
    api.CloseHandle.argtypes = [w.HANDLE]
    api.CloseHandle.restype = w.BOOL
    api.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
    api.GetFinalPathNameByHandleW.restype = w.DWORD
    return c, api, Standard, Identity, Attributes, Compression


def file_measurement(path, info):
    """Return identity and detectable allocation; never read the data stream."""
    if os.name != 'nt':
        identity = (info.st_dev, info.st_ino) if info.st_ino else None
        return identity, getattr(info, 'st_blocks', None) * 512 if hasattr(info, 'st_blocks') else None, 'stat_blocks'
    c, api, Standard, Identity, Attributes, Compression = _windows_api()
    name = str(Path(path).absolute())
    if not name.startswith('\\\\?\\'):
        name = '\\\\?\\' + name
    # OPEN_REPARSE_POINT prevents a substituted final link from being followed.
    handle = api.CreateFileW(name, 0x80, 7, None, 3, 0x00200000, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        def query(kind, target):
            return bool(api.GetFileInformationByHandleEx(handle, kind, c.byref(target), c.sizeof(target)))
        attributes = Attributes()
        if not query(9, attributes):
            raise c.WinError(c.get_last_error())
        if attributes.attributes & REPARSE:
            raise ValueError('Storage file became a reparse point')
        final_name = c.create_unicode_buffer(32768)
        length = api.GetFinalPathNameByHandleW(handle, final_name, len(final_name), 0)
        if not length or length >= len(final_name):
            raise c.WinError(c.get_last_error())
        if os.path.normcase(final_name.value) != os.path.normcase(name):
            raise ValueError('Storage file path was redirected while opening')
        identity, standard = Identity(), Standard()
        known_id = query(18, identity)
        if not query(1, standard):
            raise c.WinError(c.get_last_error())
        if standard.size != info.st_size:
            raise ValueError('Storage file changed while measuring')
        allocation, method = standard.allocation, 'windows_allocation_size'
        if attributes.attributes & (0x800 | 0x200):  # compressed or sparse
            compressed = Compression()
            allocation = compressed.size if query(8, compressed) else None
            method = 'windows_compression_info' if allocation is not None else 'unknown_compressed_allocation'
        file_id = bytes(identity.file_id).hex()
        return ((identity.volume, file_id) if known_id and int(file_id, 16) else None), allocation, method
    finally:
        api.CloseHandle(handle)


class _BudgetExceeded(RuntimeError):
    pass


class StorageUsageService:
    def __init__(self, root, *, max_files=500_000, max_seconds=180, ttl_seconds=60):
        self.root = Path(os.path.abspath(root))
        self.cache = self.root / 'var/cache/storage_usage'
        self.max_files, self.max_seconds, self.ttl_seconds = max_files, max_seconds, ttl_seconds

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, str) or scope not in {'lake', 'runtime'}:
            raise ValueError('Storage scope must be lake or runtime')
        return scope

    def _cached(self, scope):
        path = _safe(self.cache / (scope + '.json'), self.root)
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value, dict) or any(value.get(key) is not None and not isinstance(value[key], dict)
                                                  for key in ('last_good', 'last_attempt')):
                raise ValueError('Invalid storage usage cache')
            return value
        except FileNotFoundError:
            return {}

    def summary(self, scope='lake'):
        scope = self._scope(scope)
        try:
            cached = self._cached(scope)
        except (OSError, ValueError) as error:
            return dict(schema='storage_usage_v1', scope=scope, status='unavailable', snapshot=None, stale=True, error=str(error))
        attempt, good = cached.get('last_attempt'), cached.get('last_good')
        snapshot = good or (attempt if attempt and attempt.get('status') == 'partial' else None)
        age = max(0., time.time() - datetime.fromisoformat(good['finished_at']).timestamp()) if good else None
        return dict(schema='storage_usage_v1', scope=scope, status=(attempt or {}).get('status', 'not_measured'),
                    snapshot=snapshot, last_attempt=attempt, last_good_scan_id=(good or {}).get('scan_id'),
                    cache_age_seconds=age, stale=not good or age > self.ttl_seconds or attempt.get('status') != 'completed',
                    limits=dict(max_files=self.max_files, max_seconds=self.max_seconds))

    def _save(self, scope, cached, attempt):
        _safe(self.cache, self.root).mkdir(parents=True, exist_ok=True)
        _safe(self.cache, self.root)
        value = dict(cached, last_attempt=attempt)
        if attempt['status'] == 'completed':
            value['last_good'] = attempt
        path = _safe(self.cache / (scope + '.json'), self.root)
        fd, temporary = tempfile.mkstemp(prefix=scope + '-', suffix='.tmp', dir=self.cache)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _roots(self, scope):
        config = _safe(self.root / 'configs/cnequity.toml', self.root)
        raw = tomllib.loads(config.read_text(encoding='utf-8')).get('data', {}).get('root', 'var/lake') if config.is_file() else 'var/lake'
        lake = Path(raw).expanduser()
        lake = Path(os.path.abspath(lake if lake.is_absolute() else self.root / lake))
        relative = lake.relative_to(self.root / 'var')
        if not relative.parts or any(relative.is_relative_to(Path(item)) or Path(item).is_relative_to(relative) for item in EXCLUDED):
            raise ValueError('Configured lake root is outside the storage inventory whitelist')
        roots = [lake]
        if scope == 'runtime':
            roots += [self.root / 'var' / name for name in RUNTIME_ROOTS]
        disjoint = []
        for path in sorted(set(roots), key=lambda value: len(value.parts)):
            if not any(path.is_relative_to(parent) for parent in disjoint):
                disjoint.append(path)
        return lake, disjoint

    def _classify(self, path, lake, current):
        if path.is_relative_to(lake):
            parts = path.relative_to(lake).parts
            if parts[0] in {'curated', 'derived'}:
                return parts[0] + '_working', parts[1] if len(parts) > 2 else None
            if parts[:2] == ('meta', 'raw'):
                return 'raw_archive', parts[2] if len(parts) > 3 else None
            if parts[:3] == ('meta', 'revisions', 'data') and len(parts) > 5:
                dataset, rid = parts[3:5]
                kind = 'revision_candidates' if rid.startswith('.') else 'unclassified_generations' if dataset not in current else (
                    'current_generation' if current[dataset] == rid else 'historical_generations')
                return kind, dataset
            if parts[:2] == ('meta', 'revisions'):
                return 'revision_metadata', parts[2] if len(parts) > 3 and parts[2] != 'data' else None
            if parts[0] == 'staging':
                return 'staging', parts[1] if len(parts) > 2 else None
            if parts[0] == 'meta':
                return ('cache_metadata' if len(parts) > 1 and parts[1] in {'source_cache', 'adj_factors_cache', 'stats'} else 'lake_metadata'), None
            return ('logs' if parts[0] == 'logs' else 'lake_indexes' if parts[0] == 'duckdb' else 'lake_other'), None
        parts = path.relative_to(self.root / 'var').parts
        first = parts[0]
        if first == 'learning':
            if parts[1:2] == ('models',) or parts[1:3] == ('archive', 'models'):
                return 'registered_models', None
            return ('experiment_models' if 'models' in parts[1:-1] else 'learning_datasets' if path.name == 'dataset.parquet'
                    else 'learning_reports' if path.name == 'report.json' else 'learning_artifacts'), None
        return dict(models='model_assets', research_inputs='research_inputs', legacy='legacy_inputs',
                    pipeline='pipeline_reports', market_views='market_views', logs='logs',
                    jobs='job_reports' if path.suffix == '.json' else 'job_index',
                    research='research_artifacts' if parts[1:2] == ('artifacts',) else 'research_store')[first], None

    def refresh(self, params, progress=None, check_cancel=None):
        if not isinstance(params, dict) or set(params) - {'scope'}:
            raise ValueError('Only a fixed storage scope may be requested')
        scope = self._scope(params.get('scope', 'lake'))
        _safe(self.cache, self.root).mkdir(parents=True, exist_ok=True)
        lease = FileLock(str(_safe(self.cache / (scope + '.lock'), self.root)))
        try:
            lease.acquire(timeout=0)
        except Timeout:
            return dict(status='partial', reason='storage_scope_already_running', scope=scope,
                        active_scan_id=(self.summary(scope).get('last_attempt') or {}).get('scan_id'))
        try:
            return self._refresh(scope, progress, check_cancel)
        finally:
            lease.release()

    def _refresh(self, scope, progress, check_cancel):
        cached = self._cached(scope)
        attempt = dict(scan_id='usage-' + uuid.uuid4().hex, scope=scope, status='running', started_at=_now(), errors=[])
        self._save(scope, cached, attempt)
        started = time.monotonic()
        groups, categories, datasets, methods = {}, {}, {}, Counter()
        file_count = logical_bytes = unknown_ids = unknown_allocations = errors_count = 0
        last_progress = 0.

        def error(path, reason, detail=''):
            nonlocal errors_count
            errors_count += 1
            if len(attempt['errors']) < 50:
                attempt['errors'].append(dict(path=str(path.relative_to(self.root)) if path.is_relative_to(self.root) else 'configured_root',
                                              reason=reason, detail=str(detail)[:300]))

        def checkpoint():
            if check_cancel and check_cancel():
                raise JobCancelled('Storage inventory cancelled')
            if file_count >= self.max_files or time.monotonic() - started >= self.max_seconds:
                raise _BudgetExceeded('Storage inventory reached its file or time budget')

        def pointers(lake):
            result = {}
            base = _safe(lake / 'meta/revisions', self.root)
            if not base.exists():
                return result
            with os.scandir(base) as entries:
                for entry in entries:
                    checkpoint()
                    if entry.name == 'data':
                        continue
                    path = Path(entry.path) / 'current.json'
                    try:
                        _safe(path, self.root)
                        if path.lstat().st_size > 1_048_576:
                            raise ValueError('Revision pointer is too large')
                        value = json.loads(path.read_text(encoding='utf-8'))
                        rid = value['revision_id']
                        if not isinstance(rid, str) or not re.fullmatch(r'[a-f0-9]{32}', rid):
                            raise ValueError('Invalid revision identity')
                    except FileNotFoundError:
                        continue
                    except (OSError, ValueError, KeyError) as failure:
                        error(path, 'pointer_unavailable', failure)
                        continue
                    generation = base / 'data' / entry.name / rid
                    try:
                        if not _safe(generation, self.root).is_dir():
                            raise FileNotFoundError('Current generation is missing')
                        result[entry.name] = rid
                    except (OSError, ValueError) as failure:
                        error(generation, 'generation_unavailable', failure)
            return result

        try:
            lake, roots = self._roots(scope)
            current = pointers(lake)
            attempt['included_roots'] = [str(path.relative_to(self.root)) for path in roots]
            attempt['excluded_roots'] = ['var/' + name for name in EXCLUDED] + ['.venv', 'src', 'frontend', 'vendor', 'packages']
            attempt['root_states'] = []
            volumes = {}
            for selected in roots:
                checkpoint()
                try:
                    _safe(selected, self.root)
                    selected.lstat()
                except FileNotFoundError:
                    attempt['root_states'].append(dict(path=str(selected.relative_to(self.root)), status='not_present'))
                    continue
                except (OSError, ValueError) as failure:
                    error(selected, 'root_unavailable', failure)
                    continue
                attempt['root_states'].append(dict(path=str(selected.relative_to(self.root)), status='scanned'))
                volume = shutil.disk_usage(selected)
                volumes[selected.anchor] = dict(volume=selected.anchor, total_bytes=volume.total, free_bytes=volume.free,
                                                measurement_scope='whole_volume')
                attempt['volumes'] = list(volumes.values())
                stack = [selected]
                while stack:
                    directory = stack.pop()
                    checkpoint()
                    try:
                        _safe(directory, self.root)
                        before_directory = directory.lstat()
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                checkpoint()
                                path = Path(entry.path)
                                try:
                                    # Windows DirEntry.stat may omit inode/device identity.
                                    info = path.lstat()
                                    if _linked(info):
                                        error(path, 'excluded_reparse_point')
                                        continue
                                    if stat.S_ISDIR(info.st_mode):
                                        stack.append(path)
                                        continue
                                    if not stat.S_ISREG(info.st_mode):
                                        error(path, 'unsupported_file_type')
                                        continue
                                    _safe(path, self.root)
                                    try:
                                        identity, allocation, method = file_measurement(path, info)
                                    except OSError as failure:
                                        error(path, 'metadata_access_failed', failure)
                                        identity, allocation, method = None, None, 'unknown'
                                    after = path.lstat()
                                    if _linked(after) or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                                            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                                        error(path, 'file_changed')
                                        continue
                                    kind, dataset = self._classify(path, lake, current)
                                    file_count += 1
                                    logical_bytes += info.st_size
                                    unknown_ids += identity is None
                                    unknown_allocations += allocation is None
                                    methods[method] += 1
                                    row = categories.setdefault(kind, dict(category=kind, label=LABELS[kind], file_count=0,
                                                                          logical_path_bytes=0, unknown_measurements=0))
                                    row['file_count'] += 1
                                    row['logical_path_bytes'] += info.st_size
                                    row['unknown_measurements'] += identity is None or allocation is None
                                    if dataset:
                                        row = datasets.setdefault(dataset, dict(dataset=dataset, logical_path_bytes=0,
                                                    current_published_logical_bytes=0 if dataset in current else None, layers={}))
                                        row['logical_path_bytes'] += info.st_size
                                        row['layers'][kind] = row['layers'].get(kind, 0) + info.st_size
                                        if kind == 'current_generation':
                                            row['current_published_logical_bytes'] = (row['current_published_logical_bytes'] or 0) + info.st_size
                                    key = identity if identity is not None else ('unknown', str(path))
                                    group = groups.setdefault(key, dict(size=info.st_size, allocation=allocation, categories=set(), datasets=set(),
                                                                       known_id=identity is not None, paths=0))
                                    if group['size'] != info.st_size or group['allocation'] != allocation:
                                        error(path, 'shared_file_changed')
                                    group['categories'].add(kind)
                                    group['paths'] += 1
                                    if dataset:
                                        group['datasets'].add(dataset)
                                    # Progress itself updates the task audit in var/jobs.
                                    # Keep that short enumeration free of our own writes;
                                    # external file/directory changes are still checked.
                                    if progress and not directory.is_relative_to(self.root / 'var/jobs') and time.monotonic() - last_progress >= .5:
                                        progress(dict(phase='storage_usage', files=file_count, logical_path_bytes=logical_bytes, scope=scope))
                                        last_progress = time.monotonic()
                                except (OSError, ValueError) as failure:
                                    error(path, 'file_unavailable', failure)
                        if directory.lstat().st_mtime_ns != before_directory.st_mtime_ns:
                            error(directory, 'directory_changed')
                    except (OSError, ValueError) as failure:
                        error(directory, 'directory_unavailable', failure)
            if pointers(lake) != current:
                error(lake / 'meta/revisions', 'generation_changed')
            attempt['status'] = 'partial' if errors_count else 'completed'
        except JobCancelled:
            attempt['status'] = 'cancelled'
        except _BudgetExceeded as failure:
            attempt.update(status='partial', truncated=True, budget_reason=str(failure))
        except (OSError, ValueError, TypeError, KeyError) as failure:
            attempt.update(status='failed', error=str(failure))
        known_groups = [group for group in groups.values() if group['known_id']]
        known_allocation = sum(group['allocation'] for group in known_groups if group['allocation'] is not None)
        shared_allocation = sum(group['allocation'] for group in known_groups if group['allocation'] is not None and len(group['categories']) > 1)
        for row in categories.values():
            related = [group for group in known_groups if row['category'] in group['categories'] and group['allocation'] is not None]
            row['exclusive_allocated_bytes'] = (sum(group['allocation'] for group in related if len(group['categories']) == 1)
                                               if not row['unknown_measurements'] else None)
            row['shared_allocated_reference_bytes'] = sum(group['allocation'] for group in related if len(group['categories']) > 1)
        attempt.update(finished_at=_now(), elapsed_seconds=time.monotonic() - started, error_count=errors_count,
                       totals=dict(file_count=file_count, logical_path_bytes=logical_bytes,
                                   unique_file_count=len(groups) if not unknown_ids else None,
                                   hardlink_extra_paths=sum(group['paths'] - 1 for group in known_groups),
                                   unique_logical_bytes=sum(group['size'] for group in groups.values()) if not unknown_ids else None,
                                   allocated_bytes=known_allocation if attempt['status'] == 'completed' and not unknown_ids and not unknown_allocations else None,
                                   known_allocated_bytes=known_allocation, shared_allocated_bytes=shared_allocation,
                                   unknown_identity_files=unknown_ids, unknown_allocation_files=unknown_allocations,
                                   allocation_status='complete' if attempt['status'] == 'completed' and not unknown_ids and not unknown_allocations else 'unknown' if not known_allocation else 'partial'),
                       categories=sorted(categories.values(), key=lambda row: row['logical_path_bytes'], reverse=True),
                       datasets=sorted(datasets.values(), key=lambda row: row['logical_path_bytes'], reverse=True),
                       measurement_methods=dict(methods),
                       limits=dict(max_files=self.max_files, max_seconds=self.max_seconds),
                       notes=['逻辑字节按文件路径累计；唯一文件与分配空间按本次文件身份去硬链接重。',
                              '共享分类引用字节不能直接相加；总计已全局去重。',
                              '分配空间为可检测文件数据分配估算，不代表SSD物理扇区、CoW独占或可回收空间。',
                              '未读取文件内容，不授予清理许可；扫描期间可能有其他写入。'])
        self._save(scope, cached, attempt)
        return attempt


def create_storage_usage_router(service, submit):
    from fastapi import APIRouter, Query

    router = APIRouter(prefix='/api/v1/data/storage/usage', tags=['storage usage'])

    @router.get('')
    def summary(scope: Literal['lake', 'runtime'] = Query('lake')):
        return service.summary(scope)

    @router.post('/refresh', status_code=202)
    def refresh(body: StorageUsageRequest):
        return submit('storage_usage', body.model_dump())

    return router
