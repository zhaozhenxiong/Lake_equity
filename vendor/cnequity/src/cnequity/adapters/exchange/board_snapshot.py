"""Short-lived, session-checked sharing for official exchange board responses."""

from __future__ import annotations

import io
import json
import warnings
from datetime import date, datetime, timezone

from cnequity.domain.http_policy import record_cache_reuse, record_http_response
from cnequity.domain.rate_limit import source_request
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

_TTL_SECONDS = 300
_SOURCE = "exchange"
_TIMEOUT_SECONDS = 60.0
_SSE_HEADERS = {"Referer": "https://www.sse.com.cn/"}
_SZSE_HEADERS = {"Referer": "https://www.szse.cn/"}
# The field order of each row follows `select`; the quote and status parsers
# read rows by position from this tuple.
SSE_SELECT = ("code", "name", "open", "high", "low", "last", "volume", "amount", "prev_close")
SSE_URL = (
    "http://yunhq.sse.com.cn:32041/v1/sh1/list/exchange/equity"
    f"?select={','.join(SSE_SELECT)}&begin=0&end=6000"
)
_SZSE_URL = (
    "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx"
    "&CATALOGID=1815_stock_snapshot&TABKEY=tab1"
    "&txtBeginDate={day}&txtEndDate={day}&random=0.1"
)


def _cached(config, exchange: str, day: date, fetch):
    if config is None:
        return fetch()
    path = config.meta_root / "source_cache" / "exchange" / f"{exchange}-{day}.json"
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
                age = datetime.now(timezone.utc) - datetime.fromisoformat(item["captured_at"])
                if 0 <= age.total_seconds() < _TTL_SECONDS:
                    record_cache_reuse(config, _SOURCE, f"{exchange}_board")
                    return item["payload"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        payload = fetch()
        write_json_atomic(
            path,
            {"captured_at": datetime.now(timezone.utc).isoformat(), "payload": payload},
        )
        return payload


def sse_snapshot(trade_date: date, *, config, client_factory) -> dict:
    def fetch():
        with source_request(config, _SOURCE):
            response = client_factory().get(
                SSE_URL,
                headers=_SSE_HEADERS,
                impersonate="chrome",
                timeout=_TIMEOUT_SECONDS,
            )
            record_http_response(config, _SOURCE, response)
        response.raise_for_status()
        payload = response.json()
        raw_date = str(payload.get("date") or "")
        try:
            observed = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:8]))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"SSE snapshot carried an unreadable date {raw_date!r}") from exc
        if observed != trade_date:
            raise ValueError(f"SSE snapshot serves {observed}, not {trade_date}")
        if not isinstance(payload.get("time"), int) or payload["time"] < 150000:
            raise ValueError(f"SSE snapshot is mid-session (time={payload.get('time')})")
        if not isinstance(payload.get("list"), list) or not payload["list"]:
            raise ValueError("SSE snapshot has no board rows")
        return payload

    return _cached(config, "sse", trade_date, fetch)


def szse_report(trade_date: date, *, config, client_factory):
    import pandas as pd

    required = {
        "交易日期",
        "证券代码",
        "证券简称",
        "开盘",
        "最高",
        "最低",
        "今收",
        "成交量(万股)",
        "成交金额(万元)",
    }

    def fetch():
        with source_request(config, _SOURCE):
            response = client_factory().get(
                _SZSE_URL.format(day=trade_date.isoformat()),
                headers=_SZSE_HEADERS,
                impersonate="chrome",
                timeout=_TIMEOUT_SECONDS,
            )
            record_http_response(config, _SOURCE, response)
        response.raise_for_status()
        if not response.content:
            raise ValueError(f"SZSE published no report for {trade_date}")
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Workbook contains no default style")
            frame = pd.read_excel(io.BytesIO(response.content), dtype=str)
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"SZSE report is missing {sorted(missing)}")
        dates = pd.to_datetime(frame["交易日期"], errors="coerce").dt.date
        if frame.empty or dates.isna().any() or not dates.eq(trade_date).all():
            raise ValueError(f"SZSE report contains rows outside {trade_date}")
        return frame.where(pd.notna(frame), None).to_dict("records")

    return pd.DataFrame(_cached(config, "szse", trade_date, fetch))
