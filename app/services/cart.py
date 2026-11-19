"""The public cart: one cart per visitor **per store**, no login.

- The visitor's browser holds a random token in the ``magic_stores_cart`` cookie, whose ``Path`` is the
  store's cart API (``/api/stores/<slug>/cart``): the browser never sends one store's cart cookie to
  another store. The database keeps only the token's SHA-256, and a cart is only ever looked up
  together with its store, so a token of another store finds nothing.
- A line is a product (found by id or SKU, active products only) with a quantity. Adding the same
  product again adds to its line. An add or a quantity change that would ask for more than the
  product's stock on hand is refused (``out_of_stock`` / ``insufficient_stock``). Stock is **checked,
  not reserved** (there is no checkout yet).
- **Snapshot**: a line keeps the name, SKU and price the product had when the line was added (or last
  validated), so editing the catalog doesn't rewrite open carts. A line is **validated again** when
  its snapshot is older than ``CART_REVALIDATE_MINUTES`` (default 30; the next read or change of the
  cart does it) and whenever the visitor changes it: the snapshot is refreshed, a new price is
  applied and the old one kept in ``previous_unit_price_cents`` ("repriced"), and the quantity is
  checked against the stock (``issue``: out of stock / not enough stock / no longer sold). The
  quantity is not lowered by itself (that would tell the stock count): the visitor changes it.
- The visitor can empty the cart (every line) at once.
- Rage clicks and retries: the "add" request may carry an ``Idempotency-Key``. The key is recorded
  *before* the work, so copies of the same click arriving together can't both add: the first adds,
  a copy arriving meanwhile gets ``request_in_progress`` (409), later copies get the first answer.
  Quantity changes set an absolute number (repeating them is harmless), removing a line twice is
  harmless, and merging into a line is one conditional ``UPDATE`` (it can't go past the stock).

Not done here: payment, shipping, checkout, stock reservation, clearing old carts.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session, selectinload

from app.db import fits_db_integer
from app.config import get_settings
from app.models import (
    Cart,
    CartLine,
    CartLineIssue,
    CartRequest,
    Category,
    Product,
    PropertyType,
    Store,
    Translation,
    TranslationEntity,
    utc_now,
)
from app.services import catalog
from app.services.media import media_url
from app.services.product_admin import product_by_sku

COOKIE_NAME = "magic_stores_cart"
COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 days
TOKEN_MAX_LEN = 100
MAX_LINE_QUANTITY = 99
MAX_LINES = 100
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
IDEMPOTENCY_TTL = timedelta(hours=24)


class CartError(Exception):
    """A refused cart request: ``status`` + the error item (``loc``, ``msg``, ``type``)."""

    def __init__(self, status: int, type_: str, msg: str, loc: tuple[str, ...] = ("body",)) -> None:
        super().__init__(msg)
        self.status, self.type, self.msg, self.loc = status, type_, msg, list(loc)

    def detail(self) -> list[dict[str, Any]]:
        return [{"loc": self.loc, "msg": self.msg, "type": self.type}]


def cookie_path(store: Store) -> str:
    """The cart cookie's Path: only this store's cart API receives it."""
    return f"/api/stores/{store.slug}/cart"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------ carts


def find_cart(db: Session, store: Store, token: Optional[str]) -> Optional[Cart]:
    """The cart of this token **in this store** (None: no cookie, unknown token, another store's)."""
    if not token or len(token) > TOKEN_MAX_LEN:
        return None
    return db.scalar(select(Cart).where(Cart.token_hash == hash_token(token), Cart.store_id == store.id))


def create_cart(db: Session, store: Store) -> tuple[Cart, str]:
    """A new empty cart (committed) and the token for its cookie."""
    token = secrets.token_urlsafe(32)
    cart = Cart(store_id=store.id, token_hash=hash_token(token))
    db.add(cart)
    db.commit()
    return cart, token


def load_lines(db: Session, cart: Cart) -> list[CartLine]:
    return list(db.scalars(
        select(CartLine)
        .where(CartLine.cart_id == cart.id)
        .options(
            selectinload(CartLine.product).selectinload(Product.property_values),
            selectinload(CartLine.product).selectinload(Product.category).selectinload(Category.property_definitions),
        )
        .order_by(CartLine.id)
    ))


def product_image(product: Product) -> Optional[str]:
    """The product's picture for the cart: the first image property (in definition order) that
    has a value, as a browser URL (content paths through CONTENT_BASE_URL); None without one."""
    values = {pv.definition_id: pv.value for pv in product.property_values}
    for definition in sorted(product.category.property_definitions, key=lambda d: (d.sort_order, d.id)):
        if definition.prop_type == PropertyType.image:
            path = (values.get(definition.id) or {}).get("media_path")
            if path:
                return media_url(path)
    return None


def revalidate_after() -> timedelta:
    """How old a line's snapshot may get before it is validated again (CART_REVALIDATE_MINUTES)."""
    return timedelta(minutes=get_settings().cart_revalidate_minutes)


def line_issue(product: Product, quantity: int) -> Optional[CartLineIssue]:
    if not product.is_active:
        return CartLineIssue.unavailable
    if product.stock_on_hand <= 0:
        return CartLineIssue.out_of_stock
    if quantity > product.stock_on_hand:
        return CartLineIssue.insufficient_stock
    return None


def name_translations(product: Product) -> dict[str, str]:
    """The product name's content translations, {language: name} (the ``translations`` table)."""
    db = object_session(product)
    if db is None or product.id is None:
        return {}
    rows = db.execute(select(Translation.locale, Translation.text).where(
        Translation.entity_type == TranslationEntity.product, Translation.entity_id == product.id,
        Translation.field == "name")).all()
    return dict(sorted(rows))


def snapshot(line: CartLine, product: Product) -> None:
    """Validates the line now: copies the product's name (with its translations), SKU and price (a
    different price is a repricing: the old one goes to ``previous_unit_price_cents``) and checks its
    quantity."""
    repriced = line.validated_at is not None and line.unit_price_cents != product.price_cents
    line.previous_unit_price_cents = line.unit_price_cents if repriced else None
    line.name, line.sku, line.unit_price_cents = product.name, product.sku, product.price_cents
    line.name_translations = name_translations(product)
    line.issue = line_issue(product, line.quantity)
    line.validated_at = utc_now()


def revalidate(db: Session, cart: Optional[Cart]) -> int:
    """Validates again the cart's lines whose snapshot is older than CART_REVALIDATE_MINUTES
    (committed). Returns how many were validated."""
    if cart is None:
        return 0
    limit = utc_now() - revalidate_after()
    stale = [line for line in load_lines(db, cart) if line.validated_at is None or line.validated_at <= limit]
    for line in stale:
        snapshot(line, line.product)
    if stale:
        db.commit()
    return len(stale)


def current_cart(db: Session, store: Store, cart: Optional[Cart]) -> dict[str, Any]:
    """The cart after validating its stale lines: what every cart request answers."""
    revalidate(db, cart)
    return cart_view(db, store, cart)


def cart_view(db: Session, store: Store, cart: Optional[Cart]) -> dict[str, Any]:
    """The cart as the API answers it: the lines' snapshots (name, SKU, price), the product's
    current picture and link. Content as entered, like the rest of the JSON API; each line also
    carries its name's translations (``name_translations``) for pages in other languages."""
    lines = load_lines(db, cart) if cart is not None else []
    out_lines = []
    subtotal = 0
    money = lambda cents: catalog.format_price(cents, store.currency)  # noqa: E731
    for line in lines:
        product = line.product
        total = line.unit_price_cents * line.quantity
        subtotal += total
        previous = line.previous_unit_price_cents
        out_lines.append({
            "id": line.id,
            "product_id": product.id,
            "sku": line.sku,
            "slug": product.slug,
            "name": line.name,
            "name_translations": line.name_translations or {},
            "url": f"/stores/{store.slug}/p/{product.slug}",
            "image_url": product_image(product),
            "quantity": line.quantity,
            "unit_price_cents": line.unit_price_cents,
            "line_total_cents": total,
            "unit_price_display": money(line.unit_price_cents),
            "line_total_display": money(total),
            "price_changed": previous is not None,
            "previous_unit_price_cents": previous,
            "previous_unit_price_display": money(previous) if previous is not None else None,
            "issue": line.issue.value if line.issue else None,
            # Buyable as it is, as of its last validation (every change, or CART_REVALIDATE_MINUTES).
            "available": line.issue is None,
            "validated_at": line.validated_at.isoformat() + "Z" if line.validated_at else None,
        })
    return {
        "store": store.slug,
        "currency": store.currency,
        "lines": out_lines,
        "line_count": len(out_lines),
        "item_count": sum(line["quantity"] for line in out_lines),
        "subtotal_cents": subtotal,
        "subtotal_display": catalog.format_price(subtotal, store.currency),
    }


# ------------------------------------------------------------------------------ lines


def resolve_product(db: Session, store: Store, product_id: Optional[int], sku: Optional[str]) -> Product:
    """The store's **active** product with this id or SKU; unknown / hidden: CartError 422."""
    product = None
    if product_id is not None:
        if fits_db_integer(product_id):
            product = db.scalar(select(Product).where(Product.id == product_id, Product.store_id == store.id))
        loc = ("body", "product_id")
    else:
        product = product_by_sku(db, store, sku or "")
        loc = ("body", "sku")
    if product is None or not product.is_active:
        raise CartError(422, "unknown_product", "No such product in this store.", loc)
    return product


def check_quantity(product: Product, wanted: int) -> None:
    """``wanted`` = the line's quantity after the change."""
    if product.stock_on_hand <= 0:
        raise CartError(409, "out_of_stock", "This product is out of stock.", ("body", "quantity"))
    if wanted > product.stock_on_hand:
        # The count itself stays private (the public API only says in_stock).
        raise CartError(409, "insufficient_stock", "Not enough stock for this quantity.", ("body", "quantity"))
    if wanted > MAX_LINE_QUANTITY:
        raise CartError(422, "quantity_limit", f"At most {MAX_LINE_QUANTITY} of a product per cart.",
                        ("body", "quantity"))


def _touch(cart: Cart) -> None:
    cart.updated_at = utc_now()


def add_line(db: Session, cart: Cart, product: Product, quantity: int) -> None:
    """Adds ``quantity`` of the product (to its line if there is one). Committed; CartError if refused."""
    for _attempt in range(2):  # a second try if a parallel add created the line meanwhile
        line = db.scalar(select(CartLine).where(CartLine.cart_id == cart.id, CartLine.product_id == product.id))
        if line is None:
            check_quantity(product, quantity)
            if len(cart.lines) >= MAX_LINES:
                raise CartError(422, "cart_full", f"A cart holds at most {MAX_LINES} different products.")
            line = CartLine(cart_id=cart.id, product_id=product.id, quantity=quantity)
            snapshot(line, product)
            db.add(line)
            _touch(cart)
            try:
                db.commit()
                return
            except IntegrityError:
                db.rollback()
                continue
        check_quantity(product, line.quantity + quantity)
        # Checked again inside the UPDATE, against the stock *now* (not the value read above): a
        # parallel add or a sale in between can't take the line past the stock.
        stock_now = select(Product.stock_on_hand).where(Product.id == product.id).scalar_subquery()
        merged = db.execute(
            update(CartLine)
            .where(CartLine.id == line.id, CartLine.quantity + quantity <= stock_now,
                   CartLine.quantity + quantity <= MAX_LINE_QUANTITY)
            .values(quantity=CartLine.quantity + quantity)
            .execution_options(synchronize_session=False)
        )
        if merged.rowcount != 1:
            db.rollback()
            raise CartError(409, "insufficient_stock", "Not enough stock for this quantity.", ("body", "quantity"))
        db.refresh(line)
        snapshot(line, product)  # changed by the visitor: validated now (a new price applies)
        _touch(cart)
        db.commit()
        return
    raise CartError(409, "request_in_progress", "The cart changed meanwhile; try again.")  # pragma: no cover


def _line(db: Session, cart: Optional[Cart], line_id: int) -> Optional[CartLine]:
    if cart is None or not fits_db_integer(line_id):
        return None
    return db.scalar(select(CartLine).where(CartLine.id == line_id, CartLine.cart_id == cart.id))


def set_line_quantity(db: Session, cart: Optional[Cart], line_id: int, quantity: int) -> None:
    """Sets a line's quantity (absolute: repeating it is harmless). CartError 404 / 409 / 422."""
    line = _line(db, cart, line_id)
    if line is None:
        raise CartError(404, "line_not_found", "No such line in this cart.", ("path", "line_id"))
    check_quantity(line.product, quantity)  # refused if more than the stock now
    if quantity != line.quantity or line.issue is not None:
        line.quantity = quantity
        snapshot(line, line.product)  # changed by the visitor: validated now (a new price applies)
        _touch(cart)
        db.commit()


def clear_cart(db: Session, cart: Optional[Cart]) -> None:
    """Removes every line (the cart and its cookie stay); an empty or missing cart: nothing to do."""
    if cart is None:
        return
    deleted = db.execute(delete(CartLine).where(CartLine.cart_id == cart.id))
    if deleted.rowcount:
        _touch(cart)
    db.commit()


def remove_line(db: Session, cart: Optional[Cart], line_id: int) -> None:
    """Removes a line; a line that isn't (or is no longer) in the cart: nothing to do."""
    line = _line(db, cart, line_id)
    if line is not None:
        db.delete(line)
        _touch(cart)
        db.commit()


# ------------------------------------------------------------------------ idempotency


def body_fingerprint(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Replay:
    """The first answer to an Idempotency-Key."""

    response: dict[str, Any]


def claim_key(db: Session, store: Store, key: str, fingerprint: str, cart: Optional[Cart]) -> Optional[Replay]:
    """Records ``key`` before the work (committed). Returns the first answer for a key already
    answered; raises CartError for a key in progress (409) or reused for something else (422)."""
    if not IDEMPOTENCY_KEY_RE.match(key):
        raise CartError(422, "invalid_idempotency_key",
                        "Idempotency-Key: 8 to 64 letters, digits, '-' or '_'.", ("header", "Idempotency-Key"))
    db.execute(delete(CartRequest).where(CartRequest.store_id == store.id,
                                         CartRequest.created_at < utc_now() - IDEMPOTENCY_TTL))
    existing = db.scalar(select(CartRequest).where(CartRequest.store_id == store.id, CartRequest.key == key))
    if existing is not None:
        other_cart = cart is not None and existing.cart_id not in (None, cart.id)
        if existing.fingerprint != fingerprint or other_cart:
            raise CartError(422, "idempotency_key_reused", "This Idempotency-Key was used for another request.",
                            ("header", "Idempotency-Key"))
        if existing.response is None:
            raise CartError(409, "request_in_progress", "The same request is still being processed.",
                            ("header", "Idempotency-Key"))
        return Replay(existing.response)
    db.add(CartRequest(store_id=store.id, key=key, fingerprint=fingerprint,
                       cart_id=cart.id if cart is not None else None))
    try:
        db.commit()
    except IntegrityError:  # the same key, arriving at the same moment
        db.rollback()
        raise CartError(409, "request_in_progress", "The same request is still being processed.",
                        ("header", "Idempotency-Key")) from None
    return None


def finish_key(db: Session, store: Store, key: str, cart: Cart, response: dict[str, Any]) -> None:
    db.execute(update(CartRequest).where(CartRequest.store_id == store.id, CartRequest.key == key)
               .values(cart_id=cart.id, response=response))
    db.commit()


def release_key(db: Session, store: Store, key: str) -> None:
    """The request was refused: forget the key, so trying again (e.g. after a restock) works."""
    db.execute(delete(CartRequest).where(CartRequest.store_id == store.id, CartRequest.key == key))
    db.commit()
