"""国家统计局 — 制造业 PMI from the monthly 采购经理指数 release.

The NBS query API (``data.stats.gov.cn/easyquery.htm``) would be the obvious
source and is deliberately *not* used: it answers 403 with a WAF ``UrlACL`` body
from non-mainland egress, while the site root and the release pages under
``www.stats.gov.cn`` answer normally. Building the check on the blocked path
would leave it silently dead for exactly the users least able to diagnose it —
the same trap the ths adapter documents for EastMoney ``push2his``.

So this reads the release instead. The number lives in one sentence the NBS has
phrased the same way for years::

    7月份，制造业采购经理指数（PMI）为49.2%，比上月下降1.1个百分点

which is enough for what this is for: confirming that the value already in
curated matches what the statistics bureau published. It reads the newest
release only — the point is to catch a vendor drifting from the publisher, and a
vendor that is right this month having been wrong two years ago is not the
failure being guarded against.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from collections.abc import Callable
from datetime import date, datetime, timezone

from cnequity.domain.http_policy import record_cache_reuse, record_http_response
from cnequity.domain.rate_limit import source_request
from cnequity.file_lock import exclusive_lock
from cnequity.storage.atomic import write_json_atomic

logger = logging.getLogger(__name__)

BASE = "https://www.stats.gov.cn"
RELEASE_INDEX = f"{BASE}/sj/zxfb/"
_TIMEOUT_SECONDS = 45.0
_INDEX_CACHE_SECONDS = 3600
_RELEASE_CACHE_SECONDS = 86400
_CACHE_PARSER_VERSION = 1

_RELEASE_LINK_RE = re.compile(
    r"""href=["'](\.?/?[^"']*?/t\d{8}_\d+\.html)["'][^>]*>\s*([^<]{6,80}采购经理指数[^<]{0,40})</a>"""
)
_TITLE_MONTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")

# `非制造业采购经理指数` contains the manufacturing phrase as a substring, so the
# lookbehind is what keeps this from reading the services index instead.
_PMI_RE = re.compile(r"(?<!非)制造业采购经理指数（PMI）为([\d.]+)%")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S)
_SPACE_RE = re.compile(r"[\s　\xa0]+")


def _get(url: str, *, config=None) -> str:
    from curl_cffi import requests as cr

    # The release index and the release page are two separate wire requests.
    # Keep the lease around exactly one curl call so a slow page consumes one
    # NBS slot, while another worker can proceed once that call returns.
    with source_request(config, "nbs"):
        resp = cr.get(url, impersonate="chrome", timeout=_TIMEOUT_SECONDS)
        record_http_response(config, "nbs", resp)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return resp.text


def _cached_html(
    url: str,
    *,
    config,
    ttl_seconds: int,
    cache_name: str,
    valid: Callable[[str], bool],
) -> str:
    """Reuse one parsed publisher page; invalid or stale pages are refetched."""
    if config is None:
        return _get(url)
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    path = config.meta_root / "source_cache" / "nbs" / f"{cache_name}-{key}.json"
    with exclusive_lock(path.with_suffix(".lock")):
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                age = datetime.now(timezone.utc) - datetime.fromisoformat(saved["captured_at"])
                html = saved["html"]
                if (
                    saved["url"] == url
                    and saved["parser_version"] == _CACHE_PARSER_VERSION
                    and 0 <= age.total_seconds() < ttl_seconds
                    and isinstance(html, str)
                    and hashlib.sha256(html.encode("utf-8")).hexdigest() == saved["sha256"]
                    and valid(html)
                ):
                    record_cache_reuse(config, "nbs", cache_name)
                    return html
            except (OSError, KeyError, TypeError, ValueError) as exc:
                logger.warning("NBS cached %s invalid; refreshing: %s", cache_name, exc)
        html = _get(url, config=config)
        if valid(html):
            try:
                write_json_atomic(
                    path,
                    {
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "url": url,
                        "parser_version": _CACHE_PARSER_VERSION,
                        "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                        "html": html,
                    },
                )
            except OSError as exc:
                logger.warning("NBS valid %s could not be cached: %s", cache_name, exc)
        return html


def _plain_text(html: str) -> str:
    """Tag-stripped text with *all* whitespace removed.

    Markup splits the sentence into fragments (``制造业采购经理指数（ PMI ）为
    49.2%``), so collapsing spaces is not enough — they have to go entirely for
    the pattern to match.
    """
    return _SPACE_RE.sub("", _TAG_RE.sub(" ", _SCRIPT_RE.sub("", html)))


def find_latest_release(index_html: str | None = None, *, config=None) -> tuple[date, str] | None:
    """Newest 采购经理指数 release as ``(observation month end, absolute url)``."""
    html = (
        index_html
        if index_html is not None
        else _cached_html(
            RELEASE_INDEX,
            config=config,
            ttl_seconds=_INDEX_CACHE_SECONDS,
            cache_name="pmi_index",
            valid=lambda body: find_latest_release(body) is not None,
        )
    )
    best: tuple[date, str] | None = None
    for href, title in _RELEASE_LINK_RE.findall(html):
        match = _TITLE_MONTH_RE.search(_SPACE_RE.sub("", title))
        if not match:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            continue
        import calendar

        obs = date(year, month, calendar.monthrange(year, month)[1])
        url = href if href.startswith("http") else f"{RELEASE_INDEX}{href.lstrip('./')}"
        if best is None or obs > best[0]:
            best = (obs, url)
    return best


def parse_pmi(release_html: str) -> float | None:
    match = _PMI_RE.search(_plain_text(release_html))
    if not match:
        return None
    value = float(match.group(1))
    return value if math.isfinite(value) and 0.0 <= value <= 100.0 else None


def fetch_latest_pmi(*, config=None) -> dict | None:
    """``{"obs_date", "value", "url"}`` for the newest published month, or None.

    Never raises: this backs a cross-check, and a statistics-bureau page being
    unreachable is not a reason to fail a data run.
    """
    try:
        latest = find_latest_release(config=config)
        if latest is None:
            logger.warning("NBS: no 采购经理指数 release found on %s", RELEASE_INDEX)
            return None
        obs_date, url = latest
        value = parse_pmi(
            _cached_html(
                url,
                config=config,
                ttl_seconds=_RELEASE_CACHE_SECONDS,
                cache_name="pmi_release",
                valid=lambda body: parse_pmi(body) is not None,
            )
        )
    except Exception as exc:
        logger.warning("NBS PMI release unavailable: %s", exc)
        return None

    if value is None:
        logger.warning("NBS PMI release %s parsed to no value; wording may have changed", url)
        return None
    return {"obs_date": obs_date, "value": value, "url": url}
