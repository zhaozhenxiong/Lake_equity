"""Shared discovery for 调查统计司 statistical tables.

The PBOC publishes each statistic as an Excel attachment under a per-year
section. Numeric section ids change every year, so every hop keys on a stable
Chinese label instead:

1. the statistics index lists ``<YYYY>年统计数据`` → that year's section
2. the year section links a topic (``社会融资规模`` / ``货币统计概览``)
3. the topic page links the workbook next to its table title

Both 社融 and 货币供应量 follow this shape, so the walk lives here and each
adapter supplies only its two labels.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import io
import json
import logging
import math
import re
from datetime import date, datetime, timezone

from cnequity.domain.http_policy import record_cache_reuse, record_http_response
from cnequity.domain.rate_limit import source_request
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

logger = logging.getLogger(__name__)

BASE = "https://www.pbc.gov.cn"
STATS_INDEX = f"{BASE}/diaochatongjisi/116219/116319/index.html"
TIMEOUT_SECONDS = 60.0


class PBOCSeriesError(RuntimeError):
    """The publisher did not provide a complete requested series."""


# The site mixes single- and double-quoted attributes within one page, so every
# href pattern here accepts either.
_YEAR_SECTION_RE = re.compile(r"""href=["']([^"']+)["'][^>]*>\s*(\d{4})年统计数据\s*</a>""")
_WORKBOOK_RE = re.compile(r"""href=["']([^"']+\.xlsx?)["']""")
# The workbook link sits within the same layout block as its table title; bound
# the search so a later table's attachment cannot be picked up by mistake.
_LABEL_WINDOW = 1200

# Month cells arrive as "2026.01" (str) in the legacy .xls and as the float
# 2026.01 in .xlsx — where October becomes 2026.1, which formats back to
# "2026.10" only at two decimals. Anything else is a note or a blank row.
_MONTH_RE = re.compile(r"^(\d{4})\.(\d{1,2})$")


def client():
    # Chrome impersonation: the site is served behind a WAF that drops a plain
    # httpx handshake on the attachment paths.
    from curl_cffi import requests as cr

    return cr


def get_text(url: str, *, config=None, source: str = "pboc") -> str:
    with source_request(config, source):
        resp = client().get(url, impersonate="chrome", timeout=TIMEOUT_SECONDS)
        record_http_response(config, source, resp)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def get_bytes(url: str, *, config=None, source: str = "pboc") -> bytes:
    with source_request(config, source):
        resp = client().get(url, impersonate="chrome", timeout=TIMEOUT_SECONDS)
        record_http_response(config, source, resp)
    resp.raise_for_status()
    return resp.content


def absolute(href: str) -> str:
    return href if href.startswith("http") else f"{BASE}{href}"


def year_sections(
    index_html: str | None = None, *, config=None, source: str = "pboc"
) -> dict[int, str]:
    """Map year → that year's statistics section URL."""
    if index_html is None:
        if config is None:
            html = get_text(STATS_INDEX, config=config, source=source)
        else:
            path = config.meta_root / "source_cache" / "pboc" / "index.json"
            with exclusive_lock(path.with_suffix(".lock")):
                cached = _read_fresh(path, 3600)
                if cached is None:
                    html = get_text(STATS_INDEX, config=config, source=source)
                    write_json_atomic(path, {"captured_at": _now(), "payload": html})
                else:
                    record_cache_reuse(config, source, "year_index")
                    html = cached
    else:
        html = index_html
    return {int(year): absolute(href) for href, year in _YEAR_SECTION_RE.findall(html)}


def workbook_url(
    year_section_url: str,
    *,
    topic: str,
    table_label: str,
    config=None,
    source: str = "pboc",
) -> str | None:
    """Walk a year section → topic page → the workbook beside ``table_label``."""
    topic_re = re.compile(r"""href=["']([^"']+)["'][^>]*>\s*""" + re.escape(topic) + r"""\s*</a>""")
    section_html = get_text(year_section_url, config=config, source=source)
    match = topic_re.search(section_html)
    if not match:
        return None
    topic_html = get_text(absolute(match.group(1)), config=config, source=source)
    label_at = topic_html.find(table_label)
    if label_at < 0:
        return None
    link = _WORKBOOK_RE.search(topic_html[label_at : label_at + _LABEL_WINDOW])
    return absolute(link.group(1)) if link else None


def month_end(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, float):
        # 2026.1 is October, not January — two decimals disambiguate.
        text = f"{value:.2f}"
    else:
        text = str(value).strip()
    match = _MONTH_RE.match(text)
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def parse_month_column(content: bytes, value_column: int, *, unit: str = "亿元") -> list[dict]:
    """Rows from a workbook as ``[{"obs_date", "value"}, ...]``.

    Column 0 is the month; ``value_column`` selects the series. Header, note and
    not-yet-published rows fail the month parse and drop out.

    Some sheets stack more than one table — the 2019 社融 workbook carries
    ``表1 …增量数据`` in 亿元 followed by ``表2 …增量占比数据`` in %, and both have
    a month column, so reading every month-shaped row pulled a column of literal
    ``100``s into the series. Each table declares its own ``单位`` line, so
    collection follows that declaration and stops as soon as the unit changes.
    """
    import pandas as pd

    pdf = pd.read_excel(io.BytesIO(content), header=None)
    rows: list[dict] = []
    in_wanted_table = False
    for record in pdf.itertuples(index=False):
        if len(record) <= value_column:
            continue
        header = "" if record[0] is None or pd.isna(record[0]) else str(record[0])
        if "单位" in header:
            in_wanted_table = unit in header
            continue
        if not in_wanted_table:
            continue
        obs = month_end(record[0])
        if obs is None:
            continue
        raw = record[value_column]
        if raw is None or pd.isna(raw):
            # A month the PBOC has not published yet — the row exists, blank.
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        rows.append({"obs_date": obs, "value": value})
    return rows


