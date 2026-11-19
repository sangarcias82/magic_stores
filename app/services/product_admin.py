"""Admin-side product management. A product belongs to one category; its form is driven by that
category's property definitions and values are stored as typed ``PropertyValue`` JSON:

- text / enum      -> {"value": "<str>"}
- number           -> {"value": <int | float>}
- boolean          -> {"value": true | false}
- image            -> {"media_path": "<url or /path>"}
- category_ref     -> {"category_id": <int>}

An empty field means "no value" (no PropertyValue row).

Every product saved through the admin form has a **SKU**: the store's own product code, required,
unique within the store (another store may use the same one), stored upper-case so ``ab-1`` and
``AB-1`` are the same SKU.

It also has its **stock on hand** (``stock_on_hand``): required, a whole number from 0 up.

Validation returns **error codes** (``{field: (code, params)}`` in ``ProductForm.errors``), not texts:
the admin router shows them in the page language (``admin.product.error.<code>`` in app/locales/),
like the shipping form.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import fits_db_integer
from app.models import (
    Category,
    Product,
    PropertyDefinition,
    PropertyType,
    PropertyValue,
    StockMovementKind,
    Store,
    User,
)
from app.services import category_admin
from app.services import low_stock
from app.services import stock as stock_svc
from app.services.stock import STOCK_MAX, STOCK_MAX_LEN, STOCK_RE
from app.services.store_admin import SLUG_MAX, SLUG_RE

NAME_MAX = 200
DESCRIPTION_MAX = 5000
TEXT_VALUE_MAX = 2000
URL_MAX = 1000
PRICE_MAX_CENTS = 99_999_999_999  # 999,999,999.99
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
NUMBER_MAX_LEN = 30
CENT = Decimal("0.01")
TRUE_WORDS = {"true", "yes", "1", "on"}
FALSE_WORDS = {"false", "no", "0", "off"}
FIELD_PREFIX = "prop_"

# A validation error: (code, placeholders of its message), e.g. ("name_too_long", {"max_len": 200}).
# The message is admin.product.error.<code> in app/locales/*.json (app/routers/admin_products.py).
FieldError = tuple[str, dict[str, Any]]
SKU_MAX = 64
# ASCII letters / digits, then also "-", "_", ".": no spaces, so a SKU can't hide a typo.
SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

STATUS_ACTIVE = "active"
STATUS_HIDDEN = "hidden"


def field_name(definition: PropertyDefinition) -> str:
    """Form field name for a definition's value, e.g. ``prop_color``."""
    return f"{FIELD_PREFIX}{definition.key}"


# ------------------------------------------------------------------------------ queries


def store_products(
    db: Session, store: Store, category_id: Optional[int] = None, sku_contains: str = ""
) -> list[Product]:
    """All products of a store (active and hidden), with their category loaded. ``sku_contains``:
    only products whose SKU contains it, ignoring case (see :func:`sku_search_term`)."""
    stmt = (
        select(Product)
        .where(Product.store_id == store.id)
        .options(selectinload(Product.category))
        .order_by(Product.name, Product.id)
    )
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    term = sku_search_term(sku_contains)
    if term:
        # SKUs are stored upper-case; "_" is a SKU character, so LIKE's wildcards are escaped.
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Product.sku.like(f"%{escaped}%", escape="\\"))
    return list(db.scalars(stmt).all())


def store_has_products(db: Session, store: Store) -> bool:
    return db.scalar(select(Product.id).where(Product.store_id == store.id).limit(1)) is not None


def sku_search_term(raw: str) -> str:
    """What the admin typed in the SKU search, normalized like stored SKUs (trimmed, upper-case),
    at most ``SKU_MAX`` characters."""
    return normalize_sku(raw)[:SKU_MAX]


def product_by_sku(db: Session, store: Store, raw_sku: str) -> Optional[Product]:
    """The store's product with this SKU (exact, ignoring case and surrounding spaces), or None.
    A value that can't be a SKU is never looked up."""
    sku = raw_sku.strip()
    if not sku or len(sku) > SKU_MAX or not SKU_RE.match(sku):
        return None
    return db.scalar(
        select(Product)
        .where(Product.store_id == store.id, Product.sku == normalize_sku(sku))
        .options(selectinload(Product.category))
    )


def get_product(db: Session, store: Store, product_id: int) -> Optional[Product]:
    """Product by id, only if it belongs to ``store``."""
    if not fits_db_integer(product_id):
        return None
    return db.scalar(
        select(Product)
        .where(Product.id == product_id, Product.store_id == store.id)
        .options(
            selectinload(Product.property_values),
            selectinload(Product.category).selectinload(Category.property_definitions),
        )
    )


