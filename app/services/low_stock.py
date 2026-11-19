"""Low stock: a simple threshold, at two levels.

- **Store**: ``Store.low_stock_threshold`` (units, default ``DEFAULT_LOW_STOCK_THRESHOLD``), set on the
  admin page Inventory -> Low stock.
- **Product**: ``Product.low_stock_threshold``, optional (product form); empty = the store's.

A product is **low** when its stock on hand is at or below its threshold, and **out of stock** at 0
(always listed, whatever the threshold: 0 units is never "enough"). Hidden products are listed too
(marked): their stock is still the store's. Nothing is sent or blocked: it is a list to restock from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import Product, Store
from app.services.store_admin import FieldError

THRESHOLD_MAX = 1_000_000  # units; far more than any shelf
THRESHOLD_RE = re.compile(r"^[0-9]{1,7}$")  # ASCII digits only (no signs, separators or decimals)


@dataclass(frozen=True)
class LowStockRow:
    product: Product
    threshold: int  # the one that applies
    own_threshold: bool  # True: the product's own; False: the store's

    @property
    def out_of_stock(self) -> bool:
        return self.product.stock_on_hand <= 0


def effective_threshold(product: Product, store: Store) -> int:
    return product.low_stock_threshold if product.low_stock_threshold is not None else store.low_stock_threshold


def is_low(product: Product, store: Store) -> bool:
    return product.stock_on_hand <= effective_threshold(product, store)


def parse_threshold(raw: str, *, required: bool) -> tuple[Optional[int], Optional[FieldError]]:
    """Form value -> units. Empty: None (allowed only when not ``required``: the product form,
    where empty means "the store's")."""
    raw = (raw or "").strip()
    if not raw:
        return None, (("threshold_required", {}) if required else None)
    if raw.startswith("-") and THRESHOLD_RE.match(raw[1:]):
        return None, ("threshold_negative", {})
    if not THRESHOLD_RE.match(raw) or int(raw) > THRESHOLD_MAX:
        return None, ("threshold_format", {"max": THRESHOLD_MAX})
    return int(raw), None


def low_stock_condition(store: Store):
    """SQL condition: the product's stock is at or below the threshold that applies to it."""
    threshold = func.coalesce(Product.low_stock_threshold, store.low_stock_threshold)
    return or_(Product.stock_on_hand <= 0, Product.stock_on_hand <= threshold)


def low_stock_rows(db: Session, store: Store) -> list[LowStockRow]:
    """The store's low products: out of stock first, then the fewest units, then by name."""
    products = db.scalars(
        select(Product)
        .where(Product.store_id == store.id, low_stock_condition(store))
        .options(selectinload(Product.category))
        .order_by(Product.stock_on_hand, Product.name, Product.id)
    )
    return [LowStockRow(p, effective_threshold(p, store), p.low_stock_threshold is not None) for p in products]


def count_low(db: Session, store: Store) -> int:
    return db.scalar(select(func.count()).select_from(Product).where(Product.store_id == store.id,
                                                                     low_stock_condition(store)))


def set_store_threshold(db: Session, store: Store, units: int) -> None:
    store.low_stock_threshold = units
    db.commit()
