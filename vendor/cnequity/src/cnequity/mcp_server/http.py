"""MCP over Streamable HTTP: the same JSON-RPC handler behind ``POST /mcp``.

WHY THIS EXISTS. stdio covers every client that can spawn a process — Claude,
Cursor, Codex, Gemini CLI, VS Code. ChatGPT cannot: it only reaches remote
servers over HTTPS, so a local lake gets to it through a tunnel, and a tunnel
needs an HTTP listener at the other end.

WHAT IT SPEAKS. Streamable HTTP lets a server answer each POST with a plain
JSON body instead of opening an SSE stream, and this server never has anything
to push, so it does exactly that. No sessions, no ``GET`` stream: every request
is self-contained, the same as one line on the stdio pipe.

ACCESS. ChatGPT sends no custom headers and offers only OAuth or no auth, so a
bearer header alone would lock it out. The token is therefore also accepted in
the path (``/mcp/<token>``) and the query string. When a token is set, every
request must carry it — whatever the Host header says, because a tunnel may
rewrite Host to localhost. Without a token only plain local requests pass: a
loopback Host, no forwarding headers, and no foreign Origin (the spec's DNS
rebinding rule; a browser page can POST ``text/plain`` without a preflight).
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from cnequity.config import Config
from cnequity.mcp_server.protocol import INVALID_REQUEST, PARSE_ERROR, handle_message

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
# Set by cloudflared, ngrok, nginx and friends. Any of them means the request
# did not originate on this machine, whatever Host was rewritten to.
_FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip", "cf-connecting-ip")


def _hostname(value: str) -> str | None:
    return urlsplit("//" + value).hostname


def _supplied_token(request: Request) -> str | None:
    path_token = request.path_params.get("token")
    if path_token:
        return path_token
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token")


def _denied(request: Request, token: str | None) -> str | None:
    """Why this request may not reach the tools, or None when it may."""
    if token:
        supplied = _supplied_token(request) or ""
        if len(supplied) == len(token) and secrets.compare_digest(supplied, token):
            return None
        return "missing or wrong token"
    if _hostname(request.headers.get("host", "")) not in LOOPBACK_HOSTS:
        return "non-loopback Host without --token"
    if any(request.headers.get(name) for name in _FORWARDING_HEADERS):
        return "proxied request without --token"
    origin = request.headers.get("origin")
    if origin and _hostname(origin.split("://", 1)[-1]) not in LOOPBACK_HOSTS:
        return "foreign Origin without --token"
    return None


def _rpc_error(code: int, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}},
        status_code=status,
    )


def create_app(config: Config, *, token: str | None = None) -> Starlette:
    """Build the HTTP app. *token*, when set, is required on every request."""

    def _process(message: object) -> object | None:
        if isinstance(message, list):
            replies = [r for r in (_process(m) for m in message) if r is not None]
            return replies or None
        if not isinstance(message, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": INVALID_REQUEST, "message": "message must be an object"},
            }
        return handle_message(config, message)

    async def mcp(request: Request) -> Response:
        reason = _denied(request, token)
        if reason is not None:
            return JSONResponse({"error": reason}, status_code=403)
        if request.method != "POST":
            # No server-initiated stream and no sessions to end.
            return Response(status_code=405, headers={"Allow": "POST"})
        try:
            message = json.loads(await request.body())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _rpc_error(PARSE_ERROR, str(exc), 400)
        # Tools read DuckDB and Parquet synchronously; keep the event loop free.
        reply = await run_in_threadpool(_process, message)
        if reply is None:
            # Notifications and responses: accepted, nothing to say back.
            return Response(status_code=202)
        return Response(
            json.dumps(reply, ensure_ascii=False, default=str),
            media_type="application/json",
        )

    methods = ["GET", "POST", "DELETE"]
    return Starlette(
        routes=[
            Route("/mcp", mcp, methods=methods),
            Route("/mcp/{token}", mcp, methods=methods),
        ]
    )
