"""The dashboard's server-side display text exists in English and in Chinese.

Operation titles, summaries, form labels, confirmations and the settings page
are English in Python, marked with `N_()`, and Chinese in the package catalog.
Each payload carries both: ``key`` in Chinese and ``key_en`` in English.
`test_i18n_catalog` fails when a marked string has no Chinese; this file fails
when display text is not marked, or a payload loses a language.
"""

from __future__ import annotations

import re

from cnequity.serve.display import en, zh
from cnequity.serve.ops import catalog, settings
from cnequity.serve.ops.records import public_record

_CJK = re.compile(r"[一-鿿]")


def _display_strings() -> list[str]:
    found: list[str] = []
    for spec in catalog.OPS:
        found += [spec.title, spec.summary, spec.group, spec.heavy_text, spec.needs_reason]
        for param in spec.params:
            found += [param.label, param.help, param.empty_reason]
    found.append(catalog.STANDARD_ACK["text"])
    found += [
        settings.HOME_NOTE,
        settings.NOTE_CHANGED,
        settings.NOTE_UNCHANGED,
        settings.ACKNOWLEDGEMENT,
        settings.PUSH2_ENV_NOTE,
    ]
    found += [title for _, title in settings.GROUPS]
    for spec in settings.SETTINGS:
        found += [spec.label, spec.help, *(label for _, label in spec.choices)]
    return [text for text in found if text]


def test_display_text_is_english_source():
    chinese = sorted(text for text in _display_strings() if _CJK.search(text))
    assert chinese == []


# Labels that are command or record names, read the same in both languages.
SAME_IN_BOTH = {"init run"}


def _is_name(text: str) -> bool:
    """A source or flag name such as `Baostock` or `run` reads the same in both."""
    return " " not in text.strip() or text in SAME_IN_BOTH


def test_every_display_string_has_chinese():
    untranslated = sorted(
        text for text in _display_strings() if zh(text) == text and not _is_name(text)
    )
    assert untranslated == []


def test_cards_carry_both_languages():
    cards = catalog.describe(None, setup=True)
    assert cards
    for card in cards:
        for key in ("title", "summary", "group"):
            assert card[f"{key}_en"] and not _CJK.search(card[f"{key}_en"]), (card["id"], key)
            assert card[key] == zh(card[f"{key}_en"])
            assert _CJK.search(card[key]) or _is_name(card[key]), (card["id"], key)
        for param in card["params"]:
            assert not _CJK.search(param["label_en"] or ""), (card["id"], param["name"])
            assert param["label"] == zh(param["label_en"])
            assert _CJK.search(param["label"]) or _is_name(param["label"]), (
                card["id"],
                param["name"],
            )


def test_a_record_written_before_english_source_still_shows_english(config):
    spec = catalog.OPS[0]
    legacy = {"job_id": "ab" * 16, "state": "complete", "title": zh(spec.title)}
    shown = public_record(config, legacy)
    assert shown["title"] == zh(spec.title)
    assert shown["title_en"] == spec.title
    assert en(zh(spec.title)) == spec.title
