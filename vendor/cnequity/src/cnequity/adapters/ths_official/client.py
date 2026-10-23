"""同花顺 official API client — envelope, error taxonomy, and pacing.

Every quirk this module encodes was measured against the live service on
2026-09-08/09, because the published contract is wrong about several of them.
Downstream adapters should not re-discover any of this.

**HTTP is always 200.** The outcome lives in the envelope
``{code, message, request_id, data}``. A non-200 is therefore not a business
error but a transport or infrastructure failure, and is retried as one.

**``2001`` never appears.** The docs reserve ``2001`` for a missing or invalid
key and ``2003`` for "capability not granted", but the live service answers
``2003`` for all three: no key ("Missing X-api-key"), a junk key ("Invalid or
revoked API key"), and — per the docs — an ungranted capability. The code alone
cannot separate them, so :class:`ThsOfficialAuthError` carries the message and
``granted_elsewhere`` distinguishes "this key is broken" from "this key does not
reach this endpoint", which callers must treat differently: the first is fatal
for the whole run, the second only disables one capability.

**``3002`` is an empty result, not a failure.** ``No adjustment events for
thscode=...`` means the security has none, and an adapter that raises on it
would fail on every symbol that never paid a dividend.

**Presigned URLs must never be cached.** ``/api/dump/**`` returns a
``presigned_url`` valid for ``expires_in_seconds`` (300 observed). Replaying a
stored envelope yields a dead link and a misleading 403.

**The dump path has two spellings.** ``/dump/**`` is the browser cookie entry
point and is served by the docs site's SPA, so a client that uses it silently
receives HTML instead of JSON. API clients must use ``/api/dump/**``.
"""

from __future__ import annotations

import logging
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any

import httpx

from cnequity.domain.http_policy import record_http_response
from cnequity.domain.rate_limit import source_request

if TYPE_CHECKING:
    from cnequity.config import Config

logger = logging.getLogger(__name__)

__all__ = [
    "BASE_URL",
    "SOURCE",
    "ThsOfficialAuthError",
    "ThsOfficialClient",
    "ThsOfficialError",
    "ThsOfficialParameterError",
    "ThsOfficialRateLimited",
]

BASE_URL = "https://fuyao.aicubes.cn"
SOURCE = "ths_official"

CODE_OK = 0
# Both spellings of "the key did not work". See the module docstring: the live
# service uses 2003 for all of them and 2001 was never observed.
AUTH_CODES = frozenset({2001, 2003})
# The security exists but has no rows for this query.
CODE_NO_DATA = 3002
CODE_RATE_LIMITED = 4001
PARAMETER_CODES = frozenset({1001, 1002, 1003, 1004})

# Substrings that mark a 2003 as "the key itself is bad" rather than "this key
# does not reach this capability". Matched case-insensitively.
_BAD_KEY_MARKERS = ("missing x-api-key", "invalid or revoked")

_MAX_ATTEMPTS = 4


