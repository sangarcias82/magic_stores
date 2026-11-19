"""Admin: store theme editor (primary / secondary / accent colors)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import translator
from app.models import Store
from app.routers.admin import admin_title, flash, render_admin
from app.routers.admin_catalog import get_store
from app.routers.admin_form_errors import shown
from app.services.store_admin import ThemeForm, save_theme, validate_theme
from app.services.storefront import DEFAULT_THEME, THEME_COLOR_KEYS

router = APIRouter(
    prefix="/admin/stores/{store_slug}/theme",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


def _theme_page(request: Request, store: Store, form: ThemeForm, status_code: int = 200):
    t = translator(request)
    return render_admin(
        request,
        "admin/theme.html",
        {
            "page_title": admin_title(t, t("admin.tabs.theme"), store.name),
            "section": "stores",
            "store_tab": "theme",
            "store": store,
            "form": shown(form, t),  # errors in the page language
            "labels": {key: t(f"admin.theme.label.{key}") for key in THEME_COLOR_KEYS},
            "defaults": DEFAULT_THEME,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def theme_edit(request: Request, store: Store = Depends(get_store)):
    return _theme_page(request, store, ThemeForm.from_store(store))


@router.post("", response_class=HTMLResponse)
def theme_update(
    request: Request,
    primary: str = Form(""),
    secondary: str = Form(""),
    accent: str = Form(""),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    form = ThemeForm(primary=primary, secondary=secondary, accent=accent)
    colors = validate_theme(form)
    if form.errors:
        return _theme_page(request, store, form, status_code=status.HTTP_400_BAD_REQUEST)
    save_theme(db, store, colors)
    flash(request, "flash.theme_saved")
    return RedirectResponse(f"/admin/stores/{store.slug}/theme", status_code=status.HTTP_303_SEE_OTHER)
