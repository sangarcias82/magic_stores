from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import DEFAULT_LOCALE, Translator, available_locales, translate, translator
from app.models import User
from app.routers.admin_form_errors import shown
from app.services import catalog, storefront
from app.services.store_admin import StoreInput, clean_store_input, create_store

# Router-level guard: every route added under /admin requires an admin session.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

FLASH_KEY = "admin_flash"
# Highest ?page= of the paged admin lists (imports, payments): bigger numbers are capped, so the
# database OFFSET stays within what it accepts (a huge page used to be a 500).
MAX_PAGE = 100_000


def admin_title(t: Translator, *parts: str) -> str:
    """Page title like "Stores · Admin" (in the request's language)."""
    return " · ".join([*parts, t("admin.title")])


def parse_page(raw: Optional[str]) -> int:
    """``?page=`` of a paged admin list: a whole number from 1 to MAX_PAGE; anything else is page 1."""
    try:
        page = int(raw or 1)
    except ValueError:  # not a number (also: absurdly long digit strings)
        return 1
    return min(max(page, 1), MAX_PAGE)


def flash(request: Request, msgid: str, /, **params: str) -> None:
    """Store a translated one-time message for the next admin page."""
    request.session[FLASH_KEY] = translator(request)(msgid, **params)


def render_admin(request: Request, template: str, context: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    context.setdefault("flash", request.session.pop(FLASH_KEY, None))
    response = storefront.platform_templates().TemplateResponse(
        request, template, context, status_code=status_code
    )
    # Don't let the browser cache admin pages (back button after logout).
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("", response_class=HTMLResponse)
def admin_home(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render_admin(
        request,
        "admin/dashboard.html",
        {
            "page_title": admin_title(translator(request)),
            "user": user,
            "store_count": len(catalog.list_stores(db)),
            "section": "dashboard",
        },
    )


@router.get("/stores", response_class=HTMLResponse)
def admin_store_list(request: Request, db: Session = Depends(get_db)):
    t = translator(request)
    return render_admin(
        request,
        "admin/stores.html",
        {"page_title": admin_title(t, t("admin.stores.title")), "stores": catalog.list_stores(db), "section": "stores"},
    )


def _store_form(request: Request, data: StoreInput, status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    return render_admin(
        request,
        "admin/store_form.html",
        {
            "page_title": admin_title(t, t("admin.store_form.title")),
            "form": shown(data, t),  # errors in the page language
            "section": "stores",
            # Each language is listed under its own name (its catalog's "lang.name").
            "locale_choices": [(code, translate(code, "lang.name")) for code in available_locales()],
        },
        status_code=status_code,
    )


@router.get("/stores/new", response_class=HTMLResponse)
def admin_store_new(request: Request):
    return _store_form(request, StoreInput())


@router.post("/stores", response_class=HTMLResponse)
def admin_store_create(
    request: Request,
    slug: str = Form(""),
    name: str = Form(""),
    currency: str = Form("USD"),
    locale_default: str = Form(DEFAULT_LOCALE),
    db: Session = Depends(get_db),
):
    data = clean_store_input(db, slug, name, currency, locale_default)
    if not data.is_valid:
        return _store_form(request, data, status_code=status.HTTP_400_BAD_REQUEST)
    store = create_store(db, data)
    if store is None:  # slug taken between validation and insert
        data.errors["slug"] = ("store_slug_taken", {})
        return _store_form(request, data, status_code=status.HTTP_400_BAD_REQUEST)
    flash(request, "flash.store_created", name=store.name, slug=store.slug)
    return RedirectResponse("/admin/stores", status_code=status.HTTP_303_SEE_OTHER)
