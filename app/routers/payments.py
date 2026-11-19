"""Payment API: a store's payment terminals, and payments through them (README "Payments (mock)").

Who may call what:

- ``GET  …/payment/terminals``      public (read-only configuration: codes, names, enabled flags)
- ``POST …/payments``               admin session (there is no customer checkout yet)
- ``GET  …/payments/{payment_id}``  admin session; ids are random ("pay_…"), never sequential
- ``POST …/payments/callback``      the terminal only: HMAC-signed with PAYMENT_CALLBACK_SECRET
                                    (app/services/payment_callbacks.py). The only way to set the outcome.

Without a session the JSON routes answer 401 and a non-admin 403, before any lookup, so they don't
reveal whether a store or payment exists.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.models import Payment, PaymentStatus, Store, User, UserRole
from app.schemas import PaymentCallbackIn, PaymentCreateIn, PaymentOut, PaymentTerminalOut
from app.services import catalog, payment_callbacks, payments

router = APIRouter(prefix="/api/stores/{store_slug}", tags=["payments"])

# HTTP status of each PaymentError kind: bad input -> 422, wrong moment (already final) -> 409.
# A bank transfer is decided by an admin (app/services/bank_transfer.py), never by a callback.
_ERROR_STATUS = {"invalid_transition": 409, "operator_confirmation_required": 409}


def require_admin_api(user: Optional[User] = Depends(get_current_user)) -> User:
    """Admin session for a JSON route: 401 / 403 as JSON (no redirect to the sign-in page)."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in as an admin to use the payments API")
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def callback_body(request: Request) -> bytes:
    """The raw callback body (the signature covers these exact bytes), at most MAX_BODY_BYTES."""
    limit = payment_callbacks.MAX_BODY_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=f"Callback body larger than {limit} bytes")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(status_code=413, detail=f"Callback body larger than {limit} bytes")
    return body


def _store_or_404(db: Session, store_slug: str) -> Store:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return store


def _error(exc: payments.PaymentError) -> HTTPException:
    # Same shape as FastAPI's own 422 body errors (and the shipping quote errors).
    return HTTPException(
        status_code=_ERROR_STATUS.get(exc.kind, 422),
        detail=[{"loc": ["body", "payment" if exc.field == "payment_id" else exc.field], "msg": exc.message,
                 "type": exc.kind}],
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc)  # stored naive UTC; sent as "…Z"


def _out(store: Store, payment: Payment) -> PaymentOut:
    return PaymentOut(
        id=payment.public_id,
        store=store.slug,
        terminal=payment.terminal.code,
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        status=payment.status.value,
        mock_reference=payment.mock_reference,
        transfer_reference=payment.transfer_reference,
        created_at=_utc(payment.created_at),
        updated_at=_utc(payment.updated_at),
    )


@router.get("/payment/terminals", response_model=List[PaymentTerminalOut])
def api_list_payment_terminals(store_slug: str, db: Session = Depends(get_db)) -> List[PaymentTerminalOut]:
    store = _store_or_404(db, store_slug)
    return [
        PaymentTerminalOut(code=t.code, name=t.name, kind=t.kind.value, enabled=t.is_enabled, sort_order=t.sort_order)
        for t in payments.list_terminals(db, store)
    ]


@router.post("/payments", response_model=PaymentOut, status_code=201, dependencies=[Depends(require_admin_api)])
def api_create_payment(store_slug: str, body: PaymentCreateIn, db: Session = Depends(get_db)) -> PaymentOut:
    store = _store_or_404(db, store_slug)
    try:
        payment = payments.create_payment(db, store, body.terminal, body.amount_cents)
    except payments.PaymentError as exc:
        raise _error(exc) from exc
    return _out(store, payment)


@router.post("/payments/callback", response_model=PaymentOut)
def api_payment_callback(
    request: Request,
    store_slug: str,
    raw: bytes = Depends(callback_body),
    db: Session = Depends(get_db),
) -> PaymentOut:
    """The terminal's answer (succeeded / failed), signed. Checked before anything is looked up."""
    secret = get_settings().payment_callback_secret
    if not secret:
        raise HTTPException(status_code=503, detail="Payment callbacks are disabled: PAYMENT_CALLBACK_SECRET is not set")
    try:
        payment_callbacks.verify(secret, request.headers.get(payment_callbacks.TIMESTAMP_HEADER),
                                 request.headers.get(payment_callbacks.SIGNATURE_HEADER), raw)
    except payment_callbacks.CallbackRejected as exc:
        raise HTTPException(status_code=401, detail=f"Callback rejected: {exc}") from exc
    try:
        body = PaymentCallbackIn.model_validate_json(raw)
    except ValidationError as exc:
        errors = [{"loc": ["body", *map(str, e["loc"])], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        raise HTTPException(status_code=422, detail=errors) from exc
    store = _store_or_404(db, store_slug)
    payment = payments.get_payment(db, store, body.payment)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    try:
        payment = payments.mark_payment(db, payment, PaymentStatus(body.status), body.mock_reference)
    except payments.PaymentError as exc:
        raise _error(exc) from exc
    return _out(store, payment)


@router.get("/payments/{payment_id}", response_model=PaymentOut, dependencies=[Depends(require_admin_api)])
def api_get_payment(store_slug: str, payment_id: str, db: Session = Depends(get_db)) -> PaymentOut:
    store = _store_or_404(db, store_slug)
    payment = payments.get_payment(db, store, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return _out(store, payment)
