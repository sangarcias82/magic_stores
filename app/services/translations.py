"""Translations, level 2: the ``translations`` table (level 1 = JSON catalogs in app/locales/).

Two kinds of rows (see ``app.models.Translation``):

UI overrides
    Replace the text of one JSON catalog key for one language, platform-wide (``store=None``)
    or only on one store's public pages. Resolution order (``app.i18n.lookup``)::

        store override -> platform override -> JSON catalog    (page language, then es)

    A language must have a JSON catalog to be used at all; the table only refines it.
    Keys and ``{placeholders}`` are checked against the Spanish catalog.

Content translations
    Per row and per field of the catalog data, in the page language::

        store               name
        category            name, description
        property_definition label, option:<enum option>
        product             name, description, value:<property key>   (text / enum values)
        shipping_method     name
        payment_terminal    name, note   (note: a bank transfer's note for the shopper)

    Resolution: the translation for the page language, else the text as entered.
    A product's enum value uses ``value:<key>`` on the product first, then the property's
    ``option:<option>``. The storefront shows translated content; admin and the JSON API
    show (and edit) the content as entered, except shipping method names, which the admin
    Shipping and Try payment pages show in the page language (the original next to them).

Writes validate their input and commit (``commit=False`` to join a larger transaction, as
the store pack import does). The admin "Translations" tab of a store uses
``translatable_fields`` / ``save_content_translations`` (content) and ``set_ui_translation`` /
``delete_ui_translation`` / ``store_ui_overrides`` (that store's UI overrides). Deleting a store / category / property / product / shipping method deletes its
translations (ORM hooks in ``app.models``).
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Union

from fastapi import Depends, Request
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app import i18n
from app.models import (
    Category,
    Product,
    PropertyDefinition,
    PropertyType,
    PaymentTerminal,
    PropertyValue,
    ShippingMethod,
    Store,
    Translation,
    TranslationEntity,
)
from app.schemas import CategoryOut, ProductListItem, ProductOut, PropertyDefinitionOut, StoreOut
from app.services.category_admin import DESCRIPTION_MAX as CATEGORY_DESCRIPTION_MAX
from app.services.bank_transfer import NOTE_MAX as TERMINAL_NOTE_MAX
from app.services.category_admin import NAME_MAX, OPTION_MAX_LEN
from app.services.product_admin import DESCRIPTION_MAX as PRODUCT_DESCRIPTION_MAX
from app.services.product_admin import TEXT_VALUE_MAX

TEXT_MAX = 5000  # UI overrides
OPTION_PREFIX = "option:"
VALUE_PREFIX = "value:"
# A content translation's language: a code like "en" or "pt-BR" (lower-case language part).
CONTENT_LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
CONTENT_FIELDS: dict[TranslationEntity, tuple[str, ...]] = {
    TranslationEntity.store: ("name",),
    TranslationEntity.category: ("name", "description"),
    TranslationEntity.property_definition: ("label", OPTION_PREFIX),
    TranslationEntity.product: ("name", "description", VALUE_PREFIX),
    TranslationEntity.shipping_method: ("name",),
    TranslationEntity.payment_terminal: ("name", "note"),
}
ENTITY_OF_MODEL = {
    Store: TranslationEntity.store,
    Category: TranslationEntity.category,
    PropertyDefinition: TranslationEntity.property_definition,
    Product: TranslationEntity.product,
    ShippingMethod: TranslationEntity.shipping_method,
    PaymentTerminal: TranslationEntity.payment_terminal,
}
Entity = Union[Store, Category, PropertyDefinition, Product, ShippingMethod, PaymentTerminal]


class TranslationError(ValueError):
    """Invalid translation input (unknown key, field, locale, placeholders, empty text).

    ``str(error)`` is an English message; ``code`` + ``params`` let a UI show its own
    (translated) message: "empty", "too_long" (max_len), "not_ui_language" (locale),
    "unknown_key" (key), "placeholders" (expected), "placeholder_syntax", "invalid"."""

    def __init__(self, message: str, code: str = "invalid", **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.params = params


def _placeholders(text: str) -> set[str]:
    try:
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}
    except ValueError as exc:  # e.g. a lone "{"
        raise TranslationError(f"invalid placeholder syntax: {exc}", "placeholder_syntax") from exc


def _clean_text(text: Any, max_len: int = TEXT_MAX) -> str:
    if not isinstance(text, str) or not text.strip():
        raise TranslationError("text must be a non-empty string", "empty")
    if len(text.strip()) > max_len:
        raise TranslationError(f"text must be at most {max_len} characters", "too_long", max_len=max_len)
    return text.strip()


def content_locale(locale: Any) -> str:
    """The language a content translation is stored under: the base code, lower-case
    ("en-US" -> "en", "pt-BR" -> "pt"). Pages are always in a base language (the JSON catalog
    codes), so a regional variant could never be shown; variants of one language are one language."""
    if not isinstance(locale, str) or not CONTENT_LOCALE_RE.match(locale.strip()):
        raise TranslationError('locale must be a language code like "en" or "pt-BR"')
    return locale.strip().split("-", 1)[0].lower()


def content_text_max(entity_type: TranslationEntity, field_name: str) -> int:
    """Same limit as the field it translates (admin form rules)."""
    if field_name.startswith(OPTION_PREFIX):
        return OPTION_MAX_LEN
    if field_name.startswith(VALUE_PREFIX):
        return TEXT_VALUE_MAX
    if field_name == "note":
        return TERMINAL_NOTE_MAX
    if field_name == "description":
        return CATEGORY_DESCRIPTION_MAX if entity_type == TranslationEntity.category else PRODUCT_DESCRIPTION_MAX
    return NAME_MAX  # name, label


# ------------------------------------------------------------------------ UI overrides


def check_ui_override(locale: str, key: str, text: Any) -> tuple[str, str]:
    """Validated (locale, text) for a UI override; raises :class:`TranslationError`."""
    code = i18n.normalize_locale(locale)
    if code is None:
        raise TranslationError(f'"{locale}" is not a UI language; add app/locales/<code>.json first',
                               "not_ui_language", locale=locale)
    reference = i18n.catalogs()[i18n.DEFAULT_LOCALE].get(key)
    if reference is None:
        raise TranslationError(f'unknown UI key "{key}" (not in app/locales/{i18n.DEFAULT_LOCALE}.json)',
                               "unknown_key", key=key)
    text = _clean_text(text)
    expected, got = _placeholders(reference), _placeholders(text)
    if got != expected:
        shown = ", ".join("{" + p + "}" for p in sorted(expected)) or "none"
        raise TranslationError(f"placeholders must be the same as the catalog's ({shown})", "placeholders",
                               expected=shown)
    return code, text


def _upsert(db: Session, *, store_id: Optional[int], locale: str, entity_type: TranslationEntity,
            entity_id: Optional[int], field_name: str, text: str) -> Translation:
    # Matched in code: the unique constraint can't catch NULL store_id / entity_id in SQLite.
    row = db.scalar(
        select(Translation).where(
            Translation.store_id.is_(None) if store_id is None else Translation.store_id == store_id,
            Translation.locale == locale,
            Translation.entity_type == entity_type,
            Translation.entity_id.is_(None) if entity_id is None else Translation.entity_id == entity_id,
            Translation.field == field_name,
        )
    )
    if row is None:
        row = Translation(store_id=store_id, locale=locale, entity_type=entity_type, entity_id=entity_id,
                          field=field_name, text=text)
        db.add(row)
    else:
        row.text = text
    return row


def set_ui_translation(db: Session, locale: str, key: str, text: str, store: Optional[Store] = None,
                       *, commit: bool = True) -> Translation:
    """Override the UI text of ``key`` in ``locale``: everywhere, or on ``store``'s pages."""
    code, text = check_ui_override(locale, key, text)
    row = _upsert(db, store_id=store.id if store else None, locale=code, entity_type=TranslationEntity.ui,
                  entity_id=None, field_name=key, text=text)
    if commit:
        db.commit()
    return row


