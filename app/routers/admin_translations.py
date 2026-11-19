"""Admin: translations of one store (level 2 of the translations), tab "Translations".

- ``GET  /admin/stores/<slug>/translations?locale=en``: edit form for that language.
- ``POST /admin/stores/<slug>/translations``: save the content translations (all or nothing).
- ``POST /admin/stores/<slug>/translations/ui``: set this store's UI override for one key.
- ``POST /admin/stores/<slug>/translations/ui/delete``: remove one of those overrides.

The language to edit is ``?locale=`` (``?lang=`` already chooses the admin's own language).
All reads and writes go through ``app.services.translations``.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import DEFAULT_LOCALE, Translator, available_locales, catalogs, normalize_locale, translate, translator
from app.models import Store
from app.routers.admin import admin_title, flash, render_admin
from app.routers.admin_catalog import get_store
from app.services.translations import (
    ContentTranslations,
    TranslationError,
    delete_ui_translation,
    save_content_translations,
    set_ui_translation,
    store_ui_overrides,
    translatable_fields,
)

router = APIRouter(
    prefix="/admin/stores/{store_slug}/translations",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

# A store can have many products; each has several translatable texts (form fields).
MAX_FORM_FIELDS = 20_000


def _target_locale(store: Store, raw: Optional[str]) -> str:
    """The language being edited: ``?locale=`` if it is a UI language, else the first UI
    language that is not the store's own (its content is written in that one)."""
    chosen = normalize_locale(raw)
    if chosen:
        return chosen
    own = normalize_locale(store.locale_default) or DEFAULT_LOCALE
    return next((code for code in available_locales() if code != own), own)


def _error_text(t: Translator, error: TranslationError) -> str:
    """The service's error in the admin's language."""
    messages = {
        "empty": lambda: t("admin.translations.error.empty"),
        "too_long": lambda: t("admin.translations.error.too_long", **error.params),
        "unknown_key": lambda: t("admin.translations.error.unknown_key", **error.params),
        "placeholders": lambda: t("admin.translations.error.placeholders", **error.params),
        "placeholder_syntax": lambda: t("admin.translations.error.placeholder_syntax"),
    }
    return messages.get(error.code, lambda: str(error))()


def _page(request: Request, db: Session, store: Store, locale: str, *, values: Optional[dict[str, str]] = None,
          field_errors: Optional[dict[str, str]] = None, ui_form: Optional[dict[str, str]] = None,
          ui_error: Optional[str] = None, status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    fields = translatable_fields(db, store)
    saved = ContentTranslations.load(db, locale, [store.id]).texts
    if values is None:  # the stored translations; after a failed save, what the admin typed
        values = {f.form_name: saved.get((f.entity_type, f.entity_id, f.field), "") for f in fields}
    json_texts = catalogs().get(locale, {})
    return render_admin(
        request,
        "admin/translations.html",
        {
            "page_title": admin_title(t, t("admin.tabs.translations"), store.name),
            "section": "stores",
            "store_tab": "translations",
            "store": store,
            "locale": locale,
            "language": translate(locale, "lang.name"),  # the edited language, under its own name
            "locale_choices": [(code, translate(code, "lang.name")) for code in available_locales()],
            "source_locale": store.locale_default,
            "fields": fields,
            "values": values,
            "field_errors": field_errors or {},
            "translated_count": sum(1 for f in fields if saved.get((f.entity_type, f.entity_id, f.field))),
            "overrides": store_ui_overrides(db, store, locale),
            "json_texts": json_texts,
            "ui_keys": sorted(catalogs()[DEFAULT_LOCALE]),
            "ui_form": ui_form or {"key": "", "text": ""},
            "ui_error": ui_error,
        },
        status_code=status_code,
    )


def _back(store: Store, locale: str) -> RedirectResponse:
    query = urlencode({"locale": locale})
    return RedirectResponse(f"/admin/stores/{store.slug}/translations?{query}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def translations_page(request: Request, locale: Optional[str] = None, store: Store = Depends(get_store),
                      db: Session = Depends(get_db)):
    return _page(request, db, store, _target_locale(store, locale))


async def large_form(request: Request) -> dict[str, str]:
    """All submitted text fields (the edit form can be much larger than Starlette's default
    limit of 1,000 fields)."""
    form = await request.form(max_fields=MAX_FORM_FIELDS)
    return {key: value for key, value in form.items() if isinstance(value, str)}


@router.post("", response_class=HTMLResponse)
def translations_save(request: Request, submitted: dict[str, str] = Depends(large_form),
                      store: Store = Depends(get_store), db: Session = Depends(get_db)):
    locale = _target_locale(store, submitted.get("locale"))
    changes, errors = save_content_translations(db, store, locale, submitted)
    t = translator(request)
    if errors:
        return _page(request, db, store, locale, values=submitted,
                     field_errors={name: _error_text(t, error) for name, error in errors.items()},
                     status_code=status.HTTP_400_BAD_REQUEST)
    flash(request, "flash.translations_saved", count=changes, language=translate(locale, "lang.name"))
    return _back(store, locale)


@router.post("/ui", response_class=HTMLResponse)
def ui_override_set(request: Request, locale: str = Form(""), key: str = Form(""), text: str = Form(""),
                    store: Store = Depends(get_store), db: Session = Depends(get_db)):
    target = _target_locale(store, locale)
    key = key.strip()
    try:
        set_ui_translation(db, target, key, text, store=store)
    except TranslationError as exc:
        return _page(request, db, store, target, ui_form={"key": key, "text": text},
                     ui_error=_error_text(translator(request), exc), status_code=status.HTTP_400_BAD_REQUEST)
    flash(request, "flash.ui_override_saved", key=key)
    return _back(store, target)


@router.post("/ui/delete")
def ui_override_delete(request: Request, locale: str = Form(""), key: str = Form(""),
                       store: Store = Depends(get_store), db: Session = Depends(get_db)):
    target = _target_locale(store, locale)
    if delete_ui_translation(db, target, key, store=store):
        flash(request, "flash.ui_override_deleted", key=key)
    return _back(store, target)
