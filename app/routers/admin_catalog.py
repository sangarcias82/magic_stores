"""Admin: per-store category and property-definition management (create / edit / delete)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import Translator, translator
from app.models import Category, PropertyDefinition, PropertyType, Store
from app.routers.admin import admin_title, flash, render_admin
from app.routers.admin_form_errors import form_error_text, shown
from app.services import catalog
from app.services import category_admin as svc

router = APIRouter(
    prefix="/admin/stores/{store_slug}",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


# ----------------------------------------------------------------------------- lookups


def get_store(store_slug: str, db: Session = Depends(get_db)) -> Store:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return store


def get_store_category(
    category_id: int, store: Store = Depends(get_store), db: Session = Depends(get_db)
) -> Category:
    category = svc.get_category(db, store, category_id)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    return category


def get_category_definition(
    definition_id: int, category: Category = Depends(get_store_category), db: Session = Depends(get_db)
) -> PropertyDefinition:
    definition = svc.get_definition(db, category, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return definition


def _redirect(request: Request, url: str, msgid: str, /, **params: str) -> RedirectResponse:
    flash(request, msgid, **params)
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def property_type_labels(t: Translator) -> dict[PropertyType, str]:
    """Localized names of the property types, in the engine's order."""
    return {prop_type: t(f"proptype.{prop_type.value}") for prop_type in svc.PROPERTY_TYPE_LABELS}


def _store_url(store: Store) -> str:
    return f"/admin/stores/{store.slug}"


def _category_url(store: Store, category: Category) -> str:
    return f"{_store_url(store)}/categories/{category.id}"


# ------------------------------------------------------------------------ store detail


