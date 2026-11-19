"""Admin: a store's inventory page: export the stock numbers as CSV, and import them back
(app/services/inventory_export.py, inventory_import.py). Only stock changes. Also the stock log
and the low-stock list with the store's threshold (app/services/low_stock.py)."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import get_locale, translator
from app.models import Store, User
from app.db import fits_db_integer
from app.models import StockMovementKind
from app.routers.admin import admin_title, flash, parse_page, render_admin
from app.routers.admin_products import field_error_text, movement_kind_labels
from app.routers.admin_catalog import get_store
from app.services import inventory_import as svc
from app.services import low_stock
from app.services import stock as stock_svc
from app.services.catalog import format_price
from app.services.product_admin import store_has_products

router = APIRouter(
    prefix="/admin/stores/{store_slug}/inventory",
    tags=["admin"],
    dependencies=[Depends(require_admin)],
)

# 413 by number (see app/routers/admin_packs.py: the Starlette constant was renamed).
HTTP_413_CONTENT_TOO_LARGE = 413


def _page(
    request: Request,
    db: Session,
    store: Store,
    *,
    report: Optional[svc.InventoryImportReport] = None,
    error: Optional[str] = None,
    force: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    t = translator(request)
    locale = get_locale(request)
    return render_admin(
        request,
        "admin/inventory.html",
        {
            "page_title": admin_title(t, t("admin.inventory.title"), store.name),
            "section": "stores",
            "store_tab": "inventory",
            "store": store,
            "store_has_products": store_has_products(db, store),
            "low_count": low_stock.count_low(db, store),
            "report": report,
            "refused": [(row, svc.error_text(locale, row.error)) for row in report.refused] if report else [],
            "error": error,
            "force": force,
            "max_mb": svc.MAX_MB,
            "format_price": format_price,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def inventory_page(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    return _page(request, db, store)


@router.post("/import", response_class=HTMLResponse)
def inventory_import(
    request: Request,
    file: Optional[UploadFile] = File(None),
    force: Optional[str] = Form(None),
    store: Store = Depends(get_store),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Applies the uploaded CSV (partial: refused rows are listed, the others applied). The
    answer is the page with the report; a file that can't be used at all: 400 / 413, nothing applied."""
    apply_anyway = force is not None
    if file is None or not file.filename:
        return _page(request, db, store, error=translator(request)("admin.inventory.error.no_file"),
                     force=apply_anyway, status_code=status.HTTP_400_BAD_REQUEST)
    raw = file.file.read(svc.MAX_BYTES + 1)  # never more than the limit in memory
    try:
        report = svc.import_inventory(db, store.slug, raw, filename=file.filename, user=user, force=apply_anyway)
    except svc.InventoryImportError as exc:
        code = HTTP_413_CONTENT_TOO_LARGE if exc.error[0] == "too_large" else status.HTTP_400_BAD_REQUEST
        return _page(request, db, store, error=svc.error_text(get_locale(request), exc.error),
                     force=apply_anyway, status_code=code)
    return _page(request, db, store, report=report, force=apply_anyway)


@router.get("/log", response_class=HTMLResponse)
def stock_log(
    request: Request,
    sku: Optional[str] = None,
    kind: Optional[str] = None,
    product: Optional[str] = None,
    reason: Optional[str] = None,
    page: Optional[str] = None,
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    """The store's stock movement log (every product), newest first, 50 per page; filters: SKU
    contains, reason contains, type, one product (``?product=<id>``, linked from the product page)."""
    t = translator(request)
    chosen_kind = kind if kind in {k.value for k in StockMovementKind} else None
    try:
        product_id = int(product) if product else None
    except ValueError:
        product_id = None
    if product_id is not None and not fits_db_integer(product_id):
        product_id = None
    current = parse_page(page)
    sku_term = (sku or "").strip().upper()[:64]
    reason_term = " ".join((reason or "").split())[:stock_svc.LOG_REASON_FILTER_MAX]
    movements, total = stock_svc.store_movements(
        db, store.id, sku_contains=sku_term, kind=StockMovementKind(chosen_kind) if chosen_kind else None,
        product_id=product_id, reason_contains=reason_term, page=current,
    )
    product_filter = next((m.product for m in movements if m.product_id == product_id), None) if product_id else None
    pages = max(1, -(-total // stock_svc.LOG_PAGE_SIZE))
    query = {k: v for k, v in (("sku", sku_term), ("reason", reason_term), ("kind", chosen_kind or ""),
                               ("product", str(product_id) if product_id else "")) if v}
    return render_admin(
        request,
        "admin/stock_log.html",
        {
            "page_title": admin_title(t, t("admin.stock_log.title"), store.name),
            "section": "stores",
            "store_tab": "inventory",
            "store": store,
            "movements": movements,
            "total": total,
            "page": current,
            "pages": pages,
            "query": query,
            "query_string": urlencode(query),
            "sku_query": sku_term,
            "reason_query": reason_term,
            "kind": chosen_kind,
            "product_id": product_id,
            "product_filter": product_filter,
            "kind_labels": movement_kind_labels(t),
            "kinds": list(StockMovementKind),
        },
    )


# ------------------------------------------------------------------------------ low stock


def _low_stock_page(request: Request, db: Session, store: Store, *, threshold_form: Optional[str] = None,
                    error: Optional[str] = None, status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    return render_admin(
        request,
        "admin/low_stock.html",
        {
            "page_title": admin_title(t, t("admin.low_stock.title"), store.name),
            "section": "stores",
            "store_tab": "inventory",
            "store": store,
            "rows": low_stock.low_stock_rows(db, store),
            "threshold_form": str(store.low_stock_threshold) if threshold_form is None else threshold_form,
            "error": error,
            "threshold_max": low_stock.THRESHOLD_MAX,
        },
        status_code=status_code,
    )


@router.get("/low-stock", response_class=HTMLResponse)
def low_stock_list(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    """The store's products at or below their low-stock threshold (the product's own, else the
    store's), out of stock first; and the store's threshold (app/services/low_stock.py)."""
    return _low_stock_page(request, db, store)


@router.post("/low-stock/threshold", response_class=HTMLResponse)
def low_stock_threshold(request: Request, threshold: Optional[str] = Form(None), store: Store = Depends(get_store),
                        db: Session = Depends(get_db)):
    """Sets the store's threshold (0 to THRESHOLD_MAX units); 303 back to the list."""
    units, error = low_stock.parse_threshold(threshold or "", required=True)
    if error:
        t = translator(request)
        return _low_stock_page(request, db, store, threshold_form=threshold or "",
                               error=field_error_text(t, error), status_code=status.HTTP_400_BAD_REQUEST)
    low_stock.set_store_threshold(db, store, units)
    flash(request, "flash.low_stock_threshold_saved", threshold=units)
    return RedirectResponse(f"/admin/stores/{store.slug}/inventory/low-stock", status_code=status.HTTP_303_SEE_OTHER)
