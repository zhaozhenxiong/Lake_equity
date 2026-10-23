from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tomllib
import uuid
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

import polars as pl
from filelock import FileLock, Timeout

from cnequity.config import Config, load_config
from cnequity.domain.contracts import contract_fingerprint
from cnequity.domain.datasets import DATASETS
from cnequity.domain.schemas import DAILY_BARS_SCHEMA, data_version_for
from cnequity.query import dataset_attempt, dataset_state, list_datasets, load_with_receipt, scan
from cnequity.storage.atomic import write_json_atomic
from cnequity.storage.parquet import StagingWriter, compact_dataset
from cnequity.storage.revisions import RevisionStore


class DataOperationCancelled(RuntimeError):
    pass


def _cancel(check_cancel: Callable | None) -> None:
    if check_cancel is not None and check_cancel():
        raise DataOperationCancelled("Data operation cancelled")


def _notify(progress: Callable | None, **payload) -> None:
    if progress is not None:
        progress(payload)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _jsonable(value):
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Expected a regular source SQLite file: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    return connection


def _require_frozen(path: Path) -> None:
    for suffix in ("-wal", "-journal"):
        companion = Path(str(path) + suffix)
        if companion.is_file() and companion.stat().st_size:
            raise ValueError("Use a frozen SQLite backup without an active WAL or journal")


