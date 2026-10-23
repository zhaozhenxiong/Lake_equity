"""Confirmed IPO calendar listings; subscription dates are never listing dates."""

import json
import re
from datetime import date, datetime, timedelta, timezone

import httpx

from cnequity.domain.http_policy import record_http_response
from cnequity.domain.rate_limit import source_request
from cnequity.domain.symbols import is_all_a_symbol
from cnequity.storage.atomic import write_json_atomic
from cnequity.storage.raw_archive import RawPayloadArchive

IPO_CALENDAR_URL = "https://www.cninfo.com.cn/new/ipoSpecialArea/queryIPOEvent"


def fetch_listing_calendar(config, day: date, *, client=None) -> dict[str, date]:
    """Replay recent evidence or archive a paced, explicitly dated calendar read."""
    if not config.sources.get("cninfo", False):
        raise ValueError("cninfo source disabled")
    params = {"queryDate": str(day)}
    path = config.meta_root / "source_cache" / "stock_listing_dates" / f"{day}.json"
    archive = RawPayloadArchive(config.meta_root, enabled=True, datasets=["instruments"])
    try:
        record = json.loads(path.read_text())
        if (
            record.get("url") != IPO_CALENDAR_URL
            or record.get("request_params") != params
            or record.get("source") != "cninfo"
            or record.get("dataset") != "instruments"
        ):
            raise ValueError("cached IPO calendar identity differs")
        captured = datetime.fromisoformat(record["captured_at"])
        age = datetime.now(timezone.utc) - captured
        if timedelta(0) <= age < timedelta(hours=6):
            return parse_listing_calendar(archive.read(record), day)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    owned = client is None
    if owned:
        client = httpx.Client(timeout=45, follow_redirects=True)
    try:
        with source_request(config, "cninfo"):
            response = client.get(IPO_CALENDAR_URL, params=params)
            record_http_response(config, "cninfo", response)
        response.raise_for_status()
        confirmed = parse_listing_calendar(response.content, day)
        record = archive.archive(
            "instruments",
            response.content,
            source="cninfo",
            run_id=f"daily-stock-listings-{day}",
            url=IPO_CALENDAR_URL,
            request_params=params,
            payload_format="json",
        ).to_dict()
        write_json_atomic(path, record)
        return confirmed
    finally:
        if owned:
            client.close()


def parse_listing_calendar(payload: bytes, day: date) -> dict[str, date]:
    response = json.loads(payload)
    if not isinstance(response, dict) or not isinstance(response.get("listed"), list):
        raise ValueError("CNINFO IPO calendar lacks the listed event group")
    dates = {}
    for item in response["listed"]:
        if not isinstance(item, dict):
            raise ValueError("CNINFO listed event is not an object")
        code = str(item.get("seccode", ""))
        listed = date.fromisoformat(str(item.get("listedTime", "")))
        if not re.fullmatch(r"\d{6}", code) or listed != day:
            raise ValueError("CNINFO listed event code/date does not match request")
        plate = item.get("plate")
        exchange = {"012046": "BJ", "012015": "SZ", "012029": "SH"}.get(plate)
        if exchange is None:
            if code.startswith(("000", "001", "002", "003", "300", "301")):
                exchange = "SZ"
            elif code.startswith(("600", "601", "603", "605", "688", "689")):
                exchange = "SH"
            else:
                raise ValueError("CNINFO listed event is outside the supported stock universe")
        if not is_all_a_symbol(code, exchange):
            raise ValueError("CNINFO listed event code and exchange conflict")
        symbol = f"{code}.{exchange}"
        if symbol in dates:
            raise ValueError(f"CNINFO IPO calendar duplicates {symbol}")
        dates[symbol] = listed
    return dates
