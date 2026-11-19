"""UI translations (Spanish first), in two levels.

Level 1 - JSON catalogs: flat files in ``app/locales/<code>.json`` ({"message.key": "text"}).
``es`` is the reference catalog and must contain every key; other languages may be partial.
A language is "available" when its catalog file exists.

Level 2 - the ``translations`` table (``app/services/translations.py``): finer-grained UI
overrides for one language, platform-wide or for one store's pages. This module does not touch
the database; the table is plugged in per request through ``request.state.ui_override_loader``.

A message key is resolved, first match wins::

    store override -> platform override -> JSON catalog   (page language)
    store override -> platform override -> JSON catalog   (es)
    the key itself

Which language a page uses (first match wins, unsupported codes are skipped):

1. ``?lang=<code>`` in the URL (on GET pages also remembered in the ``magic_stores_lang`` cookie)
2. the ``magic_stores_lang`` cookie (an earlier ``?lang=`` choice)
3. on a store's public pages: that store's ``Store.locale_default``
4. ``es``

Only UI chrome is translated here (navigation, headings, buttons, labels, flash messages).
Catalog content (store / category / product names) is data and is shown as entered.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlencode

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_LOCALE = "es"
QUERY_PARAM = "lang"
COOKIE_NAME = "magic_stores_lang"
COOKIE_MAX_AGE = 365 * 24 * 3600
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

Translator = Callable[..., str]


@lru_cache(maxsize=1)
def catalogs() -> dict[str, dict[str, str]]:
    """{code: {key: text}} for every ``locales/*.json`` file."""
    loaded = {}
    for path in sorted(LOCALES_DIR.glob("*.json")):
        loaded[path.stem.lower()] = json.loads(path.read_text(encoding="utf-8"))
    if DEFAULT_LOCALE not in loaded:
        raise RuntimeError(f"Missing reference catalog {LOCALES_DIR / (DEFAULT_LOCALE + '.json')}")
    return loaded


def available_locales() -> list[str]:
    """Supported codes, Spanish first."""
    return sorted(catalogs(), key=lambda code: (code != DEFAULT_LOCALE, code))


def normalize_locale(value: Optional[str]) -> Optional[str]:
    """'EN', 'en-US', 'en_GB' -> 'en' if that catalog exists; anything else -> None."""
    if not value or not isinstance(value, str):
        return None
    code = value.strip().replace("_", "-").split("-", 1)[0].lower()
    return code if code in catalogs() else None


OverridesFor = Callable[[str], Mapping[str, str]]  # locale -> {key: text} (level 2)


def lookup(locale: str, msgid: str, overrides_for: Optional[OverridesFor] = None) -> str:
    """Unformatted text for ``msgid``: level 2 overrides, then the JSON catalog, for ``locale``
    and then for Spanish; the id itself if nothing matches."""
    table = catalogs()
    for code in dict.fromkeys((locale, DEFAULT_LOCALE)):
        if overrides_for is not None:
            text = overrides_for(code).get(msgid)
            if text is not None:
                return text
        text = table.get(code, {}).get(msgid)
        if text is not None:
            return text
    return msgid


def format_text(text: str, params: Mapping[str, Any]) -> str:
    if params:
        try:
            text = text.format(**params)
        except (KeyError, IndexError, ValueError):
            pass  # a bad placeholder in a catalog must not break the page
    return text


def translate(locale: str, msgid: str, /, **params: Any) -> str:
    """Level 1 only (JSON catalogs): text for ``msgid`` in ``locale``, falling back to Spanish,
    then to the id itself. ``params`` fill ``{placeholders}``; the id is positional-only so any
    name (even ``key``) can be a placeholder."""
    return format_text(lookup(locale, msgid), params)


def requested_locale(request: Request) -> Optional[str]:
    """The visitor's explicit choice: ``?lang=`` first, then the remembered cookie."""
    return normalize_locale(request.query_params.get(QUERY_PARAM)) or normalize_locale(
        request.cookies.get(COOKIE_NAME)
    )


def use_store_locale(request: Request, store: Any) -> None:
    """Mark this request as a public page of ``store``: its ``locale_default`` and its UI
    overrides (level 2) apply."""
    request.state.store_locale = getattr(store, "locale_default", None)
    request.state.store_id = getattr(store, "id", None)


def ui_overrides(request: Request, locale: str) -> Mapping[str, str]:
    """Level 2 UI overrides for ``locale`` on this page (cached per request); {} when the
    database layer is not plugged in."""
    loader = getattr(request.state, "ui_override_loader", None)
    if loader is None:
        return {}
    store_id = getattr(request.state, "store_id", None)
    cache = getattr(request.state, "ui_overrides_cache", None)
    if cache is None:
        cache = request.state.ui_overrides_cache = {}
    if (locale, store_id) not in cache:
        cache[(locale, store_id)] = loader(locale, store_id)
    return cache[(locale, store_id)]


def get_locale(request: Request) -> str:
    return (
        requested_locale(request)
        or normalize_locale(getattr(request.state, "store_locale", None))
        or DEFAULT_LOCALE
    )


def translator(request_or_locale: Any) -> Translator:
    """``t(msgid, **params)`` for a request (both levels, page language) or for a locale code
    (JSON catalogs only)."""
    if isinstance(request_or_locale, str):
        locale, overrides_for = request_or_locale, None
    else:
        request = request_or_locale
        locale = get_locale(request)

        def overrides_for(code: str) -> Mapping[str, str]:
            return ui_overrides(request, code)

    def t(msgid: str, /, **params: Any) -> str:
        return format_text(lookup(locale, msgid, overrides_for), params)

    return t


def language_links(request: Request, current: str) -> list[dict[str, Any]]:
    """Switcher entries: same page, other ``lang``. Empty for non-GET pages (e.g. a form
    re-rendered after a POST), whose URL can't necessarily be opened with GET."""
    if request.method != "GET":
        return []
    links = []
    for code in available_locales():
        query = [(k, v) for k, v in request.query_params.multi_items() if k != QUERY_PARAM]
        query.append((QUERY_PARAM, code))
        links.append(
            {
                "code": code,
                "label": translate(code, "lang.name"),
                "url": f"{request.url.path}?{urlencode(query)}",
                "current": code == current,
            }
        )
    return links


def template_context(request: Request) -> dict[str, Any]:
    """Jinja context processor: ``t(key, **params)``, ``lang`` and ``language_links``."""
    locale = get_locale(request)
    return {"t": translator(request), "lang": locale, "language_links": language_links(request, locale)}


REMEMBER_METHODS = frozenset({"GET", "HEAD"})


class LanguageCookieMiddleware:
    """Remembers a valid ``?lang=`` in a cookie, so the choice survives navigation.

    Only page views (GET / HEAD) set the cookie, and only when it changes: a form POST is
    rendered in the requested language but never sets cookies (e.g. a failed sign-in sets none).
    """

    def __init__(self, app: ASGIApp, https_only: bool = False) -> None:
        self.app = app
        self.https_only = https_only

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        chosen = normalize_locale(request.query_params.get(QUERY_PARAM))
        if (
            chosen is None
            or request.method not in REMEMBER_METHODS
            or normalize_locale(request.cookies.get(COOKIE_NAME)) == chosen
        ):
            await self.app(scope, receive, send)
            return

        cookie = Response()
        cookie.set_cookie(
            COOKIE_NAME, chosen, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=self.https_only
        )
        set_cookie_header = cookie.headers["set-cookie"]

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message).append("set-cookie", set_cookie_header)
            await send(message)

        await self.app(scope, receive, send_with_cookie)
