"""Shared TDX session primitives.

The vendored wire client owns a socket (and, when enabled, a heartbeat
thread), so a client must never be shared between threads.  Earlier versions
used ``TDX_SESSION_LOCK`` around every fetch.  That made the macOS thread
pool merely a serial queue even though every lane could safely own a separate
client.  The only process-local mutable state that needs coordination is the
server discovery/cache; request traffic is deliberately *not* protected by a
global lock.
"""

from __future__ import annotations

import threading

# Protects server discovery/cache publication only.  It is intentionally not
# held while a socket request is in flight.
TDX_DISCOVERY_LOCK = threading.Lock()

# Compatibility name for integrations that imported the old symbol.  Internal
# adapters no longer acquire this lock around network calls.  Keep it as a
# separate lock rather than aliasing ``TDX_DISCOVERY_LOCK`` so an old caller
# cannot accidentally serialize discovery with request traffic.
TDX_SESSION_LOCK = threading.Lock()


def close_quotes_client(client: object) -> None:
    """Close a TDX client so its heartbeat thread dies (else the process
    can't exit — a serial daily run creates one client per fetch)."""
    if client is None:
        return
    inner = getattr(client, "client", None)
    close = getattr(inner, "close", None) or getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
