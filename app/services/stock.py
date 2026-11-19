"""Stock on hand and its movement log (``StockMovement``).

Every change of ``Product.stock_on_hand`` made by the app goes through here and adds a movement in
the same transaction:

- :func:`adjust_stock`: an admin adds or removes units (``delta``) with a short reason. The change
  is one conditional ``UPDATE`` (``... WHERE stock_on_hand + delta >= 0``), so a change that would go
  below zero is refused even when two admins adjust the same product at the same moment.
- :func:`set_stock`: set a number, only if the stock is still what the caller saw (the product
  form's Stock field, an inventory CSV import row), so it never overwrites a change made meanwhile.
- :func:`record_movement`: for callers that set the stock themselves (a new product, a pack import).

Validation errors are codes ``(code, params)`` like the product form's; the admin router shows
them in the page language (``admin.stock.error.<code>``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.models import Product, StockMovement, StockMovementKind, User
from app.services.store_admin import FieldError

STOCK_MAX = 2_147_483_647  # a plain INTEGER column on any database
STOCK_RE = re.compile(r"^-?[0-9]+$")  # ASCII digits only: int() would also take "1_000" or "١٢"
STOCK_MAX_LEN = 20
DELTA_RE = re.compile(r"^[+-]?[0-9]+$")  # an adjustment may be written "+5"
REASON_MAX = 200  # "a short reason": one line
HISTORY_LIMIT = 50  # movements shown on the product page (newest first)


class StockError(Exception):
    """A stock change that can't be applied (nothing was written). ``error`` is ``(code, params)``."""

    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.error: FieldError = (code, params)


# ------------------------------------------------------------------------------ the form


@dataclass
class AdjustmentForm:
    """The "adjust stock" form: a signed whole number and why."""

    delta: str = ""
    reason: str = ""
    errors: dict[str, FieldError] = field(default_factory=dict)


def parse_adjustment(form: AdjustmentForm) -> tuple[int, str]:
    """Checks the form (fills ``form.errors``); returns (delta, reason) cleaned."""
    form.delta = form.delta.strip()
    form.reason = " ".join(form.reason.split())
    delta = 0
    if not form.delta:
        form.errors["delta"] = ("delta_required", {})
    elif not DELTA_RE.match(form.delta):
        form.errors["delta"] = ("delta_format", {})
    elif len(form.delta.lstrip("+-")) > STOCK_MAX_LEN:
        form.errors["delta"] = ("delta_too_large", {})
    else:
        delta = int(form.delta)
        if delta == 0:
            form.errors["delta"] = ("delta_zero", {})
        elif abs(delta) > STOCK_MAX:
            form.errors["delta"] = ("delta_too_large", {})
    if not form.reason:
        form.errors["reason"] = ("reason_required", {})
    elif len(form.reason) > REASON_MAX:
        form.errors["reason"] = ("reason_too_long", {"max_len": REASON_MAX})
    return delta, form.reason


# ------------------------------------------------------------------------------ changes


def record_movement(
    db: Session,
    product: Product,
    kind: StockMovementKind,
    delta: int,
    quantity_after: int,
    *,
    reason: Optional[str] = None,
    user: Optional[User] = None,
) -> StockMovement:
    """Adds a movement row (not committed): for callers that changed the stock themselves."""
    movement = StockMovement(
        store_id=product.store_id,
        product_id=product.id,
        kind=kind,
        delta=delta,
        quantity_after=quantity_after,
        reason=reason,
        user_id=user.id if user is not None else None,
    )
    db.add(movement)
    return movement


def _current_stock(db: Session, product_id: int) -> int:
    return db.scalar(select(Product.stock_on_hand).where(Product.id == product_id))


