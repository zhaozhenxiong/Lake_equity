"""Compare the release contract with the previous stable Git tag, offline.

Breaking changes are allowed with a schema bump and per-dataset migration
notes. The baseline bytes come from the tag, never an editable working copy.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from cnequity.domain.contracts import diff_contracts, load_contract


def previous_release(repo: Path, version: str) -> str:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise ValueError(f"unsupported package version: {version}")
    current = tuple(map(int, match.groups()))
    tags = subprocess.check_output(["git", "tag", "--list", "v*"], cwd=repo, text=True).splitlines()
    candidates = []
    for tag in tags:
        parsed = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
        if parsed and (parts := tuple(map(int, parsed.groups()))) < current:
            candidates.append((parts, tag))
    if not candidates:
        raise ValueError("no previous stable release tag; fetch full release history")
    return max(candidates)[1]


def compatibility_errors(old: dict, new: dict, notes: Path) -> list[str]:
    errors = []
    changed = {item["dataset"] for item in diff_contracts(old, new)["breaking"]}
    for dataset in sorted(changed):
        previous = old["datasets"][dataset]
        current = new["datasets"].get(dataset)
        if current is not None and current["schema_version"] <= previous["schema_version"]:
            errors.append(f"{dataset}: breaking change requires a schema_version increase")
        note = notes / f"{dataset}.md"
        body = note.read_text(encoding="utf-8") if note.is_file() else ""
        for heading in ("## Change", "## Migration", "## Rollback"):
            if heading not in body or not body.split(heading, 1)[1].split("##", 1)[0].strip():
                errors.append(f"{dataset}: {note} needs a nonempty {heading} section")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--current", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current_path = args.current if args.current.is_absolute() else args.repo / args.current
    version = current_path.stem.removeprefix("v")
    tag = previous_release(args.repo, version)
    old = json.loads(
        subprocess.check_output(
            ["git", "show", f"{tag}:contracts/{tag}.json"], cwd=args.repo, text=True
        )
    )
    new = load_contract(current_path)
    report = dict(diff_contracts(old, new))
    report["baseline_tag"] = tag
    report["policy_errors"] = compatibility_errors(
        old, new, args.repo / "contracts" / "migrations" / version
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(
        f"{tag} -> {version}: {report['breaking_count']} breaking, "
        f"{report['compatible_count']} compatible changes"
    )
    for error in report["policy_errors"]:
        print(error)
    return int(bool(report["policy_errors"]))


if __name__ == "__main__":
    raise SystemExit(main())
