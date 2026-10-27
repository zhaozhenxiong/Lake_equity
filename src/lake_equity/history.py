"""Read existing journals and immutable results without legacy runtimes."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from types import SimpleNamespace


def _connection(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise ValueError("Historical database cannot be a symbolic link")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table(connection, name):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


class ReadOnlyResearchStore:
    """The existing CAS reader, opened without schema/TTL/journal mutations."""
    def __init__(self, root):
        from my_strategy.storage.research_store import ResearchStore
        self._reader = object.__new__(ResearchStore)
        self._reader.root = Path(root).resolve()
        self._reader.objects = self._reader.root / "objects"
        if self._reader.objects.is_symlink():
            raise ValueError("Historical objects cannot be symbolic links")
        self._reader._owned_objects_root = self._reader.objects.resolve()
        self._reader._lock = threading.RLock()
        self._reader.clock = time.time
        self._reader.connection = _connection(self._reader.root / "research.db")
        self.connection = self._reader.connection
        self._lock = self._reader._lock

    def get_by_key(self, key, *, touch=False):
        if touch:
            raise ValueError("Historical reads cannot update cache access or expiry")
        return self._reader.get_by_key(key, touch=False)

    def list(self, namespace=None, limit=100):
        return self._reader.list(namespace=namespace, limit=limit)

    def close(self):
        self.connection.close()


class HistoryReader:
    def __init__(self, settings, bridge, config):
        self.settings, self.bridge, self.config = settings, bridge, config
        self.research_root = settings.metadata_root / "light_research"
        self.journals = {"khquant": (settings.metadata_root / "czsc_tasks.db", "czsc_tasks"),
                         "light": (self.research_root / "task_journal.db", "jobs")}

    @staticmethod
    def _job(provider, row):
        identity = row.get("job_id", row.get("id"))
        if not isinstance(identity, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", identity) is None:
            raise ValueError("Invalid historical job identity")
        recorded = row.get("status", row.get("state", "unknown"))
        status = {"starting": "queued", "success": "succeeded", "complete": "succeeded", "completed": "succeeded"}.get(recorded, recorded)
        if provider != "cnequity" and status in {"queued", "running", "cancelling", "starting"}:
            status = "historical"
        return {**row, "id": provider + ":" + str(identity), "provider": provider,
                "source_id": identity, "status": status, "recorded_status": recorded,
                "kind": row.get("kind", row.get("op", row.get("op_id", "data_operation"))),
                "historical_read_only": provider != "cnequity", "automatic_resume": False, "result": None}

    def jobs(self, limit=100):
        items = []
        for provider, (path, table) in self.journals.items():
            if not path.is_file():
                continue
            with closing(_connection(path)) as connection:
                if not _table(connection, table):
                    continue
                rows = connection.execute(f"SELECT payload FROM {table} ORDER BY rowid DESC LIMIT ?", (-1 if limit is None else limit,)).fetchall()
                items.extend(self._job(provider, json.loads(row[0])) for row in rows)
        directory = self.config.meta_root / "serve_jobs"
        if directory.is_dir():
            for path in directory.glob("*.json"):
                if path.is_symlink():
                    raise ValueError("Historical operation receipt cannot be a symbolic link")
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("job_id"):
                    items.append(self._job("cnequity", value))
        return items

    def job(self, identity):
        if not isinstance(identity, str) or re.fullmatch(r"(?:khquant|light|cnequity):[A-Za-z0-9_.-]{1,200}", identity) is None:
            raise KeyError(identity)
        provider, source_id = identity.split(":", 1)
        if provider == "cnequity":
            path = self.config.meta_root / "serve_jobs" / (source_id + ".json")
            if not path.is_file() or path.is_symlink():
                raise KeyError(identity)
            value = json.loads(path.read_text(encoding="utf-8"))
        else:
            path, table = self.journals[provider]
            if not path.is_file():
                raise KeyError(identity)
            with closing(_connection(path)) as connection:
                if not _table(connection, table):
                    raise KeyError(identity)
                row = connection.execute(f"SELECT payload FROM {table} WHERE job_id=?", (source_id,)).fetchone()
                if row is None:
                    raise KeyError(identity)
                value = json.loads(row[0])
        if value.get("job_id", value.get("id")) != source_id:
            raise ValueError("Historical job payload identity mismatch")
        shown = self._job(provider, value)
        reference = value.get("run_id") or value.get("result_id") or (value.get("result_ref") or {}).get("result_id")
        if reference:
            try:
                shown["result"] = self.bridge.read_report(reference)
            except KeyError:
                shown["result_unavailable"] = "Saved reference is unavailable; the historical job was not rerun"
        return shown

    def accounts(self):
        path = self.research_root / "research.db"
        if not path.is_file():
            return {"accounts": [], "last_daily": None}
        with closing(_connection(path)) as connection:
            if not _table(connection, "settings"):
                return {"accounts": [], "last_daily": None}
            row = connection.execute("SELECT value_json FROM settings WHERE name='enabled_accounts'").fetchone()
            daily = connection.execute("SELECT value_json FROM settings WHERE name='last_daily'").fetchone()
        saved = json.loads(row[0]) if row else {}
        return {"accounts": [{**value, "account_id": key, "historical_read_only": True,
                              "automatic_continuation": False, "identity_upgraded": False}
                             for key, value in saved.items()], "last_daily": json.loads(daily[0]) if daily else None}

    def saved_simulation(self, result_id):
        if not isinstance(result_id, str) or re.fullmatch(r"[0-9a-f]{64}", result_id) is None:
            raise ValueError("Invalid saved account identity")
        if not (self.research_root / "research.db").is_file():
            raise KeyError(result_id)
        with closing(ReadOnlyResearchStore(self.research_root)) as store:
            row = store.connection.execute("SELECT namespace FROM results WHERE cache_key=?", (result_id,)).fetchone()
            if row is None or row[0] != "simulations":
                raise KeyError(result_id)
            value = store.get_by_key(result_id)
            if value is None:
                raise KeyError(result_id)
            pinned = store.connection.execute("SELECT 1 FROM pins WHERE cache_key=? LIMIT 1", (result_id,)).fetchone()
            return {**value, "saved": bool(pinned)}

    def account_chart(self, result_id, symbol, strategy, as_of):
        from my_strategy.services.saved_account_chart import account_chart
        return account_chart(SimpleNamespace(result=self.saved_simulation), result_id, symbol, strategy, as_of)

    def account_options(self, symbol, as_of):
        from my_strategy.services.saved_account_chart import chart_options
        if not (self.research_root / "research.db").is_file():
            return {"symbol": symbol, "as_of": as_of, "results": [], "lookup_limit": 50,
                    "prediction_started": False, "account_mutated": False}
        with closing(ReadOnlyResearchStore(self.research_root)) as store:
            return chart_options(SimpleNamespace(store=store), symbol, as_of)

    def reports(self, kind=None, limit=100):
        result = self.bridge.list_reports(kind=kind, limit=limit)
        items = list(result.get("items", result.get("reports", [])))
        path = self.research_root / "research.db"
        if kind in {None, "simulation", "backtest"} and path.is_file():
            with closing(_connection(path)) as connection:
                if _table(connection, "results"):
                    rows = connection.execute("SELECT cache_key,created_at FROM results WHERE namespace='simulations' ORDER BY created_at DESC LIMIT ?", (-1 if limit is None else limit,)).fetchall()
                    items.extend({"run_id": row["cache_key"], "kind": "simulation", "created_at": row["created_at"],
                                  "historical_read_only": True, "status": "saved"} for row in rows)
        return {"reports": items}

    def prediction_chart(self, reference, prediction_run_id=None):
        from my_strategy.services.prediction_chart import prediction_chart
        if not (self.research_root / "research.db").is_file():
            raise KeyError(reference)
        with closing(ReadOnlyResearchStore(self.research_root)) as store:
            result = prediction_chart(SimpleNamespace(store=store, raw_db=self.settings.raw_db), reference)
        if prediction_run_id is not None and result.get('history_verification', {}).get('reason_code') == 'history_price_contract_unverified':
            return self._analysis_bound_prediction_chart(reference, prediction_run_id, result)
        return result

    def _analysis_bound_prediction_chart(self, reference, prediction_run_id, result):
        """First-party display proof for bound THS inputs; never changes prediction quality."""
        from lake_equity.data import InputContract
        from lake_equity.research.prediction_binding import bind_analysis
        from my_strategy.services.kronos_prediction import VERIFIED_PRICE_SOURCES, prepare_input

        saved = self.bridge.read_report(prediction_run_id)
        if (saved.get('result_ref') or {}).get('cache_key') != reference:
            raise ValueError('Prediction report reference mismatch')
        binding = saved.get('analysis_binding') or {}
        info = result.get('input') or {}
        sources = set(info.get('source') or [])
        if (binding.get('compatible') is not True or 'ths' not in sources or not sources.issubset(VERIFIED_PRICE_SOURCES)
                or info.get('price_basis') != 'unadjusted' or info.get('volume_unit') != 'shares'
                or info.get('amount_unit') != 'CNY' or info.get('unverified_trade_price_bars') != 0):
            return result
        request = dict(symbol=result['symbol'], end=result['as_of'],
                       lookback=info['lookback'], horizon=info['horizon'])
        future = [row['date'] for row in result.get('forecast') or []]
        calendar = SimpleNamespace(calendar=lambda: (future, result.get('calendar')))
        try:
            verified, raw, observed, original = bind_analysis(self.bridge, binding['analysis_run_id'], request,
                                                            binding.get('input_manifest_id'), calendar)
            if binding != verified or saved.get('input') != info or observed != info:
                raise ValueError('Saved analysis and prediction input identities differ')
            frame, _, _ = prepare_input(raw, future, symbol=request['symbol'], as_of=request['end'],
                                         lookback=request['lookback'], horizon=request['horizon'])
            current = InputContract(self.settings.raw_db).validate()
            if current.manifest_sha256 != original.manifest_sha256 or current.stat_identity != original.stat_identity:
                raise ValueError('Analysis input changed while its chart was read')
        except (ValueError, KeyError, TypeError, OSError) as error:
            return {**result, 'history': [], 'history_verification': {**result['history_verification'],
                    'status': 'unavailable', 'reason_code': 'analysis_binding_unverified',
                    'reason': str(error), 'input_sha256_match': False}}
        history = [{'date': str(day)[:10], **{key: float(row[key]) for key in
                   ('open', 'high', 'low', 'close', 'volume', 'amount')}} for day, row in frame.iterrows()]
        return {**result, 'history': history, 'history_verification': {**result['history_verification'],
                'status': 'verified', 'input_sha256_match': True, 'analysis_binding_verified': True,
                'display_only_degraded': True, 'reason_code': 'analysis_bound_source_verified',
                'reason': '原预测与同代际分析尾窗核验一致；仅降级展示，不提升原预测资格'}}
