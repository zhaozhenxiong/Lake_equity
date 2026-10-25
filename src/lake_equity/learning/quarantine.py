"""Read-only preview and explicitly confirmed cleanup of failed workflow build directories.

Failed builds intentionally keep an isolated ``.<kind>-<id>.building`` directory
(``docs/LEARNING.md``); this module only lists them and removes exactly the
confirmed objects after a fresh preview/recompute, with an audit record.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3

from filelock import FileLock

from lake_equity.jobs import ACTIVE

from .registry import canonical

BUILD_NAME = re.compile(r'^\.(?P<kind>scan|backtest|learn|pipeline|bplan)-(?P<id>[0-9a-f]{32})\.building$')
MAX_ITEMS = 500
JOB_MATCH_SECONDS = 900


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class QuarantineService:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'var/learning/workflows'
        self.jobs_db = self.root / 'var/jobs/jobs.db'
        self.audit_path = self.root / 'var/logs/ai_changes.jsonl'
        self.database = self.root / 'var/learning/workflows.db'

    # -- read-only listing -------------------------------------------------
    def _jobs(self):
        if not self.jobs_db.is_file():
            return []
        try:
            with closing(sqlite3.connect(self.jobs_db.as_uri() + '?mode=ro', uri=True)) as connection:
                rows = [json.loads(row[0]) for row in
                        connection.execute('SELECT body FROM jobs ORDER BY rowid DESC LIMIT 500')]
        except (sqlite3.Error, ValueError, OSError):
            return []
        return rows

    @staticmethod
    def _stamp(value):
        try:
            return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        except (TypeError, ValueError):
            return None

    def _match_job(self, item, jobs):
        kind, created = item['kind'], item['created_at_epoch']
        best, best_gap = None, None
        for job in jobs:
            if job.get('kind') != kind:
                continue
            if job.get('run_id') == f"{item['kind']}-{item['id']}":
                best = job
                break
            stamp = self._stamp(job.get('updated_at') or job.get('created_at'))
            if stamp is None:
                continue
            gap = abs(stamp - created)
            if gap <= JOB_MATCH_SECONDS and (best_gap is None or gap < best_gap):
                best, best_gap = job, gap
        if best is None:
            return dict(job_id='', job_status='', job_kind='', job_error='', job_run_id='')
        return dict(job_id=best.get('id') or '', job_status=best.get('status') or '',
                    job_kind=best.get('kind') or '', job_error=str(best.get('error') or '')[:500],
                    job_run_id=best.get('run_id') or '')

    def listing(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        jobs = self._jobs()
        items = []
        if self.directory.is_symlink():
            raise ValueError('Workflow directory cannot be a symlink')
        for folder in sorted(self.directory.iterdir()):
            match = BUILD_NAME.match(folder.name)
            if not match or not folder.is_dir() or folder.is_symlink() or len(items) >= MAX_ITEMS:
                continue
            resolved = folder.resolve()
            if resolved.parent != self.directory.resolve():
                continue
            try:
                files = [path for path in resolved.rglob('*') if path.is_file() and not path.is_symlink()]
                size = sum(path.stat().st_size for path in files)
                stat = resolved.stat()
            except OSError:
                continue
            item = dict(id=match.group('id'), kind=match.group('kind'), dir_name=folder.name,
                        path_rel=resolved.relative_to(self.root.resolve()).as_posix(),
                        created_at=datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec='seconds'),
                        created_at_epoch=stat.st_mtime, bytes=size, file_count=len(files),
                        run_id='', job_id='', job_status='', job_kind='', job_error='')
            item.update(self._match_job(item, jobs))
            if item['job_status'] in ACTIVE:
                continue
            if item['job_run_id'] and item['job_run_id'] == f"{item['kind']}-{item['id']}":
                item['run_id'] = item['job_run_id']
            item.pop('job_run_id', None)
            item.pop('created_at_epoch', None)
            items.append(item)
        plan_id = 'qplan-' + _sha([{key: item[key] for key in
                                   ('id', 'kind', 'dir_name', 'path_rel', 'created_at', 'bytes', 'file_count')}
                                  for item in items])[:32]
        return dict(plan_id=plan_id, items=items, total_bytes=sum(item['bytes'] for item in items),
                    note='只读预览；确认后仅删除这些失败构建目录，不删除已保存报告、模型、账户或原始数据。')

    # -- confirmed cleanup -------------------------------------------------
    def confirm(self, plan_id, ids, confirm):
        if confirm is not True:
            raise ValueError('删除失败构建目录需要显式确认（confirm=true）')
        if not isinstance(plan_id, str) or not plan_id.startswith('qplan-'):
            raise ValueError('plan_id must come from a fresh quarantine preview')
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) for item in ids):
            raise ValueError('ids must be a nonempty list of previewed item ids')
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate ids in confirmation')
        lease = self.root / 'var/learning/compute.lock'
        lease.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lease), timeout=0):
            return self._confirm(plan_id, ids)

    def _confirm(self, plan_id, ids):
        listing = self.listing()
        if listing['plan_id'] != plan_id:
            raise ValueError('失败构建目录清单已变化；请刷新预览后重新确认')
        known = {item['id']: item for item in listing['items']}
        missing = [item for item in ids if item not in known]
        if missing:
            raise ValueError('ids must belong to the previewed quarantine plan: ' + ', '.join(missing))
        deleted, freed = [], 0
        for identity in ids:
            item = known[identity]
            folder = (self.root / item['path_rel']).resolve()
            if folder.parent != self.directory.resolve() or folder.is_symlink() or not folder.is_dir():
                raise ValueError('Refusing to delete an unexpected path: ' + item['path_rel'])
            if not BUILD_NAME.match(folder.name):
                raise ValueError('Refusing to delete a non-build directory: ' + folder.name)
            shutil.rmtree(folder)
            deleted.append(item['path_rel'])
            freed += int(item['bytes'])
        event = dict(event='workflow_build_quarantine_confirmed', at=_now(), plan_id=plan_id,
                     deleted=deleted, bytes_freed=freed,
                     note='Failed build directories removed after fresh preview + explicit confirmation; reports/models/accounts/raw data untouched.')
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        return dict(deleted=deleted, bytes_freed=freed, at=event['at'], plan_id=plan_id,
                    audit_event='workflow_build_quarantine_confirmed')
