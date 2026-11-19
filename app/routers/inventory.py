"""Inventory JSON API (admin session): look a product up by its SKU.

``GET /api/stores/{store_slug}/skus/{sku}`` answers the product that has the SKU in that store and
its stock on hand. SKUs and stock counts are admin data (the public product API only says
``in_stock``), so like the payments API it needs an admin session: 401 / 403 as JSON, answered
before any lookup, so it can't be used to probe which stores or SKUs exist.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import User, UserRole
from app.schemas import SkuLookupOut, SkuProductOut
from app.services import catalog
from app.services import product_admin

router = APIRouter(prefix="/api/stores/{store_slug}", tags=["inventory"])


def require_admin_api(user: Optional[User] = Depends(get_current_user)) -> User:
    """Admin session for a JSON route: 401 / 403 as JSON (no redirect to the sign-in page)."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in as an admin to use the inventory API")
    if user.role != UserRole.admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/skus/{sku}", response_model=SkuLookupOut, dependencies=[Depends(require_admin_api)])
def api_product_by_sku(store_slug: str, sku: str, db: Session = Depends(get_db)) -> SkuLookupOut:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    product = product_admin.product_by_sku(db, store, sku)
    if product is None:
        raise HTTPException(status_code=404, detail="No product with this SKU in this store")
    return SkuLookupOut(
        store=store.slug,
        currency=store.currency,
        sku=product.sku,
        stock_on_hand=product.stock_on_hand,
        in_stock=product.in_stock,
        product=SkuProductOut.model_validate(product),
    )
