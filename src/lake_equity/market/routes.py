from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from filelock import Timeout
from pydantic import BaseModel, ConfigDict


class PrepareMarketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basis: Literal["members", "source"] = "members"
    end: date | None = None


def create_market_router(root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/market", tags=["行情特征"])

    @lru_cache(maxsize=1)
    def service():
        from lake_equity.market.engine import MarketFeaturesService
        from lake_equity.market.store import MarketStore

        return MarketFeaturesService(MarketStore(root))

    def invoke(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except Timeout as error:
            raise HTTPException(409, "行情特征正在准备，请稍后读取已发布版本") from error
        except FileNotFoundError as error:
            raise HTTPException(404, "指定行情视图或来源版本已不可用，请显式准备新视图") from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @router.get("/status")
    def status():
        return {"latest_view": invoke(service().status)}

    @router.post("/prepare")
    def prepare(body: PrepareMarketRequest):
        return invoke(service().prepare, end=body.end.isoformat() if body.end else None, basis=body.basis)

    @router.get("/features")
    def features(view_id: str, kind: Literal["all", "industry", "concept", "mixed"] = "all",
                 sort_by: Literal["amount", "amount_change", "relative_amount", "activity"] = "amount",
                 limit: int = Query(30, ge=1, le=100)):
        return invoke(service().capital_view, view_id, kind=kind, sort_by=sort_by, limit=limit)

    @router.get("/boards/{board_id:path}/history")
    def history(board_id: str, view_id: str, window: Literal["20", "60"] = "20"):
        return invoke(service().history, view_id, board_id, window=int(window))

    @router.get("/boards/{board_id:path}/members")
    def members(board_id: str, view_id: str, offset: int = Query(0, ge=0),
                limit: int = Query(100, ge=1, le=100)):
        return invoke(service().members, view_id, board_id, offset=offset, limit=limit)

    @router.get("/specialties")
    def specialties(view_id: str, section: Literal["breadth", "valuation", "flows", "industry", "derivatives", "macro", "intraday"]):
        from lake_equity.market.specialties import specialty_view

        current = invoke(service().capital_view, view_id, limit=1)
        snapshot = invoke(service().store.read_snapshot, current["read_ref"])
        return invoke(specialty_view, service().store, snapshot, section)

    return router