def delete_ui_translation(db: Session, locale: str, key: str, store: Optional[Store] = None) -> bool:
    """Remove an override (the JSON catalog text applies again). True if one existed."""
    code = i18n.normalize_locale(locale) or locale
    result = db.execute(
        delete(Translation).where(
            Translation.entity_type == TranslationEntity.ui,
            Translation.store_id.is_(None) if store is None else Translation.store_id == store.id,
            Translation.locale == code,
            Translation.field == key,
        )
    )
    db.commit()
    return bool(result.rowcount)


def load_ui_overrides(db: Session, locale: str, store_id: Optional[int]) -> dict[str, str]:
    """{key: text} for ``locale``: platform overrides, replaced by the store's where both exist."""
    scope = Translation.store_id.is_(None)
    if store_id is not None:
        scope = or_(scope, Translation.store_id == store_id)
    rows = db.execute(
        select(Translation.store_id, Translation.field, Translation.text).where(
            Translation.entity_type == TranslationEntity.ui, Translation.locale == locale, scope
        )
    ).all()
    merged = {key: text for row_store, key, text in rows if row_store is None}
    merged.update({key: text for row_store, key, text in rows if row_store is not None})
    return merged


def bind_translations(request: Request, db: Session = Depends(get_db)) -> None:
    """App-wide dependency: plugs level 2 into ``app.i18n`` for this request (lazy: the table
    is only queried when a page or message is actually translated)."""
    request.state.ui_override_loader = lambda locale, store_id: load_ui_overrides(db, locale, store_id)


