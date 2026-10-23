"""Small helpers for retaining exact EastMoney HTTP observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from cnequity.storage.raw_archive import RawArchiveError, RawPayloadArchive, begin_capture


def configured_archive(
    config: Any,
    dataset: str,
    *,
    run_id: str | None = None,
    request_scope: str | None = None,
    source: str = "eastmoney",
) -> RawPayloadArchive | None:
    """Build the archive selected by *config*, or ``None`` when disabled.

    Adapters call this before parsing a response so critical feeds can hand
    the archive the original response bytes.  Keeping the policy here avoids
    accidentally turning a disabled archive into a required network feature.
    """
    if config is None or not hasattr(config, "meta_root"):
        return None
    should_archive = getattr(config, "should_archive_raw", None)
    if callable(should_archive) and not should_archive(dataset):
        return None
    enabled = bool(getattr(config, "raw_archive_enabled", True))
    if not enabled:
        return None
    scope = str(request_scope or f"dataset:{dataset}")
    nonce = begin_capture(
        config,
        dataset,
        run_id,
        source=source,
        request_scope=scope,
    )
    return RawPayloadArchive(
        config.meta_root,
        enabled=True,
        datasets=[dataset],
        compression=getattr(config, "raw_archive_compression", "gzip"),
        max_payload_bytes=getattr(config, "raw_archive_max_payload_bytes", None),
        capture_owner=config,
        capture_run_id=run_id,
        capture_source=source,
        capture_scope=scope,
        capture_nonce=nonce,
    )


def exact_response_bytes(response: Any) -> bytes:
    """Return transport bytes without manufacturing a JSON serialization."""
    wire = getattr(response, "content", None)
    if isinstance(wire, bytearray):
        wire = bytes(wire)
    elif isinstance(wire, memoryview):
        wire = wire.tobytes()
    if not isinstance(wire, bytes):
        raise RawArchiveError(
            "EastMoney response has no exact wire bytes; refusing to create a replayable archive"
        )
    return wire


def archive_response(
    archive: RawPayloadArchive | None,
    dataset: str,
    response: Any,
    *,
    request_params: Mapping[str, Any] | None = None,
    run_id: str | None = None,
    url: str | None = None,
    observation_id: str | None = None,
    pagination: Mapping[str, Any] | None = None,
    request_scope: str | None = None,
    source: str = "eastmoney",
    captured_at: datetime | None = None,
) -> None:
    """Persist one exact response, failing closed when bytes are unavailable.

    *captured_at* is for replaying bytes captured earlier (the shared push2
    snapshot); a live response leaves it unset and is stamped now.
    """
    if archive is None or not archive.enabled:
        return
    wire = exact_response_bytes(response)
    if observation_id is None:
        # Every published receipt must name this concrete request observation.
        # Include the request shape and response digest so a changed retry
        # cannot collide with the first page sidecar.
        params_blob = json.dumps(
            dict(request_params or {}), ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
        request_digest = hashlib.sha256(params_blob).hexdigest()[:16]
        wire_digest = hashlib.sha256(wire).hexdigest()[:16]
        observation_id = (
            f"{run_id or 'anonymous'}:{source}:{request_scope or 'scope-unknown'}:"
            f"request={request_digest}:payload={wire_digest}"
        )
    headers = getattr(response, "headers", {})
    content_type = None
    if isinstance(headers, Mapping):
        content_type = headers.get("content-type") or headers.get("Content-Type")
    http_metadata: dict[str, Any] = {
        "wire_exact": True,
        "json_parsed": True,
        "response_envelope": "json",
    }
    if content_type:
        http_metadata["content_type"] = str(content_type)
    archive.archive(
        dataset,
        wire,
        source=source,
        request_params=request_params,
        run_id=run_id,
        url=url or getattr(response, "url", None),
        response_status=getattr(response, "status_code", None),
        payload_format="bytes",
        http_metadata=http_metadata,
        pagination=pagination,
        observation_id=observation_id,
        request_scope=request_scope,
        captured_at=captured_at,
    )
