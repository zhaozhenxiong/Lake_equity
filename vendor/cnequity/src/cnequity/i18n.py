"""The language people read CNEquity in, and the catalog that speaks it.

Source text is English and the Chinese lives in a gettext catalog shipped with
the package (``locale/zh_CN/LC_MESSAGES/cnequity.mo``). A string nobody has
translated yet therefore falls back to English, which is also the default — a
gap reads as the default language rather than as a broken page.

One language per process, chosen once: ``CNE_LANG``, then the system locale,
then English. Only text meant for a person goes through here. JSON, status
markers such as ``HEALTHY``, exit codes, persisted metadata and the log file
stay in one language whatever this says, because something parses them.

Nothing here touches the process-wide gettext state (``textdomain``,
``install``). The CLI is importable, and a host application that uses gettext
for itself must not find its domain switched by importing ``cnequity``.
"""

from __future__ import annotations

import gettext as _gettext
import inspect
import locale
import logging
import os
import sys
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from pathlib import Path

logger = logging.getLogger(__name__)

DOMAIN = "cnequity"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"
ENV_VAR = "CNE_LANG"
DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "zh_CN")

# Simplified Chinese only. A Traditional-Chinese locale (zh_TW, zh_HK, zh_Hant)
# gets English rather than a script it did not ask for; CNE_LANG=zh_CN is one
# variable away for anyone who prefers it.
_SIMPLIFIED = {"zh", "zh_cn", "zh_sg", "zh_hans", "zh_hans_cn", "zh_hans_sg"}


def normalize_language(tag: str | None) -> str | None:
    """Map a locale or language tag onto a supported language, or None.

    Accepts the shapes people and systems actually write: ``zh-CN``,
    ``zh_CN.UTF-8``, ``zh_Hans_CN``, ``en_US``, ``C``.
    """
    if not tag:
        return None
    value = tag.strip().split(".", 1)[0].split("@", 1)[0].replace("-", "_").lower()
    if value in {"c", "posix"} or value == "en" or value.startswith("en_"):
        return "en"
    if value in _SIMPLIFIED:
        return "zh_CN"
    return None


def _system_language(environ: Mapping[str, str]) -> str | None:
    """The language the system locale asks for, read the way gettext reads it.

    The first of LC_ALL, LC_MESSAGES and LANG that is set *is* the locale; a C
    locale means "untranslated" and also silences LANGUAGE, as in GNU gettext.
    A launchd or Task Scheduler job usually has none of these, and Windows
    rarely sets them at all, so Windows falls back to the user's UI language.
    """
    current = next(
        (environ[name] for name in ("LC_ALL", "LC_MESSAGES", "LANG") if environ.get(name)), None
    )
    if current is not None and current.split(".", 1)[0] in {"C", "POSIX"}:
        return "en"
    for tag in environ.get("LANGUAGE", "").split(":"):
        if language := normalize_language(tag):
            return language
    if current is not None:
        return normalize_language(current)
    if sys.platform == "win32":
        return _windows_ui_language()
    return None


def _windows_ui_language() -> str | None:
    try:
        import ctypes

        lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    return normalize_language(locale.windows_locale.get(lang_id))


def resolve_language(environ: Mapping[str, str] | None = None) -> str:
    """CNE_LANG, then the system locale, then English.

    An unsupported CNE_LANG is reported and answered with the default: someone
    who asked for a language by name should hear that it is not available, not
    get whichever one their locale happens to name.
    """
    env = os.environ if environ is None else environ
    requested = env.get(ENV_VAR, "").strip()
    if requested:
        language = normalize_language(requested)
        if language is None:
            logger.warning(
                "%s=%r is not a supported language (%s); using %s",
                ENV_VAR,
                requested,
                ", ".join(SUPPORTED_LANGUAGES),
                DEFAULT_LANGUAGE,
            )
            return DEFAULT_LANGUAGE
        return language
    return _system_language(env) or DEFAULT_LANGUAGE


_current: str | None = None

# A language for one piece of work inside the process: `cne serve` answers
# each browser in the language its page is set to, whatever the server's own.
_scoped: ContextVar[str | None] = ContextVar("cnequity_language", default=None)


def current_language() -> str:
    """The language in force: a scoped one (see `use_language`), else the process's.

    The process's is resolved on first use and then fixed. Threads share it. A
    spawned child resolves it again from the same environment and lands on the
    same answer.
    """
    scoped = _scoped.get()
    if scoped is not None:
        return scoped
    global _current
    if _current is None:
        _current = resolve_language()
    return _current


@contextmanager
def use_language(language: str | None) -> Iterator[None]:
    """Speak `language` inside the block; None keeps the language in force.

    It follows the context, so it reaches code run through `asyncio`, a
    Starlette thread pool or `contextvars.copy_context().run` — but not a
    plain `threading.Thread`, which starts from an empty context (see
    `carry_language`).
    """
    token = _scoped.set(normalize_language(language) if language else None)
    try:
        yield
    finally:
        _scoped.reset(token)


