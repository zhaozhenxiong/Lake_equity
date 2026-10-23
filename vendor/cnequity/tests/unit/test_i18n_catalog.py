"""The shipped translation catalog matches the code and compiles to the committed .mo."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "i18n_catalog.py"
spec = importlib.util.spec_from_file_location("i18n_catalog", SCRIPT)
i18n_catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(i18n_catalog)


def test_catalog_is_complete_and_compiled():
    assert i18n_catalog.problems() == []


def test_placeholders_compare_positions_and_names():
    same = i18n_catalog.placeholders
    assert same("Step %s took %s") == same("步骤 %s 耗时 %s")
    assert same("%(n)d rows in %(name)s") == same("%(name)s 中有 %(n)d 行")
    assert same("{count} rows") == same("{count} 行")
    assert same("100%% done %s") == same("完成 100%% %s")
    assert same("Step %s took %s") != same("步骤 %s")
    assert same("%s rows") != same("%d 行")
    assert same("{count} rows") != same("{total} 行")
