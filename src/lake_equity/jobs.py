from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Callable
import uuid


ACTIVE = {"queued", "running", "cancelling"}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobCancelled(RuntimeError):
    pass


class JobManager:
    def __init__(self, root: Path, dispatch: Callable):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "jobs.db"
        self.dispatch = dispatch
        self.lock = threading.RLock()
        self.stops: dict[str, threading.Event] = {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lake-jobs")
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_key TEXT NOT NULL, body TEXT NOT NULL)")
            for identity, raw in connection.execute("SELECT id,body FROM jobs").fetchall():
                body = json.loads(raw)
                if body["status"] in ACTIVE:
                    body.update(status="interrupted", updated_at=now(), error="服务已重启；该任务没有自动恢复，请检查已保存产物后重新提交。")
                    connection.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False), identity))
            connection.commit()

    def connection(self):
        return closing(sqlite3.connect(self.path, timeout=30))

    def _save(self, body: dict) -> None:
        with self.connection() as connection:
            connection.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(body, ensure_ascii=False, default=str), body["id"]))
            connection.commit()

    def list(self, limit: int = 100) -> list[dict]:
        with self.connection() as connection:
            return [json.loads(row[0]) for row in connection.execute("SELECT body FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,))]

    def get(self, identity: str) -> dict:
        with self.connection() as connection:
            row = connection.execute("SELECT body FROM jobs WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise KeyError(identity)
        return json.loads(row[0])

    def submit(self, kind: str, params: dict) -> dict:
        canonical = json.dumps({"kind": kind, "params": params}, sort_keys=True, ensure_ascii=False, default=str)
        key = hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock:
            with self.connection() as connection:
                for row in connection.execute("SELECT body FROM jobs WHERE request_key=?", (key,)):
                    previous = json.loads(row[0])
                    if previous["status"] in ACTIVE:
                        return previous
                identity = "lake-" + uuid.uuid4().hex[:16]
                body = {"id": identity, "provider": "lake_equity", "kind": kind, "params": params,
                        "status": "queued", "created_at": now(), "updated_at": now(), "progress": {}, "error": None, "result": None}
                connection.execute("INSERT INTO jobs VALUES (?,?,?)", (identity, key, json.dumps(body, ensure_ascii=False)))
                connection.commit()
            self.stops[identity] = threading.Event()
            self.executor.submit(self._execute, identity)
            return body

    def _execute(self, identity: str) -> None:
        def cancelled() -> bool:
            return self.stops[identity].is_set()

        def progress(*values, **fields):
            if values and isinstance(values[0], dict):
                shown = {**values[0], **fields}
            elif values:
                shown = {**dict(zip(("stage", "current", "total", "failed"), values)), **fields, "progress_scope": "current_stage"}
            else:
                shown = fields
            with self.lock:
                body = self.get(identity)
                body.update(progress=shown, updated_at=now())
                self._save(body)

        with self.lock:
            body = self.get(identity)
            if cancelled():
                body.update(status="cancelled", updated_at=now())
                self._save(body)
                return
            body.update(status="running", updated_at=now())
            self._save(body)
        try:
            result = self.dispatch(body["kind"], body["params"], progress, cancelled)
            result_path = self.root / f"{identity}.json"
            encoded = json.dumps(result, ensure_ascii=False, default=str, allow_nan=False).encode("utf-8")
            temporary = result_path.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(result_path)
            with self.lock:
                body = self.get(identity)
                reported = result.get("status")
                outcome = "failed" if reported == "failed" else "cancelled" if reported == "cancelled" else "partial" if reported in {"partial", "blocked", "pending", "interrupted", "incomplete", "deferred"} else "succeeded"
                body.update(status=outcome, updated_at=now(), result={"path": result_path.name, "sha256": hashlib.sha256(encoded).hexdigest(),
                            "run_id": result.get("lake_run_id", result.get("run_id", result.get("manifest_id")))})
                body["business_status"] = reported
                if cancelled() and outcome != "cancelled":
                    body.update(cancel_requested=True, cancel_effective=False,
                                cancel_note="取消请求到达时本次计算已完成并保存；结果保留，没有将已完成任务标为已停止。")
                elif outcome == "cancelled":
                    body.update(cancel_requested=True, cancel_effective=True, cancel_note="任务已响应取消，已完成批次和报告保留。")
                body["run_id"] = body["result"]["run_id"]
                self._save(body)
        except Exception as error:
            with self.lock:
                body = self.get(identity)
                body.update(status="cancelled" if cancelled() or isinstance(error, JobCancelled) else "failed", updated_at=now(),
                            error=f"{type(error).__name__}: {error}")
                if body["status"] == "cancelled":
                    body.update(cancel_effective=True, cancel_note="任务已响应取消，已保存批次保留。")
                self._save(body)

    def result(self, identity: str) -> dict:
        body = self.get(identity)
        if not body.get("result"):
            raise ValueError("任务尚未生成结果")
        path = self.root / body["result"]["path"]
        encoded = path.read_bytes()
        if hashlib.sha256(encoded).hexdigest() != body["result"]["sha256"]:
            raise ValueError("任务结果内容核验失败")
        return json.loads(encoded)

    def cancel(self, identity: str) -> dict:
        with self.lock:
            body = self.get(identity)
            if body["status"] in ACTIVE:
                self.stops[identity].set()
                body.update(status="cancelling", updated_at=now(), cancel_requested=True)
                self._save(body)
            return body

    def close(self) -> None:
        for stop in self.stops.values():
            stop.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
