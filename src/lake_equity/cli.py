from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
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
    patches = {}
    patch_manifest = settings.root / "docs/provenance/local_patches.json"
    try:
        if patch_manifest.exists():
            payload = json.loads(patch_manifest.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or type(payload.get("schema")) is not int or payload["schema"] != 1 or not isinstance(payload.get("patches"), list):
                raise ValueError("Invalid local patch manifest")
            for patch in payload["patches"]:
                if (not isinstance(patch, dict) or patch.get("source_manifest") not in {"cnequity_source.json", "khquant_source.json"}
                        or not isinstance(patch.get("path"), str) or not isinstance(patch.get("reason"), str) or not patch["reason"].strip()
                        or any(not isinstance(patch.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", patch[key]) for key in ("original_sha256", "sha256"))):
                    raise ValueError("Invalid local patch declaration")
                key = patch["source_manifest"], patch["path"]
                if key in patches:
                    raise ValueError("Duplicate local patch declaration")
                patches[key] = patch
    except (OSError, UnicodeError, ValueError) as error:
        return {"status": "failed", "sources": [{"source": "local_patches.json", "files_checked": 0,
                "failures": [{"path": "local_patches.json", "reason": "invalid_manifest", "error": str(error)}]}]}
    applied_patches = set()
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
            key = filename, entry["path"]
            patch = patches.get(key)
            expected = entry["sha256"]
            if patch:
                applied_patches.add(key)
                if patch["original_sha256"] != expected:
                    failures.append({"path": entry["path"], "reason": "patch_original_sha256_mismatch"})
                    continue
                expected = patch["sha256"]
            path = base / entry["path"]
            if filename == "khquant_source.json" and not path.is_file():
                path = base.parent / entry["path"]
            if not path.is_file():
                failures.append({"path": entry["path"], "reason": "missing"})
                continue
            with path.open("rb") as handle:
                observed = hashlib.file_digest(handle, "sha256").hexdigest()
            if observed != expected:
                failures.append({"path": entry["path"], "reason": "sha256_mismatch"})
            checked += 1
        results.append({"source": filename, "files_checked": checked, "failures": failures})
    if applied_patches != set(patches):
        results.append({"source": "local_patches.json", "files_checked": 0,
                        "failures": [{"path": path, "reason": "unknown_source_patch"}
                                     for source, path in sorted(set(patches) - applied_patches)]})
    return {"status": "ok" if not any(row["failures"] for row in results) else "failed", "sources": results}


def _compute(settings, action):
    from filelock import FileLock
    path = settings.root / 'var/learning/compute.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path), timeout=0):
        return action()


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
    learn.add_argument("--device", choices=("cpu", "cuda"), default=None, help="MLP compute device; omitted means CUDA when available, CPU otherwise")
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
    scan.add_argument("--model-id")
    scan.add_argument("--release-id", help="Frozen strategy release, or active research default")
    scan.add_argument("--scope", choices=("symbols", "all"), default="symbols")
    scan.add_argument("--symbols", default="")
    scan.add_argument("--end", required=True)
    scan.add_argument("--top-k", type=int, default=20)
    scan.add_argument("--universe-scope", choices=("release", "explicit_symbols"), default="release")
    scan.add_argument("--expected-release-id")
    backtest = commands.add_parser("model-backtest", help="Evaluate frozen weights or a saved OOS model series")
    backtest.add_argument("--mode", choices=("frozen", "rolling", "historical", "research_replay", "watchlist_replay"), default="frozen")
    backtest.add_argument("--model-id")
    backtest.add_argument("--series-id")
    backtest.add_argument("--release-id")
    backtest.add_argument("--study-id")
    backtest.add_argument("--scope", choices=("symbols", "all"), default="symbols")
    backtest.add_argument("--symbols", default="")
    backtest.add_argument("--universe-scope", choices=("release", "explicit_symbols"), default="release")
    backtest.add_argument("--expected-release-id")
    backtest.add_argument("--start", required=True)
    backtest.add_argument("--end", required=True)
    backtest.add_argument("--top-k", type=int, default=10)
    backtest.add_argument("--initial-cash", type=float, default=1_000_000)
    refit = commands.add_parser("model-refit", help="Explicitly refit one fixed study protocol into a new inference version")
    refit.add_argument("--run-id", required=True)
    refit.add_argument("--model-name", required=True)
    refit.add_argument("--end")
    pipeline = commands.add_parser("pipeline", help="One-click full data update, default-method screen and backtest")
    pipeline.add_argument("--end", help="Target trade date (default: latest closed session)")
    pipeline.add_argument("--packs", choices=("core", "extended", "full"), default="full")
    pipeline.add_argument("--refit", choices=("auto", "never", "now"), default="auto")
    pipeline.add_argument("--dry-run", action="store_true", help="Preflight and coverage only; never writes data")
    pipeline.add_argument("--skip-update", action="store_true", help="Reuse the published lake without ingesting")
    pipeline.add_argument("--no-scan", action="store_true")
    pipeline.add_argument("--no-backtest", action="store_true")
    pipeline.add_argument("--json", action="store_true", help="Accepted for scripts; JSON is always printed")
    study = commands.add_parser('study', help='Run a resumable chronological research study and optionally activate its provisional winner')
    study.add_argument('--spec', type=Path, required=True)
    study.add_argument('--preflight', action='store_true')
    study.add_argument('--resume-study-id')
    study.add_argument('--activate', action='store_true')
    fusion = commands.add_parser('fusion-scan', help='Fuse a pinned model selection with bounded Kronos research predictions')
    fusion.add_argument('--spec', type=Path, required=True)
    plan = commands.add_parser('trade-plan', help='Build a next-session conditional plan from an existing account snapshot and saved fusion')
    plan.add_argument('--spec', type=Path, required=True)
    maintenance = commands.add_parser('maintenance', help='Inspect and explicitly confirm cleanup of failed workflow build directories')
    maintenance.add_argument('--quarantine-preview', action='store_true', help='Read-only listing; never deletes')
    maintenance.add_argument('--quarantine-confirm', metavar='PLAN_ID', help='Plan id from a fresh preview')
    maintenance.add_argument('--ids', default='', help='Comma-separated previewed item ids to delete after confirmation')
    forward = commands.add_parser('forward', help='Record and verify post-release forward signals against later published bars')
    forward_commands = forward.add_subparsers(dest='action', required=True)
    forward_record = forward_commands.add_parser('record', help="Freeze one session's factor and Kronos signals before any outcome exists")
    forward_record.add_argument('--day', required=True, help='Newest verified trading session to record')
    forward_record.add_argument('--symbols', required=True, help='Comma-separated watchlist symbols (up to 200)')
    forward_record.add_argument('--release-id', help='Frozen strategy release; omitted uses the active research default')
    forward_record.add_argument('--horizon', type=int, default=5)
    forward_record.add_argument('--device', choices=('cpu', 'cuda'), default=None, help='Kronos device; omitted means CUDA when available')
    forward_record.add_argument('--max-calls', type=int, default=20)
    forward_record.add_argument('--budget-seconds', type=float, default=300.)
    forward_record.add_argument('--read-ref', help='Pin a published data generation instead of the newest one')
    forward_record.add_argument('--origin', choices=('live', 'backfill'), default='live',
                                help='live: newest published session; backfill: an earlier session reconstructed from data up to that day')
    forward_verify = forward_commands.add_parser('verify', help='Reconcile recorded signals with the realized executable return')
    forward_verify.add_argument('--day', action='append', default=[], help='Signal day to verify; repeat for several days')
    forward_verify.add_argument('--read-ref', help='Pin a published data generation instead of the newest one')
    forward_report = forward_commands.add_parser('report', help='Read one recorded day, or aggregate metrics when --day is omitted')
    forward_report.add_argument('--day')
    forward_report.add_argument('--limit', type=int, default=60)
    forward_days = forward_commands.add_parser('days', help='List recorded forward days')
    forward_days.add_argument('--limit', type=int, default=30)
    forward_ingest = forward_commands.add_parser('ingest', help='Ingest a saved fusion candidate cross-section as one forward record')
    forward_ingest.add_argument('--fusion-run-id', required=True)
    forward_ingest.add_argument('--origin', choices=('live', 'backfill'), help='Omitted means inferred from the newest published session')
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
        from lake_equity.learning.device import resolve as resolve_device
        if 'mlp' not in args.models and (args.device not in (None, 'cpu') or args.max_epochs != 50):
            parser.error('--device and --max-epochs require --models mlp')
        spec = {"symbols": [value.strip().upper() for value in args.symbols.split(",") if value.strip()], "scope": args.scope,
                "start": args.start, "end": args.end,
                "factor_groups": [value.strip() for value in args.groups.split(",") if value.strip()],
                "backend": args.backend, "models": args.models, "horizon": args.horizon,
                "train_sessions": args.train_sessions, "valid_sessions": args.valid_sessions,
                "test_sessions": args.test_sessions, "max_folds": args.max_folds,
                "top_k": args.top_k, "initial_cash": args.initial_cash, "seed": args.seed}
        if 'mlp' in args.models:
            spec['model_options'] = {'device': args.device or resolve_device(), 'max_epochs': args.max_epochs}
        if args.preflight:
            from lake_equity.learning.workflow import LearningWorkflow
            result = LearningWorkflow(settings.root).preflight(spec)
        else:
            result = _compute(settings, lambda: LearningService(settings.root).run(spec, progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)))
    elif args.command == 'study':
        from lake_equity.learning.study import StudyService
        from lake_equity.learning.releases import ReleaseRegistry
        spec = json.loads(args.spec.read_text(encoding='utf-8'))
        if args.resume_study_id:
            spec['resume_study_id'] = args.resume_study_id
        service = StudyService(settings.root)
        result = service.preflight(spec) if args.preflight else _compute(settings, lambda: service.run(spec,
            progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)))
        if args.activate and not args.preflight and result.get('best_model_id'):
            registry = ReleaseRegistry(settings.root)
            release = registry.from_study(result)
            registry.activate(release['release_id'], reason='Completed chronological research study; stage default remains research only')
            result = {**result, 'release_id': release['release_id'], 'research_active': True}
    elif args.command == 'maintenance':
        from lake_equity.learning.quarantine import QuarantineService
        quarantine = QuarantineService(settings.root)
        if args.quarantine_confirm:
            ids = [value.strip() for value in args.ids.split(',') if value.strip()]
            result = quarantine.confirm(args.quarantine_confirm, ids, True)
        else:
            result = quarantine.listing()
    elif args.command in {'fusion-scan', 'trade-plan'}:
        spec = json.loads(args.spec.read_text(encoding='utf-8'))
        if args.command == 'fusion-scan':
            from lake_equity.learning.fusion import FusionService
            result = _compute(settings, lambda: FusionService(settings.root).run(spec, progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)))
        else:
            from lake_equity.learning.trading import TradingService
            result = TradingService(settings.root).build_plan(spec)
    elif args.command in {"model-scan", "model-backtest"}:
        from lake_equity.learning.workflow import LearningWorkflow
        workflow = LearningWorkflow(settings.root)
        progress = lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
        if args.command == 'model-scan':
            spec = dict(scope=args.scope, symbols=[s.strip().upper() for s in args.symbols.split(',') if s.strip()], end=args.end, top_k=args.top_k)
            if args.model_id:
                spec['model_id'] = args.model_id
            if args.release_id:
                spec['release_id'] = args.release_id
            if args.universe_scope and args.universe_scope != 'release':
                spec['universe_scope'] = args.universe_scope
            if args.expected_release_id:
                spec['expected_release_id'] = args.expected_release_id
            result = _compute(settings, lambda: workflow.scan(spec, progress=progress))
        else:
            if args.model_id and args.series_id:
                parser.error('Use exactly --series-id for rolling or --model-id for frozen mode')
            spec = dict(mode=args.mode, start=args.start, end=args.end, top_k=args.top_k, initial_cash=args.initial_cash)
            for key in ['series_id', 'model_id', 'release_id', 'study_id']:
                if getattr(args, key):
                    spec[key] = getattr(args, key)
            if args.symbols:
                spec['scope'] = args.scope
                spec['symbols'] = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
            if args.universe_scope and args.universe_scope != 'release':
                spec['universe_scope'] = args.universe_scope
            if args.expected_release_id:
                spec['expected_release_id'] = args.expected_release_id
            result = _compute(settings, lambda: workflow.backtest(spec, progress=progress))
    elif args.command == 'model-refit':
        from lake_equity.learning.refit import refit_model
        spec = dict(run_id=args.run_id, model_name=args.model_name)
        if args.end:
            spec['end'] = args.end
        result = _compute(settings, lambda: refit_model(settings.root, spec, progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)))
    elif args.command == 'forward':
        from lake_equity.learning.forward import ForwardLedger
        ledger = ForwardLedger(settings.root)
        progress = lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)
        if args.action == 'record':
            body = dict(day=args.day, symbols=[value.strip().upper() for value in args.symbols.split(',') if value.strip()],
                        horizon=args.horizon, max_calls=args.max_calls, budget_seconds=args.budget_seconds,
                        origin=args.origin)
            if args.release_id:
                body['release_id'] = args.release_id
            if args.device:
                body['device'] = args.device
            if args.read_ref:
                body['read_ref'] = args.read_ref
            result = _compute(settings, lambda: ledger.record(body, progress=progress))
        elif args.action == 'verify':
            body = {'day': args.day or None}
            if args.read_ref:
                body['read_ref'] = args.read_ref
            result = _compute(settings, lambda: ledger.verify(body, progress=progress))
        elif args.action == 'ingest':
            body = {'source': 'fusion', 'fusion_run_id': args.fusion_run_id}
            if args.origin:
                body['origin'] = args.origin
            result = _compute(settings, lambda: ledger.record(body, progress=progress))
        elif args.action == 'report':
            result = ledger.read(args.day) if args.day else ledger.metrics({'limit': args.limit})
        else:
            result = ledger.list_days(limit=args.limit)
    elif args.command == 'pipeline':
        from lake_equity.data import LakeService
        from lake_equity.pipeline import PipelineService
        params = {'packs': args.packs, 'refit': args.refit,
                  'scan': not args.no_scan, 'backtest': not args.no_backtest}
        if args.end:
            params['end'] = args.end
        if args.dry_run:
            params['dry_run'] = True
        if args.skip_update:
            params['skip_update'] = True
        service = PipelineService(settings.root, LakeService(settings.root, source_db=settings.source_db))
        result = _compute(settings, lambda: service.run(params, progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True)))
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
    if args.command == "pipeline" and isinstance(result, dict):
        raise SystemExit(int(result.get("exit_code") or 0))


if __name__ == "__main__":
    main()
