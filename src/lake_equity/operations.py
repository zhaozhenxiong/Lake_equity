"""First-party v1 operations, backed by explicit plain-service contracts."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from cnequity.i18n import _, use_language
from cnequity.serve.ops.catalog import OpsError


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Preview(Body):
    op: str
    params: dict = Field(default_factory=dict)


class Start(Body):
    preview_id: str
    launch_token: str
    acknowledged: list[str] = Field(default_factory=list)


class Values(Body):
    values: dict


class Apply(Body):
    token: str
    acknowledged: StrictBool = False


class Schedule(Body):
    daily: StrictBool
    stale: StrictBool
    daily_run_at: str
    stale_run_at: str
    backup: StrictBool = False
    backup_run_at: str = "23:00"
    backup_datasets: list[str] = Field(default_factory=list)
    backup_root: str | None = None
    events: StrictBool = False
    events_group: str | None = None
    events_interval_minutes: int = Field(60, ge=1, le=1440)
    daily_packs: list[str] = Field(default_factory=list)


class StorageReview(Body):
    kind: Literal["revisions", "staging"]
    phase: Literal["mark", "purge"] = "purge"
    resume_plan_id: str | None = None
    object_ids: list[str] | None = None


class StorageConfirmation(Body):
    review_id: str
    confirmation_token: str
    confirmed: StrictBool
    maintenance_confirmed: StrictBool = False


class LearningRetentionReview(Body):
    object_ids: list[str] = Field(min_length=1, max_length=2000)


class LearningRetentionConfirmation(LearningRetentionReview):
    plan_id: str
    confirmation_token: str
    confirmed: StrictBool
    maintenance_confirmed: StrictBool


class LearningRetentionReconcile(Body):
    maintenance_confirmed: StrictBool


class DataOperations:
    # A reviewed list, independent of arbitrary HTTP handlers or shell text.
    ALLOWED = frozenset({"snapshot.create", "snapshot.verify", "snapshot.restore", "daily.full", "daily.group",
        "daily.stale", "events.run", "run.retry", "run.retry_failed_groups", "run.compact", "diag.doctor",
        "backfill.run", "derive.run", "check.status", "check.verify", "check.audit", "check.sources_probe",
        "maint.stats_rebuild", "config.upgrade_preview"})

    def __init__(self, config, *, root=None, busy=lambda: False):
        from cnequity.serve.ops.runner import OpsService
        from cnequity.serve.storage import MaintenanceGate
        self.config = config
        self.root = Path(root) if root is not None else Path(config.data_root).parent.parent
        self.gate = MaintenanceGate()
        self.active_jobs = lambda: False
        self.external_busy = busy
        self._settings = self._schedule = self._storage = None
        # The upstream guard deliberately covers all local writes. Its generic
        # maintenance message is adapted below without weakening that guard.
        self.ops = OpsService(config, storage_busy=lambda: self.external_busy() or self.maintenance_busy())

    def maintenance_busy(self):
        return self.gate.closed or bool(self._storage and self._storage.busy)

    def busy_reason(self, message):
        if (message == _("Storage maintenance is running; wait for it to finish before starting.")
                and self.external_busy() and not self.maintenance_busy()):
            return "已有本地数据或研究写入任务正在运行，请在任务与报告中查看进度，等待完成后再执行。"
        return message

    def settings(self):
        if self._settings is None:
            from cnequity.serve.ops.settings import SettingsService
            self._settings = SettingsService(self.config)
        return self._settings

    def schedules(self):
        if self._schedule is None:
            from cnequity.serve.ops.scheduler import ScheduleService
            self._schedule = ScheduleService(self.config)
        return self._schedule

    def storage(self):
        if self._storage is None:
            from lake_equity.storage_protection import ProtectedStorageMaintenance
            self._storage = ProtectedStorageMaintenance(self.root, self.config, gate=self.gate,
                active=lambda: self.external_busy() or self.active_jobs())
        return self._storage

    def home(self):
        with use_language("zh_CN"):
            value = self.ops.home()
        value["occupancy"].update(storage_busy=self.maintenance_busy(), local_job_busy=bool(self.external_busy()))
        retired = []
        for item in value["operations"]:
            if item.get("id") not in self.ALLOWED:
                item.update(available=False, unavailable_reason="Existing-lake initialization is retired; it can replace the independently verified trading calendar")
                retired.append(item["id"])
        value["retired_operations"] = retired
        value["capabilities"] = {"settings": True, "schedules": True, "backups": True,
                                 "storage_preview": True, "storage_confirm": True,
                                 "storage_confirm_reason": "Exact review, retained research references, idle jobs and maintenance confirmation are required",
                                 "legacy_console_retired": True, "automatic_scheduler_started": False}
        return value

    def preview(self, op, params):
        if op not in self.ALLOWED:
            raise ValueError("Operation is outside the LakeEquity whitelist")
        if op == "backfill.run" and params.get("dataset") == "trading_calendar":
            raise ValueError("The controlled trading calendar cannot be rebuilt through source backfill")
        with use_language("zh_CN"):
            result = self.ops.preview(op, params)
            result["blockers"] = [self.busy_reason(message) for message in result["blockers"]]
        return result

    def start(self, preview_id, launch_token, acknowledged):
        with use_language("zh_CN"):
            try:
                return self.ops.start(preview_id, launch_token, acknowledged, requested_by="LakeEquity local user")
            except OpsError as error:
                message = self.busy_reason(str(error))
                if message != str(error):
                    raise OpsError(message) from error
                raise


def create_operations_router(service: DataOperations):
    router = APIRouter(prefix="/api/v1/data", tags=["data operations"])

    @router.get("/operations")
    def home():
        return service.home()

    @router.post("/operations/preview")
    def preview(body: Preview):
        return service.preview(body.op, body.params)

    @router.post("/operations/start", status_code=202)
    def start(body: Start):
        return service.start(body.preview_id, body.launch_token, body.acknowledged)

    @router.get("/operations/jobs")
    def jobs(limit: int = Query(50, ge=1, le=50)):
        return {"jobs": service.ops.list_jobs(limit)}

    @router.get("/operations/jobs/{job_id}")
    def job(job_id: str):
        return service.ops.get_job(job_id)

    @router.post("/operations/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return service.ops.cancel(job_id, requested_by="LakeEquity local user")

    @router.get("/settings")
    def settings():
        return service.settings().home()

    @router.post("/settings/preview")
    def settings_preview(body: Values):
        return service.settings().preview(body.values)

    @router.post("/settings/apply")
    def settings_apply(body: Apply):
        return service.settings().apply(body.token, acknowledged=body.acknowledged)

    @router.get("/schedules")
    def schedules():
        return service.schedules().home()

    @router.post("/schedules/preview")
    def schedules_preview(body: Schedule):
        return service.schedules().preview(**body.model_dump())

    @router.post("/schedules/apply")
    def schedules_apply(body: Apply):
        return service.schedules().apply(body.token, acknowledged=body.acknowledged, requested_by="LakeEquity local user")

    @router.get("/backups")
    def backups(root: str | None = None):
        from cnequity.serve.ops.backups import inventory
        if root is not None and not Path(root).is_absolute():
            raise ValueError("Backup root must be absolute")
        return inventory(service.config, root)

    @router.get("/storage")
    def storage():
        return {**service.storage().summary(), "confirm_enabled": True,
                "confirm_reason": "Retained model/run/read references are protected and rechecked before confirmation"}

    @router.post("/storage/reviews", status_code=202)
    def storage_review(body: StorageReview):
        return service.storage().review(**body.model_dump())

    @router.get("/storage/jobs/{job_id}")
    def storage_job(job_id: str):
        return service.storage().job(job_id)

    @router.post("/storage/confirm", status_code=202)
    def storage_confirm(body: StorageConfirmation):
        return service.storage().confirm(body.review_id, body.confirmation_token,
            confirmed=body.confirmed, maintenance=body.maintenance_confirmed)

    return router


def create_learning_retention_router(service, jobs):
    router=APIRouter(prefix='/api/v1/data/learning-retention')

    @router.get('/candidates')
    def candidates():
        return service.candidates()

    @router.post('/reviews')
    def review(body: LearningRetentionReview):
        return service.review(body.object_ids)

    @router.get('/plans/{plan_id}')
    def plan(plan_id: str):
        return service.read_plan(plan_id)

    @router.get('/runs/{run_id}')
    def run_state(run_id: str):
        return service.status(run_id)

    @router.post('/confirm',status_code=202)
    def confirm(body: LearningRetentionConfirmation):
        params=body.model_dump()
        service.check_confirmation(params['plan_id'],params['confirmation_token'],params['object_ids'],
            confirmed=params['confirmed'],maintenance_confirmed=params['maintenance_confirmed'])
        job=jobs.submit('learning_retention',params)
        service.bind_job(params['plan_id'],job['id'])
        return {**job,'plan_id':params['plan_id']}

    @router.post('/plans/{plan_id}/cancel')
    def cancel(plan_id: str):
        saved=service.read_plan(plan_id)
        if not saved.get('job_id'):
            raise ValueError('This retention plan has no cancellable background job')
        return {**jobs.cancel(saved['job_id']),'plan_id':plan_id}

    @router.post('/plans/{plan_id}/reconcile')
    def reconcile(plan_id: str,body: LearningRetentionReconcile):
        return service.reconcile(plan_id,maintenance_confirmed=body.maintenance_confirmed)

    return router