# ---------------------------------------------------------------------------- helpers


def price_to_form(cents: int) -> str:
    return f"{Decimal(cents) / 100:.2f}"


def parse_price(raw: str, errors: dict[str, FieldError]) -> int:
    raw = raw.strip().replace(",", "")
    if not raw:
        errors["price"] = ("price_required", {})
        return 0
    if len(raw) > 20:
        errors["price"] = ("price_too_large", {})
        return 0
    try:
        amount = Decimal(raw)
        if not amount.is_finite():
            raise InvalidOperation
        if amount != amount.quantize(CENT):
            errors["price"] = ("price_decimals", {})
            return 0
    except InvalidOperation:  # not a number, or too large to quantize (e.g. 1e99999)
        errors["price"] = ("price_format", {})
        return 0
    cents = int(amount * 100)
    if cents < 0:
        errors["price"] = ("price_negative", {})
    elif cents > PRICE_MAX_CENTS:
        errors["price"] = ("price_too_large", {})
    return cents


def parse_stock(raw: str, errors: dict[str, FieldError]) -> int:
    """Form value -> units on hand (whole number >= 0). Errors go to ``errors["stock"]``."""
    raw = raw.strip()
    if not raw:
        errors["stock"] = ("stock_required", {})
        return 0
    if not STOCK_RE.match(raw):
        # No thousands separators: "1,000" / "1.000" mean different things per language.
        errors["stock"] = ("stock_format", {})
        return 0
    if len(raw) > STOCK_MAX_LEN:  # digits only, but far too many to convert
        errors["stock"] = ("stock_too_large", {})
        return 0
    units = int(raw)
    if units < 0:
        errors["stock"] = ("stock_negative", {})
    elif units > STOCK_MAX:
        errors["stock"] = ("stock_too_large", {})
    return units


def normalize_sku(raw: str) -> str:
    """Form value -> stored SKU: surrounding spaces dropped, upper-case."""
    return raw.strip().upper()


def parse_sku(raw: str, errors: dict[str, FieldError]) -> str:
    """Checks the SKU's format (not its uniqueness). Returns it normalized."""
    value = raw.strip()
    if not value:
        errors["sku"] = ("sku_required", {})
    elif len(value) > SKU_MAX or not SKU_RE.match(value):
        errors["sku"] = ("sku_format", {"max_len": SKU_MAX})
    # Upper-case only after the ASCII check: str.upper() maps some non-ASCII letters to ASCII ones.
    return normalize_sku(value)


def sku_owner(db: Session, store: Store, sku: str, current: Optional[Product] = None) -> Optional[Product]:
    """The other product of ``store`` that already has ``sku`` (None if it is free)."""
    stmt = select(Product).where(Product.store_id == store.id, Product.sku == sku)
    if current is not None and current.id is not None:
        stmt = stmt.where(Product.id != current.id)
    return db.scalar(stmt)


def unique_clash_errors(
    db: Session, store: Store, slug: str, sku: str, current: Optional[Product] = None
) -> dict[str, FieldError]:
    """Slug / SKU already used by another product of the store: {field: error}."""
    errors: dict[str, FieldError] = {}
    clash = db.scalar(select(Product.id).where(Product.store_id == store.id, Product.slug == slug)) if slug else None
    if clash is not None and (current is None or clash != current.id):
        errors["slug"] = ("slug_taken", {})
    owner = sku_owner(db, store, sku, current) if sku else None
    if owner is not None:
        errors["sku"] = ("sku_taken", {"sku": sku, "name": owner.name, "slug": owner.slug})
    return errors


def _is_safe_image_url(value: str) -> bool:
    """http(s) URLs or site-relative paths only (no javascript:, data:, …)."""
    if value.startswith("/"):
        return not value.startswith("//") and "\\" not in value
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def ref_choices(
    categories: list[Category], definition: PropertyDefinition
) -> list[tuple[Category, int]]:
    """Categories a category_ref value may point to: the referenced category and its
    subcategories, or every category of the store when no reference is set."""
    tree = category_admin.category_tree(categories)
    if definition.ref_category_id is None:
        return tree
    allowed = {definition.ref_category_id} | category_admin.descendant_ids(categories, definition.ref_category_id)
    return [(c, d) for c, d in tree if c.id in allowed]


