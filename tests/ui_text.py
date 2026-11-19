"""Interface text for test assertions, read from the JSON catalogs the app renders from
(app/locales/<code>.json), so tests check that a page shows the right catalog entry, not a copy of
its current wording: rewording a message in the JSON doesn't break them. The one place for this in
the test suite (test modules don't define their own copies).

Two ways to compare, pick one per assertion:

    from tests.ui_text import en, es, page_text          # plain text vs the decoded page
    assert en("flash.terminal_saved", name="Mock card") in page_text(response)

    from tests.ui_text import en_html, es_html           # escaped text vs the raw HTML
    assert en_html("flash.pack_created", name=…) in response.text

Templates escape quotes, so ``"{name}"`` in a message is ``&#34;…&#34;`` in the raw HTML: compare
plain text with ``page_text(response)``, or escaped text with ``response.text``. Keep the raw form for
tests about escaping itself (``&lt;b&gt;`` must be in the page, ``<b>`` must not). For "this message
is not shown", whatever its values: ``assert not pattern("en", "flash.store_created").search(text)``.

Every lookup requires the key to exist in that very catalog (no silent fallback to Spanish or to the
key itself), so a typo fails loudly. Catalogs are read through ``app.i18n`` at call time, so a test
that patches ``i18n.catalogs`` sees its patch here too.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any

from markupsafe import escape

from app import i18n


def ui(locale: str, key: str, /, **params: Any) -> str:
    """The catalog text of ``key`` in ``locale`` with ``params`` filled in (plain, not escaped)."""
    assert key in i18n.catalogs()[locale], f"{locale}.json has no key {key!r}"
    return i18n.translate(locale, key, **params)


def en(key: str, /, **params: Any) -> str:
    """English text (the ``client`` / ``admin_client`` fixtures call every page with ``?lang=en``)."""
    return ui("en", key, **params)


def es(key: str, /, **params: Any) -> str:
    """Spanish text (the default language: ``default_lang_client``)."""
    return ui("es", key, **params)


def ui_html(locale: str, key: str, /, **params: Any) -> str:
    """Like :func:`ui`, HTML-escaped as the templates render it: compare with the raw ``response.text``."""
    return str(escape(ui(locale, key, **params)))


def en_html(key: str, /, **params: Any) -> str:
    return ui_html("en", key, **params)


def es_html(key: str, /, **params: Any) -> str:
    return ui_html("es", key, **params)


def distinct(key: str, /, **params: Any) -> tuple[str, str]:
    """(Spanish, English) escaped text for ``key``. They must differ, or a test couldn't tell which
    language a page is in."""
    spanish, english = es_html(key, **params), en_html(key, **params)
    assert spanish != english, f"{key} is the same in es.json and en.json; pick another key"
    return spanish, english


def link(text: str) -> str:
    """The text as the label of a link: ``>text</a>``."""
    return f">{text}</a>"


def pattern(locale: str, key: str, /) -> re.Pattern[str]:
    """A regex matching the catalog text of ``key`` with any values in its ``{placeholders}``."""
    assert key in i18n.catalogs()[locale], f"{locale}.json has no key {key!r}"
    parts = re.split(r"\{[^{}]*\}", i18n.catalogs()[locale][key])
    return re.compile(".+?".join(re.escape(part) for part in parts))


def page_text(response: Any) -> str:
    """The response body with HTML entities decoded (``&#34;`` -> ``"``)."""
    return unescape(response.text)
