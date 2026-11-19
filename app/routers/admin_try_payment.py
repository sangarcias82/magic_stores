"""Admin: "Try payment", an operator demo of quote + mock payment on a store (app/services/checkout_demo.py).

1. GET  /admin/stores/<slug>/try-payment?subtotal=…&method=…&country=…   form; with input: the quote
2. POST /admin/stores/<slug>/try-payment/pay                             quote again + pending payment
3. GET  /admin/stores/<slug>/try-payment/<payment_id>                    the payment, succeed / fail buttons
   POST …/<payment_id>/succeed  and  …/<payment_id>/fail                 mock terminal answer

<payment_id> is the payment's public id ("pay_…"). The simulated answer is an admin-only stand-in for
the terminal's callback, and only for mock terminals (``PaymentTerminal.is_mock``): a real terminal's
payment can only be answered by its signed callback (app/services/payment_callbacks.py).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import Translator, get_locale, translator
from app.models import Payment, PaymentStatus, Store
from app.routers.admin import admin_title, flash, render_admin
from app.routers.admin_catalog import get_store
from app.routers.admin_shipping import field_error_text
from app.services import catalog, checkout_demo, payments, shipping
from app.services.shipping_admin import COUNTRY_RE, display_names, parse_money

router = APIRouter(
    prefix="/admin/stores/{store_slug}/try-payment",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

REFERENCE_MAX = 64


def _base(store: Store) -> str:
    return f"/admin/stores/{store.slug}/try-payment"


def _checkout_error_text(t: Translator, error: checkout_demo.CheckoutError) -> str:
    """Literal keys, so tests/test_i18n.py can check they exist."""
    messages = {
        "unknown_method": lambda: t("admin.try_payment.error.unknown_method"),
        "inactive_method": lambda: t("admin.try_payment.error.inactive_method"),
        "country_required": lambda: t("admin.try_payment.error.country_required"),
        "country_not_served": lambda: t("admin.try_payment.error.country_not_served"),
        "nothing_to_pay": lambda: t("admin.try_payment.error.nothing_to_pay"),
        "total_too_large": lambda: t("admin.try_payment.error.total_too_large"),
        "unknown_terminal": lambda: t("admin.try_payment.error.unknown_terminal"),
        "terminal_disabled": lambda: t("admin.try_payment.error.terminal_disabled"),
    }
    return messages.get(error.kind, lambda: str(error))()


def status_labels(t: Translator) -> dict[PaymentStatus, str]:
    return {
        PaymentStatus.pending: t("admin.try_payment.status.pending"),
        PaymentStatus.succeeded: t("admin.try_payment.status.succeeded"),
        PaymentStatus.failed: t("admin.try_payment.status.failed"),
    }


def _parse_input(t: Translator, subtotal: str, country: str, method: str
                 ) -> tuple[Optional[int], Optional[str], dict[str, str]]:
    """(subtotal in cents, country or None, errors by field shown on the page)."""
    errors: dict[str, str] = {}
    cents, error = parse_money(subtotal, required=True)
    if error:
        errors["subtotal"] = field_error_text(t, error)
    country_code = country.strip().upper() or None
    if country_code and not COUNTRY_RE.match(country_code):
        errors["country"] = field_error_text(t, ("country_format", {"value": country.strip()}))
    if not method.strip():
        errors["method"] = t("admin.try_payment.error.choose_method")
    return cents, country_code, errors


def _demo_page(request: Request, db: Session, store: Store, *, form: dict[str, str], errors: dict[str, str],
               checkout: Optional[checkout_demo.Checkout] = None, status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    return render_admin(
        request,
        "admin/try_payment.html",
        {
            "page_title": admin_title(t, t("admin.tabs.try_payment"), store.name),
            "section": "stores",
            "store_tab": "try_payment",
            "store": store,
            "methods": shipping.list_active_methods(db, store),
            "names": display_names(db, store, get_locale(request)),  # in the page language
            # Mock terminals only: a demo never creates a real (e.g. bank transfer) payment.
            "terminals": [terminal for terminal in payments.list_terminals(db, store)
                          if terminal.is_enabled and terminal.is_mock],
            "form": form,
            "errors": errors,
            "checkout": checkout,
            "format_price": catalog.format_price,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def try_payment(
    request: Request,
    subtotal: str = Query(""),
    country: str = Query(""),
    method: Optional[str] = Query(None),  # absent: first visit, nothing to quote yet
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    form = {"subtotal": subtotal, "country": country, "method": method or "", "terminal": ""}
    if method is None:
        return _demo_page(request, db, store, form=form, errors={})
    t = translator(request)
    cents, country_code, errors = _parse_input(t, subtotal, country, method)
    checkout = None
    if not errors:
        try:
            checkout = checkout_demo.quote(db, store, method.strip(), cents, country_code)
        except checkout_demo.CheckoutError as exc:
            errors[exc.field] = _checkout_error_text(t, exc)
    return _demo_page(request, db, store, form=form, errors=errors, checkout=checkout,
                      status_code=status.HTTP_400_BAD_REQUEST if errors else 200)


@router.post("/pay", response_class=HTMLResponse)
def try_payment_pay(
    request: Request,
    subtotal: str = Form(""),
    country: str = Form(""),
    method: str = Form(""),
    terminal: str = Form(""),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    t = translator(request)
    form = {"subtotal": subtotal, "country": country, "method": method, "terminal": terminal}
    cents, country_code, errors = _parse_input(t, subtotal, country, method)
    if not terminal.strip():
        errors["terminal"] = t("admin.try_payment.error.choose_terminal")
    else:
        chosen = payments.get_terminal(db, store, terminal.strip())
        if chosen is not None and not chosen.is_mock:  # refused: Try payment is for mock terminals only
            errors["terminal"] = t("admin.try_payment.error.not_mock_pay", terminal=chosen.name)
    if errors:
        checkout = None
        if set(errors) == {"terminal"}:  # only the terminal is wrong: keep showing the quote and its form
            try:
                checkout = checkout_demo.quote(db, store, method.strip(), cents, country_code)
            except checkout_demo.CheckoutError as exc:
                errors[exc.field] = _checkout_error_text(t, exc)
        return _demo_page(request, db, store, form=form, errors=errors, checkout=checkout,
                          status_code=status.HTTP_400_BAD_REQUEST)
    try:
        # Quoted again here: the amount never comes from the browser.
        checkout, payment = checkout_demo.pay(db, store, method.strip(), cents, country_code, terminal.strip())
    except checkout_demo.CheckoutError as exc:
        errors[exc.field] = _checkout_error_text(t, exc)
        checkout = None
        if exc.field == "terminal":  # the quote itself is fine: keep showing it
            checkout = checkout_demo.quote(db, store, method.strip(), cents, country_code)
        return _demo_page(request, db, store, form=form, errors=errors, checkout=checkout,
                          status_code=status.HTTP_400_BAD_REQUEST)
    flash(request, "flash.try_payment_created", id=payment.public_id,
          amount=catalog.format_price(payment.amount_cents, payment.currency))
    return RedirectResponse(f"{_base(store)}/{payment.public_id}", status_code=status.HTTP_303_SEE_OTHER)


def get_store_payment(payment_id: str, store: Store = Depends(get_store), db: Session = Depends(get_db)) -> Payment:
    payment = payments.get_payment(db, store, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


def _payment_page(request: Request, store: Store, payment: Payment, *, error: Optional[str] = None,
                  reference: str = "", status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    return render_admin(
        request,
        "admin/try_payment_result.html",
        {
            "page_title": admin_title(t, t("admin.try_payment.payment_title", id=payment.public_id), store.name),
            "section": "stores",
            "store_tab": "try_payment",
            "store": store,
            "payment": payment,
            "status_labels": status_labels(t),
            "error": error,
            "reference": reference,
            "format_price": catalog.format_price,
        },
        status_code=status_code,
    )


@router.get("/{payment_id}", response_class=HTMLResponse)
def try_payment_show(request: Request, store: Store = Depends(get_store),
                     payment: Payment = Depends(get_store_payment)):
    return _payment_page(request, store, payment)


def _answer(request: Request, db: Session, store: Store, payment: Payment, new_status: PaymentStatus,
            mock_reference: str):
    t = translator(request)
    if not payment.terminal.is_mock:  # a real terminal answers with its signed callback only
        return _payment_page(request, store, payment, status_code=status.HTTP_409_CONFLICT,
                             error=t("admin.try_payment.error.not_mock", terminal=payment.terminal.name))
    reference = mock_reference.strip()
    if len(reference) > REFERENCE_MAX:
        return _payment_page(request, store, payment, reference=reference, status_code=status.HTTP_400_BAD_REQUEST,
                             error=t("admin.try_payment.error.reference_too_long", max_len=str(REFERENCE_MAX)))
    try:
        payments.mark_payment(db, payment, new_status, reference or None)
    except payments.PaymentError:
        return _payment_page(request, store, payment, status_code=status.HTTP_409_CONFLICT,
                             error=t("admin.try_payment.error.already_final",
                                     status=status_labels(t)[payment.status]))
    flash(request, "flash.try_payment_succeeded" if new_status == PaymentStatus.succeeded
          else "flash.try_payment_failed", id=payment.public_id)
    return RedirectResponse(f"{_base(store)}/{payment.public_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{payment_id}/succeed", response_class=HTMLResponse)
def try_payment_succeed(request: Request, mock_reference: str = Form(""), store: Store = Depends(get_store),
                        payment: Payment = Depends(get_store_payment), db: Session = Depends(get_db)):
    return _answer(request, db, store, payment, PaymentStatus.succeeded, mock_reference)


@router.post("/{payment_id}/fail", response_class=HTMLResponse)
def try_payment_fail(request: Request, mock_reference: str = Form(""), store: Store = Depends(get_store),
                     payment: Payment = Depends(get_store_payment), db: Session = Depends(get_db)):
    return _answer(request, db, store, payment, PaymentStatus.failed, mock_reference)