def fetch_yearly_series(
    *,
    topic: str,
    table_label: str,
    value_column: int,
    unit: str = "亿元",
    config=None,
    source_name: str = "pboc",
    start_year: int = 2015,
    strict: bool = False,
    failures_by_year: dict[int, str] | None = None,
    completed_years: set[int] | None = None,
) -> list[dict]:
    """Walk every year ≥ ``start_year``, newest first, and merge the series.

    Newest year first, and the first reading of a month wins. Workbooks overlap:
    the 2019 社融 file restates 2017-2019 under the 完善后 caliber (2017-01 =
    37720) while the 2017 file still carries the original 36970.49. The later
    publication is the current official series, so descending order is
    load-bearing, not incidental.

    By default, degrades to whatever it managed to read: a year that cannot be
    reached is logged and skipped, so a later run fills the gap. ``strict`` is
    for canonical ingestion paths: any listed year's failure, empty parse, or
    unavailable index raises instead of allowing a partial series to advance
    the dataset watermark.
    """
    try:
        if config is None:
            sections = year_sections()
        else:
            sections = year_sections(config=config, source=source_name)
    except Exception as exc:
        logger.warning("PBOC statistics index unavailable: %s", exc)
        if failures_by_year is not None:
            failures_by_year[0] = f"statistics index unavailable: {exc}"
        if strict:
            raise PBOCSeriesError("PBOC statistics index unavailable") from exc
        return []

    seen: set[date] = set()
    rows: list[dict] = []
    failures: dict[int, str] = {}
    for year in sorted((y for y in sections if y >= start_year), reverse=True):
        try:
            year_rows = _year_rows(
                year,
                sections[year],
                topic=topic,
                table_label=table_label,
                value_column=value_column,
                unit=unit,
                config=config,
                source=source_name,
            )
        except Exception as exc:
            failures[year] = str(exc)
            logger.warning("PBOC %s %s skipped: %s", year, table_label, exc)
            continue
        if not year_rows:
            failures[year] = "parsed to no rows; layout may have changed"
            logger.warning(
                "PBOC %s %s parsed to no rows; layout may have changed", year, table_label
            )
            continue
        if completed_years is not None:
            completed_years.add(year)
        for row in year_rows:
            if row["obs_date"] in seen:
                continue
            seen.add(row["obs_date"])
            rows.append(row)

    if failures_by_year is not None:
        failures_by_year.update(failures)
    if strict and failures:
        details = "; ".join(f"{year}: {reason}" for year, reason in sorted(failures.items()))
        raise PBOCSeriesError(f"PBOC {table_label} series is incomplete: {details}")
    if not rows:
        logger.warning("PBOC %s returned no usable rows", table_label)
        if failures_by_year is not None and not failures:
            failures_by_year[0] = f"PBOC {table_label} returned no usable rows"
        if strict:
            raise PBOCSeriesError(f"PBOC {table_label} returned no usable rows")
    return rows


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_fresh(path, ttl_seconds: int):
    if not path.exists():
        return None
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(item["captured_at"])
        if 0 <= age.total_seconds() < ttl_seconds:
            return item["payload"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _year_rows(
    year: int,
    section: str,
    *,
    topic: str,
    table_label: str,
    value_column: int,
    unit: str,
    config,
    source: str,
) -> list[dict]:
    key = hashlib.sha256(
        f"{year}|{section}|{topic}|{table_label}|{value_column}|{unit}".encode()
    ).hexdigest()[:24]
    path = (
        config.meta_root / "source_cache" / "pboc" / f"year-{key}.json"
        if config is not None
        else None
    )

    def fetch() -> tuple[list[dict], bytes, str]:
        kwargs = {} if config is None else {"config": config, "source": source}
        url = workbook_url(section, topic=topic, table_label=table_label, **kwargs)
        if url is None:
            raise PBOCSeriesError(f"no {table_label} workbook link found")
        content = get_bytes(url, **kwargs)
        parsed = parse_month_column(content, value_column, unit=unit)
        if not parsed:
            raise PBOCSeriesError("parsed to no rows; layout may have changed")
        return parsed, content, url

    if path is None:
        return fetch()[0]
    # The newest two calendar years may be revised. Historical workbooks are
    # revisited monthly, but are never silently treated as newly observed.
    ttl = 86400 if year >= date.today().year - 1 else 30 * 86400
    with exclusive_lock(path.with_suffix(".lock")):
        cached = _read_fresh(path, ttl)
        if isinstance(cached, dict) and cached.get("parser_version") == 1:
            try:
                raw = base64.b64decode(cached["content_b64"], validate=True)
                if hashlib.sha256(raw).hexdigest() == cached["sha256"]:
                    # Parsed rows are a convenience index, not trusted source
                    # bytes. Reparse the verified workbook so a damaged JSON
                    # cache cannot silently manufacture observations.
                    parsed = parse_month_column(raw, value_column, unit=unit)
                    if parsed:
                        record_cache_reuse(config, source, "year_workbook")
                        return parsed
            except (ValueError, KeyError, TypeError):
                pass
        parsed, content, url = fetch()
        write_json_atomic(
            path,
            {
                "captured_at": _now(),
                "payload": {
                    "parser_version": 1,
                    "url": url,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_b64": base64.b64encode(content).decode("ascii"),
                    "rows": [
                        {"obs_date": row["obs_date"].isoformat(), "value": row["value"]}
                        for row in parsed
                    ],
                },
            },
        )
        return parsed