def value_to_form(definition: PropertyDefinition, stored: Optional[dict[str, Any]]) -> str:
    """Stored JSON payload -> string shown in the form field."""
    if not stored:
        return ""
    if definition.prop_type == PropertyType.image:
        return str(stored.get("media_path") or "")
    if definition.prop_type == PropertyType.category_ref:
        ref = stored.get("category_id")
        return "" if ref is None else str(ref)
    value = stored.get("value")
    if value is None:
        return ""
    if definition.prop_type == PropertyType.boolean:
        return "true" if value else "false"
    return str(value)


def parse_value(
    definition: PropertyDefinition, raw: str, categories: list[Category]
) -> tuple[Optional[dict[str, Any]], Optional[FieldError]]:
    """Form string -> (JSON payload or None for "no value", error or None)."""
    raw = raw.strip()
    if not raw:
        if definition.required:
            return None, ("value_required", {"label": definition.label})
        return None, None

    kind = definition.prop_type
    if kind == PropertyType.text:
        if len(raw) > TEXT_VALUE_MAX:
            return None, ("value_too_long", {"max_len": TEXT_VALUE_MAX})
        return {"value": raw}, None

    if kind == PropertyType.number:
        # Length cap keeps floats finite and integers reasonable.
        if len(raw) > NUMBER_MAX_LEN or not NUMBER_RE.match(raw):
            return None, ("number_format", {})
        return {"value": int(raw) if "." not in raw else float(raw)}, None

    if kind == PropertyType.boolean:
        word = raw.lower()
        if word in TRUE_WORDS:
            return {"value": True}, None
        if word in FALSE_WORDS:
            return {"value": False}, None
        return None, ("boolean_choice", {})

    if kind == PropertyType.enum:
        options = [str(o) for o in (definition.config or {}).get("options") or []]
        if raw not in options:
            return None, ("enum_choice", {})
        return {"value": raw}, None

    if kind == PropertyType.image:
        if len(raw) > URL_MAX or not _is_safe_image_url(raw):
            return None, ("image_url", {})
        return {"media_path": raw}, None

    # category_ref
    try:
        ref_id = int(raw)
    except ValueError:
        ref_id = None
    if ref_id not in {c.id for c, _ in ref_choices(categories, definition)}:
        return None, ("category_choice", {})
    return {"category_id": ref_id}, None


# ------------------------------------------------------------------------------- form