def _timestamp(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Observation timestamp must carry an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class LakeService:
    def __init__(self, project_root: str | Path, source_db: str | Path | None = None, data_root: str | Path | None = None):
        self.project_root = Path(project_root).resolve()
        self.var_root = self.project_root / "var"
        self.data_root = Path(data_root).resolve() if data_root else self.var_root / "lake"
        self.source_db = Path(source_db).resolve() if source_db else self.var_root / "legacy" / "raw" / "khquant_raw.db"
        self.config_path = self.project_root / "configs" / "cnequity.toml"
        if self.config_path.is_file():
            with self.config_path.open("rb") as handle:
                configured = tomllib.load(handle)
            configured_root = Path(configured.get("data", {}).get("root", str(self.data_root))).expanduser()
            if not configured_root.is_absolute():
                configured_root = self.project_root / configured_root
            configured_root = configured_root.resolve()
            if data_root is not None and configured_root != self.data_root:
                raise ValueError("Explicit data_root disagrees with configs/cnequity.toml")
            self.data_root = configured_root
            self.config = load_config(self.config_path)
            self.config.data_root = self.data_root
            duckdb_value = configured.get("duckdb", {}).get("path")
            if duckdb_value:
                duckdb_path = Path(duckdb_value.replace("{data.root}", str(self.data_root))).expanduser()
                self.config.duckdb_path = (duckdb_path if duckdb_path.is_absolute() else self.project_root / duckdb_path).resolve()
            else:
                self.config.duckdb_path = self.data_root / "duckdb" / "cnequity.duckdb"
        else:
            self.config = Config(data_root=self.data_root, workers=4, tdx_allow_mock=False)
        if not self.data_root.is_relative_to(self.var_root):
            raise ValueError("Lake writes must stay inside the project's var directory")
        if self.config.tdx_allow_mock:
            raise ValueError("LakeEquity does not permit fabricated market-data sources")

    def catalog(self) -> dict:
        rows = list_datasets(config=self.config).to_dicts()
        for row in rows:
            name = row["dataset"]
            spec = DATASETS[name]
            row.update(name=name, description=f"{spec.tier} · {name.replace('_', ' ')}", published_state=self.state(name))
        return _jsonable({"datasets": rows, "count": len(rows), "data_root": str(self.data_root)})

    def state(self, dataset: str) -> dict:
        return _jsonable({**asdict(dataset_state(dataset, config=self.config)), "last_attempt": dataset_attempt(dataset, config=self.config)})

    def quality(self) -> dict:
        from cnequity.serve.lake import LakeView

        payload = LakeView(self.config).quality()
        imports = self.config.meta_root / "lake_equity" / "imports"
        payload["legacy_imports"] = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(imports.glob("*.json"))][-20:]
        payload["historical_completeness"] = "not_assessed"
        payload["strict_history_eligible"] = False
        return _jsonable(payload)

    def query(self, dataset: str, *, symbols: list[str] | None = None, start=None, end=None, as_of=None, **options):
        if dataset not in DATASETS:
            raise ValueError(f"Unknown dataset: {dataset}")
        if DATASETS[dataset].pit and as_of is None:
            raise ValueError("PIT dataset reads require as_of")
        if DATASETS[dataset].pit:
            options.setdefault("pit_mode", "strict")
        return load_with_receipt(dataset, config=self.config, symbols=symbols, start=start, end=end, as_of=as_of, require_replayable=True, **options)

    def read(self, dataset: str, symbol: str | None = None, start=None, end=None, as_of=None, limit: int = 100, **options) -> dict:
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        if symbol is None and (start is None or end != start):
            raise ValueError("Multi-day browser reads require a symbol; use query() for bulk research")
        result = self.query(dataset, symbols=[symbol] if symbol else None, start=start, end=end, as_of=as_of, **options)
        return _jsonable({"rows": result.frame.tail(limit).to_dicts(), "receipt": result.receipt, "count": result.frame.height, "returned_count": min(limit, result.frame.height)})

    def _import_dir(self) -> Path:
        return self.config.meta_root / "lake_equity" / "imports"

    def _frame(self, rows: list[sqlite3.Row]) -> pl.DataFrame:
        values = []
        for row in rows:
            source = row["source"]
            if not source or not isinstance(source, str):
                raise ValueError("Legacy rows require their original nonempty source")
            prices = [row[column] for column in ("open", "high", "low", "close")]
            if any(value is None or not math.isfinite(value) or value <= 0 for value in prices):
                raise ValueError(f"Invalid original OHLC: {row['stock']} {row['date']}")
            volume = row["volume"]
            if volume is None or not math.isfinite(volume) or volume < 0 or int(volume) != volume:
                raise ValueError("Legacy volume must be a nonnegative integer number of shares")
            if row["has_trade_price"] != 1:
                raise ValueError("Legacy rows without original trade-price evidence cannot enter unadjusted daily_bars")
            values.append({"symbol": row["stock"], "trade_date": date.fromisoformat(row["date"]), **{column: row[column] for column in ("open", "high", "low", "close", "amount")}, "pre_close": None, "volume": int(volume), "source": source, "data_version": data_version_for("daily_bars"), "fetched_at": _timestamp(row["imported_at"])})
        return pl.DataFrame(values, schema=DAILY_BARS_SCHEMA)

    def import_legacy(self, progress: Callable | None = None, check_cancel: Callable | None = None, batch_size: int = 50000) -> dict:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        _cancel(check_cancel)
        _require_frozen(self.source_db)
        before = self.source_db.stat()
        _notify(progress, phase="hash_source", source_db=str(self.source_db))
        source_sha = _sha256(self.source_db)
        import_id = hashlib.sha256(("legacy_daily_bars_v1:" + source_sha).encode()).hexdigest()
        report_path = self._import_dir() / f"{import_id}.json"
        if report_path.exists():
            existing = json.loads(report_path.read_text(encoding="utf-8"))
            if existing.get("status") == "complete":
                return {**existing, "unchanged": True}
        run_id = "legacy-" + import_id[:24]
        report = {"schema_version": 1, "import_id": import_id, "run_id": run_id, "status": "running", "source_db": str(self.source_db), "source_sha256": source_sha, "volume_unit": "share", "amount_unit": "CNY", "source_mapping": "original_labels_preserved", "price_evidence": "original_snapshot_sha256", "strict_history_eligible": False, "rows_read": 0, "rows_published": 0, "started_at": datetime.now(timezone.utc).isoformat()}
        write_json_atomic(report_path, report)
        writer = StagingWriter(self.config.staging_root)
        store = RevisionStore(self.config.meta_root, self.config.curated_root, self.config.derived_root)
        try:
            with closing(_readonly(self.source_db)) as connection:
                connection.execute("BEGIN")
                total_rows, first_date, last_date = connection.execute("SELECT count(*),min(date),max(date) FROM stock_daily_normalized").fetchone()
                report.update(total_rows=total_rows, coverage_start=first_date, coverage_end=last_date)
                cursor = connection.execute("SELECT * FROM stock_daily_normalized ORDER BY date,stock")
                pending: dict[str, list[Path]] = {}
                batch_number = 0
                while rows := cursor.fetchmany(batch_size):
                    _cancel(check_cancel)
                    frame = self._frame(rows)
                    for key, group in frame.with_columns(pl.col("trade_date").dt.year().alias("_year")).partition_by("_year", as_dict=True).items():
                        year = str(key[0])
                        path = writer.write_batch("daily_bars", run_id, f"{year}-{batch_number:06d}", group.drop("_year"))
                        if pl.read_parquet(path).height != group.height:
                            raise ValueError("CNEquity row sanitization removed legacy rows; inspect the retained staging batch")
                        pending.setdefault(year, []).append(path)
                    report["rows_read"] += len(rows)
                    batch_number += 1
                    _notify(progress, phase="stage", rows_read=report["rows_read"], total_rows=total_rows)
                connection.rollback()
            after = self.source_db.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError("Source SQLite changed during import; use a frozen SQLite backup")
            from cnequity.file_lock import lake_mutation_lock

            with lake_mutation_lock(self.config.meta_root, blocking=True):
                base = store.ensure_current("daily_bars", _locked=True)
                changed_files: list[Path] = []
                for year, files in sorted(pending.items()):
                    _cancel(check_cancel)
                    compact_dataset(self.config.staging_root, self.config.curated_root, "daily_bars", run_id, changed_files=changed_files, base_root=base, staging_files=files)
                    _notify(progress, phase="compact", year=year, rows_read=report["rows_read"], total_rows=total_rows)
                _cancel(check_cancel)
                spec = DATASETS["daily_bars"]
                revision = store.commit("daily_bars", run_id=run_id, changed_files=changed_files, schema_version=spec.schema_version, contract_fingerprint=contract_fingerprint("daily_bars"), metadata={"legacy_import": report, "coverage_completeness": "not_assessed"}, _locked=True)
                report.update(status="complete", rows_published=report["rows_read"], revision_id=revision.revision_id if revision else self.state("daily_bars")["revision_id"], finished_at=datetime.now(timezone.utc).isoformat())
                write_json_atomic(report_path, report)
            _notify(progress, phase="complete", **report)
            return _jsonable(report)
        except BaseException as error:
            report.update(status="cancelled" if isinstance(error, DataOperationCancelled) else "failed", error=str(error), finished_at=datetime.now(timezone.utc).isoformat())
            write_json_atomic(report_path, report)
            raise

    def _legacy_evidence(self) -> dict | None:
        reports = []
        for path in self._import_dir().glob("*.json"):
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("status") == "complete" and report.get("source_db") == str(self.source_db):
                reports.append(report)
        if not reports:
            return None
        report = max(reports, key=lambda item: item["finished_at"])
        _require_frozen(self.source_db)
        if not self.source_db.is_file() or _sha256(self.source_db) != report["source_sha256"]:
            raise ValueError("Legacy evidence snapshot changed; original price evidence cannot be reused")
        return report

    def _copy_metadata(self, original: sqlite3.Connection | None, target: sqlite3.Connection) -> list[str]:
        copied = []
        allowed = {"warehouse_meta", "import_runs", "securities", "stock_basic", "stock_industry_metadata", "stock_industry_exclusions", "industry_classification_runs", "data_quality_issues", "data_source_status", "update_log", "data_update_runs"}
        if original is not None:
            for row in original.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"):
                name = row["name"]
                if name not in allowed:
                    continue
                target.execute(row["sql"])
                cursor = original.execute(f"SELECT * FROM {_quoted(name)}")
                insert = f"INSERT INTO {_quoted(name)} VALUES ({','.join('?' for _ in cursor.description)})"
                while values := cursor.fetchmany(5000):
                    target.executemany(insert, values)
                copied.append(name)
        if "securities" not in copied:
            target.execute("CREATE TABLE securities(stock TEXT PRIMARY KEY,exchange TEXT,name TEXT,first_date TEXT,last_date TEXT,raw_rows INTEGER,source_file TEXT,updated_at TEXT,imported_at TEXT NOT NULL)")
        target.execute("CREATE TABLE stock_daily_normalized(stock TEXT NOT NULL,date TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,factor_open REAL,factor_high REAL,factor_low REAL,factor_close REAL,volume REAL,amount REAL,turnover REAL,pct_chg REAL,source TEXT,source_file TEXT NOT NULL,duplicate_source_rows INTEGER NOT NULL DEFAULT 1,has_trade_price INTEGER NOT NULL DEFAULT 0,imported_at TEXT NOT NULL,PRIMARY KEY(stock,date))")
        target.execute("CREATE TABLE lake_bar_evidence(stock TEXT,date TEXT,source TEXT,fetched_at TEXT,available_at TEXT,has_trade_price INTEGER,legacy_import_id TEXT,revision_id TEXT,PRIMARY KEY(stock,date))")
        target.execute("CREATE VIEW v_latest_stock_daily AS SELECT * FROM stock_daily_normalized")
        if "stock_industry_metadata" in copied:
            target.execute("CREATE VIEW v_stock_industry_eligible AS SELECT * FROM stock_industry_metadata WHERE classification_eligible=1")
        return copied

    def materialize_legacy(self, destination: str | Path | None = None, progress: Callable | None = None, check_cancel: Callable | None = None, *, symbols: list[str] | None = None, start=None, end=None) -> dict:
        lock_path = self.var_root / "research_inputs/input.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(lock_path))
        while True:
            _cancel(check_cancel)
            try:
                lock.acquire(timeout=0.2)
                break
            except Timeout:
                continue
        try:
            _cancel(check_cancel)
            return self._materialize_legacy(destination, progress, check_cancel, symbols=symbols, start=start, end=end)
        finally:
            lock.release()

    def _materialize_legacy(self, destination: str | Path | None = None, progress: Callable | None = None, check_cancel: Callable | None = None, *, symbols: list[str] | None = None, start=None, end=None) -> dict:
        target = Path(destination).resolve() if destination else self.var_root / "research_inputs" / "current" / "khquant_raw.db"
        if not target.is_relative_to(self.var_root) or target == self.source_db:
            raise ValueError("Research materialization must stay in var and separate from the evidence source")
        _cancel(check_cancel)
        pins = {name: state["revision_id"] for name in DATASETS if (state := self.state(name))["revision_id"]}
        if "daily_bars" not in pins:
            raise ValueError("daily_bars has no published revision")
        evidence = self._legacy_evidence()
        bounds = scan("daily_bars", config=self.config, symbols=symbols, start=start, end=end, revision_map=pins).select(pl.col("trade_date").min().alias("first"), pl.col("trade_date").max().alias("last")).collect().row(0, named=True)
        if bounds["first"] is None:
            raise ValueError("No published bars in the requested research window")
        target.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            if Path(str(target) + suffix).exists():
                raise ValueError("Close the existing research cache before replacing its SQLite file")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.building")
        manifest = {"schema_version": 1, "kind": "cnequity_to_khquant_unadjusted_v1", "revision_map": pins, "receipt_dependencies": {}, "receipts": [], "strict_history_eligible": False, "historical_completeness": "not_assessed", "volume_unit": "share", "amount_unit": "CNY", "source_db": str(target), "legacy_evidence": evidence, "rows": 0, "legacy_price_evidence_rows": 0, "new_price_evidence_rows": 0}
        original = _readonly(self.source_db) if evidence else None
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA journal_mode=DELETE")
                connection.execute("PRAGMA synchronous=FULL")
                manifest["metadata_tables"] = self._copy_metadata(original, connection)
                for year in range(bounds["first"].year, bounds["last"].year + 1):
                    _cancel(check_cancel)
                    lower = max(bounds["first"], date(year, 1, 1))
                    upper = min(bounds["last"], date(year, 12, 31))
                    result = self.query("daily_bars", symbols=symbols, start=lower, end=upper, revision_map=pins)
                    manifest["receipts"].append(result.receipt)
                    for name, identity in result.receipt["dependencies"].items():
                        previous = manifest["receipt_dependencies"].setdefault(name, identity)
                        if previous != identity:
                            raise ValueError("Dataset identity changed inside a frozen materialization")
                    old_rows = {}
                    if original is not None:
                        sql = "SELECT * FROM stock_daily_normalized WHERE date>=? AND date<=?"
                        params = [lower.isoformat(), upper.isoformat()]
                        if symbols:
                            sql += " AND stock IN (" + ",".join("?" for _ in symbols) + ")"
                            params.extend(symbols)
                        old_rows = {(row["stock"], row["date"]): dict(row) for row in original.execute(sql, params)}
                    normalized = []
                    price_evidence = []
                    for row in result.frame.iter_rows(named=True):
                        day = row["trade_date"].isoformat()
                        key = (row["symbol"], day)
                        old = old_rows.get(key)
                        matches = old is not None and old.get("source") == row["source"] and all(old.get(column) == row[column] for column in ("open", "high", "low", "close", "volume", "amount")) and _timestamp(old["imported_at"]) == _timestamp(row["fetched_at"])
                        has_price = int(old.get("has_trade_price", 0)) if matches else 0
                        fetched = _timestamp(row["fetched_at"]).isoformat()
                        prices = [row[column] for column in ("open", "high", "low", "close")]
                        factors = [old.get("factor_" + column) for column in ("open", "high", "low", "close")] if matches else prices
                        normalized.append((*key, *prices, *factors, row["volume"], row["amount"], old.get("turnover") if matches else None, old.get("pct_chg") if matches else None, row["source"], old["source_file"] if matches else f"cnequity://daily_bars/{pins['daily_bars']}/{row['symbol']}/{day}", old.get("duplicate_source_rows", 1) if matches else 1, has_price, old["imported_at"] if matches else fetched))
                        price_evidence.append((*key, row["source"], fetched, old.get("available_at") if matches else None, has_price, evidence["import_id"] if matches else None, pins["daily_bars"]))
                        manifest["legacy_price_evidence_rows"] += int(bool(matches and has_price))
                        if len(normalized) >= 5000:
                            _cancel(check_cancel)
                            connection.executemany("INSERT INTO stock_daily_normalized VALUES (" + ",".join("?" for _ in range(19)) + ")", normalized)
                            connection.executemany("INSERT INTO lake_bar_evidence VALUES (?,?,?,?,?,?,?,?)", price_evidence)
                            normalized.clear()
                            price_evidence.clear()
                    if normalized:
                        connection.executemany("INSERT INTO stock_daily_normalized VALUES (" + ",".join("?" for _ in range(19)) + ")", normalized)
                        connection.executemany("INSERT INTO lake_bar_evidence VALUES (?,?,?,?,?,?,?,?)", price_evidence)
                    manifest["rows"] += result.frame.height
                    connection.commit()
                    _notify(progress, phase="materialize", year=year, rows_written=manifest["rows"])
                connection.execute("INSERT INTO securities(stock,exchange,first_date,last_date,raw_rows,source_file,imported_at) SELECT stock,substr(stock,-2),min(date),max(date),count(*),'cnequity://daily_bars',max(imported_at) FROM stock_daily_normalized GROUP BY stock ON CONFLICT(stock) DO UPDATE SET first_date=excluded.first_date,last_date=excluded.last_date,raw_rows=excluded.raw_rows")
                connection.execute("DELETE FROM securities WHERE stock NOT IN (SELECT DISTINCT stock FROM stock_daily_normalized)")
                connection.commit()
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Materialized SQLite failed quick_check")
            _cancel(check_cancel)
            manifest.update(status="complete", sqlite_sha256=_sha256(temporary), finished_at=datetime.now(timezone.utc).isoformat())
            encoded = json.dumps(_jsonable(manifest), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            manifest["manifest_id"] = hashlib.sha256(encoded.encode()).hexdigest()
            archive_path = target.parent / "manifests" / f"{manifest['manifest_id']}.json"
            write_json_atomic(archive_path, _jsonable(manifest))
            os.replace(temporary, target)
            write_json_atomic(target.with_suffix(".manifest.json"), _jsonable(manifest))
            from lake_equity.data.input_contract import InputContract

            snapshot = InputContract(target).pin_snapshot()
            _notify(progress, phase="complete", rows_written=manifest["rows"], manifest_id=manifest["manifest_id"])
            return {**_jsonable(manifest), "snapshot_db": str(snapshot.raw_db)}
        finally:
            if original is not None:
                original.close()
            if temporary.exists():
                temporary.unlink()
