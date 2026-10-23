from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3

from lake_equity.settings import Settings


def doctor(settings: Settings) -> dict:
    result = {"project_root": str(settings.root), "raw_database": str(settings.raw_db), "checks": {}}
    checks = result["checks"]
    for package in ("lake-equity", "cnequity", "czsc", "torch", "fastapi", "polars", "duckdb"):
        try:
            checks[package] = {"version": importlib.metadata.version(package), "available": True}
        except importlib.metadata.PackageNotFoundError:
            checks[package] = {"available": False}
    from my_strategy.adapters.czsc_adapter import engine_contract, native_runtime
    try:
        checks["native_engine"] = {"available": bool(native_runtime()), "identity": engine_contract()}
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        checks["native_engine"] = {"available": False, "error": str(error)}
    if settings.raw_db.is_file():
        with closing(sqlite3.connect(settings.raw_db.as_uri() + "?mode=ro", uri=True)) as connection:
            rows, securities, start, end = connection.execute("SELECT COUNT(*),COUNT(DISTINCT stock),MIN(date),MAX(date) FROM stock_daily_normalized").fetchone()
            quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
            checks["raw_database"] = {"available": quick_check == "ok", "rows": rows, "symbols": securities, "start": start, "end": end,
                                      "quick_check": quick_check}
    else:
        checks["raw_database"] = {"available": False, "next_step": "lake-equity migrate"}
    from my_strategy.services.kronos_assets import asset_status
    assets = asset_status()
    checks["kronos_assets"] = {**assets, "available": assets.get("ready") is True}
    from my_strategy.services.czsc_trend_support import _calendar
    try:
        dates, identity = _calendar()
        checks["calendar"] = {"available": True, "identity": identity, "start": dates[0], "end": dates[-1], "sessions": len(dates)}
    except (ValueError, FileNotFoundError) as error:
        checks["calendar"] = {"available": False, "error": str(error)}
    result["status"] = "ok" if all(value.get("available", True) for value in checks.values()) else "partial"
    return result


def _verify_release_sources(root: Path) -> dict:
    manifest_path = root / "release-manifest.json"
    source = {"source": manifest_path.name, "files_checked": 0, "failures": []}
    failures = source["failures"]
    try:
        if manifest_path.is_symlink():
            raise ValueError("Release manifest must not be a symlink")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or type(manifest.get("schema")) is not int
                or manifest["schema"] != 1 or manifest.get("kind") != "lake-equity-public-source"
                or not isinstance(manifest.get("files"), list) or not manifest["files"]):
            raise ValueError("Invalid public release manifest schema")
    except (OSError, UnicodeError, ValueError) as error:
        failures.append({"path": manifest_path.name, "reason": "invalid_manifest", "error": str(error)})
        return {"status": "failed", "sources": [source]}
    seen = set()
    for entry in manifest["files"]:
        relative = entry.get("path") if isinstance(entry, dict) else None
        try:
            if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
                    or PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute()
                    or any(part in ("", ".", "..") for part in relative.split("/"))
                    or relative == manifest_path.name):
                raise ValueError("Invalid root-relative file path")
            identity = os.path.normcase(relative)
            if identity in seen:
                raise ValueError("Duplicate file path")
            seen.add(identity)
            size, digest = entry.get("size"), entry.get("sha256")
            if (type(size) is not int or size < 0 or not isinstance(digest, str) or len(digest) != 64
                    or any(character not in "0123456789abcdefABCDEF" for character in digest)):
                raise ValueError("Invalid file size or SHA256")
            path = root / relative
            parts = PurePosixPath(relative).parts
            if any((root / Path(*parts[:index])).is_symlink() for index in range(1, len(parts) + 1)):
                raise ValueError("Release files must not use symlinks")
            if not path.resolve().is_relative_to(root):
                raise ValueError("File path leaves the release root")
            if not path.is_file():
                failures.append({"path": relative, "reason": "missing"})
                continue
            if path.stat().st_size != size:
                failures.append({"path": relative, "reason": "size_mismatch"})
                continue
            with path.open("rb") as handle:
                observed = hashlib.file_digest(handle, "sha256").hexdigest()
            source["files_checked"] += 1
            if observed != digest.lower():
                failures.append({"path": relative, "reason": "sha256_mismatch"})
        except (OSError, UnicodeError, ValueError) as error:
            failures.append({"path": relative, "reason": "invalid_file", "error": str(error)})
    return {"status": "failed" if failures else "ok", "sources": [source]}


