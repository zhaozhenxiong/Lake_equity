"""The ``hexin-v`` token 同花顺's data pages (data.10jqka.com.cn) demand.

Without it every ``/funds/...`` page answers HTTP 401; with it, 200 (measured
2026-09-26). The token is computed by 同花顺's own front-end script; AKShare
extracted a standalone copy, vendored here as ``js/hexin_v.js`` (MIT), whose
``v()`` returns the token.

That script is obfuscated third-party code, so it runs in a subprocess under
Deno with **no permission flags**: no file, network, environment or subprocess
access — it can compute a string and print it, nothing else. Node has no
equivalent default sandbox, so it is not used. Deno is found through
``[sources.ths] js_runtime``, then ``PATH``, then the usual install locations
(launchd jobs run with a minimal ``PATH``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from functools import lru_cache
from importlib import resources
from pathlib import Path

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,200}$")
_CANDIDATES = (
    "/opt/homebrew/bin/deno",
    "/usr/local/bin/deno",
    str(Path.home() / ".deno" / "bin" / "deno"),
)


class HexinTokenUnavailable(RuntimeError):
    """No sandboxed JS runtime, or the script did not produce a token."""


def find_deno(config=None) -> str:
    configured = getattr(config, "ths_js_runtime", None)
    if configured:
        if Path(configured).is_file() and os.access(configured, os.X_OK):
            return str(configured)
        raise HexinTokenUnavailable(f"[sources.ths] js_runtime is not executable: {configured}")
    found = shutil.which("deno")
    if found:
        return found
    for candidate in _CANDIDATES:
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise HexinTokenUnavailable(
        "同花顺 hexin-v needs Deno (https://deno.com) to run its token script sandboxed; "
        "install it or set [sources.ths] js_runtime"
    )


@lru_cache(maxsize=1)
def _script() -> str:
    import json

    source = resources.files("cnequity.adapters.ths").joinpath("js/hexin_v.js").read_text("utf-8")
    # Deno reads stdin as a strict-mode module; the script assigns undeclared
    # globals, which only sloppy mode allows — `new Function` provides it.
    return f"console.log(new Function({json.dumps(source + ';return v;')})()());\n"


def hexin_v(config=None, *, timeout: float = 20.0) -> str:
    """A fresh token. Each call runs the script once (~0.1 s)."""
    deno = find_deno(config)
    try:
        done = subprocess.run(
            # No --allow-* flag: the script runs with every permission denied,
            # and --no-prompt turns any attempt into an error instead of a
            # prompt. The script arrives on stdin, so not even a read is needed.
            [deno, "run", "--quiet", "--no-prompt", "--no-config", "--no-lock", "-"],
            input=_script(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "NO_COLOR": "1", "HOME": str(Path.home())},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HexinTokenUnavailable(f"hexin-v script did not run: {exc}") from exc
    token = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    if done.returncode != 0 or not _TOKEN_RE.match(token):
        raise HexinTokenUnavailable(
            f"hexin-v script failed (exit {done.returncode}): {done.stderr.strip()[:300]}"
        )
    return token
