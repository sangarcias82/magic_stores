"""Admin: a store's shipping methods (list, create, edit). Deactivate a method instead of deleting it."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import Translator, get_locale, translator
from app.models import ShippingMethod, Store
from app.routers.admin import admin_title, flash, render_admin
from app.routers.admin_catalog import get_store
from app.services import catalog
from app.services import shipping_admin as svc

router = APIRouter(
    prefix="/admin/stores/{store_slug}/shipping",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


def get_store_method(method_id: int, store: Store = Depends(get_store), db: Session = Depends(get_db)) -> ShippingMethod:
    method = svc.get_method(db, store, method_id)
    if method is None:
        raise HTTPException(status_code=404, detail="Shipping method not found")
    return method


def field_error_text(t: Translator, error: svc.FieldError) -> str:
    """A validation error in the admin's language (literal keys: tests/test_i18n.py finds them)."""
    code, params = error
    messages = {
        "required": lambda: t("admin.shipping.error.required"),
        "too_long": lambda: t("admin.shipping.error.too_long", **params),
        "code_format": lambda: t("admin.shipping.error.code_format", **params),
        "code_taken": lambda: t("admin.shipping.error.code_taken", **params),
        "money_format": lambda: t("admin.shipping.error.money_format"),
        "money_comma": lambda: t("admin.shipping.error.money_comma"),
        "money_decimals": lambda: t("admin.shipping.error.money_decimals"),
        "money_negative": lambda: t("admin.shipping.error.money_negative"),
        "money_too_large": lambda: t("admin.shipping.error.money_too_large"),
        "sort_format": lambda: t("admin.shipping.error.sort_format"),
        "sort_range": lambda: t("admin.shipping.error.sort_range", **params),
        "country_format": lambda: t("admin.shipping.error.country_format", **params),
    }
    return messages[code]()


def _list_url(store: Store) -> str:
    return f"/admin/stores/{store.slug}/shipping"


def _form_page(
    request: Request,
    db: Session,
    store: Store,
    form: svc.ShippingMethodForm,
    method: Optional[ShippingMethod] = None,
    status_code: int = 200,
) -> HTMLResponse:
    t = translator(request)
    display_name = svc.display_names(db, store, get_locale(request))[method.id] if method else None
    return render_admin(
        request,
        "admin/shipping_form.html",
        {
            "page_title": admin_title(t, display_name or t("admin.shipping.new"), store.name),
            "section": "stores",
            "store_tab": "shipping",
            "store": store,
            "method": method,
            "display_name": display_name,
            "form": form,
        },
        status_code=status_code,
    )


def _submitted(name: str, code: str, rate: str, free_from: str, countries: str, sort_order: str,
               is_active: str) -> svc.ShippingMethodForm:
    return svc.ShippingMethodForm(
        name=name, code=code, rate=rate, free_from=free_from, countries=countries, sort_order=sort_order,
        is_active=bool(is_active),  # unticked checkboxes are not sent at all
    )


def _save(request: Request, db: Session, store: Store, form: svc.ShippingMethodForm,
          method: Optional[ShippingMethod]) -> HTMLResponse | RedirectResponse:
    t = translator(request)
    values, errors = svc.validate_method(db, store, form, method)
    saved = None if errors else svc.save_method(db, store, values, method)
    if saved is None:
        if not errors:  # the code was taken between the check and the save
            errors = {"code": ("code_taken", {"code": values["code"]})}
        form.errors = {name: field_error_text(t, error) for name, error in errors.items()}
        if method is not None:
            db.refresh(method)  # show the stored name in the title, not the rejected one
        return _form_page(request, db, store, form, method, status_code=status.HTTP_400_BAD_REQUEST)
    flash(request, "flash.shipping_saved" if method else "flash.shipping_created", name=saved.name)
    return RedirectResponse(_list_url(store), status_code=status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def shipping_list(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    t = translator(request)
    return render_admin(
        request,
        "admin/shipping.html",
        {
            "page_title": admin_title(t, t("admin.shipping.title"), store.name),
            "section": "stores",
            "store_tab": "shipping",
            "store": store,
            "methods": svc.list_methods(db, store),
            "names": svc.display_names(db, store, get_locale(request)),
            "format_price": catalog.format_price,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def shipping_new(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    return _form_page(request, db, store, svc.ShippingMethodForm())


@router.post("", response_class=HTMLResponse)
def shipping_create(
    request: Request,
    name: str = Form(""),
    code: str = Form(""),
    rate: str = Form(""),
    free_from: str = Form(""),
    countries: str = Form(""),
    sort_order: str = Form("0"),
    is_active: str = Form(""),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    form = _submitted(name, code, rate, free_from, countries, sort_order, is_active)
    return _save(request, db, store, form, None)


@router.get("/{method_id}", response_class=HTMLResponse)
def shipping_edit(request: Request, store: Store = Depends(get_store),
                  method: ShippingMethod = Depends(get_store_method), db: Session = Depends(get_db)):
    return _form_page(request, db, store, svc.ShippingMethodForm.from_method(method), method)


@router.post("/{method_id}", response_class=HTMLResponse)
def shipping_update(
    request: Request,
    name: str = Form(""),
    code: str = Form(""),
    rate: str = Form(""),
    free_from: str = Form(""),
    countries: str = Form(""),
    sort_order: str = Form("0"),
    is_active: str = Form(""),
    store: Store = Depends(get_store),
    method: ShippingMethod = Depends(get_store_method),
    db: Session = Depends(get_db),
):
    form = _submitted(name, code, rate, free_from, countries, sort_order, is_active)
    return _save(request, db, store, form, method)
