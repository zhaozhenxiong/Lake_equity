from __future__ import annotations

import argparse
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import uvicorn


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "packages" / "khquant_research" / "app"))


def create_preview(root: Path = ROOT, primary_port: int = 8130) -> FastAPI:
    from lake_equity.market.routes import create_market_router

    app = FastAPI(title="LakeEquity 行情特征只读预览")
    app.include_router(create_market_router(root))

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "只接受本地同源操作"}, status_code=403)
        return await call_next(request)

    @app.get("/api/{path:path}")
    async def existing_reads(path: str, request: Request):
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                response = await client.get(f"http://127.0.0.1:{primary_port}/api/{path}",
                                            params=request.query_params)
            return Response(response.content, status_code=response.status_code,
                            media_type=response.headers.get("content-type", "application/json"))
        except httpx.HTTPError:
            return JSONResponse({"detail": "原工作台暂不可读，行情预览不启动或接管其任务"}, status_code=503)

    @app.get("/native/{path:path}")
    def native_console(path: str):
        return RedirectResponse(f"http://127.0.0.1:{primary_port}/native/{path}")

    app.mount("/assets", StaticFiles(directory=root / "frontend"), name="market-preview-assets")

    @app.get("/")
    def index():
        return FileResponse(root / "frontend" / "index.html", headers={"Cache-Control": "no-store"})

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Preview only the new market router; never creates the main job manager")
    parser.add_argument("--port", type=int, default=8131)
    args = parser.parse_args()
    uvicorn.run(create_preview(), host="127.0.0.1", port=args.port)
