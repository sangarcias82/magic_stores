"""Admin-side store management (create). The public catalog service stays read-only.

Validation errors are **codes** (``(code, params)``), not texts: the admin routers show them in the
page language (``admin.form.error.<code>``, see app/routers/admin_form_errors.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.i18n import DEFAULT_LOCALE, normalize_locale
from app.models import Store
from app.services import catalog
from app.services.storefront import DEFAULT_THEME, THEME_COLOR_KEYS, normalize_hex_color, theme_from_store

# Lower-case words joined by single hyphens, e.g. "demo-widgets". This also keeps the slug
# from colliding with the shared "_default" storefront folder.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
SLUG_MAX = 64
# Slugs that would collide with fixed admin URLs (/admin/stores/new).
RESERVED_SLUGS = frozenset({"new"})
NAME_MAX = 200

# A validation error: (code, placeholders of its message), e.g. ("name_too_long", {"max_len": 200}).
FieldError = tuple[str, dict[str, Any]]


@dataclass
class StoreInput:
    slug: str = ""
    name: str = ""
    currency: str = "USD"
    locale_default: str = DEFAULT_LOCALE
    errors: dict[str, FieldError] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def clean_store_input(
    db: Session, slug: str, name: str, currency: str, locale_default: str = DEFAULT_LOCALE
) -> StoreInput:
    data = StoreInput(
        slug=slug.strip().lower(),
        name=" ".join(name.split()),
        currency=currency.strip().upper(),
        locale_default=normalize_locale(locale_default) or locale_default.strip(),
    )
    if not data.slug:
        data.errors["slug"] = ("slug_required", {})
    elif len(data.slug) > SLUG_MAX or not SLUG_RE.match(data.slug):
        data.errors["slug"] = ("store_slug_format", {"max_len": SLUG_MAX})
    elif data.slug in RESERVED_SLUGS:
        data.errors["slug"] = ("slug_reserved", {"slug": data.slug})
    elif catalog.get_store_by_slug(db, data.slug) is not None:
        data.errors["slug"] = ("store_slug_taken", {})

    if not data.name:
        data.errors["name"] = ("name_required", {})
    elif len(data.name) > NAME_MAX:
        data.errors["name"] = ("name_too_long", {"max_len": NAME_MAX})

    if not CURRENCY_RE.match(data.currency):
        data.errors["currency"] = ("currency_format", {})

    if normalize_locale(data.locale_default) is None:
        data.errors["locale_default"] = ("locale_invalid", {})
    return data


def create_store(db: Session, data: StoreInput) -> Optional[Store]:
    """Insert a validated store with the default theme. Returns None if the slug was taken meanwhile."""
    store = Store(
        slug=data.slug,
        name=data.name,
        currency=data.currency,
        locale_default=data.locale_default,
        theme=dict(DEFAULT_THEME),
    )
    db.add(store)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(store)
    return store


# ------------------------------------------------------------------------------ theme

@dataclass
class ThemeForm:
    primary: str = ""
    secondary: str = ""
    accent: str = ""
    errors: dict[str, FieldError] = field(default_factory=dict)

    @classmethod
    def from_store(cls, store: Store) -> "ThemeForm":
        theme = theme_from_store(store)
        # The color picker needs #rrggbb; non-hex values (e.g. from a pack) show the default.
        return cls(**{k: normalize_hex_color(theme[k]) or DEFAULT_THEME[k] for k in THEME_COLOR_KEYS})


def validate_theme(form: ThemeForm) -> dict[str, str]:
    colors = {}
    for key in THEME_COLOR_KEYS:
        raw = getattr(form, key)
        color = normalize_hex_color(raw)
        if color is None:
            form.errors[key] = ("color_format", {"key": key})  # the router names the color
        else:
            setattr(form, key, color)
            colors[key] = color
    return colors


def save_theme(db: Session, store: Store, colors: dict[str, str]) -> Store:
    # Assign a new dict: in-place changes to a JSON column are not detected by SQLAlchemy.
    # Other theme keys (e.g. logo_url) are kept.
    store.theme = {**(store.theme or {}), **colors}
    db.commit()
    db.refresh(store)
    return store
