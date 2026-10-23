"""pytest plugin: report any code that stamps a column-less frame into a row.

`pl.DataFrame()` — no columns, no rows — is the codebase's "nothing to report"
value, and polars broadcasts a literal against it to length one. The fabricated
row carries only the literals, so it has no primary key; a strict validate
rejects it, but a diagonal concat with real rows fills its keys with nulls
first and launders it into the lake. `cnequity.domain.frames` explains it in
full; `tests/unit/test_blank_frame_broadcast.py` pins the guards.

This is the sweep that finds *new* occurrences. It wraps `DataFrame.with_columns`
and `DataFrame.select`, records every call that turned zero columns into one or
more rows, and writes the deduplicated stack traces at the end of the session::

    PYTHONPATH=scripts/dev .venv/bin/python3 -m pytest tests/unit -q \
        -p probe_blank_frame_broadcast

`scripts/dev/` is not a package, hence the PYTHONPATH. Two kinds of hit
are expected and not defects: the tests in
`tests/unit/test_blank_frame_broadcast.py` that demonstrate the behaviour on
purpose, and polars' own `_expand_dict_values`, which builds every
`pl.DataFrame({...})` this way. Anything under `src/cnequity/` is a defect.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import polars as pl

REPORT = Path("blank_frame_broadcasts.txt")

_original_with_columns = pl.DataFrame.with_columns
_original_select = pl.DataFrame.select
_hits: list[tuple[str, str]] = []


def _record(kind: str) -> None:
    _hits.append((kind, "".join(traceback.format_stack()[-6:-1])))


def _with_columns(self, *args, **kwargs):
    out = _original_with_columns(self, *args, **kwargs)
    if not self.width and out.height:
        _record("with_columns")
    return out


def _select(self, *args, **kwargs):
    out = _original_select(self, *args, **kwargs)
    if not self.width and out.height:
        _record("select")
    return out


pl.DataFrame.with_columns = _with_columns
pl.DataFrame.select = _select


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 — pytest hook shape
    seen: set[str] = set()
    blocks: list[str] = []
    for kind, stack in _hits:
        lines = stack.strip().splitlines()
        key = lines[-2] if len(lines) > 1 else stack
        if key in seen:
            continue
        seen.add(key)
        blocks.append(f"=== {kind} ===\n{stack}")
    header = f"broadcasts: {len(_hits)}, unique sites: {len(seen)}\n\n"
    REPORT.write_text(header + "\n".join(blocks), encoding="utf-8")
    print(f"\n[blank-frame probe] {header.strip()} — see {REPORT}")
