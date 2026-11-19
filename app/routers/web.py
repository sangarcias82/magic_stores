from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from app.db import get_db
from app.i18n import get_locale, template_context, translator, use_store_locale
from sqlalchemy import select

from app.models import PaymentTerminal, ShippingMethod, Store
from app.services import bank_transfer, catalog, checkout, storefront
from app.services.translations import ContentTranslations

router = APIRouter(tags=["web"])


def _open_store(request: Request, db: Session, store_slug: str) -> tuple[Store, ContentTranslations]:
    """The store of a public page + its content translations in the page language."""
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    use_store_locale(request, store)  # its locale_default and UI overrides apply on its pages
    return store, ContentTranslations.load(db, get_locale(request), [store.id])


def _store_context(store: Store, content: ContentTranslations, **extra):
    ctx = {
        "store": content.store(store),  # translated copy; the DB row is never modified
        "theme": storefront.theme_from_store(store),
        "store_pages": storefront.list_store_pages(store.slug),
        "format_price": catalog.format_price,
        "storefront_override": storefront.storefront_has_override(store.slug),
    }
    ctx.update(extra)
    return ctx


@router.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    content = ContentTranslations.load(db, get_locale(request))
    stores = [content.store(s) for s in catalog.list_stores(db)]
    return storefront.platform_templates().TemplateResponse(
        request,
        "home.html",
        {"stores": stores, "page_title": translator(request)("home.title")},
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
    store, content = _open_store(request, db, store_slug)
    ctx = _store_context(
        store,
        content,
        categories=content.categories(catalog.list_categories(db, store)),
        products=content.product_items(catalog.list_products(db, store)),
    )
    ctx["page_title"] = ctx["store"].name
    return storefront.templates_for_store(store.slug).TemplateResponse(request, "store.html", ctx)


@router.get("/stores/{store_slug}/c/{category_slug}", response_class=HTMLResponse)
def category_page(store_slug: str, category_slug: str, request: Request, db: Session = Depends(get_db)):
    store, content = _open_store(request, db, store_slug)
    category = catalog.get_category_by_slug(db, store, category_slug)
    if category is None:
        raise HTTPException(status_code=404, detail="Category not found")
    categories = content.categories(catalog.list_categories(db, store))
    products = content.product_items(catalog.list_products(db, store, category_id=category.id))
    cat_out = next(c for c in categories if c.id == category.id)
    ctx = _store_context(store, content, category=cat_out, categories=categories, products=products)
    ctx["page_title"] = f"{cat_out.name} · {ctx['store'].name}"
    return storefront.templates_for_store(store.slug).TemplateResponse(request, "category.html", ctx)


@router.get("/stores/{store_slug}/p/{product_slug}", response_class=HTMLResponse)
def product_page(store_slug: str, product_slug: str, request: Request, db: Session = Depends(get_db)):
    store, content = _open_store(request, db, store_slug)
    product = catalog.get_product_by_slug(db, store, product_slug)
    if product is None or not product.is_active:  # hidden products are not public
        raise HTTPException(status_code=404, detail="Product not found")
    categories_by_id = {c.id: c for c in content.categories(catalog.list_categories(db, store))}
    product = content.product(product, categories_by_id)
    ctx = _store_context(
        store,
        content,
        product=product,
        category=categories_by_id.get(product.category_id),
        categories_by_id=categories_by_id,
    )
    ctx["page_title"] = f"{product.name} · {ctx['store'].name}"
    return storefront.templates_for_store(store.slug).TemplateResponse(request, "product.html", ctx)


@router.get("/stores/{store_slug}/orders/{order_id}", response_class=HTMLResponse)
def order_page(store_slug: str, order_id: str, request: Request, db: Session = Depends(get_db)):
    """An order as the shopper sees it (the id is the key, like the API's): its lines, totals, the
    payment's status and, for a bank transfer, the deposit instructions."""
    store, content = _open_store(request, db, store_slug)
    order = checkout.get_order(db, store, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    view = checkout.order_view(order)
    method = db.scalar(select(ShippingMethod).where(ShippingMethod.store_id == store.id,
                                                    ShippingMethod.code == order.shipping_code))
    instructions = view["payment"]["instructions"]
    ctx = _store_context(
        store, content, order=order, view=view,
        # In the page language: the method's translated name (the order keeps the name as entered),
        # and the note's translation copied with the payment.
        shipping_name=content.shipping_method_name(method) if method is not None else order.shipping_name,
        transfer_note=bank_transfer.note_for(instructions, content.locale) if instructions else "",
    )
    ctx["page_title"] = f"{order.public_id} · {ctx['store'].name}"
    return storefront.templates_for_store(store.slug).TemplateResponse(request, "order.html", ctx)


@router.get("/stores/{store_slug}/cart", response_class=HTMLResponse)
def cart_page(store_slug: str, request: Request, db: Session = Depends(get_db)):
    """The visitor's cart in this store: the React app (app/static/cart/cart.js) reads and changes it
    through /api/stores/<slug>/cart; the page itself holds no cart data (it can be cached)."""
    store, content = _open_store(request, db, store_slug)
    methods = db.scalars(select(ShippingMethod).where(ShippingMethod.store_id == store.id))
    terminals = db.scalars(select(PaymentTerminal).where(PaymentTerminal.store_id == store.id))
    # Checkout choices in the page language (the JSON API gives names as entered): code -> name.
    names = {"shipping": {m.code: content.shipping_method_name(m) for m in methods},
             "terminals": {t.code: content.terminal_name(t) for t in terminals}}
    ctx = _store_context(store, content, checkout_names=names)
    return storefront.templates_for_store(store.slug).TemplateResponse(request, "cart.html", ctx)


@router.get("/stores/{store_slug}/pages/{page_slug}", response_class=HTMLResponse)
def store_custom_page(store_slug: str, page_slug: str, request: Request, db: Session = Depends(get_db)):
    store, content = _open_store(request, db, store_slug)
    page_path = storefront.resolve_page_file(store.slug, page_slug)
    if page_path is None:
        raise HTTPException(status_code=404, detail="Page not found")

    loaders = []
    for root in (storefront.storefront_dir(store.slug), storefront.default_dir()):
        pages_dir = root / "pages"
        if pages_dir.is_dir():
            loaders.append(FileSystemLoader(str(pages_dir)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html", "xml"]))
    page_templates = Jinja2Templates(env=env, context_processors=[template_context])
    ctx = _store_context(store, content)
    ctx["page_title"] = f"{page_slug} · {ctx['store'].name}"
    return page_templates.TemplateResponse(request, page_path.name, ctx)
