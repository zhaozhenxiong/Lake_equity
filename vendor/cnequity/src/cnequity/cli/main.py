"""The `cne` entry point: the command group, and everything that hangs off it.

The commands themselves live in the `*_cmds` modules beside this one, grouped by
what a person is doing rather than by when the code was written. Importing them
here is what registers them, so this module stays the one place that knows the
whole surface exists.

The split is not only for reading. Every module that patches a name for a test
now names the group it belongs to, so a patch aimed at a command that has moved
fails loudly instead of quietly patching a name nobody looks up.
"""

from __future__ import annotations

import cnequity.steps  # noqa: F401 — register steps

# Registration by import: each module attaches its commands to `cli`.
from cnequity.cli import (  # noqa: F401
    backfill_cmds,
    consume_cmds,
    decision_data_cmds,
    delisted_cmds,
    govern_cmds,
    maintain_cmds,
    quality_cmds,
    repair_cmds,
    run_cmds,
    setup_cmds,
    storage_cmds,
    ths_official_cmds,
)
from cnequity.cli._root import cli
from cnequity.cli._shared import (  # noqa: F401 — the documented config contract
    DEFAULT_CONFIG,
    EXAMPLE_CONFIG,
    USER_CONFIG,
    resolve_config_path,
)

__all__ = [
    "DEFAULT_CONFIG",
    "EXAMPLE_CONFIG",
    "USER_CONFIG",
    "cli",
    "resolve_config_path",
]
