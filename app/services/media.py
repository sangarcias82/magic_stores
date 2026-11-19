"""Media URLs: turn a stored image value (an image property, ``theme.logo_url``) into the URL a
template puts in ``src``.

Product files live in the separate content app (``../magic_stores_content``), not in Magic
Stores. An image value that is a content path, ``/content/<store-slug>/<file>`` (the content
app's own URL path), is prefixed with ``CONTENT_BASE_URL``. Everything else passes through
unchanged: absolute ``http(s)://`` URLs and this site's own paths (``/static/…``,
``/stores/<slug>/assets/…``).
"""
from __future__ import annotations

from typing import Optional

from app.config import get_settings

CONTENT_PREFIX = "/content/"


def media_url(path: Optional[str], base_url: Optional[str] = None) -> str:
    """``base_url`` defaults to the ``CONTENT_BASE_URL`` setting; ``""`` keeps paths relative."""
    if not path:
        return ""
    path = str(path)
    if not path.startswith(CONTENT_PREFIX):
        return path
    base = get_settings().content_base_url if base_url is None else base_url
    return f"{base.rstrip('/')}{path}" if base else path
