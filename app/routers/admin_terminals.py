"""Admin: a store's payment terminals (list, rename, enable / disable). See app/services/terminal_admin.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import translator
from app.models import PaymentTerminal, Store
from app.routers.admin import admin_title, flash, render_admin
from app.routers.admin_catalog import get_store
from app.routers.admin_shipping import field_error_text
from app.i18n import Translator
from app.services import bank_transfer, payments
from app.services import terminal_admin as svc
from app.services.store_admin import FieldError

router = APIRouter(
    prefix="/admin/stores/{store_slug}/terminals",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


def get_store_terminal(terminal_id: int, store: Store = Depends(get_store),
                       db: Session = Depends(get_db)) -> PaymentTerminal:
    terminal = svc.get_terminal(db, store, terminal_id)
    if terminal is None:
        raise HTTPException(status_code=404, detail="Payment terminal not found")
    return terminal


def bank_error_text(t: Translator, error: FieldError) -> str:
    """A bank-details error in the admin's language (literal keys: tests/test_i18n.py finds them)."""
    code, params = error
    messages = {
        "required": lambda: t("admin.terminals.error.required"),
        "too_long": lambda: t("admin.terminals.error.too_long", **params),
        "clabe_format": lambda: t("admin.terminals.error.clabe_format"),
        "clabe_check": lambda: t("admin.terminals.error.clabe_check"),
        "account_format": lambda: t("admin.terminals.error.account_format"),
    }
    return messages[code]()


def _list_url(store: Store) -> str:
    return f"/admin/stores/{store.slug}/terminals"


@router.get("", response_class=HTMLResponse)
def terminals_list(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    t = translator(request)
    return render_admin(
        request,
        "admin/terminals.html",
        {
            "page_title": admin_title(t, t("admin.tabs.terminals"), store.name),
            "section": "stores",
            "store_tab": "terminals",
            "store": store,
            "terminals": payments.list_terminals(db, store),
        },
    )


def _form_page(request: Request, store: Store, terminal: PaymentTerminal, form: dict, errors: dict[str, str],
               status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    return render_admin(
        request,
        "admin/terminal_form.html",
        {
            "page_title": admin_title(t, terminal.name, store.name),
            "section": "stores",
            "store_tab": "terminals",
            "store": store,
            "terminal": terminal,
            "form": form,
            "errors": errors,
        },
        status_code=status_code,
    )


@router.get("/{terminal_id}", response_class=HTMLResponse)
def terminal_edit(request: Request, store: Store = Depends(get_store),
                  terminal: PaymentTerminal = Depends(get_store_terminal)):
    form = {"name": terminal.name, "is_enabled": terminal.is_enabled, **{f: "" for f in bank_transfer.CONFIG_FIELDS},
            **(terminal.config or {})}
    return _form_page(request, store, terminal, form, {})


@router.post("/{terminal_id}", response_class=HTMLResponse)
def terminal_update(
    request: Request,
    name: str = Form(""),
    is_enabled: str = Form(""),  # unticked checkboxes are not sent at all
    bank: str = Form(""),
    beneficiary: str = Form(""),
    clabe: str = Form(""),
    account: str = Form(""),
    note: str = Form(""),
    store: Store = Depends(get_store),
    terminal: PaymentTerminal = Depends(get_store_terminal),
    db: Session = Depends(get_db),
):
    t = translator(request)
    cleaned, error = svc.clean_name(name)
    errors = {"name": field_error_text(t, error)} if error else {}
    config = None
    if terminal.is_bank_transfer:  # its deposit instructions, shown to shoppers who choose it
        config, problems = bank_transfer.validate_config(
            {"bank": bank, "beneficiary": beneficiary, "clabe": clabe, "account": account, "note": note})
        errors.update({field: bank_error_text(t, problem) for field, problem in problems.items()})
    if errors:
        form = {"name": name, "is_enabled": bool(is_enabled), "bank": bank, "beneficiary": beneficiary,
                "clabe": clabe, "account": account, "note": note}
        return _form_page(request, store, terminal, form, errors, status_code=status.HTTP_400_BAD_REQUEST)
    svc.save_terminal(db, terminal, cleaned, bool(is_enabled), config=config)
    flash(request, "flash.terminal_saved", name=terminal.name)
    return RedirectResponse(_list_url(store), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{terminal_id}/enable", response_class=HTMLResponse)
def terminal_enable(request: Request, store: Store = Depends(get_store),
                    terminal: PaymentTerminal = Depends(get_store_terminal), db: Session = Depends(get_db)):
    svc.set_enabled(db, terminal, True)
    flash(request, "flash.terminal_enabled", name=terminal.name)
    return RedirectResponse(_list_url(store), status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{terminal_id}/disable", response_class=HTMLResponse)
def terminal_disable(request: Request, store: Store = Depends(get_store),
                     terminal: PaymentTerminal = Depends(get_store_terminal), db: Session = Depends(get_db)):
    svc.set_enabled(db, terminal, False)
    flash(request, "flash.terminal_disabled", name=terminal.name)
    return RedirectResponse(_list_url(store), status_code=status.HTTP_303_SEE_OTHER)
