"""Restructuring capital-reserve conversions from CNINFO issuer notices.

A court-approved restructuring converts capital reserve into new shares that
go mostly to creditors and investors, so the ex-rights price does not follow
``1 + ratio``. The exchange rules let the issuer publish an adjusted
ex-rights reference price instead, and every vendor factor steps by
``previous close / reference price``. Those events are in no dividend feed.

A notice is used only when its own text states the conversion ratio, the
ex-date and one reference price. The caller decides whether they explain the
factor step; nothing here infers a term the notice does not state.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import date, timedelta

import httpx

from cnequity.adapters.cninfo.announcements import post_with_retry
from cnequity.adapters.cninfo.payment_notices import (
    _PDF_ROOT,
    _QUERY_URL,
    _get,
    _pdf_text,
    _source_date,
    _stock_directory,
)
from cnequity.storage.raw_archive import RawPayloadArchive, begin_capture

_DATE = r"(\d{4})年(\d{1,2})月(\d{1,2})日"
# "每10股转增约9.71919股": an issuer may round the per-10 ratio and say so.
_RATIO = re.compile(r"每10股转增约?(\d+(?:\.\d+)?)股")
_EX_DATE = re.compile(r"除权除息日(?:为|：|:)" + _DATE)
_NEXT_SESSION = re.compile(r"股权登记日次一交易日[（(]" + _DATE + r"[)）]")
# "除权除息参考价格为8.14元/股", "调整后除权参考价格为2.49元/股",
# "…除权参考价格的计算公式进行除权调整为3.16元/股". A sentence break before the
# number (a hypothetical "若按…收盘价3.17元/股") never counts.
_REFERENCE = re.compile(r"参考价格?[^。；，,;]{0,30}?为(\d+(?:\.\d+)?)元/股")
# "开盘参考价应当依据…计算公式进行除权调整，调整为3.93元/股": the comma here
# is inside the clause that states the price.
_ADJUSTED_TO = re.compile(r"进行除权调整[，,]调整为(\d+(?:\.\d+)?)元/股")
_TITLE_MARKERS = ("除权", "开盘参考价", "实施")
_SKIPPED_TITLES = ("专项意见", "法律意见", "更正", "补充", "风险")
_LOOKBACK_DAYS = 75


def parse_reorg_notice(text: str) -> dict | None:
    """What one notice states: ``{transfer_ratios, ex_dates, reference_prices}``.

    Any part may be empty; an issuer often splits the ratio, the ex-date and
    the final reference price across two notices. ``None`` when the text is
    not about a conversion at all. Combine with :func:`combine_notices`.
    """
    flat = re.sub(r"\s+", "", text or "")
    if "转增" not in flat:
        return None
    ex_dates = {date(int(y), int(m), int(d)) for y, m, d in _EX_DATE.findall(flat)}
    ex_dates |= {date(int(y), int(m), int(d)) for y, m, d in _NEXT_SESSION.findall(flat)}
    return {
        "transfer_ratios": sorted({float(m.group(1)) / 10.0 for m in _RATIO.finditer(flat)}),
        "ex_dates": sorted(ex_dates),
        "reference_prices": sorted(
            {
                v
                for pattern in (_REFERENCE, _ADJUSTED_TO)
                for m in pattern.finditer(flat)
                if (v := float(m.group(1))) > 0
            }
        ),
    }


def combine_notices(notices: list[dict]) -> dict | None:
    """One event from an issuer's notices, or ``None`` when they disagree.

    The ratio and the ex-date must each be stated once across all notices;
    every stated reference price is kept for the caller to test.
    """
    ratios = {r for n in notices for r in n["transfer_ratios"]}
    ex_dates = {d for n in notices for d in n["ex_dates"]}
    prices = sorted({p for n in notices for p in n["reference_prices"]})
    if len(ratios) != 1 or len(ex_dates) != 1 or not prices:
        return None
    return {
        "transfer_ratio": ratios.pop(),
        "ex_date": ex_dates.pop(),
        "reference_prices": prices,
    }


def _candidates(announcements: Iterable[dict], code: str, step: date) -> list[dict]:
    out = []
    for item in announcements:
        title = str(item.get("announcementTitle") or "")
        day = _source_date(item)
        if (
            "转增" in title
            and any(marker in title for marker in _TITLE_MARKERS)
            # A risk notice about the opening reference price states the
            # final price; other risk notices only warn that one is coming.
            and ("开盘参考价" in title or not any(marker in title for marker in _SKIPPED_TITLES))
            and str(item.get("secCode") or "").zfill(6) == code
            and day is not None
            and step - timedelta(days=_LOOKBACK_DAYS) <= day <= step
        ):
            out.append(item)
    # Latest first: a later notice carries the final reference price.
    return sorted(out, key=lambda item: str(item.get("announcementTime") or ""), reverse=True)


def fetch_reorg_notices(
    config, run_id: str, gaps: list[tuple[str, date]]
) -> tuple[dict[tuple[str, date], list[dict]], list[dict]]:
    """Parsed notices per ``(symbol, step_date)``, and per-gap diagnostics.

    Every query response and PDF is archived under the run before parsing.
    """
    found: dict[tuple[str, date], list[dict]] = {}
    diagnostics: list[dict] = []
    if not gaps:
        return found, diagnostics
    with httpx.Client(timeout=60.0, headers={"User-Agent": "Mozilla/5.0"}) as client:
        directory, _ = _stock_directory(client, config=config)
        for symbol, step in gaps:
            code = symbol.split(".", 1)[0]
            org_id = directory.get(code)
            if not org_id:
                diagnostics.append(
                    {"symbol": symbol, "step_date": str(step), "reason": "no_org_id"}
                )
                continue
            scope = f"cninfo-reorg-notices:{symbol}:{step.isoformat()}"
            archive = RawPayloadArchive(
                config.meta_root,
                enabled=True,
                datasets=["corporate_actions"],
                compression=config.raw_archive_compression,
                max_payload_bytes=config.raw_archive_max_payload_bytes,
                capture_owner=config,
                capture_run_id=run_id,
                capture_source="cninfo",
                capture_scope=scope,
                capture_nonce=begin_capture(
                    config, "corporate_actions", run_id, source="cninfo", request_scope=scope
                ),
            )
            body = {
                "pageNum": 1,
                "pageSize": 30,
                "column": "szse",
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{code},{org_id}",
                "searchkey": "转增",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{step - timedelta(days=_LOOKBACK_DAYS)}~{step}",
            }

            def keep(response, parsed, attempt, *, _body=body, _archive=archive, _scope=scope):
                _archive.archive(
                    "corporate_actions",
                    response.content,
                    source="cninfo",
                    request_params=_body,
                    run_id=run_id,
                    url=_QUERY_URL,
                    payload_format="json" if parsed is not None else "bytes",
                    http_metadata={"wire_exact": True, "attempt": attempt},
                    observation_id=f"{run_id}:{_scope}:query-attempt-{attempt}",
                    request_scope=_scope,
                )

            parsed = post_with_retry(client, _QUERY_URL, data=body, config=config, on_response=keep)
            notices = []
            for item in _candidates(parsed.get("announcements") or [], code, step):
                path = str(item.get("adjunctUrl") or "").lstrip("/")
                if not path:
                    continue
                payload = _get(client, _PDF_ROOT + path, config=config).content
                digest = hashlib.sha256(payload).hexdigest()
                announcement_id = str(item.get("announcementId") or "")
                archive.archive(
                    "corporate_actions",
                    payload,
                    source="cninfo",
                    request_params={"announcement_id": announcement_id, "pdf_sha256": digest},
                    run_id=run_id,
                    url=_PDF_ROOT + path,
                    payload_format="bytes",
                    http_metadata={"wire_exact": True},
                    observation_id=f"{run_id}:{scope}:notice:{announcement_id}",
                    request_scope=scope,
                )
                try:
                    facts = parse_reorg_notice(_pdf_text(payload)[0])
                except Exception as exc:  # noqa: BLE001 - bytes archived above
                    diagnostics.append(
                        {
                            "symbol": symbol,
                            "step_date": str(step),
                            "announcement_id": announcement_id,
                            "reason": f"pdf_parse_failed:{type(exc).__name__}",
                        }
                    )
                    continue
                if facts is not None:
                    notices.append(
                        {
                            **facts,
                            "announcement_id": announcement_id,
                            "title": str(item.get("announcementTitle") or ""),
                            "sha256": digest,
                        }
                    )
            if not notices:
                diagnostics.append(
                    {"symbol": symbol, "step_date": str(step), "reason": "no_parsable_notice"}
                )
            found[(symbol, step)] = notices
    return found, diagnostics
