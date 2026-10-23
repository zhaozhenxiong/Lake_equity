"""Render GitHub Release notes for a tag from its CHANGELOG.md section.

Relative links are pinned to the tag so they keep working from the release page.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_URL = "https://github.com/rootSunc/CNEquity"
_LINK = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


def changelog_section(changelog: str, version: str) -> str:
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    following = re.compile(r"^## ", re.MULTILINE).search(changelog, match.end())
    end = following.start() if following else len(changelog)
    body = changelog[match.end() : end].strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section for {version} is empty")
    return body


def render(changelog: str, version: str) -> str:
    tag = f"v{version}"
    body = _LINK.sub(
        lambda m: f"]({REPO_URL}/blob/{tag}/{m.group(1)})", changelog_section(changelog, version)
    )
    return (
        f"CNEquity {version} 已发布至 [PyPI](https://pypi.org/project/cnequity/{version}/)。\n\n"
        f"```bash\npython -m pip install --upgrade cnequity=={version}\n```\n\n{body}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, e.g. v0.17.0")
    parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args()
    if not re.fullmatch(r"v\d+\.\d+\.\d+", args.tag):
        print(f"not a stable release tag: {args.tag}", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(render(args.changelog.read_text(encoding="utf-8"), args.tag[1:]))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
