"""Admin-side management of categories and property definitions for one store.

Forms keep the raw submitted strings (so they can be re-rendered on error); ``validate_*``
functions fill ``form.errors`` with error **codes** (``(code, params)``) and return cleaned values
ready to save. The admin router shows the codes in the page language (``admin.form.error.<code>``,
see app/routers/admin_form_errors.py).

Deletes clean up dependent rows explicitly instead of relying on SQLite ``ON DELETE`` rules,
because the app engine does not enable ``PRAGMA foreign_keys``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db import fits_db_integer
from app.models import Category, Product, PropertyDefinition, PropertyType, PropertyValue, Store
from app.services.store_admin import SLUG_MAX, SLUG_RE, FieldError

NAME_MAX = 200
DESCRIPTION_MAX = 2000
KEY_MAX = 64
KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
SORT_MIN, SORT_MAX = -1_000_000, 1_000_000
OPTION_MAX_LEN = 100
OPTIONS_MAX = 200

PROPERTY_TYPE_LABELS: dict[PropertyType, str] = {
    PropertyType.text: "Text",
    PropertyType.number: "Number",
    PropertyType.boolean: "Yes / No (boolean)",
    PropertyType.enum: "Choice list (enum)",
    PropertyType.image: "Image",
    PropertyType.category_ref: "Category reference",
}


def _clean_text(value: str) -> str:
    return " ".join(value.split())


def _parse_sort_order(raw: str, errors: dict[str, FieldError]) -> int:
    raw = raw.strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        errors["sort_order"] = ("sort_format", {})
        return 0
    if not SORT_MIN <= value <= SORT_MAX:
        errors["sort_order"] = ("sort_range", {"min": f"{SORT_MIN:,}", "max": f"{SORT_MAX:,}"})
    return value


def _parse_optional_id(raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return -1  # never a real id -> reported as "not found" by callers


# --------------------------------------------------------------------------- categories


def store_categories(db: Session, store: Store) -> list[Category]:
    return list(
        db.scalars(
            select(Category)
            .where(Category.store_id == store.id)
            .options(selectinload(Category.property_definitions))
            .order_by(Category.sort_order, Category.name, Category.id)
        ).all()
    )


def get_category(db: Session, store: Store, category_id: int) -> Optional[Category]:
    """Category by id, only if it belongs to ``store``."""
    if not fits_db_integer(category_id):
        return None
    return db.scalar(
        select(Category)
        .where(Category.id == category_id, Category.store_id == store.id)
        .options(selectinload(Category.property_definitions))
    )


def category_tree(categories: list[Category]) -> list[tuple[Category, int]]:
    """Depth-first (category, depth) list. Categories whose parent is missing are treated as roots."""
    ids = {c.id for c in categories}
    children: dict[Optional[int], list[Category]] = {}
    for cat in categories:
        parent = cat.parent_id if cat.parent_id in ids else None
        children.setdefault(parent, []).append(cat)

    ordered: list[tuple[Category, int]] = []
    seen: set[int] = set()

    # Each category sits in exactly one children list and cycles are unreachable from the
    # roots, so the walk always terminates and visits every category at most once.
    def walk(parent_id: Optional[int], depth: int) -> None:
        for cat in children.get(parent_id, []):
            seen.add(cat.id)
            ordered.append((cat, depth))
            walk(cat.id, depth + 1)

    walk(None, 0)
    # Anything unreachable (a cycle in existing data) is still listed.
    ordered.extend((c, 0) for c in categories if c.id not in seen)
    return ordered


def descendant_ids(categories: list[Category], category_id: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for cat in categories:
        if cat.parent_id is not None:
            children.setdefault(cat.parent_id, []).append(cat.id)
    found: set[int] = set()
    stack = [category_id]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def parent_choices(categories: list[Category], current: Optional[Category]) -> list[tuple[Category, int]]:
    """Categories that may be chosen as parent of ``current`` (not itself, not its descendants)."""
    excluded: set[int] = set()
    if current is not None:
        excluded = {current.id} | descendant_ids(categories, current.id)
    return [(c, d) for c, d in category_tree(categories) if c.id not in excluded]


@dataclass
class CategoryForm:
    slug: str = ""
    name: str = ""
    description: str = ""
    parent_id: str = ""
    sort_order: str = "0"
    errors: dict[str, FieldError] = field(default_factory=dict)

    @classmethod
    def from_category(cls, category: Category) -> CategoryForm:
        return cls(
            slug=category.slug,
            name=category.name,
            description=category.description or "",
            parent_id=str(category.parent_id) if category.parent_id else "",
            sort_order=str(category.sort_order),
        )


def validate_category(
    db: Session, store: Store, form: CategoryForm, current: Optional[Category] = None
) -> dict[str, Any]:
    errors = form.errors
    form.slug = form.slug.strip().lower()
    form.name = _clean_text(form.name)
    form.description = form.description.strip()

    if not form.slug:
        errors["slug"] = ("slug_required", {})
    elif len(form.slug) > SLUG_MAX or not SLUG_RE.match(form.slug):
        errors["slug"] = ("slug_format", {"max_len": SLUG_MAX})
    else:
        clash = db.scalar(
            select(Category.id).where(Category.store_id == store.id, Category.slug == form.slug)
        )
        if clash is not None and (current is None or clash != current.id):
            errors["slug"] = ("category_slug_taken", {})

    if not form.name:
        errors["name"] = ("name_required", {})
    elif len(form.name) > NAME_MAX:
        errors["name"] = ("name_too_long", {"max_len": NAME_MAX})

    if len(form.description) > DESCRIPTION_MAX:
        errors["description"] = ("description_too_long", {"max_len": DESCRIPTION_MAX})

    parent_id = _parse_optional_id(form.parent_id)
    if parent_id is not None:
        categories = store_categories(db, store)
        allowed = {c.id for c, _ in parent_choices(categories, current)}
        if parent_id not in allowed:
            if current is not None and (
                parent_id == current.id or parent_id in descendant_ids(categories, current.id)
            ):
                errors["parent_id"] = ("parent_cycle", {})
            else:
                errors["parent_id"] = ("parent_invalid", {})

    sort_order = _parse_sort_order(form.sort_order, errors)
    return {
        "slug": form.slug,
        "name": form.name,
        "description": form.description or None,
        "parent_id": parent_id,
        "sort_order": sort_order,
    }


def save_category(
    db: Session, store: Store, values: dict[str, Any], current: Optional[Category] = None
) -> Optional[Category]:
    """Create or update. Returns None if a unique constraint was hit concurrently."""
    category = current or Category(store_id=store.id)
    for name, value in values.items():
        setattr(category, name, value)
    db.add(category)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(category)
    return category


def product_counts(db: Session, store: Store) -> dict[int, int]:
    """{category_id: number of products (active and hidden)} for a store."""
    rows = db.execute(
        select(Product.category_id, func.count()).where(Product.store_id == store.id).group_by(Product.category_id)
    ).all()
    return {category_id: count for category_id, count in rows}


def category_delete_blockers(db: Session, category: Category) -> list[FieldError]:
    """Why the category can't be deleted yet, as codes (shown like the form errors)."""
    blockers: list[FieldError] = []
    products = db.scalar(select(func.count()).select_from(Product).where(Product.category_id == category.id))
    if products:
        blockers.append(("has_products", {"count": products}))
    children = db.scalar(select(func.count()).select_from(Category).where(Category.parent_id == category.id))
    if children:
        blockers.append(("has_subcategories", {"count": children}))
    return blockers


