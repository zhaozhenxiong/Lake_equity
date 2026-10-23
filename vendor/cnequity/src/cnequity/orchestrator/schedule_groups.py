"""Read the runnable daily group order from the configured schedule."""

from __future__ import annotations

import argparse
from pathlib import Path

from cnequity.orchestrator.cadence import DEFAULT_SCHEDULE_GROUPS, scheduled_group_names


def resolve_group_names(config_path: Path) -> list[str]:
    """Use the installed config; retain the package default before first setup."""
    if not config_path.is_file():
        return list(DEFAULT_SCHEDULE_GROUPS)
    from cnequity.config import load_config

    config = load_config(config_path)
    if not config.schedule_groups:
        raise ValueError(f"{config_path}: [job.daily.groups] is empty")
    names = scheduled_group_names(config)
    if not names:
        raise ValueError(f"{config_path}: all daily groups are disabled")
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    print(" ".join(resolve_group_names(args.config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
