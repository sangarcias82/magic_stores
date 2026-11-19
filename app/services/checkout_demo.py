"""Quote + pay, connected: the operator demo in admin ("Try payment", app/routers/admin_try_payment.py).

    subtotal + shipping method (+ country) --quote--> total = subtotal + shipping (store currency)
                                           --pay-->   mock payment of that total (pending)
                                           --succeed / fail (app/services/payments.py)

It only combines app/services/shipping.py and app/services/payments.py, the same code as the quote
and payments APIs. :func:`pay` quotes again instead of taking an amount from the browser, so the payment is always
for what the current shipping rules say. There are no orders yet: the payment doesn't remember the
subtotal / shipping split, only the total.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import MAX_AMOUNT_CENTS, Payment, ShippingMethod, Store
from app.services import payments, shipping


class CheckoutError(Exception):
    """Why a quote or a payment can't be made. ``kind`` is one of the shipping quote errors
    (``unknown_method``, ``inactive_method``, ``country_required``, ``country_not_served``), the
    payment errors (``unknown_terminal``, ``terminal_disabled``, ``invalid_amount``), or
    ``nothing_to_pay`` / ``total_too_large``. ``field`` is the input at fault."""

    def __init__(self, kind: str, field: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.kind = kind
        self.field = field
        self.params = params


@dataclass(frozen=True)
class Checkout:
    method: ShippingMethod
    country: Optional[str]
    currency: str  # the store's
    subtotal_cents: int
    shipping_cents: int
    free_shipping: bool

    @property
    def total_cents(self) -> int:
        return self.subtotal_cents + self.shipping_cents


def quote(db: Session, store: Store, method_code: str, subtotal_cents: int, country: Optional[str]) -> Checkout:
    try:
        result = shipping.quote(db, store, method_code, subtotal_cents=subtotal_cents, country=country)
    except shipping.ShippingQuoteError as exc:
        raise CheckoutError(exc.kind, exc.field, exc.message) from exc
    checkout = Checkout(
        method=result.method,
        country=result.country,
        currency=store.currency,
        subtotal_cents=subtotal_cents,
        shipping_cents=result.amount_cents,
        free_shipping=result.free_shipping,
    )
    if checkout.total_cents <= 0:
        raise CheckoutError("nothing_to_pay", "subtotal", "The total is 0: there is nothing to pay.")
    if checkout.total_cents > MAX_AMOUNT_CENTS:
        raise CheckoutError("total_too_large", "subtotal", "The total is larger than a payment can be.")
    return checkout


def pay(
    db: Session, store: Store, method_code: str, subtotal_cents: int, country: Optional[str], terminal_code: str
) -> tuple[Checkout, Payment]:
    """Quote again, then create a ``pending`` mock payment of the total through the terminal."""
    checkout = quote(db, store, method_code, subtotal_cents, country)
    try:
        payment = payments.create_payment(db, store, terminal_code, checkout.total_cents)
    except payments.PaymentError as exc:
        raise CheckoutError(exc.kind, exc.field, exc.message) from exc
    return checkout, payment