def delete_category(db: Session, category: Category) -> None:
    """Delete a category and its property definitions. Callers must check blockers first."""
    def_ids = [d.id for d in category.property_definitions]
    if def_ids:
        db.execute(delete(PropertyValue).where(PropertyValue.definition_id.in_(def_ids)))
    # Other definitions that referenced this category lose the reference (schema says SET NULL).
    db.execute(
        update(PropertyDefinition)
        .where(PropertyDefinition.ref_category_id == category.id)
        .values(ref_category_id=None)
    )
    db.delete(category)  # ORM cascade removes its property definitions
    db.commit()


# ------------------------------------------------------------------ property definitions


def get_definition(db: Session, category: Category, definition_id: int) -> Optional[PropertyDefinition]:
    """Definition by id, only if it belongs to ``category``."""
    if not fits_db_integer(definition_id):
        return None
    return db.scalar(
        select(PropertyDefinition).where(
            PropertyDefinition.id == definition_id, PropertyDefinition.category_id == category.id
        )
    )


def sorted_definitions(category: Category) -> list[PropertyDefinition]:
    return sorted(category.property_definitions, key=lambda d: (d.sort_order, d.id))


def count_values(db: Session, definition: PropertyDefinition) -> int:
    return db.scalar(
        select(func.count()).select_from(PropertyValue).where(PropertyValue.definition_id == definition.id)
    )