def adjust_stock(db: Session, product: Product, delta: int, reason: str, user: Optional[User] = None) -> StockMovement:
    """Adds ``delta`` units (negative = removes) and logs it; committed. Raises :class:`StockError`
    (``below_zero`` / ``too_large``, with the current stock) and writes nothing if the result would
    be out of range, whatever other adjustments happened meanwhile."""
    if delta == 0:
        raise StockError("delta_zero")
    new_stock = Product.stock_on_hand + delta
    result = db.execute(
        update(Product)
        .where(Product.id == product.id, new_stock >= 0, new_stock <= STOCK_MAX)
        .values(stock_on_hand=new_stock)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        current = _current_stock(db, product.id)
        code = "below_zero" if current + delta < 0 else "too_large"
        raise StockError(code, current=current, delta=f"{delta:+d}", result=current + delta)
    after = _current_stock(db, product.id)
    movement = record_movement(db, product, StockMovementKind.adjustment, delta, after, reason=reason, user=user)
    db.commit()
    db.refresh(product)
    return movement


def set_stock(
    db: Session,
    product: Product,
    new_stock: int,
    seen: Optional[int],
    kind: StockMovementKind,
    *,
    reason: Optional[str] = None,
    user: Optional[User] = None,
) -> Optional[StockMovement]:
    """Sets the stock to ``new_stock`` and logs it (not committed: part of the caller's transaction).

    ``seen`` is the stock the caller based its number on (the form when it was opened, the CSV
    when it was exported). ``new_stock == seen``: nothing to do (None), so a change made meanwhile
    is kept. Otherwise it is set only if the stock is still ``seen`` (one conditional UPDATE); if it
    changed, :class:`StockError` ``stock_changed`` (with the current stock) and nothing is written,
    unless it is already ``new_stock`` (nothing to do: None). ``seen`` None: based on the current
    stock (set as given)."""
    if seen is None:
        seen = _current_stock(db, product.id)
    if new_stock == seen:
        return None
    result = db.execute(
        update(Product)
        .where(Product.id == product.id, Product.stock_on_hand == seen)
        .values(stock_on_hand=new_stock)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        current = _current_stock(db, product.id)
        if current == new_stock:  # someone (or an earlier import) already set it: no conflict
            return None
        raise StockError("stock_changed", current=current)
    return record_movement(db, product, kind, new_stock - seen, new_stock, reason=reason, user=user)


def set_stock_from_form(
    db: Session, product: Product, new_stock: int, seen: Optional[int], user: Optional[User] = None
) -> Optional[StockMovement]:
    """The product form's Stock number (``seen``: the stock the form showed when it was opened;
    None for clients that don't send it). See :func:`set_stock`."""
    return set_stock(db, product, new_stock, seen, StockMovementKind.edit, user=user)


LOG_PAGE_SIZE = 50
LOG_REASON_FILTER_MAX = 200


def _like_escape(text: str) -> str:
    """Text for a LIKE pattern: its own %, _ and \\ match themselves."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def store_movements(
    db: Session,
    store_id: int,
    *,
    sku_contains: str = "",
    kind: Optional[StockMovementKind] = None,
    product_id: Optional[int] = None,
    reason_contains: str = "",
    page: int = 1,
) -> tuple[list[StockMovement], int]:
    """A page of the store's movement log, newest first, and how many movements match. Filters: the
    product's SKU contains ``sku_contains`` (ignoring case), the movement ``kind``, one product, the
    reason contains ``reason_contains`` (ignoring case of ASCII letters; ``%`` / ``_`` are plain text)."""
    conditions = [StockMovement.store_id == store_id]
    if kind is not None:
        conditions.append(StockMovement.kind == kind)
    if product_id is not None:
        conditions.append(StockMovement.product_id == product_id)
    term = sku_contains.strip().upper()
    if term:
        conditions.append(StockMovement.product_id.in_(
            select(Product.id).where(Product.store_id == store_id,
                                     Product.sku.like(f"%{_like_escape(term)}%", escape="\\"))
        ))
    reason = reason_contains.strip()
    if reason:
        conditions.append(StockMovement.reason.ilike(f"%{_like_escape(reason)}%", escape="\\"))
    total = db.scalar(select(func.count()).select_from(StockMovement).where(*conditions))
    movements = list(db.scalars(
        select(StockMovement)
        .where(*conditions)
        .options(selectinload(StockMovement.user), selectinload(StockMovement.product))
        .order_by(StockMovement.id.desc())
        .offset((page - 1) * LOG_PAGE_SIZE)
        .limit(LOG_PAGE_SIZE)
    ))
    return movements, total


def product_history(db: Session, product: Product, limit: int = HISTORY_LIMIT) -> tuple[list[StockMovement], int]:
    """(the newest ``limit`` movements, newest first; total number of movements)."""
    movements = list(db.scalars(
        select(StockMovement)
        .where(StockMovement.product_id == product.id)
        .options(selectinload(StockMovement.user))
        .order_by(StockMovement.id.desc())
        .limit(limit)
    ))
    total = db.scalar(select(func.count()).select_from(StockMovement).where(StockMovement.product_id == product.id))
    return movements, total