class ThsOfficialError(RuntimeError):
    """Any 同花顺 official API failure that is not an empty result."""

    def __init__(self, message: str, *, code: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class ThsOfficialAuthError(ThsOfficialError):
    """Code 2001/2003.

    ``granted_elsewhere`` is True when the key parsed but this capability was
    refused, which disables one endpoint rather than the whole source.
    """

    def __init__(self, message: str, *, code: int, request_id: str | None, granted_elsewhere: bool):
        super().__init__(message, code=code, request_id=request_id)
        self.granted_elsewhere = granted_elsewhere


class ThsOfficialParameterError(ThsOfficialError):
    """Codes 1001-1004 — a request this client built wrong. Never retried."""


class ThsOfficialRateLimited(ThsOfficialError):
    """Code 4001, still returned after the client's own backoff."""


def _classify_auth(message: str) -> bool:
    """True when the key itself is bad, rather than merely out of scope."""
    lowered = message.lower()
    return any(marker in lowered for marker in _BAD_KEY_MARKERS)


class ThsOfficialClient:
    """One pooled HTTP session, paced by the shared ``ths_official`` budget.

    The key is held only in the session's default headers. It is never logged,
    never placed in a URL, and the raw archive's redaction already covers the
    ``api[_-]?key`` spelling of the header it travels in.
    """

    def __init__(
        self,
        api_key: str,
        *,
        config: Config | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
        archive: Any = None,
        archive_dataset: str | None = None,
        run_id: str | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ThsOfficialError("ths_official requires an API key")
        self._config = config
        # Curated rows must be traceable to the exact bytes that produced them,
        # so every 200 is archived before it is parsed. `write_fetched` refuses
        # to stage a dataset configured for archiving without that receipt.
        self._archive = archive
        self._archive_dataset = archive_dataset
        self._run_id = run_id
        resolved_timeout = timeout if timeout is not None else _configured_timeout(config)
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(resolved_timeout, connect=15.0),
            headers={"X-api-key": api_key.strip()},
            # Every attempt must pass shared admission. Transport-level
            # retries would silently multiply the outer retry budget.
            transport=transport if transport is not None else httpx.HTTPTransport(retries=0),
        )

    def __enter__(self) -> ThsOfficialClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, **params: Any) -> dict[str, Any] | None:
        """Return the envelope's ``data``, or ``None`` for an empty result.

        ``None`` means code 3002 — the query was valid and the security has no
        rows. Callers distinguish that from ``{}``/empty lists, which mean the
        endpoint answered with a shape it considers populated.
        """
        payload = self._request(path, params)
        code = payload.get("code")
        message = str(payload.get("message") or "")
        request_id = payload.get("request_id")

        if code == CODE_OK:
            data = payload.get("data")
            return data if isinstance(data, dict) else {"item": data}
        if code == CODE_NO_DATA:
            logger.debug("ths_official: no rows for %s %s (%s)", path, params, message)
            return None
        if code in AUTH_CODES:
            raise ThsOfficialAuthError(
                f"{path}: {message}",
                code=int(code),
                request_id=request_id,
                granted_elsewhere=not _classify_auth(message),
            )
        if code in PARAMETER_CODES:
            raise ThsOfficialParameterError(
                f"{path}: {message}", code=int(code), request_id=request_id
            )
        if code == CODE_RATE_LIMITED:
            raise ThsOfficialRateLimited(
                f"{path}: {message}", code=int(code), request_id=request_id
            )
        raise ThsOfficialError(
            f"{path}: unexpected code {code} ({message})",
            code=code if isinstance(code, int) else None,
            request_id=request_id,
        )

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        last_transport_error: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with source_request(self._config, SOURCE):
                    response = self._http.get(path, params=_clean(params))
                    record_http_response(self._config, SOURCE, response)
            except httpx.HTTPError as exc:
                last_transport_error = exc
                if attempt == _MAX_ATTEMPTS - 1:
                    break
                time.sleep(2**attempt)
                continue

            if response.status_code != 200:
                # The envelope invariant says business errors arrive as 200, so a
                # non-200 is infrastructure. 4xx other than 408/429 will not fix
                # itself, and an HTML body means the caller used a docs-site path.
                status = response.status_code
                if status == 429:
                    raise ThsOfficialRateLimited(f"{path}: HTTP 429", code=429, request_id=None)
                if status == 408 or 500 <= status <= 599:
                    last_transport_error = httpx.HTTPStatusError(
                        f"HTTP {status}", request=response.request, response=response
                    )
                    if attempt == _MAX_ATTEMPTS - 1:
                        break
                    time.sleep(2**attempt)
                    continue
                raise ThsOfficialError(f"{path}: HTTP {status}")

            self._archive_response(path, params, response)
            try:
                payload = response.json()
            except ValueError as exc:
                if "text/html" in response.headers.get("content-type", ""):
                    raise ThsOfficialError(
                        f"{path}: server returned HTML, not an API response — "
                        "a /dump/** path was used where /api/dump/** was meant"
                    ) from exc
                raise ThsOfficialError(f"{path}: response is not JSON") from exc

            if not isinstance(payload, dict):
                raise ThsOfficialError(f"{path}: response is not an object")
            if payload.get("code") == CODE_RATE_LIMITED:
                # This same HTTP 200 response was already metered above; the
                # synthetic 429 is solely for the shared refusal policy.
                record_http_response(self._config, SOURCE, response, status=429, meter=False)
            return payload

        raise ThsOfficialError(f"{path}: transport failed — {last_transport_error}")

    def _archive_response(
        self, path: str, params: dict[str, Any], response: httpx.Response
    ) -> None:
        if self._archive is None or not getattr(self._archive, "enabled", False):
            return
        if not self._archive_dataset:
            return
        scope = getattr(self._archive, "capture_scope", None)
        # One logical observation per (run, endpoint, parameter set): the receipt
        # check rejects a record without one, and two endpoints can legitimately
        # return byte-identical payloads for the same symbol.
        signature = ",".join(f"{k}={v}" for k, v in sorted(_clean(params).items()))
        self._archive.archive(
            self._archive_dataset,
            response.content,
            source=SOURCE,
            request_params=_clean(params),
            observation_id=f"{self._run_id or 'anonymous'}:{path}:{signature}:scope={scope}",
            run_id=self._run_id,
            url=str(response.request.url),
            response_status=response.status_code,
            payload_format="bytes",
            http_metadata={"wire_exact": True, "protocol": "http"},
            # The receipt check compares the record's scope against the caller's,
            # so it has to be stamped explicitly rather than left to default.
            request_scope=scope,
        )

    def download_url(self, kind: str) -> tuple[str, int]:
        """Return ``(presigned_url, expires_in_seconds)`` for a market dump.

        ``kind`` is one of ``daily-k``, ``daily-k-10d``, ``adjustment-factors``.
        The URL is short-lived — 300 seconds observed — so fetch it immediately
        before the download and never persist it. Nothing here is cached.
        """
        data = self.get(f"/api/dump/market-dumps/{kind}/download-url")
        if not data or not data.get("presigned_url"):
            raise ThsOfficialError(f"market dump {kind}: response carried no presigned_url")
        return str(data["presigned_url"]), int(data.get("expires_in_seconds") or 0)


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _configured_timeout(config: Config | None) -> float:
    return float(getattr(config, "ths_official_timeout_sec", 30.0) or 30.0)


def client_from_config(config: Config) -> ThsOfficialClient | None:
    """Build a client, or ``None`` when this lake has no key.

    The source is optional by design: a lake without a key keeps its existing
    sources, so every caller treats ``None`` as "skip this check" rather than as
    an error.
    """
    key = getattr(config, "ths_official_api_key", None)
    if not key:
        return None
    if not config.sources.get(SOURCE, False):
        return None
    return ThsOfficialClient(key, config=config)
