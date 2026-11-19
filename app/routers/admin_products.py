"""Admin: products of a store (create / edit / delete). Form fields come from the product's
category property definitions; see ``app.services.product_admin`` for the value format."""

from __future__ import annotations

from dataclasses import replace
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import Translator, get_locale, translator
from app.models import Category, Product, StockMovementKind, Store, User
from app.routers.admin import admin_title, flash, render_admin
from app.routers.admin_catalog import confirm_page, get_store, property_type_labels
from app.services import catalog, category_admin
from app.services import product_admin as svc
from app.services import stock as stock_svc
from app.services.inventory_export import InventoryExportError, export_inventory

router = APIRouter(
    prefix="/admin/stores/{store_slug}/products",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)


async def form_fields(request: Request) -> dict[str, str]:
    """All submitted form fields as strings (the product form has dynamic property fields)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


def get_store_product(product_id: int, store: Store = Depends(get_store), db: Session = Depends(get_db)) -> Product:
    product = svc.get_product(db, store, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def _products_url(store: Store) -> str:
    return f"/admin/stores/{store.slug}/products"


def field_error_text(t: Translator, error: svc.FieldError) -> str:
    """A product-form validation error in the admin's language (literal keys: tests/test_i18n.py
    finds them)."""
    code, params = error
    messages = {
        "slug_required": lambda: t("admin.product.error.slug_required"),
        "slug_format": lambda: t("admin.product.error.slug_format", **params),
        "slug_taken": lambda: t("admin.product.error.slug_taken"),
        "sku_required": lambda: t("admin.product.error.sku_required"),
        "sku_format": lambda: t("admin.product.error.sku_format", **params),
        "sku_taken": lambda: t("admin.product.error.sku_taken", **params),
        "name_required": lambda: t("admin.product.error.name_required"),
        "name_too_long": lambda: t("admin.product.error.name_too_long", **params),
        "description_too_long": lambda: t("admin.product.error.description_too_long", **params),
        "price_required": lambda: t("admin.product.error.price_required"),
        "price_format": lambda: t("admin.product.error.price_format"),
        "price_decimals": lambda: t("admin.product.error.price_decimals"),
        "price_negative": lambda: t("admin.product.error.price_negative"),
        "price_too_large": lambda: t("admin.product.error.price_too_large"),
        "stock_required": lambda: t("admin.product.error.stock_required"),
        "stock_format": lambda: t("admin.product.error.stock_format"),
        "stock_negative": lambda: t("admin.product.error.stock_negative"),
        "stock_too_large": lambda: t("admin.product.error.stock_too_large"),
        "threshold_required": lambda: t("admin.low_stock.error.required"),
        "threshold_negative": lambda: t("admin.low_stock.error.negative"),
        "threshold_format": lambda: t("admin.low_stock.error.format", **params),
        "status_invalid": lambda: t("admin.product.error.status_invalid"),
        "value_required": lambda: t("admin.product.error.value_required", **params),
        "value_too_long": lambda: t("admin.product.error.value_too_long", **params),
        "number_format": lambda: t("admin.product.error.number_format"),
        "boolean_choice": lambda: t("admin.product.error.boolean_choice"),
        "enum_choice": lambda: t("admin.product.error.enum_choice"),
        "image_url": lambda: t("admin.product.error.image_url"),
        "category_choice": lambda: t("admin.product.error.category_choice"),
    }
    return messages[code]()


def stock_error_text(t: Translator, error: svc.FieldError) -> str:
    """A stock adjustment / stock field error in the admin's language (literal keys for
    tests/test_i18n.py)."""
    code, params = error
    messages = {
        "delta_required": lambda: t("admin.stock.error.delta_required"),
        "delta_format": lambda: t("admin.stock.error.delta_format"),
        "delta_zero": lambda: t("admin.stock.error.delta_zero"),
        "delta_too_large": lambda: t("admin.stock.error.delta_too_large"),
        "reason_required": lambda: t("admin.stock.error.reason_required"),
        "reason_too_long": lambda: t("admin.stock.error.reason_too_long", **params),
        "below_zero": lambda: t("admin.stock.error.below_zero", **params),
        "too_large": lambda: t("admin.stock.error.too_large", **params),
        "stock_changed": lambda: t("admin.stock.error.stock_changed", **params),
    }
    return messages[code]()


def movement_kind_labels(t: Translator) -> dict[StockMovementKind, str]:
    return {
        StockMovementKind.initial: t("admin.stock.kind.initial"),
        StockMovementKind.edit: t("admin.stock.kind.edit"),
        StockMovementKind.adjustment: t("admin.stock.kind.adjustment"),
        StockMovementKind.pack_import: t("admin.stock.kind.pack_import"),
        StockMovementKind.inventory_import: t("admin.stock.kind.inventory_import"),
        StockMovementKind.order: t("admin.stock.kind.order"),
    }


def _any_error_text(t: Translator, error: svc.FieldError) -> str:
    """Product form errors, plus the stock ones its Stock field can get."""
    try:
        return field_error_text(t, error)
    except KeyError:
        return stock_error_text(t, error)


def _shown_adjustment(t: Translator, form: Optional[stock_svc.AdjustmentForm]) -> Optional[stock_svc.AdjustmentForm]:
    if form is None:
        return None
    return replace(form, errors={name: stock_error_text(t, error) for name, error in form.errors.items()})


def _lost_unique_race(db: Session, store: Store, form: svc.ProductForm, current: Optional[Product] = None) -> None:
    """The save hit a unique constraint after validation passed (another request took the slug or
    the SKU in between): say which one, like validation would have."""
    clashes = svc.unique_clash_errors(db, store, form.slug, form.sku, current)
    form.errors.update(clashes or {"slug": ("slug_taken", {})})


def _parse_id(raw: Optional[str]) -> Optional[int]:
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


# ------------------------------------------------------------------------------ list


def _list_page(
    request: Request,
    db: Session,
    store: Store,
    category_id: Optional[str],
    sku: Optional[str] = None,
    adjusting: Optional[tuple[int, stock_svc.AdjustmentForm]] = None,
    status_code: int = 200,
    page_error: Optional[str] = None,
):
    """The product list, filtered by category and / or SKU (contains, ignoring case).
    ``adjusting``: (product id, its refused adjustment form) to show open, with its errors, in
    that product's row. ``page_error``: a message above the list (e.g. nothing to export)."""
    categories = category_admin.store_categories(db, store)
    selected = _parse_id(category_id)
    if selected not in {c.id for c in categories}:
        selected = None
    sku_query = svc.sku_search_term(sku or "")
    t = translator(request)
    return render_admin(
        request,
        "admin/products.html",
        {
            "page_title": admin_title(t, t("admin.tabs.products"), store.name),
            "section": "stores",
            "store_tab": "products",
            "store": store,
            "products": svc.store_products(db, store, category_id=selected, sku_contains=sku_query),
            "store_has_products": svc.store_has_products(db, store),  # the export button
            "category_tree": category_admin.category_tree(categories),
            "selected_category": selected,
            "sku_query": sku_query,
            "format_price": catalog.format_price,
            "adjusting_id": adjusting[0] if adjusting else None,
            "adjusting_form": _shown_adjustment(t, adjusting[1]) if adjusting else None,
            "page_error": page_error,
        },
        status_code=status_code,
    )


