"""Language selection, catalog lookup, and render-time Click help translation."""

from __future__ import annotations

import gettext
import logging

import click
import pytest
from babel.messages.catalog import Catalog
from babel.messages.mofile import write_mo
from click.testing import CliRunner

from cnequity import i18n
from cnequity.cli._root import SectionedGroup


@pytest.fixture
def language(monkeypatch):
    """Switch the process language for one test, and restore it afterwards."""

    def use(value: str) -> None:
        monkeypatch.setenv(i18n.ENV_VAR, value)
        i18n.reset()

    yield use
    i18n.reset()


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    """Point the package at a throwaway zh_CN catalog built from `entries`."""

    def build(entries: dict[str, str]) -> None:
        target = tmp_path / "zh_CN" / "LC_MESSAGES"
        target.mkdir(parents=True, exist_ok=True)
        cat = Catalog(locale="zh_CN", domain=i18n.DOMAIN, fuzzy=False)
        for msgid, msgstr in entries.items():
            cat.add(msgid, msgstr)
        with (target / f"{i18n.DOMAIN}.mo").open("wb") as handle:
            write_mo(handle, cat)
        monkeypatch.setattr(i18n, "LOCALE_DIR", tmp_path)
        i18n.translator.cache_clear()

    yield build
    i18n.translator.cache_clear()


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("zh_CN.UTF-8", "zh_CN"),
        ("zh-CN", "zh_CN"),
        ("zh_Hans_CN", "zh_CN"),
        ("zh", "zh_CN"),
        ("ZH_cn", "zh_CN"),
        ("zh_TW.UTF-8", None),
        ("zh_HK", None),
        ("en_US.UTF-8", "en"),
        ("en", "en"),
        ("C", "en"),
        ("POSIX", "en"),
        ("fr_FR", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_language(tag, expected):
    assert i18n.normalize_language(tag) == expected


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"CNE_LANG": "zh_CN", "LANG": "en_US.UTF-8"}, "zh_CN"),
        ({"CNE_LANG": "en", "LANG": "zh_CN.UTF-8"}, "en"),
        ({"LANG": "zh_CN.UTF-8"}, "zh_CN"),
        ({"LC_ALL": "en_US.UTF-8", "LANG": "zh_CN.UTF-8"}, "en"),
        ({"LC_MESSAGES": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}, "zh_CN"),
        ({"LANGUAGE": "fr:zh_CN:en", "LANG": "fr_FR.UTF-8"}, "zh_CN"),
        # A C locale silences LANGUAGE, as GNU gettext does.
        ({"LANGUAGE": "zh_CN", "LC_ALL": "C"}, "en"),
        ({"LANG": "zh_TW.UTF-8"}, "en"),
        ({"LANG": "fr_FR.UTF-8"}, "en"),
        ({}, "en"),
    ],
)
def test_resolution_order(monkeypatch, environ, expected):
    monkeypatch.setattr(i18n.sys, "platform", "linux")
    assert i18n.resolve_language(environ) == expected


def test_unsupported_cne_lang_is_reported_and_uses_the_default(caplog):
    with caplog.at_level(logging.WARNING, logger="cnequity.i18n"):
        assert i18n.resolve_language({"CNE_LANG": "fr", "LANG": "zh_CN.UTF-8"}) == "en"
    assert "CNE_LANG='fr' is not a supported language" in caplog.text


def test_windows_without_locale_variables_reads_the_ui_language(monkeypatch):
    monkeypatch.setattr(i18n.sys, "platform", "win32")
    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: "zh_CN")
    assert i18n.resolve_language({}) == "zh_CN"
    # An explicit locale variable still wins over the UI language.
    assert i18n.resolve_language({"LANG": "en_US"}) == "en"


def test_language_is_resolved_once_until_reset(monkeypatch, language):
    language("zh_CN")
    assert i18n.current_language() == "zh_CN"
    monkeypatch.setenv(i18n.ENV_VAR, "en")
    assert i18n.current_language() == "zh_CN"
    i18n.reset()
    assert i18n.current_language() == "en"


def test_shipped_catalog_loads_from_the_package(language):
    language("zh_CN")
    assert i18n.gettext("Usage: ") == "用法："
    language("en")
    assert i18n.gettext("Usage: ") == "Usage: "


def test_untranslated_text_comes_back_exactly_as_written(language, catalog):
    catalog({})
    language("zh_CN")
    written = "  Indented\n  on purpose\n"
    assert i18n.gettext(written) == written
    assert i18n.gettext("not in any catalog") == "not in any catalog"
    assert i18n.ngettext("%d file", "%d files", 2) == "%d files"
    assert i18n.pgettext("menu", "Open") == "Open"


