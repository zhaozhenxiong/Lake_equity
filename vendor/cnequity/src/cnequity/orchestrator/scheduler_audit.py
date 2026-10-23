"""Extract concrete run identities from CLI JSON for the shell health gate."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def run_ids_from_output(text: str) -> list[str]:
    if not text.strip():
        return []  # A command can fail before creating a run.
    start = text.find("{")
    if start < 0:
        raise ValueError("scheduled command did not return a JSON result")
    payload = json.loads(text[start:])
    found: list[str] = []

    def visit(value):
        if isinstance(value, dict):
            run_id = value.get("run_id")
            if "run_id" in value:
                if run_id is None and value.get("status") == "skipped_not_scheduled":
                    return
                if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", run_id):
                    raise ValueError("scheduled command returned an invalid run_id")
                found.append(run_id)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return list(dict.fromkeys(found))


if __name__ == "__main__":
    print(" ".join(run_ids_from_output(Path(sys.argv[1]).read_text(encoding="utf-8"))))
