"""The public cart: API (/api/stores/<slug>/cart), its cookie scoped to one store, stock checks,
rage-click guards (idempotency keys), and the storefront pages that load the React cart."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import Cart, CartLine, CartRequest, Category, Product, Store, utc_now
from app.services import cart as svc
from app.services import stock as stock_svc
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from tests.ui_text import es, page_text

PROJECT = Path(__file__).resolve().parent.parent
SOLARES = read_store_pack(PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json")
API = "/api/stores/solares/cart"
LINES = f"{API}/lines"


@pytest.fixture()
def solares(db_session):
    """The solares fixture store: every product has a SKU, stock 1 and a photo."""
    import_store_pack(db_session, SOLARES)
    return db_session.scalar(select(Store).where(Store.slug == "solares"))


def _product(db_session, sku: str) -> Product:
    db_session.expire_all()
    return db_session.scalar(select(Product).where(Product.sku == sku))


def _set_stock(db_session, sku: str, units: int) -> None:
    product = _product(db_session, sku)
    product.stock_on_hand = units
    db_session.commit()


def _add(client, **body):
    headers = {"Idempotency-Key": body.pop("key")} if "key" in body else {}
    return client.post(LINES, json=body, headers=headers)


def _sets_cart_cookie(response) -> bool:
    return any(value.startswith(f"{svc.COOKIE_NAME}=") for value in response.headers.get_list("set-cookie"))


def _error(response) -> tuple[int, str]:
    return response.status_code, response.json()["detail"][0]["type"]


# ======================================================================= empty cart


def test_empty_cart_without_a_cookie(client, solares, db_session):
    response = client.get(API)
    assert response.status_code == 200
    assert response.json() == {"store": "solares", "currency": "MXN", "lines": [], "line_count": 0, "item_count": 0,
                               "subtotal_cents": 0, "subtotal_display": "MXN 0.00"}
    assert not _sets_cart_cookie(response)  # reading doesn't create a cart
    assert response.headers["cache-control"] == "no-store"
    assert db_session.scalar(select(func.count()).select_from(Cart)) == 0


def test_unknown_store_is_404(client):
    assert client.get("/api/stores/nope/cart").status_code == 404
    assert client.post("/api/stores/nope/cart/lines", json={"sku": "X"}).status_code == 404


def test_cart_emptied_by_removing_its_lines(client, solares):
    line = _add(client, sku="SOL-8T-ALTA").json()["lines"][0]
    emptied = client.delete(f"{LINES}/{line['id']}")
    assert emptied.status_code == 200 and emptied.json()["lines"] == [] and emptied.json()["item_count"] == 0
    assert client.get(API).json()["subtotal_cents"] == 0


# ======================================================================= happy path


def test_happy_path_add_by_sku_and_by_id_then_read(client, solares, db_session):
    boiler = _product(db_session, "GAS-BOIL-40L")
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    first = _add(client, sku="  sol-8t-alta ", quantity=2)  # SKU: any case, spaces trimmed
    assert first.status_code == 200
    assert _sets_cart_cookie(first)
    cookie = next(v for v in first.headers.get_list("set-cookie") if v.startswith(f"{svc.COOKIE_NAME}="))
    for attribute in ("HttpOnly", "Path=/api/stores/solares/cart", "SameSite=lax", f"Max-Age={svc.COOKIE_MAX_AGE}"):
        assert attribute in cookie, attribute

    second = _add(client, product_id=boiler.id)
    assert second.status_code == 200 and not _sets_cart_cookie(second)  # same cart

    cart = client.get(API).json()
    eight = _product(db_session, "SOL-8T-ALTA")
    assert [(line["sku"], line["quantity"]) for line in cart["lines"]] == [("SOL-8T-ALTA", 2), ("GAS-BOIL-40L", 1)]
    line = cart["lines"][0]
    assert line == {
        "id": line["id"], "product_id": eight.id, "sku": "SOL-8T-ALTA", "slug": "solar-8-tubos-alta",
        "name": eight.name, "url": "/stores/solares/p/solar-8-tubos-alta",
        "name_translations": {"en": "Solar heater, 8 tubes, high pressure"},  # the pack's English name
        "image_url": "http://127.0.0.1:8001/content/solares/tubo-8-alta.jpg",  # the mini picture
        "quantity": 2, "unit_price_cents": eight.price_cents, "line_total_cents": 2 * eight.price_cents,
        "unit_price_display": "MXN 6,499.00", "line_total_display": "MXN 12,998.00", "price_changed": False,
        "previous_unit_price_cents": None, "previous_unit_price_display": None, "issue": None, "available": True,
        "validated_at": line["validated_at"],
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z", line["validated_at"])
    assert cart["lines"][1]["image_url"] == "http://127.0.0.1:8001/content/solares/gas-boiler.jpg"
    assert (cart["line_count"], cart["item_count"]) == (2, 3)
    assert cart["subtotal_cents"] == 2 * eight.price_cents + boiler.price_cents
    assert cart["subtotal_display"] == "MXN 18,197.00"


def test_adding_the_same_product_again_adds_to_its_line(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    _add(client, sku="SOL-8T-ALTA")
    cart = _add(client, sku="SOL-8T-ALTA", quantity=3).json()
    assert cart["line_count"] == 1 and cart["lines"][0]["quantity"] == 4


def test_change_quantity_and_remove(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    line = _add(client, sku="SOL-8T-ALTA").json()["lines"][0]
    changed = client.patch(f"{LINES}/{line['id']}", json={"quantity": 4})
    assert changed.status_code == 200 and changed.json()["lines"][0]["quantity"] == 4
    assert client.patch(f"{LINES}/{line['id']}", json={"quantity": 4}).json()["item_count"] == 4  # same: harmless
    removed = client.delete(f"{LINES}/{line['id']}")
    assert removed.json()["lines"] == []
    again = client.delete(f"{LINES}/{line['id']}")  # rage click on "remove": harmless
    assert again.status_code == 200 and again.json()["lines"] == []


def test_product_without_picture_has_no_image(client, solares, db_session):
    store = solares
    category = db_session.scalar(select(Category).where(Category.store_id == store.id))
    db_session.add(Product(store_id=store.id, category_id=category.id, slug="plain", name="Plain", sku="PLAIN-1",
                           stock_on_hand=3, price_cents=100))
    db_session.commit()
    assert _add(client, sku="PLAIN-1").json()["lines"][0]["image_url"] is None


# ======================================================================= oversell refused


def test_adding_more_than_the_stock_is_refused(client, solares, db_session):
    response = _add(client, sku="SOL-8T-ALTA", quantity=2)  # stock 1
    assert _error(response) == (409, "insufficient_stock")
    assert not _sets_cart_cookie(response)  # refused before any cart exists
    assert db_session.scalar(select(func.count()).select_from(Cart)) == 0
    assert "1" not in response.json()["detail"][0]["msg"]  # the stock count stays private


def test_adding_past_the_stock_to_an_existing_line_is_refused(client, solares, db_session):
    assert _add(client, sku="SOL-8T-ALTA").status_code == 200  # 1 of 1
    assert _error(_add(client, sku="SOL-8T-ALTA")) == (409, "insufficient_stock")
    assert client.get(API).json()["lines"][0]["quantity"] == 1


def test_raising_a_line_past_the_stock_is_refused(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 3)
    line = _add(client, sku="SOL-8T-ALTA").json()["lines"][0]
    assert _error(client.patch(f"{LINES}/{line['id']}", json={"quantity": 4})) == (409, "insufficient_stock")
    assert client.get(API).json()["lines"][0]["quantity"] == 1


def test_out_of_stock_is_refused(client, solares, db_session):
    _set_stock(db_session, "GAS-BOIL-40L", 0)
    assert _error(_add(client, sku="GAS-BOIL-40L")) == (409, "out_of_stock")


def test_merge_is_checked_against_the_stock_now(client, solares, db_session):
    """The stock drops between the check and the update (a sale): the conditional UPDATE, not the
    value read by the request, decides."""
    _set_stock(db_session, "SOL-8T-ALTA", 3)
    _add(client, sku="SOL-8T-ALTA")
    cart = db_session.scalar(select(Cart))
    stale = _product(db_session, "SOL-8T-ALTA")  # says 3
    other = sessionmaker(bind=db_session.get_bind())()
    other.execute(Product.__table__.update().where(Product.__table__.c.id == stale.id).values(stock_on_hand=1))
    other.commit()
    with pytest.raises(svc.CartError) as refused:
        svc.add_line(db_session, cart, stale, 1)
    assert refused.value.type == "insufficient_stock"
    assert db_session.scalar(select(CartLine.quantity)) == 1


# ======================================================================= other refusals


@pytest.mark.parametrize("body,code", [
    ({"sku": "NOPE-1"}, "unknown_product"),
    ({"sku": "has space"}, "unknown_product"),
    ({"product_id": 999999}, "unknown_product"),
    ({"product_id": 10 ** 30}, "unknown_product"),
])
def test_unknown_products_are_refused(client, solares, body, code):
    assert _error(_add(client, **body)) == (422, code)


def test_hidden_products_and_other_stores_products_are_unknown(client, solares, db_session):
    hidden = _product(db_session, "SOL-12T-BAJA")
    hidden.is_active = False
    db_session.commit()
    assert _error(_add(client, sku="SOL-12T-BAJA")) == (422, "unknown_product")
    other = Store(slug="otra", name="Otra")
    db_session.add(other)
    db_session.flush()
    category = Category(store_id=other.id, slug="c", name="C")
    db_session.add(category)
    db_session.flush()
    foreign = Product(store_id=other.id, category_id=category.id, slug="p", name="P", sku="OTRA-1", stock_on_hand=5)
    db_session.add(foreign)
    db_session.commit()
    assert _error(_add(client, product_id=foreign.id)) == (422, "unknown_product")
    assert _error(_add(client, sku="OTRA-1")) == (422, "unknown_product")


@pytest.mark.parametrize("body", [{}, {"sku": "A", "product_id": 1}, {"sku": "SOL-8T-ALTA", "quantity": 0},
                                  {"sku": "SOL-8T-ALTA", "quantity": 100}, {"sku": "SOL-8T-ALTA", "extra": 1},
                                  {"sku": "SOL-8T-ALTA", "quantity": "2.5"}, {"sku": ""}])
def test_malformed_bodies_are_422(client, solares, body):
    assert client.post(LINES, json=body).status_code == 422


def test_non_json_bodies_are_refused(client, solares):
    """What a form on another site could send: refused, so it can't fill a visitor's cart."""
    for content_type in ("application/x-www-form-urlencoded", "text/plain"):
        response = client.post(LINES, content=json.dumps({"sku": "SOL-8T-ALTA"}), headers={"Content-Type": content_type})
        assert response.status_code == 422, content_type


def test_quantity_limit_and_cart_size(client, solares, db_session, monkeypatch):
    _set_stock(db_session, "SOL-8T-ALTA", 500)
    assert _add(client, sku="SOL-8T-ALTA", quantity=99).status_code == 200
    assert _error(_add(client, sku="SOL-8T-ALTA")) == (422, "quantity_limit")
    monkeypatch.setattr(svc, "MAX_LINES", 1)
    _set_stock(db_session, "GAS-BOIL-40L", 5)
    assert _error(_add(client, sku="GAS-BOIL-40L")) == (422, "cart_full")


def test_unknown_line_is_404(client, solares):
    assert _error(client.patch(f"{LINES}/1", json={"quantity": 1})) == (404, "line_not_found")  # no cart
    _add(client, sku="SOL-8T-ALTA")
    assert client.patch(f"{LINES}/999", json={"quantity": 1}).status_code == 404
    assert client.patch(f"{LINES}/{10 ** 30}", json={"quantity": 1}).status_code == 404


# ======================================================================= one store per cookie


def test_the_cookie_only_reaches_its_stores_cart_api(client, solares, db_session):
    other = Store(slug="otra", name="Otra", currency="EUR")
    db_session.add(other)
    db_session.commit()
    _add(client, sku="SOL-8T-ALTA")
    assert client.get(API).json()["line_count"] == 1
    # The browser doesn't send it elsewhere (Path) ...
    assert client.get("/api/stores/otra/cart").json() == {
        "store": "otra", "currency": "EUR", "lines": [], "line_count": 0, "item_count": 0, "subtotal_cents": 0,
        "subtotal_display": "EUR 0.00"}
    # ... and even sent by hand, another store's API doesn't find the cart.
    token = client.cookies.get(svc.COOKIE_NAME, path="/api/stores/solares/cart")
    response = client.get("/api/stores/otra/cart", headers={"Cookie": f"{svc.COOKIE_NAME}={token}"})
    assert response.json()["line_count"] == 0
    line_id = db_session.scalar(select(CartLine.id))
    assert client.delete(f"/api/stores/otra/cart/lines/{line_id}",
                         headers={"Cookie": f"{svc.COOKIE_NAME}={token}"}).json()["lines"] == []
    assert db_session.scalar(select(func.count()).select_from(CartLine)) == 1  # untouched


def test_only_the_tokens_hash_is_stored(client, solares, db_session):
    _add(client, sku="SOL-8T-ALTA")
    token = client.cookies.get(svc.COOKIE_NAME, path="/api/stores/solares/cart")
    cart = db_session.scalar(select(Cart))
    assert cart.token_hash == hashlib.sha256(token.encode()).hexdigest() and token not in cart.token_hash
    assert len(token) >= 40  # 256 random bits


@pytest.mark.parametrize("cookie", ["forged", "x" * 500, ""])
def test_unknown_or_forged_tokens_find_no_cart(client, solares, cookie):
    assert client.get(API, headers={"Cookie": f"{svc.COOKIE_NAME}={cookie}"}).json()["line_count"] == 0


def test_deleting_a_product_removes_it_from_carts(client, solares, db_session):
    _add(client, sku="SOL-8T-ALTA")
    db_session.delete(_product(db_session, "SOL-8T-ALTA"))
    db_session.commit()
    assert client.get(API).json()["lines"] == []


def test_deleting_a_store_deletes_its_carts_and_keys(client, solares, db_session):
    _add(client, sku="SOL-8T-ALTA", key="delete-me-1")
    db_session.delete(db_session.get(Store, solares.id))
    db_session.commit()
    for model in (Cart, CartLine, CartRequest):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0, model


# ======================================================================= rage clicks


def test_same_key_adds_once(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 10)
    first = _add(client, sku="SOL-8T-ALTA", key="click-0001")
    for _ in range(9):  # a rage click sending copies of the same action
        replay = _add(client, sku="SOL-8T-ALTA", key="click-0001")
        assert replay.status_code == 200 and replay.headers["idempotent-replayed"] == "true"
        assert replay.json() == first.json()
    assert client.get(API).json()["item_count"] == 1
    assert _add(client, sku="SOL-8T-ALTA", key="click-0002").json()["item_count"] == 2  # a new click adds


def test_copy_arriving_before_the_cookie_gets_the_first_answer(client, solares, db_session):
    first = _add(client, sku="SOL-8T-ALTA", key="click-0003")
    client.cookies.clear()  # the second copy left before the first answer set the cookie
    replay = _add(client, sku="SOL-8T-ALTA", key="click-0003")
    assert replay.json() == first.json() and not _sets_cart_cookie(replay)
    assert db_session.scalar(select(func.count()).select_from(Cart)) == 1


def test_key_reused_for_another_request_is_refused(client, solares, db_session):
    _set_stock(db_session, "GAS-BOIL-40L", 5)
    _add(client, sku="SOL-8T-ALTA", key="click-0004")
    assert _error(_add(client, sku="GAS-BOIL-40L", key="click-0004")) == (422, "idempotency_key_reused")


@pytest.mark.parametrize("key", ["short", "has space!", "x" * 65])
def test_invalid_keys_are_refused(client, solares, key):
    assert _error(_add(client, sku="SOL-8T-ALTA", key=key)) == (422, "invalid_idempotency_key")


def test_a_refused_request_frees_its_key(client, solares, db_session):
    _set_stock(db_session, "GAS-BOIL-40L", 0)
    assert _error(_add(client, sku="GAS-BOIL-40L", key="click-0005")) == (409, "out_of_stock")
    _set_stock(db_session, "GAS-BOIL-40L", 2)  # restocked: the same click, retried, now works
    assert _add(client, sku="GAS-BOIL-40L", key="click-0005").status_code == 200


def test_copy_arriving_while_the_first_is_processed_is_409(client, solares, db_session):
    db_session.add(CartRequest(store_id=solares.id, key="click-0006", fingerprint=svc.body_fingerprint(
        {"product_id": None, "sku": "SOL-8T-ALTA", "quantity": 1})))
    db_session.commit()  # the first copy has claimed the key and not answered yet
    assert _error(_add(client, sku="SOL-8T-ALTA", key="click-0006")) == (409, "request_in_progress")


def test_two_copies_claiming_at_the_same_moment(solares, db_session, monkeypatch):
    """Both looked, neither saw the key: the unique constraint lets one in, the other gets 409."""
    fingerprint = svc.body_fingerprint({"sku": "X"})
    assert svc.claim_key(db_session, solares, "click-0007", fingerprint, None) is None
    real_scalar = db_session.scalar
    monkeypatch.setattr(db_session, "scalar", lambda stmt: None if "cart_requests" in str(stmt) else real_scalar(stmt))
    with pytest.raises(svc.CartError) as second:
        svc.claim_key(db_session, solares, "click-0007", fingerprint, None)
    assert (second.value.status, second.value.type) == (409, "request_in_progress")


def test_key_of_another_cart_is_refused(client, solares, db_session):
    _add(client, sku="SOL-8T-ALTA", key="click-0008")
    client.cookies.clear()
    _set_stock(db_session, "GAS-BOIL-40L", 3)
    _add(client, sku="GAS-BOIL-40L")  # another visitor's cart now
    assert _error(_add(client, sku="SOL-8T-ALTA", key="click-0008")) == (422, "idempotency_key_reused")


def test_old_keys_are_forgotten(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    _add(client, sku="SOL-8T-ALTA", key="click-0009")
    row = db_session.scalar(select(CartRequest))
    row.created_at = utc_now() - timedelta(hours=25)
    db_session.commit()
    assert _add(client, sku="SOL-8T-ALTA", key="click-0010").status_code == 200
    assert db_session.scalar(select(func.count()).select_from(CartRequest).where(CartRequest.key == "click-0009")) == 0


def test_parallel_adds_of_the_same_product_merge(client, solares, db_session, monkeypatch):
    """The line was created by a parallel request between the lookup and the insert."""
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    _add(client, sku="SOL-8T-ALTA")
    cart = db_session.scalar(select(Cart))
    product = _product(db_session, "SOL-8T-ALTA")
    calls = {"n": 0}
    real_scalar = db_session.scalar

    def first_lookup_misses(stmt):
        if "cart_lines" in str(stmt) and calls["n"] == 0:
            calls["n"] += 1
            return None
        return real_scalar(stmt)

    monkeypatch.setattr(db_session, "scalar", first_lookup_misses)
    svc.add_line(db_session, cart, product, 2)
    monkeypatch.undo()
    assert db_session.scalar(select(CartLine.quantity)) == 3


# ======================================================================= pages


def test_cart_page_loads_the_react_cart_in_the_store_language(default_lang_client, solares):
    page = default_lang_client.get("/stores/solares/cart")
    assert page.status_code == 200
    html = page.text
    assert '<div id="cart-app" data-store="solares">' in html
    for src in ("/static/vendor/react-18.3.1/react.production.min.js",
                "/static/vendor/react-18.3.1/react-dom.production.min.js",
                "/static/cart/cart-core.js", "/static/cart/cart.js"):
        assert f'<script src="{src}" defer></script>' in html, src
        assert default_lang_client.get(src).status_code == 200, src
    assert '<link rel="stylesheet" href="/static/cart/cart.css">' in html
    config = json.loads(re.search(r'<script type="application/json" id="cart-config">(.*?)</script>', html, re.S).group(1))
    assert config["api"] == API and config["cart_url"] == "/stores/solares/cart"
    assert config["texts"]["add"] == es("cart.add") and config["texts"]["error.insufficient_stock"] == es(
        "cart.error.insufficient_stock")
    assert f"<h1>{es('cart.title')}</h1>" in html
    assert default_lang_client.get("/stores/nope/cart").status_code == 404


def test_product_pages_get_add_to_cart_and_every_page_the_cart_link(client, solares, db_session):
    eight = _product(db_session, "SOL-8T-ALTA")
    product_page = client.get("/stores/solares/p/solar-8-tubos-alta").text  # solares' own product.html
    assert f'<div id="cart-add" data-product-id="{eight.id}" data-in-stock="true"></div>' in product_page
    _set_stock(db_session, "SOL-8T-ALTA", 0)
    assert 'data-in-stock="false"' in client.get("/stores/solares/p/solar-8-tubos-alta").text
    home = client.get("/stores/solares").text
    assert 'id="cart-add"' not in home
    assert '<a href="/stores/solares/cart" class="cart-link">' in home and "data-cart-count" in home


def test_config_json_is_safe_in_the_page(client, solares, db_session):
    """Store texts (overridable) end up in a <script> block: tojson escapes </script>."""
    from app.services.translations import set_ui_translation

    set_ui_translation(db_session, "en", "cart.add", "Add </script><script>alert(1)</script>",
                       store=db_session.get(Store, solares.id))
    html = client.get("/stores/solares/cart").text
    assert "<script>alert(1)" not in html
    config = json.loads(re.search(r'id="cart-config">(.*?)</script>', html, re.S).group(1))
    assert config["texts"]["add"] == "Add </script><script>alert(1)</script>"


VENDORED = {  # React 18.3.1 production builds from the npm registry packages (see app/static/vendor/README.md)
    "react.production.min.js": "d949f1c3687aedadcedac85261865f29b17cd273997e7f6b2bfc53b2f9d4c4dd",
    "react-dom.production.min.js": "35f4f974f4b2bcd44da73963347f8952e341f83909e4498227d4e26b98f66f0d",
}


def test_vendored_react_is_the_published_build():
    folder = PROJECT / "app" / "static" / "vendor" / "react-18.3.1"
    for name, digest in VENDORED.items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest, name
    assert "MIT License" in (folder / "LICENSE").read_text()


# ======================================================================= snapshots and revalidation


def _age_lines(db_session, minutes: int) -> None:
    """As if the lines were last validated ``minutes`` ago."""
    db_session.execute(CartLine.__table__.update().values(validated_at=utc_now() - timedelta(minutes=minutes)))
    db_session.commit()


def _edit_product(db_session, current_sku: str, /, **fields) -> Product:
    product = _product(db_session, current_sku)
    for name, value in fields.items():
        setattr(product, name, value)
    db_session.commit()
    return product


def test_catalog_edits_dont_rewrite_an_open_cart(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    _add(client, sku="SOL-8T-ALTA", quantity=2)
    _edit_product(db_session, "SOL-8T-ALTA", name="Renamed heater", sku="NEW-SKU-1", price_cents=700000)
    line = client.get(API).json()["lines"][0]  # within the 30 minutes
    assert (line["name"], line["sku"], line["unit_price_cents"], line["line_total_cents"]) == (
        "Calentador solar 8 tubos, alta presión", "SOL-8T-ALTA", 649900, 1299800)
    assert line["price_changed"] is False
    assert client.get(API).json()["subtotal_cents"] == 1299800


def test_after_the_threshold_the_line_is_repriced(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    _add(client, sku="SOL-8T-ALTA", quantity=2)
    _edit_product(db_session, "SOL-8T-ALTA", name="Renamed heater", sku="NEW-SKU-1", price_cents=700000)
    _age_lines(db_session, 31)
    cart = client.get(API).json()
    line = cart["lines"][0]
    assert (line["name"], line["sku"], line["unit_price_cents"], line["line_total_cents"]) == (
        "Renamed heater", "NEW-SKU-1", 700000, 1400000)
    assert (line["price_changed"], line["previous_unit_price_cents"], line["previous_unit_price_display"]) == (
        True, 649900, "MXN 6,499.00")
    assert cart["subtotal_display"] == "MXN 14,000.00"
    assert client.get(API).json()["lines"][0]["price_changed"] is True  # shown until the next validation
    _age_lines(db_session, 31)
    assert client.get(API).json()["lines"][0]["price_changed"] is False  # same price since: nothing to say


def test_a_line_just_under_the_threshold_is_not_revalidated(client, solares, db_session):
    _add(client, sku="SOL-8T-ALTA")
    _edit_product(db_session, "SOL-8T-ALTA", price_cents=1)
    _age_lines(db_session, 29)
    assert client.get(API).json()["lines"][0]["unit_price_cents"] == 649900


def test_the_threshold_is_configurable(client, solares, db_session, monkeypatch):
    from app.config import get_settings

    _add(client, sku="SOL-8T-ALTA")
    _edit_product(db_session, "SOL-8T-ALTA", price_cents=650000)
    _age_lines(db_session, 31)
    monkeypatch.setattr(get_settings(), "cart_revalidate_minutes", 60)
    assert client.get(API).json()["lines"][0]["unit_price_cents"] == 649900  # 31 < 60
    monkeypatch.setattr(get_settings(), "cart_revalidate_minutes", 0)  # every request
    assert client.get(API).json()["lines"][0]["unit_price_cents"] == 650000
    _edit_product(db_session, "SOL-8T-ALTA", price_cents=660000)
    assert client.get(API).json()["lines"][0]["unit_price_cents"] == 660000


@pytest.mark.parametrize("value,valid", [(0, True), (30, True), (10080, True), (-1, False), (10081, False)])
def test_threshold_setting_range(value, valid):
    from pydantic import ValidationError

    from app.config import Settings

    if valid:
        assert Settings(cart_revalidate_minutes=value).cart_revalidate_minutes == value
    else:
        with pytest.raises(ValidationError, match="CART_REVALIDATE_MINUTES"):
            Settings(cart_revalidate_minutes=value)
    assert Settings().cart_revalidate_minutes == 30


@pytest.mark.parametrize("change,issue", [
    ({"stock_on_hand": 1}, "insufficient_stock"),
    ({"stock_on_hand": 0}, "out_of_stock"),
    ({"is_active": False}, "unavailable"),
])
def test_revalidation_checks_the_quantity(client, solares, db_session, change, issue):
    _set_stock(db_session, "SOL-8T-ALTA", 3)
    _add(client, sku="SOL-8T-ALTA", quantity=3)
    _edit_product(db_session, "SOL-8T-ALTA", **change)
    assert client.get(API).json()["lines"][0]["available"] is True  # not checked yet (within 30 minutes)
    _age_lines(db_session, 31)
    line = client.get(API).json()["lines"][0]
    assert (line["issue"], line["available"], line["quantity"]) == (issue, False, 3)  # not lowered by itself


def test_updating_past_the_stock_is_refused_and_fixing_it_clears_the_issue(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 3)
    line = _add(client, sku="SOL-8T-ALTA", quantity=3).json()["lines"][0]
    _set_stock(db_session, "SOL-8T-ALTA", 2)  # a sale meanwhile
    url = f"{LINES}/{line['id']}"
    assert _error(client.patch(url, json={"quantity": 3})) == (409, "insufficient_stock")  # same number: still too many
    assert _error(client.patch(url, json={"quantity": 5})) == (409, "insufficient_stock")
    fixed = client.patch(url, json={"quantity": 2}).json()["lines"][0]
    assert (fixed["quantity"], fixed["issue"], fixed["available"]) == (2, None, True)


def test_changing_a_line_validates_it_now(client, solares, db_session):
    """A change is checked against the product right away: its new price applies (shown as repriced)."""
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    line = _add(client, sku="SOL-8T-ALTA").json()["lines"][0]
    _edit_product(db_session, "SOL-8T-ALTA", price_cents=600000)
    changed = client.patch(f"{LINES}/{line['id']}", json={"quantity": 2}).json()["lines"][0]
    assert (changed["unit_price_cents"], changed["previous_unit_price_cents"], changed["line_total_cents"]) == (
        600000, 649900, 1200000)
    _edit_product(db_session, "SOL-8T-ALTA", price_cents=610000)
    merged = _add(client, sku="SOL-8T-ALTA").json()["lines"][0]  # adding more merges and validates too
    assert (merged["quantity"], merged["unit_price_cents"], merged["previous_unit_price_cents"]) == (3, 610000, 600000)


def test_only_stale_lines_are_revalidated(client, solares, db_session):
    _add(client, sku="SOL-8T-ALTA")
    _add(client, sku="GAS-BOIL-40L")
    db_session.execute(CartLine.__table__.update().where(CartLine.__table__.c.sku == "SOL-8T-ALTA")
                       .values(validated_at=utc_now() - timedelta(minutes=45)))
    db_session.commit()
    for sku in ("SOL-8T-ALTA", "GAS-BOIL-40L"):
        _edit_product(db_session, sku, price_cents=100)
    prices = {line["sku"]: line["unit_price_cents"] for line in client.get(API).json()["lines"]}
    assert prices == {"SOL-8T-ALTA": 100, "GAS-BOIL-40L": 519900}


def test_revalidate_without_a_cart(db_session):
    from app.services.cart import revalidate

    assert revalidate(db_session, None) == 0


# ======================================================================= clear the cart


def test_clear_cart_removes_every_line(client, solares, db_session):
    _set_stock(db_session, "SOL-8T-ALTA", 5)
    _add(client, sku="SOL-8T-ALTA", quantity=3)
    _add(client, sku="GAS-BOIL-40L")
    cleared = client.delete(API)
    assert cleared.status_code == 200
    body = cleared.json()
    assert (body["lines"], body["item_count"], body["subtotal_cents"]) == ([], 0, 0)
    assert cleared.headers["cache-control"] == "no-store"
    assert client.delete(API).json()["lines"] == []  # again (rage click): harmless
    again = _add(client, sku="SOL-8T-ALTA")  # the same cart keeps being used
    assert again.status_code == 200 and not _sets_cart_cookie(again)
    assert db_session.scalar(select(func.count()).select_from(Cart)) == 1


def test_clear_without_a_cart_and_only_ones_own(client, solares, db_session):
    assert client.delete(API).json()["line_count"] == 0  # no cart: nothing to do, none created
    assert db_session.scalar(select(func.count()).select_from(Cart)) == 0
    _add(client, sku="SOL-8T-ALTA")
    client.cookies.clear()  # another visitor
    assert client.delete(API).json()["line_count"] == 0
    assert db_session.scalar(select(func.count()).select_from(CartLine)) == 1  # the first visitor's cart is intact


# ======================================================================= older databases


def test_older_cart_lines_get_snapshots_at_startup(solares, db_session):
    """A cart_lines table from before snapshots: the columns are added and filled from the products;
    the first read validates them."""
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import StaticPool

    from app.db import init_db
    from app.schema_upgrades import add_cart_line_snapshots

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, store_id INTEGER, category_id INTEGER, "
                                "slug VARCHAR(64), sku VARCHAR(64), name VARCHAR(200), description TEXT, "
                                "price_cents INTEGER, stock_on_hand INTEGER NOT NULL DEFAULT 0, is_active BOOLEAN, "
                                "created_at DATETIME)"))
        connection.execute(text("INSERT INTO products (id, store_id, category_id, slug, sku, name, price_cents, is_active) "
                                "VALUES (1, 1, 1, 'p', 'P-1', 'Product', 1234, 1)"))
        connection.execute(text("CREATE TABLE cart_lines (id INTEGER PRIMARY KEY, cart_id INTEGER, product_id INTEGER, "
                                "quantity INTEGER, created_at DATETIME)"))
        connection.execute(text("INSERT INTO cart_lines (id, cart_id, product_id, quantity) VALUES (1, 1, 1, 2), "
                                "(2, 1, 99, 1)"))
    init_db(bind=engine)
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, name, sku, unit_price_cents, validated_at FROM cart_lines ORDER BY id"))
        assert rows.all() == [(1, "Product", "P-1", 1234, None), (2, "", None, 0, None)]
    assert {c["name"] for c in inspect(engine).get_columns("cart_lines")} >= {"previous_unit_price_cents", "issue"}
    assert add_cart_line_snapshots(engine) == 0  # already done
    assert add_cart_line_snapshots(create_engine("sqlite://")) == 0  # no cart_lines table