@dataclass
class ProductForm:
    slug: str = ""
    sku: str = ""
    name: str = ""
    description: str = ""
    price: str = ""
    stock: str = "0"  # a new product starts at 0; the field must still be sent
    # The stock the edit form showed when it was opened (hidden field): saving an unchanged Stock
    # field never overwrites an adjustment made meanwhile (app/services/stock.py).
    stock_seen: str = ""
    # Own low-stock threshold (app/services/low_stock.py): "" = the store's; None = not sent (an
    # older client): the product keeps what it has.
    low_stock: Optional[str] = None
    status: str = STATUS_ACTIVE
    values: dict[str, str] = field(default_factory=dict)  # definition key -> raw string
    errors: dict[str, FieldError] = field(default_factory=dict)  # field name -> error code

    @classmethod
    def from_product(cls, product: Product) -> ProductForm:
        by_def = {pv.definition_id: pv.value for pv in product.property_values}
        return cls(
            slug=product.slug,
            sku=product.sku or "",  # a product without SKU yet (e.g. from a pack) must get one on save
            name=product.name,
            description=product.description or "",
            price=price_to_form(product.price_cents),
            stock=str(product.stock_on_hand),
            stock_seen=str(product.stock_on_hand),
            low_stock="" if product.low_stock_threshold is None else str(product.low_stock_threshold),
            status=STATUS_ACTIVE if product.is_active else STATUS_HIDDEN,
            values={
                d.key: value_to_form(d, by_def.get(d.id))
                for d in category_admin.sorted_definitions(product.category)
            },
        )

    @classmethod
    def from_post(cls, data: dict[str, str], category: Category) -> ProductForm:
        return cls(
            slug=data.get("slug", ""),
            sku=data.get("sku", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            price=data.get("price", ""),
            # Missing = error, never 0: edits replace every field, so a post without it
            # (e.g. an old curl call) must not wipe the stock.
            stock=data.get("stock", ""),
            stock_seen=data.get("stock_seen", ""),
            low_stock=data.get("low_stock"),
            status=data.get("status", STATUS_ACTIVE) or STATUS_ACTIVE,
            values={d.key: data.get(field_name(d), "") for d in category.property_definitions},
        )


def validate_product(
    db: Session,
    store: Store,
    category: Category,
    form: ProductForm,
    current: Optional[Product] = None,
) -> tuple[dict[str, Any], dict[int, Optional[dict[str, Any]]]]:
    """Returns (product column values, {definition_id: payload or None})."""
    errors = form.errors
    form.slug = form.slug.strip().lower()
    form.name = " ".join(form.name.split())
    form.description = form.description.strip()

    if not form.slug:
        errors["slug"] = ("slug_required", {})
    elif len(form.slug) > SLUG_MAX or not SLUG_RE.match(form.slug):
        errors["slug"] = ("slug_format", {"max_len": SLUG_MAX})

    form.sku = parse_sku(form.sku, errors)

    # Uniqueness only for values that are well-formed (a bad one already has its message).
    clashes = unique_clash_errors(
        db, store,
        slug=form.slug if "slug" not in errors else "",
        sku=form.sku if "sku" not in errors else "",
        current=current,
    )
    for name, error in clashes.items():
        errors.setdefault(name, error)

    if not form.name:
        errors["name"] = ("name_required", {})
    elif len(form.name) > NAME_MAX:
        errors["name"] = ("name_too_long", {"max_len": NAME_MAX})

    if len(form.description) > DESCRIPTION_MAX:
        errors["description"] = ("description_too_long", {"max_len": DESCRIPTION_MAX})

    price_cents = parse_price(form.price, errors)
    form.stock = form.stock.strip()
    stock_on_hand = parse_stock(form.stock, errors)
    low_stock_columns = {}
    if form.low_stock is not None:
        form.low_stock = form.low_stock.strip()
        threshold, threshold_error = low_stock.parse_threshold(form.low_stock, required=False)
        if threshold_error:
            errors["low_stock"] = threshold_error
        low_stock_columns["low_stock_threshold"] = threshold

    if form.status not in (STATUS_ACTIVE, STATUS_HIDDEN):
        errors["status"] = ("status_invalid", {})

    categories = category_admin.store_categories(db, store)
    payloads: dict[int, Optional[dict[str, Any]]] = {}
    for definition in category.property_definitions:
        payload, error = parse_value(definition, form.values.get(definition.key, ""), categories)
        if error:
            errors[field_name(definition)] = error
        payloads[definition.id] = payload

    columns = {
        "slug": form.slug,
        "sku": form.sku,
        "name": form.name,
        "description": form.description or None,
        "price_cents": price_cents,
        "stock_on_hand": stock_on_hand,
        "is_active": form.status == STATUS_ACTIVE,
        **low_stock_columns,
    }
    return columns, payloads


def parse_stock_seen(raw: str) -> Optional[int]:
    """The hidden ``stock_seen`` field: the stock the form showed, or None if absent / not a number."""
    raw = raw.strip()
    if not raw or not STOCK_RE.match(raw) or len(raw) > STOCK_MAX_LEN:
        return None
    return int(raw)


def save_product(
    db: Session,
    store: Store,
    category: Category,
    columns: dict[str, Any],
    payloads: dict[int, Optional[dict[str, Any]]],
    current: Optional[Product] = None,
    *,
    stock_seen: Optional[int] = None,
    user: Optional[User] = None,
) -> Optional[Product]:
    """Create or update a product and its property values. None if the slug or SKU was taken
    concurrently (see ``unique_clash_errors`` for which).

    The stock is logged as a movement: a new product's initial stock, or a changed Stock field
    (:func:`app.services.stock.set_stock_from_form`, which raises ``StockError("stock_changed")``
    when an adjustment happened since the form was opened; nothing is saved then)."""
    columns = dict(columns)
    stock = columns.pop("stock_on_hand")
    product = current or Product(store_id=store.id, category_id=category.id, stock_on_hand=stock)
    for name, value in columns.items():
        setattr(product, name, value)
    existing = {pv.definition_id: pv for pv in product.property_values}
    for definition_id, payload in payloads.items():
        pv = existing.get(definition_id)
        if payload is None:
            if pv is not None:
                product.property_values.remove(pv)  # delete-orphan cascade removes the row
        elif pv is not None:
            pv.value = payload  # new dict object -> change detected
        else:
            product.property_values.append(PropertyValue(definition_id=definition_id, value=payload))
    db.add(product)
    try:
        db.flush()  # the product's id (new) / its columns (edit), before its stock
        if current is None:
            if stock > 0:
                stock_svc.record_movement(db, product, StockMovementKind.initial, stock, stock, user=user)
        else:
            stock_svc.set_stock_from_form(db, product, stock, stock_seen, user=user)
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    except stock_svc.StockError:
        db.rollback()
        raise
    db.refresh(product)
    return product


def delete_product(db: Session, product: Product) -> None:
    db.delete(product)  # ORM cascade deletes its property values
    db.commit()
