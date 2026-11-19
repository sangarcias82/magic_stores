"""Admin: a store's payments, most recent first (the list is read-only; 50 per page, ``?page=2``, …),
and a payment's page. On that page an admin **confirms or rejects a bank transfer** (offline: the
shopper deposited, the admin checks the bank account): that decides its outcome, with an optional
note (e.g. the bank's tracking key). Mock payments are answered in Try payment or by their terminal."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import require_admin
from app.db import get_db
from app.i18n import translator
from app.models import Order, Payment, PaymentStatus, Store, User
from app.routers.admin import admin_title, flash, parse_page, render_admin
from app.routers.admin_catalog import get_store
from app.routers.admin_try_payment import status_labels
from app.services import bank_transfer, catalog, payments

router = APIRouter(
    prefix="/admin/stores/{store_slug}/payments",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

PAGE_SIZE = 50
NOTE_MAX = 200  # Payment.decision_note


def _amount(cents: int) -> str:
    return f"{Decimal(cents) / 100:,.2f}"  # the currency is its own column


@router.get("", response_class=HTMLResponse)
def payments_list(request: Request, page: Optional[str] = None, store: Store = Depends(get_store),
                  db: Session = Depends(get_db)):
    t = translator(request)
    page_number = parse_page(page)
    rows = payments.list_payments(db, store, offset=(page_number - 1) * PAGE_SIZE, limit=PAGE_SIZE + 1)
    return render_admin(
        request,
        "admin/payments.html",
        {
            "page_title": admin_title(t, t("admin.tabs.payments"), store.name),
            "section": "stores",
            "store_tab": "payments",
            "store": store,
            "payments": rows[:PAGE_SIZE],
            "has_older": len(rows) > PAGE_SIZE,
            "page": page_number,
            "status_labels": status_labels(t),
            "amount": _amount,
        },
    )


# ------------------------------------------------------------------------ one payment


def get_store_payment(payment_id: str, store: Store = Depends(get_store), db: Session = Depends(get_db)) -> Payment:
    payment = payments.get_payment(db, store, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


def _detail_page(request: Request, db: Session, store: Store, payment: Payment, *, error: str = "", note: str = "",
                 status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    order = db.scalar(select(Order).where(Order.payment_id == payment.id).options(selectinload(Order.lines)))
    return render_admin(
        request,
        "admin/payment_detail.html",
        {
            "page_title": admin_title(t, payment.public_id, store.name),
            "section": "stores",
            "store_tab": "payments",
            "store": store,
            "payment": payment,
            "order": order,
            "instructions": bank_transfer.shopper_view(payment),
            "status_labels": status_labels(t),
            "format_price": catalog.format_price,
            "error": error,
            "note": note,
            "note_max": NOTE_MAX,
        },
        status_code=status_code,
    )


@router.get("/{payment_id}", response_class=HTMLResponse)
def payment_detail(request: Request, store: Store = Depends(get_store), payment: Payment = Depends(get_store_payment),
                   db: Session = Depends(get_db)):
    return _detail_page(request, db, store, payment)


def _decide(request: Request, db: Session, store: Store, payment: Payment, user: User, new_status: PaymentStatus,
            note: str):
    t = translator(request)
    note = " ".join(note.split())
    if not payment.terminal.is_bank_transfer:  # mocks: Try payment; gateways: their signed callback
        return _detail_page(request, db, store, payment, status_code=status.HTTP_409_CONFLICT,
                            error=t("admin.payment.error.not_offline", terminal=payment.terminal.name))
    if len(note) > NOTE_MAX:
        return _detail_page(request, db, store, payment, note=note, status_code=status.HTTP_400_BAD_REQUEST,
                            error=t("admin.payment.error.note_too_long", max_len=NOTE_MAX))
    try:
        payments.mark_payment(db, payment, new_status, operator=user, note=note or None)
    except payments.PaymentError:  # already decided the other way
        db.refresh(payment)
        return _detail_page(request, db, store, payment, note=note, status_code=status.HTTP_409_CONFLICT,
                            error=t("admin.payment.error.already", status=status_labels(t)[payment.status]))
    flash(request, "flash.payment_confirmed" if new_status == PaymentStatus.succeeded else "flash.payment_rejected",
          id=payment.public_id)
    return RedirectResponse(f"/admin/stores/{store.slug}/payments/{payment.public_id}",
                            status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{payment_id}/confirm", response_class=HTMLResponse)
def payment_confirm(request: Request, note: str = Form(""), store: Store = Depends(get_store),
                    payment: Payment = Depends(get_store_payment), user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    """The deposit arrived: the payment succeeds (and its order's stock is taken)."""
    return _decide(request, db, store, payment, user, PaymentStatus.succeeded, note)


@router.post("/{payment_id}/reject", response_class=HTMLResponse)
def payment_reject(request: Request, note: str = Form(""), store: Store = Depends(get_store),
                   payment: Payment = Depends(get_store_payment), user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """No (valid) deposit: the payment fails (its order takes no stock)."""
    return _decide(request, db, store, payment, user, PaymentStatus.failed, note)
