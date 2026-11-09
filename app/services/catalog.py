from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Category, Product, PropertyDefinition, PropertyValue, Store
from app.schemas import (
    CategoryOut,
    ProductListItem,
    ProductOut,
    PropertyDefinitionOut,
    PropertyValueOut,
    StoreOut,
)


def list_stores(db: Session) -> list[StoreOut]:
    stores = db.scalars(select(Store).order_by(Store.name)).all()
    return [StoreOut.model_validate(s) for s in stores]


def get_store_by_slug(db: Session, slug: str) -> Optional[Store]:
    return db.scalar(select(Store).where(Store.slug == slug))


def list_categories(db: Session, store: Store) -> list[CategoryOut]:
    cats = db.scalars(
        select(Category)
        .where(Category.store_id == store.id)
        .options(selectinload(Category.property_definitions))
        .order_by(Category.sort_order, Category.name)
    ).all()
    result: list[CategoryOut] = []
    for cat in cats:
        defs = sorted(cat.property_definitions, key=lambda d: (d.sort_order, d.id))
        result.append(
            CategoryOut(
                id=cat.id,
                slug=cat.slug,
                name=cat.name,
                description=cat.description,
                sort_order=cat.sort_order,
                parent_id=cat.parent_id,
                property_definitions=[PropertyDefinitionOut.model_validate(d) for d in defs],
            )
        )
    return result


def get_category_by_slug(db: Session, store: Store, slug: str) -> Optional[Category]:
    return db.scalar(
        select(Category)
        .where(Category.store_id == store.id, Category.slug == slug)
        .options(selectinload(Category.property_definitions))
    )


def list_products(
    db: Session,
    store: Store,
    category_id: Optional[int] = None,
    active_only: bool = True,
) -> list[ProductListItem]:
    stmt = select(Product).where(Product.store_id == store.id)
    if category_id is not None:
        stmt = stmt.where(Product.category_id == category_id)
    if active_only:
        stmt = stmt.where(Product.is_active.is_(True))
    stmt = stmt.order_by(Product.name)
    products = db.scalars(stmt).all()
    return [ProductListItem.model_validate(p) for p in products]


def _product_properties(product: Product) -> list[PropertyValueOut]:
    by_def: dict[int, PropertyValue] = {pv.definition_id: pv for pv in product.property_values}
    defs: list[PropertyDefinition] = sorted(
        product.category.property_definitions,
        key=lambda d: (d.sort_order, d.id),
    )
    out: list[PropertyValueOut] = []
    for definition in defs:
        pv = by_def.get(definition.id)
        out.append(
            PropertyValueOut(
                key=definition.key,
                label=definition.label,
                prop_type=definition.prop_type,
                value=pv.value if pv else {},
            )
        )
    return out


def get_product_by_slug(db: Session, store: Store, slug: str) -> Optional[ProductOut]:
    product = db.scalar(
        select(Product)
        .where(Product.store_id == store.id, Product.slug == slug)
        .options(
            selectinload(Product.property_values),
            selectinload(Product.category).selectinload(Category.property_definitions),
        )
    )
    if product is None:
        return None
    return ProductOut(
        id=product.id,
        slug=product.slug,
        name=product.name,
        description=product.description,
        price_cents=product.price_cents,
        is_active=product.is_active,
        category_id=product.category_id,
        properties=_product_properties(product),
    )


def format_price(cents: int, currency: str) -> str:
    amount = cents / 100
    return f"{currency} {amount:,.2f}"
