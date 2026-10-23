"""Share complete, unadjusted daily K windows across BaoStock consumers."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

from cnequity.adapters.baostock._session import check_result
from cnequity.domain.http_policy import record_cache_reuse
from cnequity.domain.rate_limit import source_request
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

FIELDS = "date,code,open,high,low,close,volume,amount,turn,tradestatus,peTTM,pbMRQ,psTTM,isST"
_COLUMNS = FIELDS.split(",")
_CACHE_VERSION = 1


class _RowsResult:
    def __init__(self, rows, fields, error_code="0", error_msg=""):
        self.fields = fields
        self.error_code = error_code
        self.error_msg = error_msg
        self.rows = rows
        self.position = -1

    def next(self):
        self.position += 1
        return self.position < len(self.rows)

    def get_row_data(self):
        return self.rows[self.position]


def _valid_rows(rows, code: str, year: int) -> bool:
    return isinstance(rows, list) and all(
        isinstance(row, list)
        and len(row) == len(_COLUMNS)
        and str(row[1]).strip().lower() == code.lower()
        and str(row[0]).startswith(f"{year}-")
        for row in rows
    )


def _fresh(path, year: int, code: str):
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(item["captured_at"])
        ttl = 86400 if year >= date.today().year - 1 else 30 * 86400
        if item["version"] != _CACHE_VERSION or not 0 <= age.total_seconds() < ttl:
            return None
        rows = item["rows"]
        digest = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
        return rows if digest == item["sha256"] and _valid_rows(rows, code, year) else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def query_history(
    bs,
    code: str,
    fields: str,
    *,
    start_date: str,
    end_date: str,
    frequency: str,
    adjustflag: str,
    config=None,
):
    """Return the requested field shape; wide-cache only real module sessions.

    Injectable fake sessions keep their existing exact-field protocol. The
    production BaoStock module receives one complete calendar-year response
    shared by valuation, ST labels and delisted bars. A damaged or misrouted
    year never enters the cache.
    """
    args = dict(
        start_date=start_date, end_date=end_date, frequency=frequency, adjustflag=adjustflag
    )
    if (
        config is None
        or getattr(bs, "__name__", None) != "baostock"
        or (frequency != "d" or adjustflag != "3")
    ):
        with source_request(config, "baostock"):
            return check_result(bs.query_history_k_data_plus(code, fields, **args), config=config)

    wanted = fields.split(",")
    if not set(wanted).issubset(_COLUMNS):
        raise ValueError(f"unsupported BaoStock history fields: {fields}")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    rows: list[list[str]] = []
    for year in range(start.year, end.year + 1):
        path = (
            config.meta_root
            / "source_cache"
            / "baostock"
            / (f"{code.replace('.', '-')}-{year}-daily-unadjusted.json")
        )
        with exclusive_lock(path.with_suffix(".lock")):
            year_rows = _fresh(path, year, code)
            if year_rows is not None:
                record_cache_reuse(config, "baostock", "wide_year")
            if year_rows is None:
                with source_request(config, "baostock"):
                    result = check_result(
                        bs.query_history_k_data_plus(
                            code,
                            FIELDS,
                            start_date=f"{year}-01-01",
                            end_date=f"{year}-12-31",
                            frequency="d",
                            adjustflag="3",
                        ),
                        config=config,
                    )
                    if result.error_code != "0":
                        return _RowsResult(
                            [], wanted, result.error_code, getattr(result, "error_msg", "")
                        )
                    returned_fields = getattr(result, "fields", None)
                    if returned_fields is not None and list(returned_fields) != _COLUMNS:
                        return _RowsResult(
                            [], wanted, "invalid_fields", "BaoStock wide response fields changed"
                        )
                    year_rows = []
                    while result.next():
                        year_rows.append(list(result.get_row_data()))
                if year_rows:
                    if not _valid_rows(year_rows, code, year):
                        return _RowsResult(
                            [],
                            wanted,
                            "invalid_identity",
                            "BaoStock wide response identity/shape mismatch",
                        )
                    digest = hashlib.sha256(
                        json.dumps(year_rows, separators=(",", ":")).encode()
                    ).hexdigest()
                    write_json_atomic(
                        path,
                        {
                            "version": _CACHE_VERSION,
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "sha256": digest,
                            "rows": year_rows,
                        },
                    )
            indices = [_COLUMNS.index(name) for name in wanted]
            for row in year_rows:
                if start_date <= row[0] <= end_date:
                    rows.append([row[index] for index in indices])
    return _RowsResult(rows, wanted)