def carry_language(target: Callable[..., object]) -> Callable[..., object]:
    """`target`, bound to the language in force now, for a background thread.

    A job a browser starts reports back in the language that browser reads.
    """
    language = current_language()

    def run(*args: object, **kwargs: object) -> object:
        with use_language(language):
            return target(*args, **kwargs)

    return run


def reset() -> None:
    """Forget the resolved language, so the next call reads the environment."""
    global _current
    _current = None


@cache
def translator(language: str) -> _gettext.NullTranslations:
    """The catalog for `language`; English, the source text, needs none."""
    if language == DEFAULT_LANGUAGE:
        return _gettext.NullTranslations()
    return _gettext.translation(DOMAIN, localedir=LOCALE_DIR, languages=[language], fallback=True)


def catalog_key(text: str) -> str:
    """The msgid a command docstring is filed under.

    Click shows a command's docstring through `inspect.cleandoc`, and runs it
    over option help when the option is built, so multi-line help never reaches
    a lookup as it was written. Docstrings are extracted in this form, and help
    lookups fall back to it. Every other message is filed exactly as written:
    its leading newline or indentation is part of what it says.
    """
    return inspect.cleandoc(text) if "\n" in text else text


def translate_to(language: str, message: str) -> str:
    """`message` in `language`, whatever the process speaks.

    For output that carries every language at once, such as the dashboard
    payloads, where the browser picks.
    """
    return translator(language).gettext(message)


@cache
def _sources(catalog: _gettext.NullTranslations) -> dict[str, str]:
    entries = getattr(catalog, "_catalog", {})
    return {text: msgid for msgid, text in entries.items() if isinstance(msgid, str) and msgid}


def source_text(language: str, translated: str) -> str:
    """The English a `language` translation came from; `translated` if none.

    Text written to disk before its source became English — a job record from
    an older version stores the Chinese title — still has to be shown in
    English.
    """
    return _sources(translator(language)).get(translated, translated)


def gettext(message: str) -> str:
    """`message` in the process's language; the original when untranslated."""
    return translate_to(current_language(), message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return translator(current_language()).ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    return translator(current_language()).pgettext(context, message)


_ = gettext


def N_(message: str) -> str:  # noqa: N802 — the gettext convention extractors look for
    """Mark `message` for the catalog without translating it now.

    For text whose language must be chosen when it is shown rather than when
    the module is imported: Click help is the main case.
    """
    return message


def NP_(context: str, message: str) -> str:  # noqa: N802 — see N_
    """Mark `message` under `context`, for a word that means different things.

    A step status like "success" and a button labelled "success" may want
    different Chinese; the context keeps their catalog entries apart.
    """
    return message


class Message(str):
    """A log argument that is English for the log file and any language on screen.

    It *is* its English text — a `str` — so `logging`, every handler that does
    not know about translation, and any code comparing arguments see exactly
    what they always saw. The terminal formatter calls `render` instead.
    Arguments may themselves be `Message`s, which is how a pre-built fragment
    such as " · ~3m left" is translated with the line it belongs to.
    """

    template: str
    args: tuple[object, ...]
    context: str | None

    def __new__(cls, template: str, *args: object, context: str | None = None) -> Message:
        english = _format(template, args, DEFAULT_LANGUAGE) if args else template
        self = super().__new__(cls, english)
        self.template, self.args, self.context = template, args, context
        return self

    @classmethod
    def word(cls, context: str, word: str) -> Message:
        """A single word, translated under `context` (see `NP_`)."""
        return cls(word, context=context)

    def render(self, language: str = DEFAULT_LANGUAGE) -> str:
        if language == DEFAULT_LANGUAGE:
            return str.__str__(self)
        if self.context is not None:
            template = translator(language).pgettext(self.context, self.template)
        else:
            template = translate_to(language, self.template)
        if not self.args:
            return template
        try:
            return _format(template, self.args, language)
        except (TypeError, ValueError, KeyError):
            # A catalog mistake must not cost the line; the source still fits.
            return str.__str__(self)

    def __repr__(self) -> str:
        return f"Message({self.template!r}, *{self.args!r})"


def _format(template: str, args: tuple[object, ...], language: str) -> str:
    rendered = tuple(arg.render(language) if isinstance(arg, Message) else arg for arg in args)
    return template % rendered


def localize(text: str) -> str:
    """`text` for a person, in the language in force.

    A `Message` renders in that language; any other string is looked up as a
    msgid, which is how an `N_()` constant or an English value read back from a
    report finds its translation. "" passes through (it is the catalog header).
    """
    if isinstance(text, Message):
        return text.render(current_language())
    return gettext(text) if text else text


def translate_help(text: str | None) -> str | None:
    """Click help text in the process's language; None and "" pass through.

    Looked up as given, then in the cleaned form command docstrings are filed
    under (see `catalog_key`); the original is returned when neither is found.
    """
    if not text:
        return text
    catalog = translator(current_language())
    translated = catalog.gettext(text)
    if translated != text:
        return translated
    key = catalog_key(text)
    translated = catalog.gettext(key)
    return text if translated == key else translated
