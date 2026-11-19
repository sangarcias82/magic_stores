"""Public cart JSON API (no login): ``/api/stores/{slug}/cart``. The visitor's cart is found by the
``magic_stores_cart`` cookie, scoped to this path, so each store has its own. Rules:
app/services/cart.py. Errors use FastAPI's validation shape: ``{"detail": [{"loc", "msg", "type"}]}``."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Cart, Store
from app.schemas import CartAddIn, CartOut, CartQuantityIn
from app.services import cart as svc
from app.services import catalog

router = APIRouter(prefix="/api/stores/{store_slug}/cart", tags=["cart"])

NO_STORE = {"Cache-Control": "no-store"}  # a visitor's own data: never cached


def get_store(store_slug: str, db: Session = Depends(get_db)) -> Store:
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")
    return store


def _cart(request: Request, db: Session, store: Store) -> Optional[Cart]:
    return svc.find_cart(db, store, request.cookies.get(svc.COOKIE_NAME))


def _answer(body: dict[str, Any], status: int = 200, *, store: Optional[Store] = None,
            token: Optional[str] = None, replayed: bool = False) -> JSONResponse:
    response = JSONResponse(body, status_code=status, headers=dict(NO_STORE))
    if token is not None and store is not None:  # a new cart: its cookie, for this store's cart API only
        response.set_cookie(
            svc.COOKIE_NAME, token, max_age=svc.COOKIE_MAX_AGE, path=svc.cookie_path(store),
            httponly=True, samesite="lax", secure=get_settings().session_https_only,
        )
    if replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return response


def _refused(error: svc.CartError) -> JSONResponse:
    return JSONResponse({"detail": error.detail()}, status_code=error.status, headers=dict(NO_STORE))


@router.get("", response_model=CartOut)
def api_get_cart(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    """The visitor's cart in this store (empty without one: no cart or cookie is created). Lines
    older than CART_REVALIDATE_MINUTES are validated again first (repriced, stock checked)."""
    return _answer(svc.current_cart(db, store, _cart(request, db, store)))


@router.delete("", response_model=CartOut)
def api_clear_cart(request: Request, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    """Empties the cart: every line is removed (doing it again changes nothing)."""
    cart = _cart(request, db, store)
    svc.clear_cart(db, cart)
    return _answer(svc.current_cart(db, store, cart))


@router.post("/lines", response_model=CartOut)
def api_add_line(
    request: Request,
    body: CartAddIn,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    """Adds a product (by id or SKU) to the cart, creating the cart on the first add."""
    cart = _cart(request, db, store)
    try:
        if idempotency_key is not None:
            replay = svc.claim_key(db, store, idempotency_key, svc.body_fingerprint(body.model_dump()), cart)
            if replay is not None:
                return _answer(replay.response, replayed=True)
        try:
            product = svc.resolve_product(db, store, body.product_id, body.sku)
            token = None
            if cart is None:
                svc.check_quantity(product, body.quantity)  # refuse before creating a cart
                cart, token = svc.create_cart(db, store)
            svc.add_line(db, cart, product, body.quantity)
        except svc.CartError:
            if idempotency_key is not None:
                svc.release_key(db, store, idempotency_key)
            raise
    except svc.CartError as error:
        return _refused(error)
    view = svc.current_cart(db, store, cart)
    if idempotency_key is not None:
        svc.finish_key(db, store, idempotency_key, cart, view)
    return _answer(view, store=store, token=token)


@router.patch("/lines/{line_id}", response_model=CartOut)
def api_set_quantity(
    request: Request, line_id: int, body: CartQuantityIn, store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    """Sets a line's quantity (absolute, so repeating it is harmless); more than the stock: refused.
    The line is validated again (a new price applies)."""
    cart = _cart(request, db, store)
    try:
        svc.set_line_quantity(db, cart, line_id, body.quantity)
    except svc.CartError as error:
        return _refused(error)
    return _answer(svc.current_cart(db, store, cart))


@router.delete("/lines/{line_id}", response_model=CartOut)
def api_remove_line(request: Request, line_id: int, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    """Removes a line (removing it again, or a line of another cart, changes nothing)."""
    cart = _cart(request, db, store)
    svc.remove_line(db, cart, line_id)
    return _answer(svc.current_cart(db, store, cart))
