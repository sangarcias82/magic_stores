"""Checkout: turn the visitor's cart into an order with a payment for its total.

The shopper picks a **shipping method** and a **payment terminal** separately, at checkout; the
cart never holds either. The existing services do the work:

- the quote is ``app.services.checkout_demo.quote`` (the shipping quote + the total checks used by
  admin "Try payment"), on the subtotal of the cart's lines;
- the payment is ``app.services.payments.create_payment`` (a ``pending`` mock payment, answered
  later by its terminal's signed callback or, for mock terminals, the admin simulator).

Steps, in this order (nothing is created if a check fails):

1. Every line is validated **now** (``cart.snapshot``): the product must still be sold, its stock
   must cover the quantity, and its price must be the one the cart shows. Otherwise: refused (each
   line's problem, or ``price_changed``; the cart now shows the new prices).
2. The quote (method, country) and the total; the terminal must exist and be enabled.
3. The payment of ``subtotal + shipping`` is created (``pending``).
4. In one transaction: the order and its line copies are written and the cart's lines removed.

**Stock is not taken at checkout** (nor while the payment is pending): step 1 only checks it. It is
taken when the payment **succeeds** (:func:`take_stock_for_payment`, called by
``payments.mark_payment`` in the same transaction as the status change, so it happens once, for the
terminal's signed callback and the admin simulator alike): one ``order`` stock movement per line.
A **failed** payment (declined / cancelled by its terminal) takes nothing, so no stock stays taken.
If a product sold out between checkout and payment, its line takes what is left (never below 0)
and records the rest as ``short_quantity``: the order is paid, an admin restocks or refunds.

After a checkout **the cart is empty** (its lines are in the order) and stays usable: the visitor
can keep shopping with the same cookie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Cart,
    CartLine,
    Order,
    OrderLine,
    Payment,
    Product,
    Store,
    StockMovementKind,
    utc_now,
)
from app.services import cart as cart_svc
from app.services import bank_transfer, checkout_demo, payments
from app.services import stock as stock_svc
from app.services.catalog import format_price

class CheckoutRefused(Exception):
    """The checkout can't be done; nothing was created. ``status`` and
    ``detail`` follow the cart API's error shape: ``[{"loc", "msg", "type"}]``."""

    def __init__(self, status: int, detail: list[dict[str, Any]]) -> None:
        super().__init__(detail[0]["msg"])
        self.status = status
        self.detail = detail

    @classmethod
    def one(cls, status: int, type_: str, msg: str, loc: list[Any]) -> "CheckoutRefused":
        return cls(status, [{"loc": loc, "msg": msg, "type": type_}])


@dataclass
class CheckoutResult:
    order: Order
    payment: Payment


LINE_PROBLEMS = {
    "out_of_stock": "This product is out of stock.",
    "insufficient_stock": "Not enough stock for this quantity.",
    "unavailable": "This product is no longer sold.",
}


def _validated_lines(db: Session, cart: Optional[Cart]) -> list[CartLine]:
    """Step 1: the cart's lines, validated now (committed). Refused if empty, if any line can't be
    bought as it is, or if a price changed."""
    lines = cart_svc.load_lines(db, cart) if cart is not None else []
    if not lines:
        raise CheckoutRefused.one(409, "cart_empty", "The cart is empty.", ["cart"])
    for line in lines:
        cart_svc.snapshot(line, line.product)
    db.commit()
    problems = [
        {"loc": ["cart", "lines", line.id], "msg": LINE_PROBLEMS[line.issue.value], "type": line.issue.value}
        for line in lines if line.issue is not None
    ]
    if problems:
        raise CheckoutRefused(409, problems)
    repriced = [
        {"loc": ["cart", "lines", line.id], "type": "price_changed",
         "msg": f"The price changed from {line.previous_unit_price_cents} to {line.unit_price_cents} cents; "
                "check the cart and confirm again."}
        for line in lines if line.previous_unit_price_cents is not None
    ]
    if repriced:
        raise CheckoutRefused(409, repriced)
    return lines


def _quote(db: Session, store: Store, subtotal: int, method: str, country: Optional[str], terminal: str):
    """Step 2: the shipping quote and the total (checkout_demo.quote), and the terminal."""
    try:
        quote = checkout_demo.quote(db, store, method, subtotal, country)
    except checkout_demo.CheckoutError as exc:
        loc = ["cart"] if exc.field == "subtotal" else ["body", {"method": "shipping_method"}.get(exc.field, exc.field)]
        raise CheckoutRefused.one(422, exc.kind, str(exc), loc) from exc
    found = payments.get_terminal(db, store, terminal)
    if found is None or not found.is_enabled:
        kind = "unknown_terminal" if found is None else "terminal_disabled"
        message = (f"Unknown payment terminal '{terminal}' for this store." if found is None
                   else f"Payment terminal '{terminal}' is disabled.")
        raise CheckoutRefused.one(422, kind, message, ["body", "terminal"])
    return quote


