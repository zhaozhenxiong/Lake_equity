"""Read-only lake inspection without constructing the retired web application."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query


def create_data_views_router(config):
    from cnequity.domain.datasets import DATASETS
    from cnequity.serve.lake import LakeView, RUN_STATUSES

    view = LakeView(config)
    router = APIRouter(prefix="/api/v1/data", tags=["data inspection"])

    def known(dataset):
        if dataset not in DATASETS:
            raise HTTPException(404, "Unknown lake dataset")

    @router.get("/datasets/{dataset}")
    def detail(dataset: str):
        known(dataset)
        return view.dataset_detail(dataset)

    @router.get("/datasets/{dataset}/partitions")
    def partitions(dataset: str):
        known(dataset)
        return view.partitions(dataset)

    @router.get("/datasets/{dataset}/dates")
    def dates(dataset: str):
        known(dataset)
        return view.date_options(dataset)

    @router.get("/datasets/{dataset}/provenance")
    def provenance(dataset: str):
        known(dataset)
        return view.provenance(dataset)

    @router.get("/datasets/{dataset}/provenance/series")
    def provenance_series(dataset: str):
        known(dataset)
        return view.provenance_series(dataset)

    @router.get("/quality/runs/{run_id}")
    def quality_run(run_id: str):
        result = view.quality_run(run_id)
        if result is None:
            raise HTTPException(404, "No saved quality artifacts for this run")
        return result

    @router.get("/runs")
    def runs(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
             status: str = "", open_runs: bool = Query(False, alias="open")):
        statuses = tuple(dict.fromkeys(part.strip() for part in status.split(",") if part.strip()))
        if set(statuses) - RUN_STATUSES:
            raise HTTPException(422, "Unknown ingestion run status")
        total, records, failed = view.runs(limit=limit, offset=offset, statuses=statuses or None,
                                          open_only=open_runs)
        return {"total": total, "offset": offset, "limit": limit, "runs": records,
                "failed_daily_groups": failed}

    @router.get("/runs/{run_id}")
    def run(run_id: str):
        result = view.run_detail(run_id)
        if result is None:
            raise HTTPException(404, "Unknown ingestion run")
        return result

    @router.get("/heatmap")
    def heatmap(days: int = Query(90, ge=1, le=750)):
        return view.heatmap(days=days)

    return router
