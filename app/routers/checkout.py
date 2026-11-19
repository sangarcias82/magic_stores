"""Public checkout API: ``POST /api/stores/{slug}/cart/checkout`` turns the visitor's cart into an
order with a pending payment (it sits under the cart's path because the cart cookie is scoped to
``/api/stores/{slug}/cart``: see app/routers/cart.py); ``GET /api/stores/{slug}/orders/{id}``
reads an order by its unguessable id. Rules: app/services/checkout.py. Errors use the cart API's shape."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Store
from app.routers.cart import NO_STORE, get_store
from app.schemas import CheckoutIn, CheckoutOut
from app.services import cart as cart_svc
from app.services import checkout as svc

router = APIRouter(prefix="/api/stores/{store_slug}", tags=["checkout"])


@router.post("/cart/checkout", response_model=CheckoutOut, status_code=201)
def api_checkout(
    request: Request,
    body: CheckoutIn,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    store: Store = Depends(get_store),
    db: Session = Depends(get_db),
):
    """Order + payment from the cart: the shipping method and the terminal are chosen here."""
    cart = cart_svc.find_cart(db, store, request.cookies.get(cart_svc.COOKIE_NAME))
    try:
        if idempotency_key is not None:
            fingerprint = cart_svc.body_fingerprint({"checkout": body.model_dump()})
            replay = cart_svc.claim_key(db, store, idempotency_key, fingerprint, cart)
            if replay is not None:  # a copy of a checkout already done: the same order, not a second one
                return JSONResponse(replay.response, status_code=201,
                                    headers={**NO_STORE, "Idempotent-Replayed": "true"})
        try:
            result = svc.checkout(db, store, cart, shipping_method=body.shipping_method, country=body.country,
                                  terminal=body.terminal)
        except svc.CheckoutRefused:
            if idempotency_key is not None:
                cart_svc.release_key(db, store, idempotency_key)
            raise
    except cart_svc.CartError as error:
        return JSONResponse({"detail": error.detail()}, status_code=error.status, headers=dict(NO_STORE))
    except svc.CheckoutRefused as refused:
        return JSONResponse({"detail": refused.detail}, status_code=refused.status, headers=dict(NO_STORE))
    view = svc.order_view(result.order)
    if idempotency_key is not None:
        cart_svc.finish_key(db, store, idempotency_key, cart, view)
    return JSONResponse(view, status_code=201, headers=dict(NO_STORE))


@router.get("/orders/{order_id}", response_model=CheckoutOut)
def api_get_order(order_id: str, store: Store = Depends(get_store), db: Session = Depends(get_db)):
    """An order with its payment's current status (the id is the capability: 144 random bits)."""
    order = svc.get_order(db, store, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return JSONResponse(svc.order_view(order), headers=dict(NO_STORE))
