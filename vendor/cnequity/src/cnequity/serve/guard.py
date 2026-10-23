"""Same-origin checks shared by storage cleanup and the operations page.

Anonymous access is limited to a loopback Host so a page on another name
cannot read the CSRF token through DNS rebinding. Mutations also require the
page's own Origin and a process-scoped token. Remote ingestion is a separate
switch: a bearer token in the query string is enough to browse, and not enough
to start a fetch, unless ``cne serve`` was started with ``--allow-remote-ops``.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from cnequity.i18n import _

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def client_host(request: Request) -> str | None:
    return urlsplit("//" + request.headers.get("host", "")).hostname


def ops_enabled(request: Request) -> bool:
    """Whether this request may start an operation, aside from the CSRF check."""
    app = request.app.state
    remote_setup = app.setup and client_host(request) not in LOOPBACK_HOSTS
    if app.read_only or remote_setup:
        return False
    if client_host(request) not in LOOPBACK_HOSTS and not app.allow_remote_ops:
        return False
    return True


def check_browser(request: Request, *, mutation: bool, scope: str) -> None:
    """Refuse a request that did not come from this process's own page.

    *scope* is ``storage`` or ``ops``. Storage keeps its existing header so the
    current page does not have to change; operations send ``X-CNE-CSRF``.
    Both compare against the one token minted at startup.
    """
    host = client_host(request)
    if not request.app.state.token and host not in LOOPBACK_HOSTS:
        if scope == "storage":
            detail = _(
                "Open storage operations from localhost or 127.0.0.1; remote access needs a token."
            )
        else:
            detail = _(
                "Open dashboard operations from localhost or 127.0.0.1; remote access needs a token."
            )
        raise HTTPException(403, detail)
    if request.app.state.setup and host not in LOOPBACK_HOSTS:
        raise HTTPException(403, _("First-time setup can only be done on this machine."))
    if (
        scope == "ops"
        and mutation
        and host not in LOOPBACK_HOSTS
        and not request.app.state.allow_remote_ops
    ):
        raise HTTPException(
            403,
            _(
                "Remote access cannot start operations from the dashboard by default. Start serve with --allow-remote-ops to enable it."
            ),
        )
    if not mutation:
        return
    origin = request.headers.get("origin", "")
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if origin != expected or request.headers.get("sec-fetch-site") not in (None, "same-origin"):
        raise HTTPException(
            403, _("Submissions are only accepted from the current operations page.")
        )
    header = "x-cne-storage-csrf" if scope == "storage" else "x-cne-csrf"
    supplied = request.headers.get(header, "")
    expected_token = request.app.state.csrf
    if (
        not supplied
        or len(supplied) != len(expected_token)
        or not secrets.compare_digest(supplied, expected_token)
    ):
        raise HTTPException(403, _("The page confirmation token has expired; refresh the page."))