def test_help_falls_back_to_the_cleaned_docstring_form(language, catalog):
    catalog({"First line.\n\nSecond paragraph.": "第一行。\n\n第二段。"})
    language("zh_CN")
    raw = "First line.\n\n    Second paragraph.\n    "
    assert i18n.translate_help(raw) == "第一行。\n\n第二段。"
    # Runtime messages are exact: their layout is part of what they say.
    assert i18n.gettext(raw) == raw


def test_runtime_messages_keep_leading_newlines_and_indentation(language, catalog):
    catalog({"\n  Written: {n} rows": "\n  写入：{n} 行"})
    language("zh_CN")
    assert i18n.gettext("\n  Written: {n} rows") == "\n  写入：{n} 行"


def test_nothing_touches_process_wide_gettext_state(language):
    domain_before = gettext.textdomain()
    language("zh_CN")
    result = CliRunner().invoke(_demo_group(), ["show", "--help"])
    assert "用法：" in result.output
    assert gettext.textdomain() == domain_before
    # Click's own lookups, and any host's, still go through the untouched default.
    assert gettext.gettext("Usage:") == "Usage:"


def _demo_group() -> click.Group:
    """A small sectioned group built the way `cne` builds its own."""

    @click.group(cls=SectionedGroup)
    def root():
        """Demo root."""

    @root.command()
    @click.option("--limit", type=int, help="How many rows to print.")
    @click.option("--raw", is_flag=True, help="Not translated anywhere.")
    def show(limit, raw):
        """Print the demo rows.

        Reads nothing from the network.
        """

    return root


DEMO_ZH = {
    "Print the demo rows.\n\nReads nothing from the network.": "打印演示行。\n\n不访问网络。",
    "How many rows to print.": "打印多少行。",
    "Usage: ": "用法：",
    "Options": "选项",
    "Show this message and exit.": "显示帮助并退出。",
}


def test_click_help_renders_in_the_language_in_force_when_shown(language, catalog):
    catalog(DEMO_ZH)
    root = _demo_group()  # built once; the language changes afterwards
    runner = CliRunner()

    language("en")
    english = runner.invoke(root, ["show", "--help"]).output
    assert "Usage: root show [OPTIONS]" in english
    assert "Print the demo rows." in english
    assert "How many rows to print." in english
    assert "Options:" in english

    language("zh_CN")
    chinese = runner.invoke(root, ["show", "--help"]).output
    assert "用法：root show [OPTIONS]" in chinese
    assert "打印演示行。" in chinese and "不访问网络。" in chinese
    assert "打印多少行。" in chinese
    assert "选项:" in chinese
    # Untranslated help shows as written; the help option comes from the catalog.
    assert "Not translated anywhere." in chinese
    assert "显示帮助并退出。" in chinese


def test_group_listing_and_attributes_read_translated(language, catalog):
    catalog(DEMO_ZH)
    root = _demo_group()
    language("zh_CN")
    listing = CliRunner().invoke(root, ["--help"]).output
    assert "打印演示行。" in listing

    show = root.commands["show"]
    ctx = click.Context(show)
    limit = next(p for p in show.get_params(ctx) if p.name == "limit")
    # Anything that inspects the attribute — the docs generator, to_info_dict —
    # sees what a person would, while the msgid is kept underneath.
    assert limit.help == "打印多少行。"
    assert limit.__dict__["help"] == "How many rows to print."
    assert isinstance(limit, click.Option)
    assert show.to_info_dict(ctx)["help"].startswith("打印演示行。")


def test_usage_errors_use_the_translated_usage_line(language, catalog):
    catalog(DEMO_ZH)
    language("zh_CN")
    result = CliRunner().invoke(_demo_group(), ["show", "--limit", "x"])
    assert result.exit_code == 2
    assert "用法：root show [OPTIONS]" in result.output


def test_translate_to_ignores_the_process_language(language, catalog):
    catalog({"Create data backup": "创建数据备份"})
    language("en")
    assert i18n.translate_to("zh_CN", "Create data backup") == "创建数据备份"
    assert i18n.translate_to("en", "Create data backup") == "Create data backup"
    assert i18n.gettext("Create data backup") == "Create data backup"


def test_source_text_maps_a_translation_back_to_its_english(catalog):
    catalog({"Create data backup": "创建数据备份"})
    assert i18n.source_text("zh_CN", "创建数据备份") == "Create data backup"
    assert i18n.source_text("zh_CN", "没有这一条") == "没有这一条"