# ------------------------------------------------------------------ content translations


def _entity_type(entity: Entity) -> TranslationEntity:
    for model, entity_type in ENTITY_OF_MODEL.items():
        if isinstance(entity, model):
            return entity_type
    raise TranslationError(f"{type(entity).__name__} rows can't be translated")


def _store_id(entity: Entity) -> int:
    if isinstance(entity, Store):
        return entity.id
    if isinstance(entity, PropertyDefinition):
        return entity.category.store_id
    return entity.store_id


def check_content_field(entity_type: TranslationEntity, field_name: str) -> None:
    allowed = CONTENT_FIELDS[entity_type]
    names = [f for f in allowed if not f.endswith(":")]
    prefixes = [f for f in allowed if f.endswith(":")]
    ok = field_name in names or any(field_name.startswith(p) and len(field_name) > len(p) for p in prefixes)
    if not ok or len(field_name) > 200:
        shown = ", ".join(f + "<...>" if f.endswith(":") else f for f in allowed)
        raise TranslationError(f'{entity_type.value} has no translatable field "{field_name}" (allowed: {shown})')


def set_content_translation(db: Session, entity: Entity, locale: str, field_name: str, text: str,
                            *, commit: bool = True) -> Translation:
    """Translate one text of a catalog row, e.g. ``(product, "en", "name", "Blue widget")``.
    ``locale`` may be regional ("en-US"); it is stored as its language ("en")."""
    entity_type = _entity_type(entity)
    check_content_field(entity_type, field_name)
    text = _clean_text(text, content_text_max(entity_type, field_name))
    row = _upsert(db, store_id=_store_id(entity), locale=content_locale(locale), entity_type=entity_type,
                  entity_id=entity.id, field_name=field_name, text=text)
    if commit:
        db.commit()
    return row


def delete_content_translations(db: Session, entity: Entity, *, commit: bool = True) -> int:
    """Remove every translation of one row (all languages and fields). Returns how many."""
    result = db.execute(
        delete(Translation).where(Translation.entity_type == _entity_type(entity), Translation.entity_id == entity.id)
    )
    if commit:
        db.commit()
    return result.rowcount


