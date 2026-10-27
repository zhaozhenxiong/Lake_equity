"""Bounded, read-only progress for the upstream daily-update subprocess.

Source meters describe all callers of that source. They are activity evidence,
never a stock-completion count or a coverage verdict.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import time
import tomllib


_READ_BYTES = 64 * 1024
_LINE_BYTES = 8192
_STEP = re.compile(r"\bStep ([a-zA-Z_][a-zA-Z_0-9]{0,63}) (starting|success|warning|failed|source unavailable)\b")
_BATCH = re.compile(r"\b([a-zA-Z_][a-zA-Z_0-9]{0,63}) (\d+)/(\d+) batches\b")
_THS = re.compile(r"\bTHS sweep: (\d+)/(\d+) boards\b")


def _json(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = handle.read(16 * 1024 + 1)
        value = json.loads(data) if len(data) <= 16 * 1024 else None
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _rate_root(root: Path) -> Path:
    try:
        with (root / "configs/cnequity.toml").open("rb") as handle:
            raw = tomllib.load(handle)
        configured = Path(raw["data"]["root"]).expanduser()
        data_root = configured if configured.is_absolute() else root / configured
    except (OSError, ValueError, KeyError, TypeError):
        data_root = root / "var/lake"
    return data_root / "meta/rate_limits"


class UpdateProgress:
    def __init__(self, root: Path, log_path: Path, offset: int, *, clock=time.monotonic):
        self.log_path = Path(log_path)
        self.offset = offset
        self.clock = clock
        self.started = self.active_at = clock()
        self.pending = b""
        self.skip_line = False
        self.message = "数据采集启动中"
        self.source_warning = None
        self.current = self.total = None
        self.daily_active = self.sina_active = False
        self.rate_root = _rate_root(Path(root))
        self.meters = {}
        for name in ("meter-sina", "wire-sina"):
            day, count = self._meter(name)
            self.meters[name] = {"day": day, "baseline": count, "count": count}

    def _meter(self, name: str):
        value = _json(self.rate_root / f"{name}.json")
        aliases = value.get("aliases")
        if name == "meter-sina":
            count = aliases.get("sina_bars") if isinstance(aliases, dict) else None
        else:
            count = value.get("responses")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return value.get("policy_day"), count
        return None, None

    def _line(self, text: str) -> bool:
        if not text.strip() or "still working:" in text or "no output for" in text:
            return False
        step = _STEP.search(text)
        if step:
            name, state = step.groups()
            self.current = self.total = None
            if state == "starting":
                self.source_warning = None
            self.message = f"{name} · " + {"starting": "采集中", "success": "已完成",
                                          "warning": "完成但有缺项", "failed": "失败",
                                          "source unavailable": "数据源不可用"}[state]
            if name == "daily_bars":
                self.daily_active = state == "starting"
                self.sina_active = False
                self.source_warning = None
        batch = _BATCH.search(text)
        ths = _THS.search(text)
        if batch or ths:
            name, current, total = batch.groups() if batch else ("同花顺板块", *ths.groups())
            if 0 <= int(current) <= int(total) and int(total) > 0:
                self.current, self.total = int(current), int(total)
                unit = "批" if batch else "板块"
                self.message = f"{name} · {current}/{total} {unit}（当前采集步骤）"
                self.sina_active = False
        if "TDX fetch failed" in text or "no TDX server responded with data" in text:
            self.source_warning = "通达信行情源取数失败；等待采集结果及备用源处理。"
        if self.daily_active and "sina bars failed" in text:
            self.sina_active = True
            self.current = self.total = None
            self.message = "daily_bars · 新浪备用源采集中"
            self.source_warning = ("新浪备用源有请求超时，正在继续采集。" if "timed out" in text.lower() or "超时" in text
                                   else "新浪备用源有请求失败，正在继续采集。")
        # Upstream success output is sparse. Any non-heartbeat output is
        # observable activity, without exposing raw URLs, errors or credentials.
        return True

    def _read_log(self) -> bool:
        try:
            with self.log_path.open("rb") as handle:
                if handle.seek(0, 2) < self.offset:
                    self.offset, self.pending, self.skip_line = 0, b"", False
                handle.seek(self.offset)
                chunk = handle.read(_READ_BYTES)
                self.offset = handle.tell()
        except OSError:
            return False
        lines = (self.pending + chunk).split(b"\n")
        tail = lines.pop()
        if self.skip_line and lines:
            lines.pop(0)
            self.skip_line = False
        if self.skip_line or len(tail) > _LINE_BYTES:
            self.pending, self.skip_line = b"", True
        else:
            self.pending = tail
        active = False
        for line in lines:
            if len(line) <= _LINE_BYTES:
                active = self._line(line.decode("utf-8", errors="replace")) or active
        return active

    def sample(self) -> dict:
        now = self.clock()
        active = self._read_log()
        meter_changed = False
        for name, previous in self.meters.items():
            day, count = self._meter(name)
            if count is None:
                continue
            if previous["count"] is None:
                previous.update(day=day, baseline=count, count=count)
                continue
            if previous["day"] is not None and day != previous["day"]:
                previous["baseline"] = 0
            if count < previous["count"]:
                previous["baseline"] = 0
            meter_changed = count > previous["count"] or meter_changed
            previous.update(day=day, count=count)
        if self.daily_active and meter_changed:
            self.sina_active = True
            self.current = self.total = None
            active = True
        if (self.daily_active and self.sina_active
                and all(value["count"] is not None for value in self.meters.values())):
            requests, responses = (max(0, self.meters[name]["count"] - self.meters[name]["baseline"])
                                   for name in ("meter-sina", "wire-sina"))
            self.message = f"新浪备用源 · 本次观察到 {requests} 次请求、{responses} 次响应（源级计数）"
        if active:
            self.active_at = now
        idle = max(0, int(now - self.active_at))
        warning = self.source_warning
        if idle >= 300:
            warning = f"已 {idle // 60} 分钟无新的采集日志或源请求活动；进程仍在运行，请检查网络或取消任务。"
        return {"message": self.message, "warning": warning,
                "elapsed_seconds": max(0, int(now - self.started)), "idle_seconds": idle,
                "current": self.current, "total": self.total, "progress_scope": "current_stage"}
