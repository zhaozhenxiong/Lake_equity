"""Gate for the hourly scheduler wake-up: is this the run, and for which session?

    scheduler_gate.py check daily|stale        print the due session, exit 0;
                                               exit 3 when nothing is due
    scheduler_gate.py mark daily|stale DATE    record that DATE's run started

daily_pipeline.sh / stale_pipeline.sh call it only when launched by the
scheduler (``CNE_SCHEDULED=1``); a manual run is never gated. See
``cnequity.orchestrator.run_window`` for the rule.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

NOT_DUE = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("check", "mark"))
    parser.add_argument("job", choices=("daily", "stale"))
    parser.add_argument("session", nargs="?")
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "CNE_CONFIG", str(Path(__file__).resolve().parents[2] / "configs" / "cnequity.toml")
        ),
    )
    args = parser.parse_args(argv)

    from cnequity.config import load_config
    from cnequity.orchestrator.run_window import mark_done, pending_session

    config = load_config(args.config)
    if args.action == "check":
        session = pending_session(config, args.job)
        if session is None:
            return NOT_DUE
        print(session.isoformat())
        return 0
    if not args.session:
        parser.error("mark needs the session date")
    mark_done(config.meta_root, args.job, date.fromisoformat(args.session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
