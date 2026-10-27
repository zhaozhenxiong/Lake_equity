"""Exact, journaled retirement of completed Qlib train/valid/test exchange files."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
import uuid

from filelock import FileLock

from lake_equity.jobs import ACTIVE, JobCancelled
from lake_equity.storage_usage import _safe, file_measurement
from .registry import canonical
from .retention_json import extract

RUN = re.compile(r'learn-[a-f0-9]{32}')
PLAN = re.compile(r'retention-[a-f0-9]{32}')
EXCHANGE = re.compile(r'models/fold-\d+/[A-Za-z0-9_-]+/(?:train|valid|test)\.parquet')
LOSS = ['original_qlib_worker_exchange_reuse', 'byte_complete_training_audit_of_retired_exchange']
PRESERVED = ['frozen_model_inference', 'saved_oos_predictions', 'rolling_watchlist_replay', 'original_report_bytes']
BENCHMARK_KEYS = {'schema','run_id','status','evaluation_mode','future_training_information_rows','oos_ranking_eligible',
    'research_only','strict_history_eligible','paper_allocation_eligible','independent_forward_status','account_scope',
    'evaluation_start','evaluation_end','price_materialization','model_name','config','execution_rules',
    'implementation_sha256','cost_model','decisions','orders','rejections','daily','trades','ledger','metrics',
    'terminal_state','limitations'}


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(canonical(value)); handle.flush(); os.fsync(handle.fileno())
    temporary.replace(path)


def signature(path):
    info = path.stat()
    return dict(device=str(info.st_dev), file_id=str(info.st_ino), size=info.st_size,
                mtime_ns=info.st_mtime_ns, links=info.st_nlink)


def _stop(check_cancel):
    if check_cancel and check_cancel():
        raise JobCancelled('Learning retention cancelled at a safe file boundary')


def json_files(folder):
    with os.scandir(folder) as entries:
        for entry in sorted(entries,key=lambda item:item.name):
            info=entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                raise ValueError('Reference subtree contains a symlink, junction or reparse point')
            path=Path(entry.path)
            if stat.S_ISDIR(info.st_mode):
                yield from json_files(path)
            elif entry.name.endswith('.json') and stat.S_ISREG(info.st_mode):
                yield path


class LearningRetentionService:
    def __init__(self, root, *, gate=None, busy=lambda: False):
        self.root = Path(root).resolve()
        self.learning = self.root / 'var/learning'
        self.runs = self.learning / 'runs'
        self.directory = self.learning / 'retention'
        self.gate, self.busy = gate, busy

    def _safe(self, path):
        return _safe(path, self.root)

    def _index(self):
        path = self._safe(self.learning / 'runs.db')
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as conn:
            return [dict(run_id=row[0], report_sha256=row[1], summary=json.loads(row[2]))
                    for row in conn.execute('SELECT run_id,report_sha256,summary FROM runs ORDER BY run_id')]

    def _references(self):
        protected, evidence, errors, active, deferred = set(), [], [], [], []
        for base in ('runs', 'studies', 'workflows'):
            directory = self._safe(self.learning / base)
            if not directory.is_dir():
                continue
            for folder in sorted(directory.iterdir()):
                if not folder.name.startswith('.') or not folder.name.endswith('.building'):
                    continue
                try:
                    self._safe(folder)
                    protected.update(RUN.findall(folder.name))
                    evidence.append(dict(path=folder.relative_to(self.root).as_posix(), signature=signature(folder)))
                    for path in json_files(folder):
                        self._safe(path)
                        if path.stat().st_size > 16 * 1024 * 1024:
                            checksum=path.with_suffix('.sha256')
                            if (base=='studies' and path.parent.name=='benchmarks' and path.parent.parent==folder
                                    and re.fullmatch(r'[a-f0-9]{64}\.json',path.name) and self._safe(checksum).is_file()):
                                declared=checksum.read_text(encoding='ascii').strip()
                                header,_=extract(path,{'config','account_scope','cost_model'})
                                if (re.fullmatch('[a-f0-9]{64}',declared) and isinstance(header['config'],dict)
                                        and header['account_scope']=='shared_cash_account'
                                        and header['cost_model'].get('version')=='flat_v1'):
                                    entry=dict(path=path.relative_to(self.root).as_posix(),signature=signature(path),
                                        sha256=declared,consumer='StudyService._benchmark:portfolio_value_no_external_asset_lookup')
                                    evidence.append(entry);deferred.append(entry)
                                    continue
                            raise ValueError('Building reference metadata exceeds its bounded reader')
                        raw = path.read_bytes(); value = json.loads(raw)
                        encoded = canonical(value)
                        protected.update(RUN.findall(encoded))
                        evidence.append(dict(path=path.relative_to(self.root).as_posix(), signature=signature(path),
                                             sha256=hashlib.sha256(raw).hexdigest()))
                        if path.name == 'owner.json' and value.get('pid'):
                            import psutil
                            if psutil.pid_exists(value['pid']):
                                try:
                                    if abs(psutil.Process(value['pid']).create_time()-value['process_created_at']) < .01:
                                        active.append(dict(kind='building_owner', pid=value['pid']))
                                except psutil.NoSuchProcess:
                                    pass
                except (ValueError, OSError, KeyError) as error:
                    errors.append(dict(path=folder.name, error=str(error)))
        jobs = self._safe(self.root / 'var/jobs/jobs.db')
        if jobs.is_file():
            try:
                with closing(sqlite3.connect(jobs.as_uri()+'?mode=ro', uri=True)) as conn:
                    for raw, in conn.execute('SELECT body FROM jobs'):
                        row = json.loads(raw)
                        if row.get('status') in ACTIVE and row.get('kind') != 'learning_retention':
                            active.append(dict(kind='job', id=row['id'], status=row['status']))
            except (sqlite3.Error, ValueError) as error:
                errors.append(dict(path=str(jobs), error=str(error)))
        import psutil
        for process in psutil.process_iter(['pid', 'cmdline']):
            try:
                command = ' '.join(process.info.get('cmdline') or [])
                if process.pid != os.getpid() and 'qlib_worker.py' in command and str(self.root).casefold() in command.casefold():
                    active.append(dict(kind='qlib_worker', pid=process.pid))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        value = dict(protected_run_ids=sorted(protected), building_evidence=evidence,
                     errors=errors, active=sorted(active, key=canonical),deferred_benchmark_checks=deferred)
        return value, sha(value)

    def _artifact_path(self, run_id, relative):
        if not RUN.fullmatch(run_id) or not EXCHANGE.fullmatch(relative):
            raise ValueError('Outside the completed Qlib exchange-file whitelist')
        return self._safe(self.runs / run_id / relative)

    def _run_metadata(self, row, allowed_missing=()):
        run_id = row['run_id']
        if not isinstance(run_id, str) or not RUN.fullmatch(run_id):
            raise ValueError('Invalid indexed learning run')
        folder = self._safe(self.runs / run_id)
        path = self._safe(folder / 'report.json')
        if row['summary'].get('status') != 'completed' or row['summary'].get('family') not in {
                'cross_sectional_return_v1', 'frozen_factor_refit_v1'}:
            raise ValueError('Run is not an identifiable completed training delivery')
        metadata, _ = extract(path, {'artifacts'})
        artifacts = metadata['artifacts']
        if not isinstance(artifacts, dict) or not artifacts or len(artifacts) > 20000:
            raise ValueError('Incomplete or unbounded artifact manifest')
        retired = {entry['artifact_path'] for entry in self.status(run_id)['retired_files']} | set(allowed_missing)
        for name, entry in artifacts.items():
            if (not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts
                    or not isinstance(entry, dict) or not re.fullmatch('[a-f0-9]{64}', str(entry.get('sha256')))
                    or not isinstance(entry.get('size_bytes'), int) or entry['size_bytes'] < 0):
                raise ValueError('Unrecognized artifact manifest record')
            asset = self._safe(folder / name)
            if name not in retired and (not asset.is_file() or asset.stat().st_size != entry['size_bytes']):
                raise ValueError('Original artifact manifest is incomplete')
        required = ['dataset.parquet'] + ([] if row['summary']['family'] == 'frozen_factor_refit_v1' else ['predictions.parquet'])
        for name in required:
            entry = artifacts.get(name)
            asset = self._safe(folder / name)
            if not isinstance(entry, dict) or not asset.is_file() or asset.stat().st_size != entry.get('size_bytes'):
                raise ValueError('Required saved replay dataset/predictions are unavailable')
        return path, artifacts

    def candidates(self):
        if (self.gate and self.gate.closed) or self.busy():
            raise ValueError('Maintenance or another writer is active')
        return self._inventory()

    def _inventory(self):
        references, fingerprint = self._references()
        items, unknown = [], []
        for row in self._index():
            try:
                report_path, artifacts = self._run_metadata(row)
                run_items = []
                retired = {entry['object_id'] for entry in self.status(row['run_id'])['retired_files']}
                models = {}
                for relative, entry in artifacts.items():
                    if not EXCHANGE.fullmatch(relative):
                        continue
                    path = self._artifact_path(row['run_id'], relative)
                    object_id = 'qlib-' + sha(dict(run_id=row['run_id'], path=relative))
                    if object_id in retired:
                        continue
                    if not isinstance(entry, dict) or not re.fullmatch('[a-f0-9]{64}', str(entry.get('sha256'))):
                        raise ValueError('Exchange artifact lacks its original SHA256')
                    parent = path.parent
                    if parent not in models:
                        detail_path=self._safe(parent/'model.json')
                        if detail_path.stat().st_size>16*1024*1024:
                            raise ValueError('Frozen model metadata exceeds its bounded reader')
                        details = json.loads(detail_path.read_text(encoding='utf-8'))
                        if not isinstance(details,dict):
                            raise ValueError('Unrecognized frozen model metadata')
                        preserved_assets=['model.json','preprocessing.json']
                        if details.get('model_type') in {'lightgbm','mlp'}:
                            preserved_assets.append('weights.npz' if details['model_type']=='mlp' else 'model.txt')
                        for name in preserved_assets:
                            key = (Path(relative).parent / name).as_posix()
                            asset=self._safe(parent/name)
                            if key not in artifacts or not asset.is_file():
                                raise ValueError('Frozen model/preprocessing evidence is incomplete')
                            if asset.stat().st_size>64*1024*1024:
                                raise ValueError('Frozen model validation exceeds its bounded asset reader')
                            with asset.open('rb') as handle:
                                if hashlib.file_digest(handle,'sha256').hexdigest()!=artifacts[key]['sha256']:
                                    raise ValueError('Frozen model/preprocessing original SHA256 mismatch')
                        if details.get('backend') != 'qlib' or not str(details.get('engine', '')).startswith('qlib.'):
                            raise ValueError('Not a recognized Qlib exchange contract')
                        from .models import load_saved
                        try:
                            load_saved(parent)
                        except Exception as error:
                            raise ValueError('Original saved model is not loadable ('+
                                parent.relative_to(self.runs/row['run_id']).as_posix()+'): '+str(error)) from error
                        models[parent] = details
                    if not path.is_file() or path.stat().st_size != entry.get('size_bytes'):
                        raise ValueError('Exchange file is missing or differs from the original length')
                    info = path.stat()
                    identity, allocated, _ = file_measurement(path, info)
                    reasons = []
                    if row['run_id'] in references['protected_run_ids']:
                        reasons.append('building_or_checkpoint_reference')
                    if references['active']:
                        reasons.append('active_job_or_worker')
                    if references['errors']:
                        reasons.append('unverified_reference_metadata')
                    if info.st_nlink != 1:
                        reasons.append('hardlink_set_not_exclusive')
                    if identity is None or allocated is None:
                        reasons.append('file_identity_or_allocation_unknown')
                    run_items.append(dict(object_id=object_id, run_id=row['run_id'], path_rel=path.relative_to(self.root).as_posix(),
                        artifact_path=relative, original_sha256=entry['sha256'], original_report_sha256=row['report_sha256'],
                        report_signature=signature(report_path), file_signature=signature(path), file_identity=list(identity) if identity else None,
                        logical_bytes=info.st_size, allocated_bytes=allocated,
                        estimated_reclaim_bytes=allocated if not reasons else 0,
                        status='protected' if reasons else 'candidate', reasons=reasons or ['completed_qlib_training_exchange'],
                        capability_loss=LOSS, preserved_capabilities=PRESERVED))
                items.extend(run_items)
            except (OSError, ValueError, KeyError, TypeError, ImportError, RuntimeError) as error:
                unknown.append(dict(run_id=row['run_id'], status='unknown', reason=str(error)))
        items.sort(key=lambda item: (-int(item['estimated_reclaim_bytes'] or 0), item['object_id']))
        return dict(schema='learning_retention_candidates_v1', status='completed', items=items, unknown_runs=unknown,
            references=references, reference_fingerprint=fingerprint, capability_loss=LOSS,
            totals=dict(candidate_count=sum(row['status']=='candidate' for row in items),
                protected_count=sum(row['status']=='protected' for row in items), unknown_runs=len(unknown),
                estimated_reclaim_bytes=sum(row['estimated_reclaim_bytes'] or 0 for row in items)),
            verification='metadata preview; original report and exchange SHA256 rechecked before any removal')

    def status(self, run_id):
        if not isinstance(run_id, str) or not RUN.fullmatch(run_id):
            raise ValueError('Invalid retention source run')
        path = self._safe(self.directory / 'runs' / (run_id+'.json'))
        if not path.is_file():
            return dict(status='not_retired', retired_files=[], capability_loss=[],
                        full_training_exchange_audit='not_reverified',original_report_unchanged=True)
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('sha256') != sha(value.get('payload')) or value['payload'].get('run_id') != run_id:
            raise ValueError('Learning retention manifest identity mismatch')
        source=next((row for row in self._index() if row['run_id']==run_id),None)
        if not source or source['report_sha256']!=value['payload']['original_report_sha256']:
            raise ValueError('Learning retention original report identity changed')
        return dict(value['payload'], original_report_unchanged=True)

    @staticmethod
    def _objects(object_ids):
        if (not isinstance(object_ids, list) or not 1 <= len(object_ids) <= 2000
                or len(set(object_ids)) != len(object_ids)
                or any(not isinstance(item, str) or not re.fullmatch('qlib-[a-f0-9]{64}', item) for item in object_ids)):
            raise ValueError('Select 1..2000 unique previewed exchange object IDs')
        return sorted(object_ids)

    def _plan_path(self, plan_id):
        if not isinstance(plan_id, str) or not PLAN.fullmatch(plan_id):
            raise ValueError('Invalid learning retention plan identity')
        return self._safe(self.directory / 'plans' / (plan_id+'.json'))

    def _load_plan(self, plan_id):
        value = json.loads(self._plan_path(plan_id).read_text(encoding='utf-8'))
        content = {key:item for key,item in value.items() if key != 'plan_id'}
        if value.get('plan_id') != plan_id or plan_id != 'retention-'+sha(content)[:32]:
            raise ValueError('Learning retention plan integrity mismatch')
        return value

    def review(self, object_ids):
        selected = self._objects(object_ids)
        inventory = self.candidates()
        by_id = {row['object_id']:row for row in inventory['items'] if row['status']=='candidate'}
        if not set(selected) <= by_id.keys():
            raise ValueError('Selection contains protected, unknown or changed exchange objects')
        token = secrets.token_urlsafe(32)
        value = dict(schema='qlib_exchange_retention_plan_v1', created_at=now(),
            expires_at=(datetime.now(timezone.utc)+timedelta(minutes=30)).isoformat(),
            items=[by_id[key] for key in selected], object_ids=selected,
            reference_fingerprint=inventory['reference_fingerprint'],
            confirmation_token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            capability_loss=LOSS, preserved_capabilities=PRESERVED,
            estimated_reclaim_bytes=sum(by_id[key]['estimated_reclaim_bytes'] for key in selected),
            reader_contract_sha256={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                    for name in ('models.py','workflow.py','registry.py','study.py','service.py')})
        value['plan_id']='retention-'+sha(value)[:32]
        atomic(self._plan_path(value['plan_id']),value)
        return {**self.read_plan(value['plan_id']), 'confirmation_token':token}

    def check_confirmation(self, plan_id, token, object_ids, *, confirmed=False, maintenance_confirmed=False):
        plan = self._load_plan(plan_id)
        if confirmed is not True or maintenance_confirmed is not True:
            raise ValueError('Exact retirement and maintenance confirmation are both required')
        if not isinstance(token,str) or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),plan['confirmation_token_sha256']):
            raise ValueError('Invalid learning retention confirmation token')
        if self._objects(object_ids) != plan['object_ids']:
            raise ValueError('Confirmation must cover the unchanged exact reviewed object set')
        if datetime.now(timezone.utc) >= datetime.fromisoformat(plan['expires_at']):
            raise ValueError('Learning retention approval expired; review remaining objects again')
        return plan

    def _events(self, plan_id):
        path=self._safe(self.directory/'journals'/(plan_id+'.jsonl'))
        if not path.is_file():
            return [],0
        events=[]; previous=''; valid=0
        with path.open('rb') as handle:
            for line in handle:
                if not line.endswith(b'\n'):
                    break  # A crash can leave only the last append incomplete.
                event=json.loads(line)
                payload={key:value for key,value in event.items() if key!='sha256'}
                if (event.get('plan_id')!=plan_id or event.get('sequence')!=len(events)
                        or event.get('previous_sha256')!=previous or event.get('sha256')!=sha(payload)):
                    raise ValueError('Learning retention journal integrity mismatch')
                previous=event['sha256'];events.append(event);valid+=len(line)
        return events,valid

    def _event(self, plan_id, **fields):
        events,valid=self._events(plan_id)
        event=dict(plan_id=plan_id,sequence=len(events),previous_sha256=events[-1]['sha256'] if events else '',at=now(),**fields)
        event['sha256']=sha(event)
        path=self._safe(self.directory/'journals'/(plan_id+'.jsonl'));path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('r+b' if path.is_file() else 'w+b') as handle:
            handle.seek(valid);handle.truncate();handle.write((canonical(event)+'\n').encode());handle.flush();os.fsync(handle.fileno())

    def read_plan(self, plan_id):
        plan=self._load_plan(plan_id);events,_=self._events(plan_id)
        statuses=[event['status'] for event in events if event.get('action')=='plan']
        removed={event['object_id'] for event in events if event.get('action')=='removed'}
        if any(event.get('object_id') not in plan['object_ids'] for event in events if event.get('object_id')):
            raise ValueError('Journal contains an object outside the confirmed set')
        expired=datetime.now(timezone.utc)>=datetime.fromisoformat(plan['expires_at'])
        job_path=self._safe(self.directory/'jobs'/(plan_id+'.json'))
        job=json.loads(job_path.read_text(encoding='utf-8')) if job_path.is_file() else {}
        job_status=None;job_progress={}
        jobs=self._safe(self.root/'var/jobs/jobs.db')
        if job and jobs.is_file():
            with closing(sqlite3.connect(jobs.as_uri()+'?mode=ro',uri=True)) as conn:
                row=conn.execute('SELECT body FROM jobs WHERE id=?',(job.get('job_id'),)).fetchone()
                if row:
                    body=json.loads(row[0]);job_status=body.get('status');job_progress=body.get('progress') or {}
        shown=statuses[-1] if statuses else 'expired' if expired else 'ready'
        if job_status in ACTIVE and shown in {'ready','failed','cancelled','expired'}:
            shown=job_status
        return {**{key:value for key,value in plan.items() if key!='confirmation_token_sha256'},
            'status':shown,'job_status':job_status,'progress':job_progress,
            'expired':expired,'removed_count':len(removed),'journal':events,'job_id':job.get('job_id'),
            'can_resume':bool(statuses and statuses[-1] in {'cancelled','failed'} and not expired),
            'can_reconcile':bool(any(event.get('action')=='intent' for event in events)
                                 and (expired or (statuses and statuses[-1] in {'cancelled','failed'}))),
            'can_cancel':bool(job_status in ACTIVE and (not statuses or statuses[-1]=='running'))}

    def bind_job(self, plan_id, job_id):
        self._load_plan(plan_id)
        atomic(self._safe(self.directory/'jobs'/(plan_id+'.json')),dict(job_id=job_id))

    @contextmanager
    def _maintenance(self):
        from cnequity.serve.storage import MaintenanceGate
        server_lease=None
        if self.gate is None:
            # A CLI cannot close the gate inside another server process.
            server_lease=FileLock(str(self._safe(self.root/'var/server.lock')),timeout=0)
            server_lease.acquire()
        gate=self.gate or MaintenanceGate()
        closed=False
        try:
            with gate.condition:
                if gate.closed or self.busy():
                    raise ValueError('Another maintenance operation or writer is active')
                gate.close();closed=True
            gate.drain(timeout=30)
            with FileLock(str(self._safe(self.learning/'compute.lock')),timeout=0):
                yield
        finally:
            if closed:gate.reopen()
            if server_lease:server_lease.release()

    def _hash(self, path, check_cancel, progress):
        value=hashlib.sha256();total=0;last=0.
        with path.open('rb') as handle:
            for block in iter(lambda:handle.read(4*1024*1024),b''):
                _stop(check_cancel);value.update(block);total+=len(block)
                if progress and time.monotonic()-last>.5:
                    progress(dict(phase='verify_exchange_sha256',path=path.relative_to(self.root).as_posix(),bytes=total));last=time.monotonic()
        return value.hexdigest()

    def _matches_file(self,item,path):
        info=path.stat();identity,allocated,_=file_measurement(path,info)
        return (signature(path)==item['file_signature'] and identity is not None
                and list(identity)==item['file_identity'] and allocated==item['allocated_bytes'] and info.st_nlink==1)

    def _record_retired(self, item, plan_id):
        existing=self.status(item['run_id'])
        rows={row['object_id']:row for row in existing['retired_files']}
        if item['object_id'] in rows:
            if rows[item['object_id']]['original_sha256']!=item['original_sha256']:
                raise ValueError('Existing retirement proof differs from the original artifact')
            return
        rows[item['object_id']]={key:item[key] for key in ('object_id','artifact_path','original_sha256',
            'original_report_sha256','file_signature','file_identity','logical_bytes','allocated_bytes')}
        rows[item['object_id']].update(plan_id=plan_id,retired_at=now(),reason='completed_qlib_training_exchange')
        payload=dict(schema='qlib_exchange_retired_v1',run_id=item['run_id'],status='training_exchange_retired',
            original_report_sha256=item['original_report_sha256'],retired_files=list(rows.values()),
            capability_loss=LOSS,preserved_capabilities=PRESERVED,
            full_training_exchange_audit='unavailable_for_retired_exchange_files')
        atomic(self._safe(self.directory/'runs'/(item['run_id']+'.json')),dict(payload=payload,sha256=sha(payload)))

    def execute(self, plan_id, token, object_ids, *, confirmed=False, maintenance_confirmed=False,
                progress=None, check_cancel=None):
        plan=self.check_confirmation(plan_id,token,object_ids,confirmed=confirmed,maintenance_confirmed=maintenance_confirmed)
        self._safe(self.directory).mkdir(parents=True,exist_ok=True)
        with FileLock(str(self._safe(self.directory/'execute.lock')),timeout=0):
            with self._maintenance():
                try:
                    self._event(plan_id,action='plan',status='running')
                    _stop(check_cancel)
                    references,fingerprint=self._references()
                    if fingerprint!=plan['reference_fingerprint'] or references['active'] or references['errors']:
                        raise ValueError('References or activity changed; review again')
                    for benchmark in references['deferred_benchmark_checks']:
                        path=self._safe(self.root/benchmark['path'])
                        last_benchmark_notice=0.
                        def benchmark_progress(count):
                            nonlocal last_benchmark_notice
                            if progress and time.monotonic()-last_benchmark_notice>.5:
                                progress(dict(phase='verify_building_benchmark',path=benchmark['path'],bytes=count))
                                last_benchmark_notice=time.monotonic()
                        metadata,actual=extract(path,{'schema','status','account_scope'},complete=True,
                            check_cancel=lambda:_stop(check_cancel),allowed_keys=BENCHMARK_KEYS,progress=benchmark_progress)
                        if (actual!=benchmark['sha256'] or metadata['schema']!='lake_target_weight_backtest_v1'
                                or metadata['status']!='completed' or metadata['account_scope']!='shared_cash_account'
                                or signature(path)!=benchmark['signature']):
                            raise ValueError('Deferred building benchmark consumer contract failed verification')
                    if any(hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()!=expected
                           for name,expected in plan['reader_contract_sha256'].items()):
                        raise ValueError('Learning reader contract changed since review')
                    events,_=self._events(plan_id)
                    state={event['object_id']:event['action'] for event in events if event.get('object_id')}
                    by_run={}
                    for item in plan['items']:by_run.setdefault(item['run_id'],[]).append(item)
                    indexed={row['run_id']:row for row in self._index()}
                    for run_id,items in by_run.items():
                        row=indexed.get(run_id)
                        if not row or row['report_sha256']!=items[0]['original_report_sha256']:
                            raise ValueError('Original completed run index changed')
                        allowed=[item['artifact_path'] for item in items if state.get(item['object_id']) in {'intent','removed'}]
                        path,artifacts=self._run_metadata(row,allowed)
                        if signature(path)!=items[0]['report_signature']:
                            raise ValueError('Original report file identity changed')
                        last_notice=0.
                        def notify_bytes(count):
                            nonlocal last_notice
                            if progress and time.monotonic()-last_notice>.5:
                                progress(dict(phase='verify_report_stream',run_id=run_id,bytes=count));last_notice=time.monotonic()
                        metadata,report_sha=extract(path,{'artifacts','run_id','status','family','sources'},complete=True,
                            check_cancel=lambda:_stop(check_cancel),progress=notify_bytes)
                        if (report_sha!=row['report_sha256'] or metadata['run_id']!=run_id or metadata['status']!='completed'
                                or metadata['family']!=row['summary']['family']
                                or metadata['artifacts']!=artifacts):
                            raise ValueError('Original report SHA256 or delivery identity mismatch')
                        # Frozen inference loads only these assets; rolling readers use
                        # the root dataset/predictions, never the worker exchange files.
                        from .models import load_saved
                        for parent in {self._artifact_path(run_id,item['artifact_path']).parent for item in items}:
                            for asset in parent.iterdir():
                                if asset.name in {'model.json','preprocessing.json','model.txt','weights.npz'}:
                                    key=asset.relative_to(self.runs/run_id).as_posix()
                                    if key not in artifacts or self._hash(self._safe(asset),check_cancel,None)!=artifacts[key]['sha256']:
                                        raise ValueError('Required frozen inference asset changed')
                            load_saved(parent)
                    # Complete verification before the first irreversible unlink.
                    for item in plan['items']:
                        path=self._artifact_path(item['run_id'],item['artifact_path'])
                        previous=state.get(item['object_id'])
                        if not path.exists():
                            if previous not in {'intent','removed'}:
                                raise ValueError('Exchange disappeared without a journaled deletion intent')
                            continue
                        if previous=='removed':
                            raise ValueError('A retired exchange file reappeared')
                        if not self._matches_file(item,path) or self._hash(path,check_cancel,progress)!=item['original_sha256']:
                            raise ValueError('Exchange file identity or SHA256 changed')
                    for number,item in enumerate(plan['items']):
                        _stop(check_cancel)
                        path=self._artifact_path(item['run_id'],item['artifact_path'])
                        if path.exists():
                            if not self._matches_file(item,path):
                                raise ValueError('Exchange changed after verification')
                            self._event(plan_id,action='intent',object_id=item['object_id'],original_sha256=item['original_sha256'])
                            state[item['object_id']]='intent'
                            path.unlink()
                        elif state.get(item['object_id']) not in {'intent','removed'}:
                            raise ValueError('Exchange disappeared after verification without our deletion intent')
                        self._record_retired(item,plan_id)
                        if state.get(item['object_id'])!='removed':
                            self._event(plan_id,action='removed',object_id=item['object_id'],allocated_bytes=item['allocated_bytes'])
                            state[item['object_id']]='removed'
                        if progress:progress(dict(phase='retire_verified_exchange',current=number+1,total=len(plan['items'])))
                    self._event(plan_id,action='plan',status='completed')
                    outcome='completed'
                except JobCancelled as error:
                    self._event(plan_id,action='plan',status='cancelled',error=str(error))
                    outcome='cancelled'
                except (OSError,ValueError,KeyError,ImportError) as error:
                    self._event(plan_id,action='plan',status='failed',error=str(error))
                    outcome='failed'
                # While this worker is returning, its generic job can still be
                # running. That polling state must not overwrite the outcome.
                return {**self.read_plan(plan_id),'status':outcome}

    def reconcile(self,plan_id,*,maintenance_confirmed=False):
        """Repair only missing files with durable old intents, even after approval expiry."""
        if maintenance_confirmed is not True:
            raise ValueError('Maintenance confirmation is required for audit reconciliation')
        plan=self._load_plan(plan_id)
        with FileLock(str(self._safe(self.directory/'execute.lock')),timeout=0):
            with self._maintenance():
                events,_=self._events(plan_id)
                states={event['object_id']:event['action'] for event in events if event.get('object_id')}
                indexed={row['run_id']:row for row in self._index()}
                verified=set()
                for item in plan['items']:
                    path=self._artifact_path(item['run_id'],item['artifact_path'])
                    if states.get(item['object_id']) not in {'intent','removed'} or path.exists():
                        continue
                    if indexed.get(item['run_id'],{}).get('report_sha256')!=item['original_report_sha256']:
                        raise ValueError('Reconciliation source report identity changed')
                    if item['run_id'] not in verified:
                        report_path=self._safe(self.runs/item['run_id']/'report.json')
                        if signature(report_path)!=item['report_signature']:
                            raise ValueError('Reconciliation original report file changed')
                        _,actual=extract(report_path,{'artifacts','run_id'},complete=True)
                        if actual!=item['original_report_sha256']:
                            raise ValueError('Reconciliation original report SHA256 mismatch')
                        verified.add(item['run_id'])
                    self._record_retired(item,plan_id)
                    if states[item['object_id']]!='removed':
                        self._event(plan_id,action='removed',object_id=item['object_id'],reconciled=True,
                                    allocated_bytes=item['allocated_bytes'])
                self._event(plan_id,action='plan',status='reconciled',new_deletions=0)
                return self.read_plan(plan_id)
