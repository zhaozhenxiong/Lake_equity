"""Route portable research requests to the original, identity-aware engines."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import gzip
import inspect
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from uuid import uuid4
from zoneinfo import ZoneInfo


class ResearchBridge:
    def __init__(self, *, raw_db: Path, research_root: Path, results_db: Path, runs_root: Path):
        self.raw_db = Path(raw_db).resolve()
        self.research_root = Path(research_root).resolve()
        self.results_db = Path(results_db).resolve()
        self.runs_root = Path(runs_root).resolve()
        self._light = None
        self._lock = threading.RLock()

    def _engine(self):
        with self._lock:
            if self._light is None:
                from my_strategy.core.paths import ARTIFACT_RUNS_ROOT, METADATA_ROOT
                from my_strategy.services.light_research import LightResearch

                if ARTIFACT_RUNS_ROOT.resolve() != self.runs_root or METADATA_ROOT.resolve() != self.research_root.parent:
                    raise ValueError("Configure KHQUANT runtime paths before importing the research engine")
                self._light = LightResearch(root=self.research_root, raw_db=self.raw_db)
            return self._light

    def close(self):
        with self._lock:
            if self._light is not None:
                self._light.close()
                self._light = None

    @staticmethod
    def _request(spec):
        request = dict(spec)
        manifest = request.pop("input_manifest_id", None)
        if manifest is not None and (not isinstance(manifest, str) or not manifest or len(manifest) > 200):
            raise ValueError("input_manifest_id must be a nonempty identifier")
        return request, manifest

    @staticmethod
    def _call(function, request, **injected):
        signature = inspect.signature(function)
        allowed = {name for name, parameter in signature.parameters.items()
                   if parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)}
        unknown = set(request) - allowed
        if unknown:
            raise ValueError("Unsupported research parameters: " + ", ".join(sorted(unknown)))
        return function(**request, **injected)

    @staticmethod
    def _symbol(value):
        from my_strategy.adapters.czsc_adapter import normalize_symbol
        from my_strategy.core.market_scope import require_supported_symbol

        return require_supported_symbol(normalize_symbol(value))

    def methods(self):
        from my_strategy.services.analysis_methods import METHODS, VERSION
        from my_strategy.services.czsc_ict_events import ICT_RULE_VERSION, ICT_SCHEMA_HASH
        from my_strategy.services.czsc_market_profiles import get_feature_profile
        from my_strategy.services.czsc_price_volume_features import get_wyckoff_profile

        return {"version": VERSION,
                "methods": [{"id": key, "name": label, "historical_only": key == "legacy_reference"}
                            for key, label in METHODS.items()],
                "features": [get_feature_profile("ma_trend"), get_feature_profile("confirmed_structure"),
                             get_wyckoff_profile(), {"name": "ict", "version": ICT_RULE_VERSION,
                                                    "schema_hash": ICT_SCHEMA_HASH}],
                "probability_semantics": "kronos_sample_frequency_is_not_calibrated_probability"}

    def features(self, symbol, end=None, profile="ma_trend_v1", *, limit=300, input_manifest_id=None):
        from my_strategy.adapters.czsc_adapter import latest_market_date, load_bars
        from my_strategy.services.light_ma_features import prepare_ma_features
        from my_strategy.services.light_research import plain

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
            raise ValueError("Feature limit must be in 1..2000")
        if profile not in {"ma_trend_v1", "native"}:
            raise ValueError("Feature profile must be ma_trend_v1 or native")
        symbol = self._symbol(symbol)
        end = end or latest_market_date(db_path=self.raw_db)
        light = self._engine()
        dates, calendar_identity = light.calendar()
        if profile == "ma_trend_v1":
            from my_strategy.services.czsc_market_profiles import MA_TREND_COLUMNS

            raw = load_bars(symbol, end=end, as_of=end, db_path=self.raw_db)
            frame = prepare_ma_features(raw, calendar_dates=dates)
            source = {"data_version": raw.attrs["data_version"], "calendar": calendar_identity}
            feature_columns = list(MA_TREND_COLUMNS)
        else:
            _, frame, _, source = light.source(symbol, end)
            feature_columns = list(frame.attrs.get("feature_columns", []))
        records = plain(frame.tail(limit).to_dict("records"))
        return {"symbol": symbol, "end": end, "profile": profile, "columns": list(frame.columns),
                "feature_columns": feature_columns,
                "rows": records, "row_count": len(frame), "returned_rows": len(records),
                "source": source, "input_manifest_id": input_manifest_id}

    def analyze(self, spec):
        from my_strategy.services.analysis_methods import COMBINED, METHODS, analyze_method
        from my_strategy.web_dashboard.trend_strategy import analyze_trend_consensus

        request, manifest = self._request(spec)
        method = request.setdefault("analysis_method", "structure")
        if method not in METHODS:
            raise ValueError("Unknown analysis method")
        request["symbol"] = self._symbol(request["symbol"])
        with self._lock:
            if method in COMBINED:
                result = analyze_method(self._engine(), request)
            else:
                request.pop("analysis_method")
                request.pop("save", None)
                allowed = {"symbol", "start", "end", "as_of", "model_run_id", "model_fold", "usage_mode",
                           "calendar_run_id", "device", "config", "consensus_config", "analysis_mode",
                           "current_position_fraction"}
                if set(request) - allowed:
                    raise ValueError("Unsupported analysis parameters: " + ", ".join(sorted(set(request) - allowed)))
                result = analyze_trend_consensus(**request, structure_only=method == "structure", db_path=self.raw_db)
                result = {**result, "analysis_method": method}
            return self._persist("analysis", result, manifest)

    def backtest(self, spec, progress=None, check_cancel=None):
        request, manifest = self._request(spec)
        method = request.pop("analysis_method", "structure")
        request["symbols"] = list(dict.fromkeys(self._symbol(value) for value in request["symbols"]))
        if not request["symbols"]:
            raise ValueError("Backtest requires at least one stock")
        if method not in {"structure", "structure_kronos", "structure_kronos_ma_mlp", "structure_kronos_ma_l2"}:
            raise ValueError("Historical reference targets cannot be used as a new account method")
        with self._lock:
            if method == "structure":
                result = self._structure_backtest(request, progress, check_cancel)
            else:
                light = self._engine()
                if method in {"structure_kronos_ma_mlp", "structure_kronos_ma_l2"}:
                    request["feature_profile"] = "ma_trend_v1"
                result = self._call(light.backtest, request, progress=progress, stop=_Cancellation(check_cancel))
                arm = {"structure_kronos": "structure_kronos", "structure_kronos_ma_mlp": "structure_kronos_score",
                       "structure_kronos_ma_l2": "structure_kronos_l2"}[method]
                result = {**result, "selected_policy": arm}
            return self._persist("backtest", {**result, "analysis_method": method}, manifest)

    def _structure_backtest(self, request, progress, check_cancel):
        from my_strategy.services.agent_evidence import volume_candidates
        from my_strategy.services.light_research import DeadlineStop
        from my_strategy.services.position_simulation import run_position_simulation

        allowed = {"symbols", "start", "end", "initial_cash", "config", "corporate_actions", "include_benchmarks", "budget_seconds"}
        if set(request) - allowed:
            raise ValueError("Unsupported structure backtest parameters: " + ", ".join(sorted(set(request) - allowed)))
        budget = request.get("budget_seconds", 300)
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 0 < budget <= 1800:
            raise ValueError("Backtest budget must be in (0,1800] seconds")
        stop = DeadlineStop(_Cancellation(check_cancel), time.monotonic() + budget)
        light = self._engine()
        dates, calendar_identity = light.calendar()
        frames, evidence, sources = {}, {}, []
        for symbol in request["symbols"]:
            stop.check()
            _, frame, events, source = light.source(symbol, request["end"], stop=stop)
            frames[symbol] = frame
            evidence[symbol] = [dict(event) for event in events + volume_candidates(frame, symbol)
                                if request["start"] <= str(event["date"])[:10] <= request["end"]]
            sources.append(source)
        config = {"candidate_sessions": 3, "fusion_mode": "evidence", "enable_agent_evidence": True,
                  **(request.get("config") or {})}
        if config.get("policy", "structure_rules") != "structure_rules":
            raise ValueError("Structure backtest requires the structure_rules policy")
        config.update(policy="structure_rules", research_only=True, research_mode="historical_candidate")
        if "initial_cash" in request:
            config["initial_cash"] = request["initial_cash"]
        result = run_position_simulation(frames, evidence, config=config, start=request["start"], end=request["end"],
                                         calendar=dates, corporate_actions=request.get("corporate_actions"),
                                         include_benchmarks=request.get("include_benchmarks", True),
                                         progress=progress, check_cancel=stop.check)
        return {**result, "sources": sources, "calendar_identity": calendar_identity,
                "status": "completed", "selected_policy": "structure_rules"}

    def predict(self, spec):
        request, manifest = self._request(spec)
        request["symbol"] = self._symbol(request["symbol"])
        from my_strategy.services.kronos_prediction import predict

        allowed = set(inspect.signature(predict).parameters) - {"frame", "future_dates", "calendar_identity",
                   "calendar_verified", "cancel_check", "asset_root", "release_after", "raw_input_sha256"}
        allowed |= {"end"}
        if set(request) - allowed:
            raise ValueError("Unsupported prediction parameters: " + ", ".join(sorted(set(request) - allowed)))
        if "as_of" in request:
            if request.get("end") and request["end"] != request["as_of"]:
                raise ValueError("Prediction end and as_of disagree")
            request["end"] = request.pop("as_of")
        with self._lock:
            return self._persist("prediction", self._engine().prediction(**request), manifest)

    def scan(self, spec, progress=None, check_cancel=None):
        from my_strategy.services.analysis_methods import COMBINED
        from my_strategy.services.method_scan_acceleration import scan_methods
        from my_strategy.storage.czsc_results import ResultStore
        from my_strategy.web_dashboard.trend_strategy import scan_trend_consensus

        request, manifest = self._request(spec)
        method = request.setdefault("analysis_method", "structure")
        if method not in {"structure", "legacy_reference", *COMBINED}:
            raise ValueError("Unknown scan method")
        if request.get("symbols"):
            request["symbols"] = list(dict.fromkeys(self._symbol(value) for value in request["symbols"]))
        if request.get("resume_scan_id"):
            previous = self.read_report(request["resume_scan_id"])
            if previous.get("input_manifest_id") != manifest:
                raise ValueError("Scan input manifest changed; create a new scan")
            request["resume_scan_id"] = previous["run_id"]
        with self._lock:
            store = ResultStore(db_path=self.results_db, runs_root=self.runs_root)
            if method in COMBINED:
                result = scan_methods(self._engine(), store, request, progress=progress, check_cancel=check_cancel)
            else:
                from my_strategy.adapters.czsc_adapter import raw_path
                from my_strategy.core.paths import ARTIFACT_RUNS_ROOT, PROCESSED_DATA_ROOT

                if (raw_path().resolve() != self.raw_db or ARTIFACT_RUNS_ROOT.resolve() != self.runs_root
                        or (PROCESSED_DATA_ROOT / "czsc" / "results.db").resolve() != self.results_db):
                    raise ValueError("Scan runtime paths do not match the configured research bridge")
                request.pop("analysis_method")
                request.setdefault("cpu_workers", 4)
                if isinstance(request["cpu_workers"], bool) or not isinstance(request["cpu_workers"], int) or not 1 <= request["cpu_workers"] <= 4:
                    raise ValueError("Source preparation workers must be in 1..4")
                result = self._call(scan_trend_consensus, request, structure_only=method == "structure",
                                    progress=progress, check_cancel=check_cancel)
            return self._persist("scan", {**result, "analysis_method": method}, manifest)

    def train(self, spec, progress=None, check_cancel=None):
        request, manifest = self._request(spec)
        request.setdefault("feature_profile", "ma_trend_v1")
        request.setdefault("variant", "A")
        request.setdefault("model_type", "mlp")
        request["symbols"] = list(dict.fromkeys(self._symbol(value) for value in request["symbols"]))
        with self._lock:
            result = self._call(self._engine().train_score, request, progress=progress, stop=_Cancellation(check_cancel))
            return self._persist("training", result, manifest)

    def list_models(self):
        models = []
        database = self.research_root / "research.db"
        if database.is_file():
            with closing(self._read_connection(database)) as connection:
                if self._table(connection, "results"):
                    rows = connection.execute("SELECT cache_key,namespace,sha256,created_at FROM results "
                        "WHERE namespace IN ('mlp_score_models','score_models') ORDER BY created_at DESC").fetchall()
                    for row in rows:
                        value = self._research_object(row["sha256"])
                        artifact = value.get("artifact") or {}
                        models.append({"model_id": row["cache_key"], "model_type": value.get("model_type", "l2"),
                                       "feature_profile": artifact.get("feature_profile", "mixed_v1"),
                                       "variant": artifact.get("variant"), "model_version": artifact.get("model_version"),
                                       "created_at": row["created_at"], "status": value.get("status"),
                                       "qualification": artifact.get("qualification"), "historical_only": False})
        metadata = self.research_root.parent
        pointer_path = metadata / "reference_current.json"
        selected = json.loads(pointer_path.read_text(encoding="utf-8")) if pointer_path.is_file() else {}
        for path in sorted(self.runs_root.glob("*/reports/reference_training.json")):
            selected_here = path.parent.parent.name == selected.get("run_id")
            report = self._report_file(path.relative_to(self.runs_root), selected.get("report_sha256") if selected_here else None)
            run_id = report.get("run_id")
            models.append({"model_id": run_id, "family": report.get("family", "reference_position_v1"),
                           "status": report.get("status"), "historical_only": True,
                           "selected_in_legacy": run_id == selected.get("run_id"),
                           "identity_upgraded": False})
        return {"items": models, "count": len(models)}

    @staticmethod
    def _read_connection(path):
        connection = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _table(connection, name):
        return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def _research_object(self, sha):
        if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
            raise ValueError("Invalid research object identity")
        objects = self.research_root / "objects"
        if objects.is_symlink():
            raise ValueError("Research objects cannot be symbolic links")
        root = objects.resolve()
        path = (root / sha[:2] / (sha + ".json.gz")).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Research object escaped its store")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != sha:
            raise ValueError("Research object SHA256 mismatch")
        return json.loads(gzip.decompress(content))

    def list_reports(self, kind=None, limit=50):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Report limit must be in 1..200")
        if not self.results_db.is_file():
            return {"items": [], "count": 0}
        items = []
        with closing(self._read_connection(self.results_db)) as connection:
            if self._table(connection, "lake_research_runs"):
                where, values = ("WHERE kind=?", [kind]) if kind else ("", [])
                rows = connection.execute("SELECT run_id,kind,created_at,report_sha256,input_manifest_id,engine_run_id "
                    "FROM lake_research_runs " + where + " ORDER BY created_at DESC LIMIT ?", [*values, limit]).fetchall()
                items.extend(dict(row) for row in rows)
            if self._table(connection, "czsc_runs"):
                where, values = ("WHERE kind=?", [kind]) if kind else ("", [])
                rows = connection.execute("SELECT run_id,kind,created_at,symbol,summary FROM czsc_runs " + where +
                    " ORDER BY created_at DESC LIMIT ?", [*values, limit]).fetchall()
                known = {item.get("engine_run_id") for item in items}
                items.extend({**dict(row), "summary": json.loads(row["summary"])} for row in rows if row["run_id"] not in known)
        def created_at(item):
            timestamp = datetime.fromisoformat(item["created_at"])
            return timestamp.replace(tzinfo=ZoneInfo("Asia/Shanghai")) if timestamp.tzinfo is None else timestamp

        items.sort(key=created_at, reverse=True)
        return {"items": items[:limit], "count": len(items[:limit])}

    def read_report(self, run_id):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", run_id):
            raise ValueError("Invalid report identity")
        if self.results_db.is_file():
            with closing(self._read_connection(self.results_db)) as connection:
                if self._table(connection, "lake_research_runs"):
                    row = connection.execute("SELECT report_path,report_sha256 FROM lake_research_runs WHERE run_id=?", (run_id,)).fetchone()
                    if row:
                        return self._report_file(row["report_path"], row["report_sha256"])
                if self._table(connection, "czsc_runs"):
                    row = connection.execute("SELECT report_path FROM czsc_runs WHERE run_id=?", (run_id,)).fetchone()
                    if row:
                        digest = None
                        if self._table(connection, "scan_views"):
                            indexed = connection.execute("SELECT report_sha256 FROM scan_views WHERE run_id=?", (run_id,)).fetchone()
                            digest = indexed[0] if indexed else None
                        return self._report_file(row[0], digest)
        database = self.research_root / "research.db"
        if database.is_file():
            with closing(self._read_connection(database)) as connection:
                if self._table(connection, "results"):
                    row = connection.execute("SELECT sha256 FROM results WHERE cache_key=? AND namespace='simulations'", (run_id,)).fetchone()
                    if row:
                        return self._research_object(row["sha256"])
        archived = self.runs_root / run_id / "reports" / "reference_training.json"
        if archived.is_file():
            pointer = self.research_root.parent / "reference_current.json"
            selected = json.loads(pointer.read_text(encoding="utf-8")) if pointer.is_file() else {}
            digest = selected.get("report_sha256") if selected.get("run_id") == run_id else None
            return self._report_file(archived.relative_to(self.runs_root), digest)
        raise KeyError(run_id)

    def _report_file(self, relative, digest=None):
        path = (self.runs_root / relative).resolve()
        if not path.is_relative_to(self.runs_root):
            raise ValueError("Report path escaped run storage")
        content = path.read_bytes()
        if digest is not None and hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Report SHA256 mismatch")
        return json.loads(content)

    def _persist(self, kind, result, input_manifest_id):
        from my_strategy.services.light_research import plain

        now = datetime.now(timezone.utc)
        run_id = "lake-" + now.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex
        value = plain(dict(result))
        value["lake_run_id"] = run_id
        value["input_manifest_id"] = input_manifest_id
        if not value.get("run_id"):
            value["run_id"] = run_id
        relative = Path(run_id) / "reports" / "lake_research.json"
        path = self.runs_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)
        manifest = {"schema": "lake_research_report_v1", "run_id": run_id, "kind": kind,
                    "created_at": now.isoformat(), "report_sha256": digest,
                    "input_manifest_id": input_manifest_id, "engine_run_id": result.get("run_id"),
                    "engine_identity_preserved": True}
        (path.parent / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.results_db.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.results_db, timeout=30)) as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS lake_research_runs (run_id TEXT PRIMARY KEY,kind TEXT NOT NULL,"
                "created_at TEXT NOT NULL,report_path TEXT NOT NULL,report_sha256 TEXT NOT NULL,input_manifest_id TEXT,engine_run_id TEXT)")
            connection.execute("INSERT INTO lake_research_runs VALUES(?,?,?,?,?,?,?)",
                (run_id, kind, now.isoformat(), relative.as_posix(), digest, input_manifest_id, result.get("run_id")))
            connection.commit()
        return {**value, "report_sha256": digest}


class _Cancellation:
    def __init__(self, callback):
        self.callback = callback

    def is_set(self):
        return bool(self.callback and self.callback())

    def check(self):
        if self.is_set():
            raise InterruptedError("Lake Equity research cancelled")
