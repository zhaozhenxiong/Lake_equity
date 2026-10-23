"""Consumption must not pull ingestion scheduling into a query or audit."""

import ast
from pathlib import Path


def test_query_and_quality_do_not_import_steps():
    root = Path(__file__).resolve().parents[2] / "src/cnequity"
    violations = []
    for package in ("query", "quality"):
        for path in (root / package).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                imports = (
                    [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else [name.name for name in node.names]
                    if isinstance(node, ast.Import)
                    else []
                )
                if any(
                    name == "cnequity.steps" or name.startswith("cnequity.steps.")
                    for name in imports
                ):
                    violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not violations, "Read-only layers import ingestion: " + ", ".join(violations)
