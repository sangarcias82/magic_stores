"""Payment terminals per store, and payments through them (mock: no card network is called).

Every store has the terminals of ``DEFAULT_PAYMENT_TERMINALS`` (app/models.py): new stores get them
when they are inserted, and :func:`ensure_default_terminals` (run at app startup) adds the ones a
store created before terminals existed is missing. Existing rows are never changed, so a terminal
turned off (``is_enabled = False``) stays off. The HTTP API is app/routers/payments.py.

Payments: :func:`create_payment` records one as ``pending`` through an enabled terminal;
:func:`mark_payment` then makes it ``succeeded`` or ``failed``, once (``PAYMENT_TRANSITIONS`` in
app/models.py). Marking it again with the same status changes nothing (safe to retry); any other
change is refused. The status change is one conditional UPDATE, so two concurrent calls can't both win.
When a payment of an order (app/services/checkout.py) becomes ``succeeded``, the order's stock is
taken in the same transaction as the status change; a ``failed`` payment takes nothing.

Offline bank transfer terminals (``TerminalKind.bank_transfer``, app/services/bank_transfer.py): a
payment gets a short transfer reference and a copy of the deposit instructions when it is created
(refused while the terminal's details are incomplete: ``terminal_not_configured``), and only an
admin decides its outcome (``mark_payment(..., operator=admin)``): the terminal's signed callback
is refused for it (``operator_confirmation_required``).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session, selectinload

from app.models import (
    MAX_AMOUNT_CENTS,
    User,
    PAYMENT_TRANSITIONS,
    Payment,
    PaymentStatus,
    PaymentTerminal,
    Store,
    default_payment_terminal_rows,
    new_payment_public_id,
    utc_now,
)
from app.services import bank_transfer


class PaymentError(Exception):
    """A payment request that can't be done. ``kind`` is a stable code for API clients; ``field``
    the input at fault."""

    def __init__(self, kind: str, field: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.field = field
        self.message = message


def list_terminals(db: Session, store: Store) -> list[PaymentTerminal]:
    """All the store's terminals, enabled or not, in display order."""
    return list(
        db.scalars(
            select(PaymentTerminal)
            .where(PaymentTerminal.store_id == store.id)
            .order_by(PaymentTerminal.sort_order, PaymentTerminal.code)
        )
    )


def ensure_default_terminals(db: Session) -> int:
    """Add every default terminal a store lacks (by code); commits. Returns the number of rows added."""
    existing = {(store_id, code) for store_id, code in db.execute(select(PaymentTerminal.store_id, PaymentTerminal.code))}
    missing = [
        row
        for store_id in db.scalars(select(Store.id))
        for row in default_payment_terminal_rows(store_id)
        if (store_id, row["code"]) not in existing
    ]
    if missing:
        db.execute(insert(PaymentTerminal), missing)
        db.commit()
    return len(missing)


def get_terminal(db: Session, store: Store, code: str) -> Optional[PaymentTerminal]:
    return db.scalar(
        select(PaymentTerminal).where(PaymentTerminal.store_id == store.id, PaymentTerminal.code == code)
    )


def create_payment(db: Session, store: Store, terminal_code: str, amount_cents: int) -> Payment:
    """A new ``pending`` payment of ``amount_cents`` in the store's currency; commits.
    Raises :class:`PaymentError` (``unknown_terminal``, ``terminal_disabled``, ``invalid_amount``)."""
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) \
            or not 0 < amount_cents <= MAX_AMOUNT_CENTS:
        raise PaymentError("invalid_amount", "amount_cents", f"Amount must be 1 to {MAX_AMOUNT_CENTS} cents.")
    terminal = get_terminal(db, store, terminal_code)
    if terminal is None:
        raise PaymentError("unknown_terminal", "terminal", f"Unknown payment terminal '{terminal_code}' for this store.")
    if not terminal.is_enabled:
        raise PaymentError("terminal_disabled", "terminal", f"Payment terminal '{terminal_code}' is disabled.")
    if terminal.is_bank_transfer and not bank_transfer.is_configured(terminal):
        raise PaymentError("terminal_not_configured", "terminal",
                           f"Payment terminal '{terminal_code}' has no complete bank details yet.")
    now = utc_now()
    payment = Payment(
        store_id=store.id,
        terminal_id=terminal.id,
        amount_cents=amount_cents,
        currency=store.currency,
        status=PaymentStatus.pending,
        created_at=now,
        updated_at=now,
    )
    if terminal.is_bank_transfer:  # the shopper's deposit instructions, with a reference of its own
        payment.public_id = new_payment_public_id()
        payment.transfer_reference = bank_transfer.new_reference(db, store.id)
        payment.instructions = bank_transfer.instructions_for(db, terminal, payment.transfer_reference,
                                                              payment.public_id)
    db.add(payment)
    db.commit()
    return payment


def list_payments(db: Session, store: Store, *, offset: int = 0, limit: int = 50) -> list[Payment]:
    """The store's payments, most recent first (with their terminal loaded), for the admin list."""
    return list(db.scalars(
        select(Payment)
        .where(Payment.store_id == store.id)
        .options(selectinload(Payment.terminal))
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .offset(offset)
        .limit(limit)
    ))


def get_payment(db: Session, store: Store, public_id: str) -> Optional[Payment]:
    """The store's payment with this public id ("pay_…"; another store's payment is None). Payments
    are never looked up by their sequential database id from outside."""
    return db.scalar(select(Payment).where(Payment.store_id == store.id, Payment.public_id == public_id))


def mark_payment(
    db: Session,
    payment: Payment,
    status: PaymentStatus,
    mock_reference: Optional[str] = None,
    *,
    operator: Optional[User] = None,
    note: Optional[str] = None,
) -> Payment:
    """Move a payment to ``succeeded`` or ``failed``; commits. Marking it with the status it already
    has returns it unchanged (the reference is kept). Raises :class:`PaymentError`
    (``invalid_transition``) for any change ``PAYMENT_TRANSITIONS`` doesn't allow, and
    (``operator_confirmation_required``) for an offline payment (bank transfer) marked without an
    ``operator``: only an admin decides those (``note``: theirs, e.g. the bank's tracking key)."""
    db.refresh(payment)  # decide on the current row, not on what this session saw earlier
    if payment.terminal.is_bank_transfer and operator is None:
        raise PaymentError(
            "operator_confirmation_required",
            "payment_id",
            f"Payment {payment.public_id} is a bank transfer: an admin confirms or rejects it, not a callback.",
        )
    if payment.status == status:
        return payment
    if status not in PAYMENT_TRANSITIONS[payment.status]:
        raise PaymentError(
            "invalid_transition",
            "payment_id",
            f"Payment {payment.public_id} is already {payment.status.value}; only pending payments can be marked.",
        )
    result = db.execute(
        update(Payment)
        .where(Payment.id == payment.id, Payment.status == payment.status)  # nobody changed it meanwhile
        .values(status=status, mock_reference=mock_reference, updated_at=utc_now(),
                decided_by_id=operator.id if operator is not None else None, decision_note=note)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 1 and status == PaymentStatus.succeeded:
        # An order's payment: its stock is taken now, committed together with the status (once:
        # only the request whose UPDATE won gets here). A failed payment takes nothing.
        from app.services.checkout import take_stock_for_payment  # (checkout uses this module)

        take_stock_for_payment(db, payment)
    db.commit()
    if result.rowcount != 1:  # another request marked it between the refresh and the update
        db.refresh(payment)
        return mark_payment(db, payment, status, mock_reference, operator=operator, note=note)
    db.refresh(payment)
    return payment