@router.get("", response_class=HTMLResponse)
def store_detail(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    return render_admin(
        request,
        "admin/store_detail.html",
        {
            "page_title": admin_title(translator(request), store.name),
            "section": "stores",
            "store_tab": "categories",
            "store": store,
            "tree": svc.category_tree(svc.store_categories(db, store)),
            "product_counts": svc.product_counts(db, store),
        },
    )


# -------------------------------------------------------------------------- categories


def _category_form_page(
    request: Request,
    db: Session,
    store: Store,
    form: svc.CategoryForm,
    category: Optional[Category] = None,
    status_code: int = 200,
):
    categories = svc.store_categories(db, store)
    t = translator(request)
    return render_admin(
        request,
        "admin/category.html",
        {
            "page_title": admin_title(t, category.name if category else t("admin.category.new"), store.name),
            "section": "stores",
            "store": store,
            "category": category,
            "form": shown(form, t),  # errors in the page language
            "parent_choices": svc.parent_choices(categories, category),
            "definitions": svc.sorted_definitions(category) if category else [],
            "type_labels": property_type_labels(t),
            "category_names": {c.id: c.name for c in categories},
            "store_tab": "categories",
            "product_count": svc.product_counts(db, store).get(category.id, 0) if category else 0,
        },
        status_code=status_code,
    )


@router.get("/categories/new", response_class=HTMLResponse)
def category_new(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    return _category_form_page(request, db, store, svc.CategoryForm())


@router.post("/categories", response_class=HTMLResponse)
def category_create(
    request: Request,
    slug: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    parent_id: str = Form(""),
    sort_order: str = Form("0"),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    form = svc.CategoryForm(slug, name, description, parent_id, sort_order)
    values = svc.validate_category(db, store, form)
    category = None if form.errors else svc.save_category(db, store, values)
    if category is None:
        if not form.errors:  # unique constraint hit between validation and insert
            form.errors["slug"] = ("category_slug_taken", {})
        return _category_form_page(request, db, store, form, status_code=status.HTTP_400_BAD_REQUEST)
    return _redirect(request, _category_url(store, category), "flash.category_created", name=category.name)


@router.get("/categories/{category_id}", response_class=HTMLResponse)
def category_edit(
    request: Request,
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    db: Session = Depends(get_db),
):
    return _category_form_page(request, db, store, svc.CategoryForm.from_category(category), category)


@router.post("/categories/{category_id}", response_class=HTMLResponse)
def category_update(
    request: Request,
    slug: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    parent_id: str = Form(""),
    sort_order: str = Form("0"),
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    db: Session = Depends(get_db),
):
    form = svc.CategoryForm(slug, name, description, parent_id, sort_order)
    values = svc.validate_category(db, store, form, current=category)
    saved = None if form.errors else svc.save_category(db, store, values, current=category)
    if saved is None:
        if not form.errors:
            form.errors["slug"] = ("category_slug_taken", {})
            category = svc.get_category(db, store, category.id)  # reload after rollback
        return _category_form_page(request, db, store, form, category, status_code=status.HTTP_400_BAD_REQUEST)
    return _redirect(request, _category_url(store, saved), "flash.category_saved", name=saved.name)


def confirm_page(request: Request, store: Store, *, title: str, message: str, consequences: list[str],
                 blockers: list[str], action: str, cancel: str, store_tab: str = "categories"):
    return render_admin(
        request,
        "admin/confirm_delete.html",
        {
            "page_title": admin_title(translator(request), title),
            "section": "stores",
            "store_tab": store_tab,
            "store": store,
            "title": title,
            "message": message,
            "consequences": consequences,
            "blockers": blockers,
            "action": action,
            "cancel": cancel,
        },
        status_code=status.HTTP_409_CONFLICT if blockers and request.method == "POST" else 200,
    )


def _category_delete_page(request: Request, db: Session, store: Store, category: Category):
    t = translator(request)
    n_defs = len(category.property_definitions)
    return confirm_page(
        request,
        store,
        title=t("admin.category.delete_title", name=category.name),
        message=t("admin.category.delete_message", name=category.name, slug=category.slug),
        consequences=[t("admin.category.delete_consequence", count=n_defs)] if n_defs else [],
        blockers=[form_error_text(t, blocker) for blocker in svc.category_delete_blockers(db, category)],
        action=f"{_category_url(store, category)}/delete",
        cancel=_category_url(store, category),
    )


@router.get("/categories/{category_id}/delete", response_class=HTMLResponse)
def category_delete_confirm(
    request: Request,
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    db: Session = Depends(get_db),
):
    return _category_delete_page(request, db, store, category)


@router.post("/categories/{category_id}/delete", response_class=HTMLResponse)
def category_delete(
    request: Request,
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    db: Session = Depends(get_db),
):
    if svc.category_delete_blockers(db, category):
        return _category_delete_page(request, db, store, category)
    name = category.name
    svc.delete_category(db, category)
    return _redirect(request, _store_url(store), "flash.category_deleted", name=name)


# ---------------------------------------------------------------- property definitions


def _property_form_page(
    request: Request,
    db: Session,
    store: Store,
    category: Category,
    form: svc.PropertyForm,
    definition: Optional[PropertyDefinition] = None,
    status_code: int = 200,
):
    t = translator(request)
    type_labels = property_type_labels(t)
    return render_admin(
        request,
        "admin/property_form.html",
        {
            "page_title": admin_title(t, definition.label if definition else t("admin.property.new"), category.name),
            "section": "stores",
            "store_tab": "categories",
            "store": store,
            "category": category,
            "definition": definition,
            "form": shown(form, t),  # errors in the page language
            "type_choices": [(prop_type.value, label) for prop_type, label in type_labels.items()],
            "type_labels_by_value": {prop_type.value: label for prop_type, label in type_labels.items()},
            "ref_choices": svc.category_tree(svc.store_categories(db, store)),
            "value_count": svc.count_values(db, definition) if definition else 0,
        },
        status_code=status_code,
    )


def _property_form_from_post(
    key: str, label: str, prop_type: str, required: Optional[str], sort_order: str, options: str, ref_category_id: str
) -> svc.PropertyForm:
    return svc.PropertyForm(
        key=key,
        label=label,
        prop_type=prop_type,
        required=required is not None,
        sort_order=sort_order,
        options=options,
        ref_category_id=ref_category_id,
    )


@router.get("/categories/{category_id}/properties/new", response_class=HTMLResponse)
def property_new(
    request: Request,
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    db: Session = Depends(get_db),
):
    return _property_form_page(request, db, store, category, svc.PropertyForm())


@router.post("/categories/{category_id}/properties", response_class=HTMLResponse)
def property_create(
    request: Request,
    key: str = Form(""),
    label: str = Form(""),
    prop_type: str = Form(PropertyType.text.value),
    required: Optional[str] = Form(None),
    sort_order: str = Form("0"),
    options: str = Form(""),
    ref_category_id: str = Form(""),
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    db: Session = Depends(get_db),
):
    form = _property_form_from_post(key, label, prop_type, required, sort_order, options, ref_category_id)
    values = svc.validate_property(db, store, category, form)
    definition = None if form.errors else svc.save_property(db, category, values)
    if definition is None:
        if not form.errors:
            form.errors["key"] = ("key_taken", {})
        return _property_form_page(request, db, store, category, form, status_code=status.HTTP_400_BAD_REQUEST)
    return _redirect(request, _category_url(store, category), "flash.property_added", label=definition.label)


@router.get("/categories/{category_id}/properties/{definition_id}", response_class=HTMLResponse)
def property_edit(
    request: Request,
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    definition: PropertyDefinition = Depends(get_category_definition),
    db: Session = Depends(get_db),
):
    return _property_form_page(
        request, db, store, category, svc.PropertyForm.from_definition(definition), definition
    )


@router.post("/categories/{category_id}/properties/{definition_id}", response_class=HTMLResponse)
def property_update(
    request: Request,
    key: str = Form(""),
    label: str = Form(""),
    prop_type: str = Form(PropertyType.text.value),
    required: Optional[str] = Form(None),
    sort_order: str = Form("0"),
    options: str = Form(""),
    ref_category_id: str = Form(""),
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    definition: PropertyDefinition = Depends(get_category_definition),
    db: Session = Depends(get_db),
):
    form = _property_form_from_post(key, label, prop_type, required, sort_order, options, ref_category_id)
    values = svc.validate_property(db, store, category, form, current=definition)
    saved = None if form.errors else svc.save_property(db, category, values, current=definition)
    if saved is None:
        if not form.errors:
            form.errors["key"] = ("key_taken", {})
            definition = svc.get_definition(db, category, definition.id)  # reload after rollback
        return _property_form_page(
            request, db, store, category, form, definition, status_code=status.HTTP_400_BAD_REQUEST
        )
    return _redirect(request, _category_url(store, category), "flash.property_saved", label=saved.label)


def _property_delete_page(request: Request, db: Session, store: Store, category: Category,
                          definition: PropertyDefinition):
    t = translator(request)
    n_values = svc.count_values(db, definition)
    return confirm_page(
        request,
        store,
        title=t("admin.property.delete_title", label=definition.label),
        message=t(
            "admin.property.delete_message", label=definition.label, key=definition.key, category=category.name
        ),
        consequences=[t("admin.property.delete_consequence", count=n_values)] if n_values else [],
        blockers=[],
        action=f"{_category_url(store, category)}/properties/{definition.id}/delete",
        cancel=_category_url(store, category),
    )


@router.get("/categories/{category_id}/properties/{definition_id}/delete", response_class=HTMLResponse)
def property_delete_confirm(
    request: Request,
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    definition: PropertyDefinition = Depends(get_category_definition),
    db: Session = Depends(get_db),
):
    return _property_delete_page(request, db, store, category, definition)


@router.post("/categories/{category_id}/properties/{definition_id}/delete")
def property_delete(
    request: Request,
    store: Store = Depends(get_store),
    category: Category = Depends(get_store_category),
    definition: PropertyDefinition = Depends(get_category_definition),
    db: Session = Depends(get_db),
):
    label = definition.label
    svc.delete_property(db, definition)
    return _redirect(request, _category_url(store, category), "flash.property_deleted", label=label)