def _list_url(store: Store, category_id: Optional[str] = None, sku: Optional[str] = None) -> str:
    """The list URL with its filters (only valid ones)."""
    query = {}
    parsed = _parse_id(category_id)
    if parsed is not None:
        query["category_id"] = parsed
    term = svc.sku_search_term(sku or "")
    if term:
        query["sku"] = term
    return _products_url(store) + (f"?{urlencode(query)}" if query else "")


@router.get("", response_class=HTMLResponse)
def product_list(
    request: Request,
    category_id: Optional[str] = None,
    sku: Optional[str] = None,
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    return _list_page(request, db, store, category_id, sku)


# Declared before "/{product_id}", which would otherwise take "export.csv" for an id.
@router.get("/export.csv")
def product_inventory_export(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    """Inventory CSV (SKU + stock on hand of every product) to edit offline; see
    app/services/inventory_export.py. A store without products: the list, with the reason (409)."""
    try:
        export = export_inventory(db, store.slug, get_locale(request))  # headers in the page's language
    except InventoryExportError:  # the store exists (get_store): it has no products
        t = translator(request)
        return _list_page(request, db, store, None, status_code=status.HTTP_409_CONFLICT,
                          page_error=t("admin.inventory.error.no_products"))
    return Response(
        content=export.csv_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )


# ------------------------------------------------------------------------------ form


def _form_page(
    request: Request,
    db: Session,
    store: Store,
    category: Category,
    form: svc.ProductForm,
    product: Optional[Product] = None,
    status_code: int = 200,
    adjustment: Optional[stock_svc.AdjustmentForm] = None,
):
    categories = category_admin.store_categories(db, store)
    definitions = category_admin.sorted_definitions(category)
    t = translator(request)
    # The page shows the errors as text in its language; the form itself keeps the codes.
    shown = replace(form, errors={name: _any_error_text(t, error) for name, error in form.errors.items()})
    history, history_total = stock_svc.product_history(db, product) if product else ([], 0)
    return render_admin(
        request,
        "admin/product_form.html",
        {
            "page_title": admin_title(t, product.name if product else t("admin.product.new"), store.name),
            "section": "stores",
            "store_tab": "products",
            "store": store,
            "category": category,
            "product": product,
            "form": shown,
            "definitions": definitions,
            "field_name": svc.field_name,
            "ref_choices": {
                d.id: svc.ref_choices(categories, d) for d in definitions if d.prop_type.value == "category_ref"
            },
            "type_labels": property_type_labels(t),
            "history": history,
            "history_total": history_total,
            "kind_labels": movement_kind_labels(t),
            "adjustment": _shown_adjustment(t, adjustment) or stock_svc.AdjustmentForm(),
        },
        status_code=status_code,
    )


def _pick_category_page(request: Request, db: Session, store: Store, error: Optional[str] = None):
    t = translator(request)
    return render_admin(
        request,
        "admin/product_pick_category.html",
        {
            "page_title": admin_title(t, t("admin.product.new"), store.name),
            "section": "stores",
            "store_tab": "products",
            "store": store,
            "tree": category_admin.category_tree(category_admin.store_categories(db, store)),
            "error": error,
        },
        status_code=status.HTTP_400_BAD_REQUEST if error else 200,
    )


@router.get("/new", response_class=HTMLResponse)
def product_new(
    request: Request,
    category_id: Optional[str] = None,
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    parsed = _parse_id(category_id)
    category = category_admin.get_category(db, store, parsed) if parsed is not None else None
    if category is None:
        # Step 1: the category decides which property fields the form has.
        return _pick_category_page(request, db, store)
    return _form_page(request, db, store, category, svc.ProductForm())


@router.post("", response_class=HTMLResponse)
def product_create(
    request: Request,
    data: dict[str, str] = Depends(form_fields),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    parsed = _parse_id(data.get("category_id"))
    category = category_admin.get_category(db, store, parsed) if parsed is not None else None
    if category is None:
        return _pick_category_page(request, db, store, error=translator(request)("admin.pick_category.invalid"))

    form = svc.ProductForm.from_post(data, category)
    columns, payloads = svc.validate_product(db, store, category, form)
    product = None if form.errors else svc.save_product(db, store, category, columns, payloads, user=user)
    if product is None:
        if not form.errors:  # unique constraint hit between validation and insert
            _lost_unique_race(db, store, form)
        return _form_page(request, db, store, category, form, status_code=status.HTTP_400_BAD_REQUEST)
    flash(request, "flash.product_created", name=product.name)
    return RedirectResponse(f"{_products_url(store)}/{product.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{product_id}", response_class=HTMLResponse)
def product_edit(
    request: Request,
    store: Store = Depends(get_store),
    product: Product = Depends(get_store_product),
    db: Session = Depends(get_db),
):
    return _form_page(request, db, store, product.category, svc.ProductForm.from_product(product), product)


@router.post("/{product_id}", response_class=HTMLResponse)
def product_update(
    request: Request,
    data: dict[str, str] = Depends(form_fields),
    store: Store = Depends(get_store),
    product: Product = Depends(get_store_product),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    category = product.category  # fixed after creation; any posted category_id is ignored
    form = svc.ProductForm.from_post(data, category)
    columns, payloads = svc.validate_product(db, store, category, form, current=product)
    try:
        saved = None if form.errors else svc.save_product(
            db, store, category, columns, payloads, current=product,
            stock_seen=svc.parse_stock_seen(form.stock_seen), user=user,
        )
    except stock_svc.StockError as exc:
        # An adjustment changed the stock after this form was opened: nothing saved. The form keeps
        # what was typed and now "saw" the current stock, so saving again applies it on purpose.
        product = svc.get_product(db, store, product.id)  # reload after rollback
        form.errors["stock"] = exc.error
        form.stock_seen = str(product.stock_on_hand)
        return _form_page(request, db, store, product.category, form, product,
                          status_code=status.HTTP_409_CONFLICT)
    if saved is None:
        if not form.errors:
            product = svc.get_product(db, store, product.id)  # reload after rollback
            category = product.category
            _lost_unique_race(db, store, form, current=product)
        return _form_page(request, db, store, category, form, product, status_code=status.HTTP_400_BAD_REQUEST)
    flash(request, "flash.product_saved", name=saved.name)
    return RedirectResponse(f"{_products_url(store)}/{saved.id}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------- stock

RETURN_LIST = "list"  # the adjustment came from the product list (else: the product page)


@router.post("/{product_id}/stock", response_class=HTMLResponse)
def product_stock_adjust(
    request: Request,
    data: dict[str, str] = Depends(form_fields),
    store: Store = Depends(get_store),
    product: Product = Depends(get_store_product),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Adjust the stock by a delta with a reason (a movement). From the list or the product page;
    answers on the same page."""
    from_list = data.get("return") == RETURN_LIST
    category_id = data.get("category_id") if from_list else None
    sku_filter = data.get("filter_sku") if from_list else None
    form = stock_svc.AdjustmentForm(delta=data.get("delta", ""), reason=data.get("reason", ""))
    delta, reason = stock_svc.parse_adjustment(form)
    if not form.errors:
        try:
            movement = stock_svc.adjust_stock(db, product, delta, reason, user=user)
        except stock_svc.StockError as exc:  # would go below zero (or over the maximum): refused
            form.errors["delta"] = exc.error
            product = svc.get_product(db, store, product.id)  # reload after rollback
    if form.errors:
        bad = status.HTTP_400_BAD_REQUEST
        if from_list:
            return _list_page(request, db, store, category_id, sku_filter, adjusting=(product.id, form),
                              status_code=bad)
        return _form_page(request, db, store, product.category, svc.ProductForm.from_product(product), product,
                          status_code=bad, adjustment=form)
    # "before" from the movement itself: exact even if another adjustment landed just before this one.
    flash(request, "flash.stock_adjusted", name=product.name, before=str(movement.quantity_after - delta),
          after=str(movement.quantity_after), delta=f"{delta:+d}")
    if from_list:
        return RedirectResponse(f"{_list_url(store, category_id, sku_filter)}#product-{product.id}",
                                status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(f"{_products_url(store)}/{product.id}#stock", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------- delete


def _delete_page(request: Request, store: Store, product: Product):
    url = f"{_products_url(store)}/{product.id}"
    t = translator(request)
    n_values = len(product.property_values)
    return confirm_page(
        request,
        store,
        title=t("admin.product.delete_title", name=product.name),
        message=t("admin.product.delete_message", name=product.name, slug=product.slug),
        consequences=[t("admin.product.delete_consequence", count=n_values)] if n_values else [],
        blockers=[],
        action=f"{url}/delete",
        cancel=url,
        store_tab="products",
    )


@router.get("/{product_id}/delete", response_class=HTMLResponse)
def product_delete_confirm(
    request: Request, store: Store = Depends(get_store), product: Product = Depends(get_store_product)
):
    return _delete_page(request, store, product)


@router.post("/{product_id}/delete")
def product_delete(
    request: Request,
    store: Store = Depends(get_store),
    product: Product = Depends(get_store_product),
    db: Session = Depends(get_db),
):
    name = product.name
    svc.delete_product(db, product)
    flash(request, "flash.product_deleted", name=name)
    return RedirectResponse(_products_url(store), status_code=status.HTTP_303_SEE_OTHER)
