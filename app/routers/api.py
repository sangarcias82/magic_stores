from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.schemas import CategoryOut, HealthOut, ProductListItem, ProductOut, StoreOut
from app.services import catalog

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    settings = get_settings()
    return HealthOut(status="ok", app=settings.app_name)


@router.get("/stores", response_model=List[StoreOut])
def api_list_stores(db: Session = Depends(get_db)) -> List[StoreOut]:
    return catalog.list_stores(db)


@router.get("/stores/{store_slug}", response_model=StoreOut)
def api_get_store(store_slug: str, db: Session = Depends(get_db)) -> StoreOut:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return StoreOut.model_validate(store)


@router.get("/stores/{store_slug}/categories", response_model=List[CategoryOut])
def api_list_categories(store_slug: str, db: Session = Depends(get_db)) -> List[CategoryOut]:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return catalog.list_categories(db, store)


@router.get("/stores/{store_slug}/products", response_model=List[ProductListItem])
def api_list_products(
    store_slug: str,
    category: Optional[str] = Query(default=None, description="Category slug filter"),
    db: Session = Depends(get_db),
) -> List[ProductListItem]:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    category_id = None
    if category:
        cat = catalog.get_category_by_slug(db, store, category)
        if cat is None:
            raise HTTPException(status_code=404, detail="Category not found")
        category_id = cat.id
    return catalog.list_products(db, store, category_id=category_id)


@router.get("/stores/{store_slug}/products/{product_slug}", response_model=ProductOut)
def api_get_product(store_slug: str, product_slug: str, db: Session = Depends(get_db)) -> ProductOut:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    product = catalog.get_product_by_slug(db, store, product_slug)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product
