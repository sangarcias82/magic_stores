"""Public shipping API: a store's active methods and flat-rate quotes (README "Shipping quotes")."""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Store
from app.schemas import ShippingMethodOut, ShippingQuoteIn, ShippingQuoteMethodOut, ShippingQuoteOut
from app.services import catalog, shipping

router = APIRouter(prefix="/api/stores/{store_slug}/shipping", tags=["shipping"])


def _store_or_404(db: Session, store_slug: str) -> Store:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return store


@router.get("/methods", response_model=List[ShippingMethodOut])
def api_list_shipping_methods(store_slug: str, db: Session = Depends(get_db)) -> List[ShippingMethodOut]:
    store = _store_or_404(db, store_slug)
    return [ShippingMethodOut.model_validate(m) for m in shipping.list_active_methods(db, store)]


@router.post("/quote", response_model=ShippingQuoteOut)
def api_shipping_quote(store_slug: str, body: ShippingQuoteIn, db: Session = Depends(get_db)) -> ShippingQuoteOut:
    store = _store_or_404(db, store_slug)
    try:
        result = shipping.quote(db, store, body.method, subtotal_cents=body.subtotal_cents, country=body.country)
    except shipping.ShippingQuoteError as exc:
        # Same shape as FastAPI's own 422 body errors, so clients parse a single format.
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body", exc.field], "msg": exc.message, "type": exc.kind}],
        ) from exc
    return ShippingQuoteOut(
        store=store.slug,
        currency=store.currency,
        method=ShippingQuoteMethodOut(code=result.method.code, name=result.method.name),
        country=result.country,
        subtotal_cents=result.subtotal_cents,
        amount_cents=result.amount_cents,
        free_shipping=result.free_shipping,
    )
