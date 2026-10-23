"""A path written into TOML must survive Windows.

`tmp_path` interpolated raw becomes `D:\\a\\_temp\\...` on the Windows job, and
`tomllib` reads those backslashes as escapes: `Invalid hex value (at line 2,
column 13)`. It cannot fail anywhere else, so it reaches CI green on three
platforms and red on the fourth — which is exactly what happened on
2026-09-19, on a test whose own subject had nothing to do with paths.

`cnequity.config.bootstrap.path_for_toml` already existed to prevent this. The
gap was that nothing required its use, so this reads the test tree and does.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parents[1]

# A TOML assignment whose value is a single interpolation: `root = "{...}"`.
# The key may follow a literal `\n` inside an f-string, which is how most of
# these are written, so that counts as a separator alongside whitespace.
ASSIGNMENT = re.compile(
    r'(?:\\n|^|(?<=[\s\[]))((?:[a-z]+_)*(?:root|path|dir|file))\s*=\s*"\{([^{}]+)\}"'
)

SAFE = ("path_for_toml(", ".as_posix()")

# For the one test that asserts a raw path *does* fail to parse.
MARKER = "raw-path-on-purpose"


def _offences() -> list[str]:
    found: list[str] = []
    for source in sorted(TESTS_ROOT.rglob("*.py")):
        if source == Path(__file__).resolve():
            # This file spells the offending shape out on purpose.
            continue
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if MARKER in line:
                continue
            for key, expression in ASSIGNMENT.findall(line):
                if any(token in expression for token in SAFE):
                    continue
                found.append(
                    f"{source.relative_to(TESTS_ROOT)}:{number}: {key} = "
                    f'"{{{expression}}}" — wrap it in path_for_toml()'
                )
    return found


def test_no_test_writes_a_bare_path_into_toml():
    offences = _offences()

    assert not offences, "\n".join(offences)


def test_the_guard_catches_the_shape_that_broke_windows():
    """The rule has to fire on the real line, or it is decoration."""
    broke_ci = 'f\'[data]\\nroot = "{tmp_path / "lake"}"\\n\''
    assert ASSIGNMENT.findall(broke_ci)

    fixed = 'f\'[data]\\nroot = "{path_for_toml(tmp_path / "lake")}"\\n\''
    key, expression = ASSIGNMENT.findall(fixed)[0]
    assert key == "root"
    assert any(token in expression for token in SAFE)
