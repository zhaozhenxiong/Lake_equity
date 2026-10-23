"""Dashboard display text in both languages.

Display text is English in the code, marked with `N_()`, and Chinese in the
package catalog. A payload carries both — ``key`` in Chinese and ``key_en`` in
English — and the page picks, so one server answers both kinds of browser.
That shape predates the catalog and is kept: the page, and job records already
on disk, read it.
"""

from __future__ import annotations

from cnequity.i18n import current_language, source_text, translate_to

PAGE_CHINESE = "zh_CN"


def zh(text: str | None) -> str | None:
    """The Chinese for display text; text with no translation as it is."""
    return translate_to(PAGE_CHINESE, text) if text else text


def en(text: str | None) -> str | None:
    """The English for display text, given either language.

    Records written before display text moved to English stored the Chinese,
    so Chinese with a catalog entry is mapped back to its source.
    """
    return source_text(PAGE_CHINESE, text) if text else text


def local(text: str | None) -> str | None:
    """Display text in the language in force, given either language.

    For a stored title woven into a sentence: the sentence is already in the
    reader's language, so the title has to follow it.
    """
    if not text:
        return text
    return zh(en(text)) if current_language() == PAGE_CHINESE else en(text)