@dataclass
class PropertyForm:
    key: str = ""
    label: str = ""
    prop_type: str = PropertyType.text.value
    required: bool = False
    sort_order: str = "0"
    options: str = ""  # enum options, one per line
    ref_category_id: str = ""
    errors: dict[str, FieldError] = field(default_factory=dict)

    @classmethod
    def from_definition(cls, definition: PropertyDefinition) -> PropertyForm:
        options = (definition.config or {}).get("options") or []
        return cls(
            key=definition.key,
            label=definition.label,
            prop_type=definition.prop_type.value,
            required=bool(definition.required),
            sort_order=str(definition.sort_order),
            options="\n".join(str(o) for o in options),
            ref_category_id=str(definition.ref_category_id) if definition.ref_category_id else "",
        )


def _parse_options(raw: str, errors: dict[str, FieldError]) -> list[str]:
    options = [line.strip() for line in raw.splitlines() if line.strip()]
    if not options:
        errors["options"] = ("options_required", {})
    elif len(options) > OPTIONS_MAX:
        errors["options"] = ("options_too_many", {"max": OPTIONS_MAX})
    elif any(len(o) > OPTION_MAX_LEN for o in options):
        errors["options"] = ("option_too_long", {"max_len": OPTION_MAX_LEN})
    else:
        seen: set[str] = set()
        dupes = [o for o in options if o in seen or seen.add(o)]
        if dupes:
            errors["options"] = ("option_duplicate", {"option": dupes[0]})
    return options


def validate_property(
    db: Session,
    store: Store,
    category: Category,
    form: PropertyForm,
    current: Optional[PropertyDefinition] = None,
) -> dict[str, Any]:
    errors = form.errors
    form.key = form.key.strip().lower()
    form.label = _clean_text(form.label)

    if not form.key:
        errors["key"] = ("key_required", {})
    elif len(form.key) > KEY_MAX or not KEY_RE.match(form.key):
        errors["key"] = ("key_format", {"max_len": KEY_MAX})
    else:
        clash = db.scalar(
            select(PropertyDefinition.id).where(
                PropertyDefinition.category_id == category.id, PropertyDefinition.key == form.key
            )
        )
        if clash is not None and (current is None or clash != current.id):
            errors["key"] = ("key_taken", {})

    if not form.label:
        errors["label"] = ("label_required", {})
    elif len(form.label) > NAME_MAX:
        errors["label"] = ("label_too_long", {"max_len": NAME_MAX})

    try:
        prop_type: Optional[PropertyType] = PropertyType(form.prop_type)
    except ValueError:
        prop_type = None
        errors["prop_type"] = ("type_invalid", {})

    if (
        prop_type is not None
        and current is not None
        and prop_type != current.prop_type
        and count_values(db, current)
    ):
        errors["prop_type"] = ("type_locked", {})

    config: dict[str, Any] = {}
    if prop_type == PropertyType.enum:
        config["options"] = _parse_options(form.options, errors)

    ref_category_id = None
    if prop_type == PropertyType.category_ref:
        ref_category_id = _parse_optional_id(form.ref_category_id)
        if ref_category_id is not None and get_category(db, store, ref_category_id) is None:
            errors["ref_category_id"] = ("ref_category_invalid", {})

    sort_order = _parse_sort_order(form.sort_order, errors)
    return {
        "key": form.key,
        "label": form.label,
        "prop_type": prop_type,
        "required": bool(form.required),
        "sort_order": sort_order,
        "config": config,
        "ref_category_id": ref_category_id,
    }


def save_property(
    db: Session,
    category: Category,
    values: dict[str, Any],
    current: Optional[PropertyDefinition] = None,
) -> Optional[PropertyDefinition]:
    definition = current or PropertyDefinition(category_id=category.id)
    for name, value in values.items():
        setattr(definition, name, value)
    db.add(definition)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    db.refresh(definition)
    return definition


def delete_property(db: Session, definition: PropertyDefinition) -> None:
    """Delete a definition and every product value stored for it."""
    db.execute(delete(PropertyValue).where(PropertyValue.definition_id == definition.id))
    db.delete(definition)
    db.commit()