def checkout(
    db: Session, store: Store, cart: Optional[Cart], *, shipping_method: str, country: Optional[str], terminal: str
) -> CheckoutResult:
    """The whole checkout (see the module docstring). Raises :class:`CheckoutRefused`."""
    lines = _validated_lines(db, cart)
    subtotal = sum(line.unit_price_cents * line.quantity for line in lines)
    quote = _quote(db, store, subtotal, shipping_method, country, terminal)

    try:  # step 3
        payment = payments.create_payment(db, store, terminal, quote.total_cents)
    except payments.PaymentError as exc:  # the terminal was disabled between step 2 and now
        raise CheckoutRefused.one(422, exc.kind, exc.message, ["body", exc.field]) from exc

    # Step 4: one transaction (no stock taken: that happens when the payment succeeds).
    order = Order(
        store_id=store.id, cart_id=cart.id, currency=store.currency, subtotal_cents=subtotal,
        shipping_cents=quote.shipping_cents, total_cents=quote.total_cents, shipping_method_id=quote.method.id,
        shipping_code=quote.method.code, shipping_name=quote.method.name, shipping_country=quote.country,
        free_shipping=quote.free_shipping, payment_id=payment.id,
    )
    order.lines = [
        OrderLine(product_id=line.product_id, sku=line.sku, name=line.name, name_translations=line.name_translations,
                  unit_price_cents=line.unit_price_cents,
                  quantity=line.quantity, line_total_cents=line.unit_price_cents * line.quantity)
        for line in lines
    ]
    db.add(order)
    db.execute(delete(CartLine).where(CartLine.cart_id == cart.id))  # the cart empties into the order
    db.commit()
    db.refresh(order)
    return CheckoutResult(order=order, payment=payment)


# ------------------------------------------------------------------------- payment success


def _take(db: Session, product_id: int, wanted: int) -> tuple[int, int]:
    """Takes up to ``wanted`` units of the product (never below 0). Returns (taken, stock after)."""
    full = db.execute(
        update(Product)
        .where(Product.id == product_id, Product.stock_on_hand >= wanted)
        .values(stock_on_hand=Product.stock_on_hand - wanted)
        .execution_options(synchronize_session=False)
    )
    if full.rowcount == 1:
        return wanted, db.scalar(select(Product.stock_on_hand).where(Product.id == product_id))
    while True:  # not enough: take what is left (compare-and-set, in case it changes meanwhile)
        current = db.scalar(select(Product.stock_on_hand).where(Product.id == product_id))
        if current <= 0:
            return 0, current
        left = db.execute(
            update(Product)
            .where(Product.id == product_id, Product.stock_on_hand == current)
            .values(stock_on_hand=0)
            .execution_options(synchronize_session=False)
        )
        if left.rowcount == 1:
            return current, 0


def take_stock_for_payment(db: Session, payment: Payment) -> Optional[Order]:
    """The payment just succeeded: take its order's stock (not committed: the caller's transaction,
    the one that marks the payment). One ``order`` stock movement per line that took units; units
    that weren't there go to the line's ``short_quantity``. A payment without an order (admin API,
    Try payment) or an order already handled: nothing. Returns the order."""
    order = db.scalar(select(Order).where(Order.payment_id == payment.id).options(selectinload(Order.lines)))
    if order is None or order.stock_taken_at is not None:
        return order
    for line in order.lines:
        product = db.get(Product, line.product_id) if line.product_id is not None else None
        if product is None:  # deleted since the checkout: nothing to take
            line.short_quantity = line.quantity
            continue
        taken, after = _take(db, product.id, line.quantity)
        line.short_quantity = line.quantity - taken
        if taken:
            stock_svc.record_movement(db, product, StockMovementKind.order, -taken, after, reason=order.public_id)
    order.stock_taken_at = utc_now()
    return order


# ------------------------------------------------------------------------------ reading


def get_order(db: Session, store: Store, public_id: str) -> Optional[Order]:
    """The store's order with this public id ("ord_…"), lines and payment loaded."""
    if not public_id or len(public_id) > 40:
        return None
    return db.scalar(
        select(Order)
        .where(Order.store_id == store.id, Order.public_id == public_id)
        .options(selectinload(Order.lines), selectinload(Order.payment).selectinload(Payment.terminal))
    )


def _instructions(payment: Payment) -> Optional[dict[str, Any]]:
    view = bank_transfer.shopper_view(payment)
    if view is not None:
        view["amount_display"] = format_price(payment.amount_cents, payment.currency)
    return view


def order_view(order: Order) -> dict[str, Any]:
    """The order as the API answers it. The payment is its own object, next to the order."""
    money = lambda cents: format_price(cents, order.currency)  # noqa: E731
    payment = order.payment
    return {
        "order": {
            "id": order.public_id,
            "status": order.status.value,
            "currency": order.currency,
            "lines": [
                {"product_id": line.product_id, "sku": line.sku, "name": line.name,
                 "name_translations": line.name_translations or {}, "quantity": line.quantity,
                 "unit_price_cents": line.unit_price_cents, "line_total_cents": line.line_total_cents,
                 "unit_price_display": money(line.unit_price_cents), "line_total_display": money(line.line_total_cents),
                 "short_quantity": line.short_quantity}
                for line in order.lines
            ],
            # Taken when the payment succeeded; "short": some units weren't there any more.
            "stock": {"taken": order.stock_taken_at is not None,
                      "taken_at": order.stock_taken_at.isoformat() + "Z" if order.stock_taken_at else None,
                      "short": any(line.short_quantity for line in order.lines)},
            "item_count": sum(line.quantity for line in order.lines),
            "subtotal_cents": order.subtotal_cents,
            "shipping": {"method": order.shipping_code, "name": order.shipping_name, "country": order.shipping_country,
                         "amount_cents": order.shipping_cents, "free_shipping": order.free_shipping},
            "total_cents": order.total_cents,
            "subtotal_display": money(order.subtotal_cents),
            "shipping_display": money(order.shipping_cents),
            "total_display": money(order.total_cents),
            "created_at": order.created_at.isoformat() + "Z",
        },
        "payment": {
            "id": payment.public_id,
            "status": payment.status.value,
            "terminal": payment.terminal.code,
            "kind": payment.terminal.kind.value,
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            # Bank transfer: where and how to deposit (the copy kept on the payment); None otherwise.
            "transfer_reference": payment.transfer_reference,
            "instructions": _instructions(payment),
        },
    }
