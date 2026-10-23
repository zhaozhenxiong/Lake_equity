"""Validated reuse of 同花顺 annual K-line files across historical windows."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from cnequity.domain.http_policy import record_cache_reuse
from cnequity.domain.market_time import shanghai_today
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

if TYPE_CHECKING:
    from cnequity.config import Config

logger = logging.getLogger(__name__)

_OLD_YEAR_CACHE_SECONDS = 30 * 86400
_RECENT_YEAR_CACHE_SECONDS = 86400


def cached_year_rows(
    url: str,
    year: int,
    *,
    config: Config | None,
    cache_name: str,
    cache_filename: str,
    fetch: Callable[[], str],
    parse: Callable[[str], list[dict]],
    current_year: int | None = None,
) -> list[dict]:
    """Cache only positive, year-matched responses; never infer a missing year."""
    if config is None:
        return parse(fetch())
    path = config.meta_root / "source_cache" / "ths_year" / cache_filename
    this_year = current_year if current_year is not None else shanghai_today().year
    ttl = _OLD_YEAR_CACHE_SECONDS if year < this_year - 1 else _RECENT_YEAR_CACHE_SECONDS
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["captured_at"])
                text = saved["text"]
                if (
                    saved["url"] == url
                    and 0 <= age.total_seconds() < ttl
                    and isinstance(text, str)
                    and hashlib.sha256(text.encode("utf-8")).hexdigest() == saved["sha256"]
                ):
                    rows = parse(text)
                    if rows and all(row["trade_date"].year == year for row in rows):
                        record_cache_reuse(config, "ths", cache_name)
                        return rows
            except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
                logger.warning("THS cached year %s invalid; refreshing: %s", url, exc)
        text = fetch()
        rows = parse(text)
        if rows and all(row["trade_date"].year == year for row in rows):
            try:
                write_json_atomic(
                    path,
                    {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "url": url,
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "text": text,
                    },
                )
            except OSError as exc:
                logger.warning("THS valid year %s could not be cached: %s", url, exc)
        return rows
