"""Admin-side management of a store's shipping methods (list, create, edit).

The form keeps the raw submitted strings so it can be shown again on error. :func:`validate_method`
returns the cleaned values plus **error codes** (``{field: (code, params)}``), not texts: the admin
router shows them in the page language (``admin.shipping.error.<code>`` in app/locales/), like the
Translations page does. The quote rules themselves are in app/services/shipping.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import fits_db_integer
from app.models import MAX_AMOUNT_CENTS, ShippingMethod, Store
from app.services.category_admin import SORT_MAX, SORT_MIN
from app.services.store_admin import SLUG_RE
from app.services.translations import ContentTranslations

NAME_MAX = 200
CODE_MAX = 64
COUNTRIES_MAX_CHARS = 1000
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
MONEY_RE = re.compile(r"^\d+(?:\.\d+)?$")

# A field's problem: (code, params), e.g. ("too_long", {"max_len": 200}).
FieldError = tuple[str, dict[str, Any]]


@dataclass
class ShippingMethodForm:
    name: str = ""
    code: str = ""
    rate: str = ""
    free_from: str = ""  # free_shipping_threshold_cents, as money; empty = never free
    countries: str = ""  # "ES, PT"; empty = ships anywhere
    sort_order: str = "0"
    is_active: bool = True
    errors: dict[str, str] = field(default_factory=dict)  # field -> message shown (filled by the router)

    @classmethod
    def from_method(cls, method: ShippingMethod) -> ShippingMethodForm:
        threshold = method.free_shipping_threshold_cents
        return cls(
            name=method.name,
            code=method.code,
            rate=cents_to_form(method.rate_cents),
            free_from="" if threshold is None else cents_to_form(threshold),
            countries=", ".join(method.countries or []),
            sort_order=str(method.sort_order),
            is_active=method.is_active,
        )


def cents_to_form(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def list_methods(db: Session, store: Store) -> list[ShippingMethod]:
    """Every method of the store, active or not, in listing order."""
    return list(
        db.scalars(
            select(ShippingMethod)
            .where(ShippingMethod.store_id == store.id)
            .order_by(ShippingMethod.sort_order, ShippingMethod.name, ShippingMethod.id)
        )
    )


def display_names(db: Session, store: Store, locale: str) -> dict[int, str]:
    """Each method's name in ``locale`` (its translation, else the name as entered), by id. Admin
    pages show names this way so an English page isn't full of Spanish labels; the form still edits
    the name as entered, and the Translations tab edits the translations."""
    translations = ContentTranslations.load(db, locale, [store.id])
    methods = db.scalars(select(ShippingMethod).where(ShippingMethod.store_id == store.id))
    return {method.id: translations.shipping_method_name(method) for method in methods}


def get_method(db: Session, store: Store, method_id: int) -> Optional[ShippingMethod]:
    """The store's method with this id (another store's method is None)."""
    if not fits_db_integer(method_id):
        return None
    return db.scalar(select(ShippingMethod).where(ShippingMethod.store_id == store.id, ShippingMethod.id == method_id))


def parse_money(raw: str, required: bool) -> tuple[Optional[int], Optional[FieldError]]:
    """Money typed with a dot for decimals ("4.95") -> cents. A comma is refused rather than
    guessed: in Spanish "4,95" means 4.95, in English "1,000" means 1000."""
    raw = raw.strip()
    if not raw:
        return None, (("required", {}) if required else None)
    if "," in raw:
        return None, ("money_comma", {})
    if raw.startswith("-"):
        return None, ("money_negative", {})
    if len(raw) > 20 or not MONEY_RE.match(raw):
        return None, ("money_format", {})
    amount = Decimal(raw)  # MONEY_RE lets only plain decimals through
    if amount != amount.quantize(Decimal("0.01")):
        return None, ("money_decimals", {})
    cents = int(amount * 100)
    if cents > MAX_AMOUNT_CENTS:
        return None, ("money_too_large", {})
    return cents, None


def _parse_countries(raw: str) -> tuple[list[str], Optional[FieldError]]:
    if len(raw) > COUNTRIES_MAX_CHARS:
        return [], ("too_long", {"max_len": COUNTRIES_MAX_CHARS})
    countries: list[str] = []
    for part in re.split(r"[\s,;]+", raw.strip()):
        if not part:
            continue
        code = part.upper()
        if not COUNTRY_RE.match(code):
            return [], ("country_format", {"value": part})
        if code not in countries:
            countries.append(code)
    return countries, None


def _parse_sort_order(raw: str) -> tuple[int, Optional[FieldError]]:
    raw = raw.strip()
    if not raw:
        return 0, None
    try:
        value = int(raw)
    except ValueError:
        return 0, ("sort_format", {})
    if not SORT_MIN <= value <= SORT_MAX:
        return 0, ("sort_range", {"min": f"{SORT_MIN:,}", "max": f"{SORT_MAX:,}"})
    return value, None


def validate_method(
    db: Session, store: Store, form: ShippingMethodForm, method: Optional[ShippingMethod] = None
) -> tuple[dict[str, Any], dict[str, FieldError]]:
    """Clean the form: (values ready to save, errors by field). ``method``: the one being edited
    (its own code doesn't count as taken)."""
    errors: dict[str, FieldError] = {}

    name = " ".join(form.name.split())
    if not name:
        errors["name"] = ("required", {})
    elif len(name) > NAME_MAX:
        errors["name"] = ("too_long", {"max_len": NAME_MAX})

    code = form.code.strip().lower()
    if not code:
        errors["code"] = ("required", {})
    elif len(code) > CODE_MAX or not SLUG_RE.match(code):
        errors["code"] = ("code_format", {"max_len": CODE_MAX})
    else:
        taken = db.scalar(
            select(ShippingMethod.id).where(ShippingMethod.store_id == store.id, ShippingMethod.code == code)
        )
        if taken is not None and (method is None or taken != method.id):
            errors["code"] = ("code_taken", {"code": code})

    rate, error = parse_money(form.rate, required=True)
    if error:
        errors["rate"] = error
    threshold, error = parse_money(form.free_from, required=False)
    if error:
        errors["free_from"] = error
    countries, error = _parse_countries(form.countries)
    if error:
        errors["countries"] = error
    sort_order, error = _parse_sort_order(form.sort_order)
    if error:
        errors["sort_order"] = error

    values = {
        "name": name,
        "code": code,
        "rate_cents": rate or 0,
        "free_shipping_threshold_cents": threshold,
        "countries": countries,
        "sort_order": sort_order,
        "is_active": form.is_active,
    }
    return values, errors


def save_method(
    db: Session, store: Store, values: dict[str, Any], method: Optional[ShippingMethod] = None
) -> Optional[ShippingMethod]:
    """Create (``method`` None) or update a method with validated values; commits. Returns None if
    the code was taken by another request in the meantime (unique constraint)."""
    if method is None:
        method = ShippingMethod(store_id=store.id)
        db.add(method)
    for key, value in values.items():
        setattr(method, key, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return method