def verify_sources(settings: Settings) -> dict:
    release_manifest = settings.root / "release-manifest.json"
    if release_manifest.exists() or release_manifest.is_symlink():
        return _verify_release_sources(settings.root.resolve())
    results = []
    for filename, base in (("cnequity_source.json", settings.root / "vendor/cnequity"),
                           ("khquant_source.json", settings.research_app)):
        try:
            manifest = json.loads((settings.root / "docs/provenance" / filename).read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
                raise ValueError("Invalid source manifest schema")
        except (OSError, UnicodeError, ValueError) as error:
            results.append({"source": filename, "files_checked": 0,
                            "failures": [{"path": filename, "reason": "invalid_manifest", "error": str(error)}]})
            continue
        failures = []
        checked = 0
        for entry in manifest["files"]:
            path = base / entry["path"]
            if filename == "khquant_source.json" and not path.is_file():
                path = base.parent / entry["path"]
            if not path.is_file():
                failures.append({"path": entry["path"], "reason": "missing"})
                continue
            with path.open("rb") as handle:
                observed = hashlib.file_digest(handle, "sha256").hexdigest()
            if observed != entry["sha256"]:
                failures.append({"path": entry["path"], "reason": "sha256_mismatch"})
            checked += 1
        results.append({"source": filename, "files_checked": checked, "failures": failures})
    return {"status": "ok" if not any(row["failures"] for row in results) else "failed", "sources": results}


def main() -> None:
    parser = argparse.ArgumentParser(prog="lake-equity")
    parser.add_argument("--root", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the unified workbench")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int)
    commands.add_parser("doctor", help="Verify local runtime, data and frozen engine")
    commands.add_parser("verify-sources", help="Verify frozen source content")
    commands.add_parser("migrate", help="Publish the frozen legacy data to CNEquity and materialize research inputs")
    commands.add_parser("materialize", help="Freeze the current lake into the research input contract")
    learn = commands.add_parser("learn", help="Train and backtest factor models from immutable lake generations")
    learn.add_argument("--symbols", default="", help="Explicit comma-separated stock codes (1 to 6000)")
    learn.add_argument("--scope", choices=("symbols", "all"), default="symbols")
    learn.add_argument("--start", required=True)
    learn.add_argument("--end", required=True)
    learn.add_argument("--groups", default="price_volume,margin", help="Comma-separated factor groups")
    learn.add_argument("--backend", choices=("local", "qlib"), default="local")
    learn.add_argument("--models", nargs="+", choices=("ridge", "lightgbm", "mlp"), default=["ridge", "lightgbm", "mlp"])
    learn.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    learn.add_argument("--max-epochs", type=int, default=50)
    learn.add_argument("--preflight", action="store_true", help="Inspect the entire requested population without fitting")
    learn.add_argument("--horizon", type=int, default=5)
    learn.add_argument("--train-sessions", type=int, default=252)
    learn.add_argument("--valid-sessions", type=int, default=63)
    learn.add_argument("--test-sessions", type=int, default=63)
    learn.add_argument("--max-folds", type=int, default=3)
    learn.add_argument("--top-k", type=int, default=10)
    learn.add_argument("--initial-cash", type=float, default=1_000_000)
    learn.add_argument("--seed", type=int, default=42)
    scan = commands.add_parser("model-scan", help="Select stocks with an explicitly frozen model; never fits")
    scan.add_argument("--model-id", required=True)
    scan.add_argument("--scope", choices=("symbols", "all"), default="symbols")
    scan.add_argument("--symbols", default="")
    scan.add_argument("--end", required=True)
    scan.add_argument("--top-k", type=int, default=20)
    backtest = commands.add_parser("model-backtest", help="Evaluate frozen weights or a saved OOS model series")
    backtest.add_argument("--mode", choices=("frozen", "rolling"), default="frozen")
    backtest.add_argument("--model-id")
    backtest.add_argument("--series-id")
    backtest.add_argument("--start", required=True)
    backtest.add_argument("--end", required=True)
    backtest.add_argument("--top-k", type=int, default=10)
    backtest.add_argument("--initial-cash", type=float, default=1_000_000)
    refit = commands.add_parser("model-refit", help="Explicitly refit one fixed study protocol into a new inference version")
    refit.add_argument("--run-id", required=True)
    refit.add_argument("--model-name", required=True)
    refit.add_argument("--end")
    args = parser.parse_args()
    settings = Settings.load(args.root)
    settings.configure()
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("LAKE_EQUITY_API_TOKEN"):
            parser.error("Non-loopback binding requires LAKE_EQUITY_API_TOKEN")
        import uvicorn
        from filelock import FileLock, Timeout
        from lake_equity.app import create_app
        try:
            with FileLock(str(settings.root / "var/server.lock"), timeout=0):
                uvicorn.run(create_app(settings), host=args.host, port=args.port or settings.port)
        except Timeout:
            parser.error("This project already has a running server. Open its workbench instead of starting another writer.")
        return
    if args.command == "doctor":
        result = doctor(settings)
    elif args.command == "verify-sources":
        result = verify_sources(settings)
    elif args.command == "learn":
        from lake_equity.learning.service import LearningService
        if 'mlp' not in args.models and (args.device != 'cpu' or args.max_epochs != 50):
            parser.error('--device and --max-epochs require --models mlp')
        spec = {"symbols": [value.strip().upper() for value in args.symbols.split(",") if value.strip()], "scope": args.scope,
                "start": args.start, "end": args.end,
                "factor_groups": [value.strip() for value in args.groups.split(",") if value.strip()],
                "backend": args.backend, "models": args.models, "horizon": args.horizon,
                "train_sessions": args.train_sessions, "valid_sessions": args.valid_sessions,
                "test_sessions": args.test_sessions, "max_folds": args.max_folds,
                "top_k": args.top_k, "initial_cash": args.initial_cash, "seed": args.seed}
        if 'mlp' in args.models:
            spec['model_options'] = {'device': args.device, 'max_epochs': args.max_epochs}
        if args.preflight:
            from lake_equity.learning.workflow import LearningWorkflow
            result = LearningWorkflow(settings.root).preflight(spec)
        else:
            result = LearningService(settings.root).run(spec, progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    elif args.command in {"model-scan", "model-backtest"}:
        from lake_equity.learning.workflow import LearningWorkflow
        workflow = LearningWorkflow(settings.root)
        progress = lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
        if args.command == 'model-scan':
            result = workflow.scan(dict(model_id=args.model_id, scope=args.scope,
                symbols=[s.strip().upper() for s in args.symbols.split(',') if s.strip()], end=args.end, top_k=args.top_k), progress=progress)
        else:
            if (args.mode == 'rolling' and (not args.series_id or args.model_id)) or (args.mode == 'frozen' and (not args.model_id or args.series_id)):
                parser.error('Use exactly --series-id for rolling or --model-id for frozen mode')
            spec = dict(mode=args.mode, start=args.start, end=args.end, top_k=args.top_k, initial_cash=args.initial_cash)
            spec['series_id' if args.mode == 'rolling' else 'model_id'] = args.series_id if args.mode == 'rolling' else args.model_id
            result = workflow.backtest(spec, progress=progress)
    elif args.command == 'model-refit':
        from lake_equity.learning.refit import refit_model
        spec = dict(run_id=args.run_id, model_name=args.model_name)
        if args.end:
            spec['end'] = args.end
        result = refit_model(settings.root, spec, progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
    else:
        from lake_equity.data import LakeService
        service = LakeService(settings.root, source_db=settings.source_db)
        progress = lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
        if args.command == "migrate":
            imported = service.import_legacy(progress=progress)
            result = {"import": imported, "materialize": service.materialize_legacy(progress=progress)}
        else:
            result = service.materialize_legacy(progress=progress)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.command == "verify-sources" and result["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