@dataclass
class ContentTranslations:
    """The content translations of one language (and store), loaded once per page, with
    helpers that return translated *copies* of the catalog read models (never the DB rows)."""

    locale: str
    texts: dict[tuple[TranslationEntity, int, str], str] = field(default_factory=dict)

    @classmethod
    def load(cls, db: Session, locale: str, store_ids: Optional[Iterable[int]] = None) -> ContentTranslations:
        query = select(Translation.entity_type, Translation.entity_id, Translation.field, Translation.text).where(
            Translation.locale == locale, Translation.entity_type != TranslationEntity.ui
        )
        if store_ids is not None:
            query = query.where(Translation.store_id.in_(list(store_ids)))
        rows = db.execute(query).all()
        return cls(locale, {(entity_type, entity_id, name): text for entity_type, entity_id, name, text in rows})

    def text(self, entity_type: TranslationEntity, entity_id: int, field_name: str, default: Any) -> Any:
        return self.texts.get((entity_type, entity_id, field_name), default)

    def store(self, store: Union[Store, StoreOut]) -> StoreOut:
        out = store if isinstance(store, StoreOut) else StoreOut.model_validate(store)
        return out.model_copy(update={"name": self.text(TranslationEntity.store, out.id, "name", out.name)})

    def definition(self, definition: PropertyDefinitionOut) -> PropertyDefinitionOut:
        label = self.text(TranslationEntity.property_definition, definition.id, "label", definition.label)
        return definition.model_copy(update={"label": label})

    def category(self, category: CategoryOut) -> CategoryOut:
        kind = TranslationEntity.category
        return category.model_copy(update={
            "name": self.text(kind, category.id, "name", category.name),
            "description": self.text(kind, category.id, "description", category.description),
            "property_definitions": [self.definition(d) for d in category.property_definitions],
        })

    def categories(self, categories: list[CategoryOut]) -> list[CategoryOut]:
        return [self.category(c) for c in categories]

    def product_item(self, product: ProductListItem) -> ProductListItem:
        return product.model_copy(update={"name": self.text(TranslationEntity.product, product.id, "name", product.name)})

    def shipping_method_name(self, method: ShippingMethod) -> str:
        return self.text(TranslationEntity.shipping_method, method.id, "name", method.name)

    def terminal_name(self, terminal: PaymentTerminal) -> str:
        return self.text(TranslationEntity.payment_terminal, terminal.id, "name", terminal.name)

    def product_items(self, products: list[ProductListItem]) -> list[ProductListItem]:
        return [self.product_item(p) for p in products]

    def product(self, product: ProductOut, categories_by_id: dict[int, CategoryOut]) -> ProductOut:
        """Name, description, property labels and text / enum values. ``categories_by_id``
        (from ``catalog.list_categories``) maps the product's property keys to definitions."""
        kind = TranslationEntity.product
        category = categories_by_id.get(product.category_id)
        definitions = {d.key: d for d in category.property_definitions} if category else {}
        properties = []
        for prop in product.properties:
            definition = definitions.get(prop.key)
            update: dict[str, Any] = {}
            if definition is not None:
                update["label"] = self.text(TranslationEntity.property_definition, definition.id, "label", prop.label)
                raw = prop.value.get("value")
                if isinstance(raw, str) and prop.prop_type in (PropertyType.text, PropertyType.enum):
                    translated = self.text(kind, product.id, f"{VALUE_PREFIX}{prop.key}", None)
                    if translated is None and prop.prop_type == PropertyType.enum:
                        translated = self.text(
                            TranslationEntity.property_definition, definition.id, f"{OPTION_PREFIX}{raw}", None
                        )
                    if translated is not None:
                        update["value"] = {**prop.value, "value": translated}
            properties.append(prop.model_copy(update=update))
        return product.model_copy(update={
            "name": self.text(kind, product.id, "name", product.name),
            "description": self.text(kind, product.id, "description", product.description),
            "properties": properties,
        })


# --------------------------------------------------------------- admin editing (per store)


@dataclass(frozen=True)
class TranslatableField:
    """One text of a store's catalog that can be translated, with its text as entered."""

    entity: Any  # Store / Category / PropertyDefinition / Product row
    entity_type: TranslationEntity
    field: str  # "name", "description", "label", "option:<option>", "value:<key>"
    source: str  # the text as entered (what the storefront shows without a translation)
    group: str  # "store", "category", "property", "product" (for display)
    owner: str  # the row's own name (category / property label / product name), for display
    definition: Any = None  # for "value:<key>": the property definition (its label)

    @property
    def entity_id(self) -> int:
        return self.entity.id

    @property
    def form_name(self) -> str:
        """Form field name; the field comes last, so it may contain any character."""
        return f"tr|{self.entity_type.value}|{self.entity.id}|{self.field}"


