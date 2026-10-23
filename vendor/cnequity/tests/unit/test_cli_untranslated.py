"""Text people read is English in the code; Chinese comes from the catalog.

A Chinese literal added straight into a command would show Chinese to English
users and skip the catalog, so it fails here. The exceptions are a catalog
message whose English names something Chinese (`THS (同花顺)`), and a line
marked `# i18n-ignore` for literals that are data rather than text, such as the
sample lake's security names.
"""

from __future__ import annotations

import ast
import gettext
import inspect
import re
from pathlib import Path

from cnequity import i18n

PACKAGE = Path(i18n.__file__).resolve().parent
CLI = PACKAGE / "cli"
# Where text is written for people: the CLI, the dashboard's server side, and
# the lower layers whose messages reach either one.
SURFACES = (
    CLI,
    PACKAGE / "serve",
    PACKAGE / "serve" / "ops",
    PACKAGE / "diagnostics",
    PACKAGE / "quality",
    PACKAGE / "research",
    PACKAGE / "domain",
    PACKAGE / "config",
    PACKAGE / "storage",
    PACKAGE / "storage" / "repairs",
    PACKAGE / "storage" / "lifecycle",
)
# Reference tables whose Chinese is the data itself: product names, the
# sentiment lexicon.
DATA_MODULES = {
    PACKAGE / "domain" / "futures_products.py",
    PACKAGE / "domain" / "sentiment.py",
}
_CJK = re.compile(r"[一-鿿]")


def _msgids() -> set[str]:
    catalog = gettext.translation(
        i18n.DOMAIN, localedir=i18n.LOCALE_DIR, languages=["zh_CN"], fallback=False
    )
    return {key for key in catalog._catalog if isinstance(key, str)}


def test_no_chinese_literal_bypasses_the_catalog():
    msgids = _msgids()
    offenders: list[str] = []
    paths = {p for surface in SURFACES for p in surface.glob("*.py")} - DATA_MODULES
    for path in sorted(paths):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        # Docstrings are code documentation, or command help the catalog check
        # already holds to a translation.
        docstrings = {
            id(owner.body[0].value)
            for owner in ast.walk(tree)
            if isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and owner.body
            and isinstance(owner.body[0], ast.Expr)
            and isinstance(owner.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            elif isinstance(node, ast.JoinedStr):
                text = ast.get_source_segment(source, node) or ""
            else:
                continue
            if not _CJK.search(text):
                continue
            span = lines[node.lineno - 1 : node.end_lineno]
            if any("# i18n-ignore" in line for line in span):
                continue
            if isinstance(node, ast.Constant) and (
                text in msgids or inspect.cleandoc(text) in msgids
            ):
                continue
            offenders.append(f"{path.name}:{node.lineno}: {text[:60]!r}")
    assert offenders == []


def _format_fields(template: str) -> set[str]:
    import string

    return {
        field.split(".")[0].split("[")[0]
        for _, field, _, _ in string.Formatter().parse(template)
        if field is not None
    }


def test_every_formatted_message_gets_exactly_its_fields():
    """`_("...{x}...").format(x=...)` must name every field, in both languages.

    The catalog check holds the Chinese to the English fields; this holds the
    English to the call, so a misspelt field fails here instead of raising
    `KeyError` for whoever runs that branch in English.
    """
    package = CLI.parent
    mismatched: list[str] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id in {"_", "gettext"}
                and node.func.value.args
                and isinstance(node.func.value.args[0], ast.Constant)
            ):
                continue
            template = node.func.value.args[0].value
            given = {keyword.arg for keyword in node.keywords}
            if None in given or node.args:
                continue  # **kwargs or positional: not checkable here
            if _format_fields(template) != given:
                mismatched.append(
                    f"{path.relative_to(package)}:{node.lineno}: "
                    f"{sorted(_format_fields(template))} vs {sorted(given)}"
                )
    assert mismatched == []
