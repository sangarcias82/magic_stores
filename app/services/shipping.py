"""Shipping methods per store and flat-rate quotes.

A quote for (store, method code, subtotal, country) is:

- rejected if the code is not one of the store's methods (``unknown_method``) or the method is
  inactive (``inactive_method``);
- rejected if the method only ships to some countries and the country is missing
  (``country_required``) or not one of them (``country_not_served``);
- otherwise ``rate_cents``, or 0 when the method has a ``free_shipping_threshold_cents`` and the
  subtotal is at least that.

Amounts are integer cents in the store's currency. The HTTP API is app/routers/shipping.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ShippingMethod, Store


class ShippingQuoteError(Exception):
    """A quote that can't be given. ``kind`` is a stable code for API clients, ``field`` the input
    at fault (``method`` or ``country``)."""

    def __init__(self, kind: str, field: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.field = field
        self.message = message


@dataclass(frozen=True)
class ShippingQuote:
    method: ShippingMethod
    country: Optional[str]
    subtotal_cents: Optional[int]
    amount_cents: int
    free_shipping: bool


def list_active_methods(db: Session, store: Store) -> list[ShippingMethod]:
    return list(
        db.scalars(
            select(ShippingMethod)
            .where(ShippingMethod.store_id == store.id, ShippingMethod.is_active.is_(True))
            .order_by(ShippingMethod.sort_order, ShippingMethod.name, ShippingMethod.id)
        )
    )


def get_method(db: Session, store: Store, code: str) -> Optional[ShippingMethod]:
    """The store's method with this code, active or not."""
    return db.scalar(select(ShippingMethod).where(ShippingMethod.store_id == store.id, ShippingMethod.code == code))


def quote(
    db: Session,
    store: Store,
    code: str,
    subtotal_cents: Optional[int] = None,
    country: Optional[str] = None,
) -> ShippingQuote:
    """Flat-rate quote for one of the store's methods. Raises ``ShippingQuoteError``."""
    method = get_method(db, store, code)
    if method is None:
        raise ShippingQuoteError("unknown_method", "method", f"Unknown shipping method '{code}' for this store.")
    if not method.is_active:
        raise ShippingQuoteError("inactive_method", "method", f"Shipping method '{code}' is not available.")

    country = country.strip().upper() if country else None
    allowed = {c.upper() for c in (method.countries or [])}
    if allowed:
        if country is None:
            raise ShippingQuoteError(
                "country_required", "country", f"Shipping method '{code}' needs a destination country."
            )
        if country not in allowed:
            raise ShippingQuoteError(
                "country_not_served", "country", f"Shipping method '{code}' does not ship to {country}."
            )

    threshold = method.free_shipping_threshold_cents
    free = threshold is not None and subtotal_cents is not None and subtotal_cents >= threshold
    return ShippingQuote(
        method=method,
        country=country,
        subtotal_cents=subtotal_cents,
        amount_cents=0 if free else method.rate_cents,
        free_shipping=free,
    )