def translatable_fields(db: Session, store: Store) -> list[TranslatableField]:
    """Every text of ``store`` that content translations can replace: store name; category names /
    descriptions; property labels and enum options; product names / descriptions and their text /
    enum values; shipping method names; payment terminal names and bank transfer notes. Empty texts
    are left out."""
    fields = [TranslatableField(store, TranslationEntity.store, "name", store.name, "store", store.name)]
    categories = list(db.scalars(
        select(Category).where(Category.store_id == store.id)
        .options(selectinload(Category.property_definitions))
        .order_by(Category.sort_order, Category.name, Category.id)
    ))
    for category in categories:
        fields.append(TranslatableField(category, TranslationEntity.category, "name", category.name, "category",
                                        category.name))
        if category.description:
            fields.append(TranslatableField(category, TranslationEntity.category, "description",
                                            category.description, "category", category.name))
    for category in categories:
        for definition in sorted(category.property_definitions, key=lambda d: (d.sort_order, d.id)):
            kind = TranslationEntity.property_definition
            fields.append(TranslatableField(definition, kind, "label", definition.label, "property", definition.label))
            if definition.prop_type == PropertyType.enum:
                for option in (definition.config or {}).get("options") or []:
                    fields.append(TranslatableField(definition, kind, f"{OPTION_PREFIX}{option}", str(option),
                                                    "property", definition.label))
    products = list(db.scalars(
        select(Product).where(Product.store_id == store.id)
        .options(selectinload(Product.property_values).selectinload(PropertyValue.definition))
        .order_by(Product.name, Product.id)
    ))
    for product in products:
        kind = TranslationEntity.product
        fields.append(TranslatableField(product, kind, "name", product.name, "product", product.name))
        if product.description:
            fields.append(TranslatableField(product, kind, "description", product.description, "product",
                                            product.name))
        for pv in sorted(product.property_values, key=lambda pv: (pv.definition.sort_order, pv.definition.id)):
            raw = (pv.value or {}).get("value")
            if pv.definition.prop_type in (PropertyType.text, PropertyType.enum) and isinstance(raw, str) and raw:
                fields.append(TranslatableField(product, kind, f"{VALUE_PREFIX}{pv.definition.key}", raw, "product",
                                                product.name, definition=pv.definition))
    methods = db.scalars(
        select(ShippingMethod).where(ShippingMethod.store_id == store.id)
        .order_by(ShippingMethod.sort_order, ShippingMethod.name, ShippingMethod.id)
    )
    for method in methods:
        fields.append(TranslatableField(method, TranslationEntity.shipping_method, "name", method.name, "shipping",
                                        method.code))
    terminals = db.scalars(
        select(PaymentTerminal).where(PaymentTerminal.store_id == store.id)
        .order_by(PaymentTerminal.sort_order, PaymentTerminal.code)
    )
    for terminal in terminals:
        kind = TranslationEntity.payment_terminal
        fields.append(TranslatableField(terminal, kind, "name", terminal.name, "terminal", terminal.code))
        note = (terminal.config or {}).get("note") if terminal.is_bank_transfer else None
        if note:
            fields.append(TranslatableField(terminal, kind, "note", note, "terminal", terminal.code))
    return fields


def delete_content_translation(db: Session, entity: Entity, locale: str, field_name: str,
                               *, commit: bool = True) -> bool:
    """Remove one translation (the text as entered shows again). True if it existed."""
    result = db.execute(
        delete(Translation).where(
            Translation.entity_type == _entity_type(entity), Translation.entity_id == entity.id,
            Translation.locale == content_locale(locale), Translation.field == field_name,
        )
    )
    if commit:
        db.commit()
    return bool(result.rowcount)


def save_content_translations(db: Session, store: Store, locale: str,
                              submitted: dict[str, str]) -> tuple[int, dict[str, TranslationError]]:
    """Apply an edit form of ``translatable_fields``: for each field present in ``submitted``
    (by ``form_name``), a non-empty text sets the translation and an empty one removes it.
    Fields that are not this store's are ignored. All or nothing: returns (changes, {}) after
    committing, or (0, {form_name: error}) after rolling everything back."""
    code = content_locale(locale)
    current = ContentTranslations.load(db, code, [store.id]).texts
    changes, errors = 0, {}
    for item in translatable_fields(db, store):
        if item.form_name not in submitted:
            continue
        new = submitted[item.form_name].strip()
        old = current.get((item.entity_type, item.entity_id, item.field), "")
        if new == old:
            continue
        try:
            if new:
                set_content_translation(db, item.entity, code, item.field, new, commit=False)
            else:
                delete_content_translation(db, item.entity, code, item.field, commit=False)
        except TranslationError as exc:
            errors[item.form_name] = exc
            continue
        changes += 1
    if errors:
        db.rollback()
        return 0, errors
    db.commit()
    return changes, {}


def store_ui_overrides(db: Session, store: Store, locale: str) -> dict[str, str]:
    """This store's own UI overrides for ``locale`` (without the platform ones), by key."""
    rows = db.execute(
        select(Translation.field, Translation.text).where(
            Translation.entity_type == TranslationEntity.ui, Translation.store_id == store.id,
            Translation.locale == locale,
        ).order_by(Translation.field)
    ).all()
    return {key: text for key, text in rows}
