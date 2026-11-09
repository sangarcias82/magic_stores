from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.db import get_db
from app.services import catalog, storefront

router = APIRouter(tags=["web"])


def _store_context(store, **extra):
    ctx = {
        "store": store,
        "theme": storefront.theme_from_store(store),
        "store_pages": storefront.list_store_pages(store.slug),
        "format_price": catalog.format_price,
        "storefront_override": storefront.storefront_has_override(store.slug),
    }
    ctx.update(extra)
    return ctx


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    stores = catalog.list_stores(db)
    return storefront.platform_templates().TemplateResponse(
        request,
        "home.html",
        {"stores": stores, "page_title": "Stores"},
    )


@router.get("/stores/{store_slug}/assets/{asset_path:path}")
def store_asset(store_slug: str, asset_path: str, db: Session = Depends(get_db)):
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    response = storefront.static_response(store.slug, asset_path)
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Asset not found")
    return response


@router.get("/stores/{store_slug}", response_class=HTMLResponse)
def store_home(store_slug: str, request: Request, db: Session = Depends(get_db)):
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    categories = catalog.list_categories(db, store)
    products = catalog.list_products(db, store)
    return storefront.templates_for_store(store.slug).TemplateResponse(
        request,
        "store.html",
        _store_context(
            store,
            categories=categories,
            products=products,
            page_title=store.name,
        ),
    )


@router.get("/stores/{store_slug}/c/{category_slug}", response_class=HTMLResponse)
def category_page(store_slug: str, category_slug: str, request: Request, db: Session = Depends(get_db)):
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    category = catalog.get_category_by_slug(db, store, category_slug)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    categories = catalog.list_categories(db, store)
    products = catalog.list_products(db, store, category_id=category.id)
    cat_out = next((c for c in categories if c.id == category.id), None)
    return storefront.templates_for_store(store.slug).TemplateResponse(
        request,
        "category.html",
        _store_context(
            store,
            category=cat_out or category,
            categories=categories,
            products=products,
            page_title=f"{category.name} · {store.name}",
        ),
    )


@router.get("/stores/{store_slug}/p/{product_slug}", response_class=HTMLResponse)
def product_page(store_slug: str, product_slug: str, request: Request, db: Session = Depends(get_db)):
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    product = catalog.get_product_by_slug(db, store, product_slug)
    if product is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return storefront.templates_for_store(store.slug).TemplateResponse(
        request,
        "product.html",
        _store_context(
            store,
            product=product,
            page_title=f"{product.name} · {store.name}",
        ),
    )


@router.get("/stores/{store_slug}/pages/{page_slug}", response_class=HTMLResponse)
def store_custom_page(store_slug: str, page_slug: str, request: Request, db: Session = Depends(get_db)):
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    page_path = storefront.resolve_page_file(store.slug, page_slug)
    if page_path is None:
        raise HTTPException(status_code=404, detail="Page not found")

    loaders = []
    for root in (storefront.storefront_dir(store.slug), storefront.default_dir()):
        pages_dir = root / "pages"
        if pages_dir.is_dir():
            loaders.append(FileSystemLoader(str(pages_dir)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html", "xml"]))
    page_templates = Jinja2Templates(env=env)
    return page_templates.TemplateResponse(
        request,
        page_path.name,
        _store_context(store, page_title=f"{page_slug} · {store.name}"),
    )
