"""Checkout: cart -> order (line copies + chosen shipping) + a payment of subtotal + quoted shipping
(app/services/checkout.py, POST /api/stores/<slug>/cart/checkout, GET /api/stores/<slug>/orders/<id>)."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import (
    Cart,
    CartLine,
    Order,
    OrderLine,
    Payment,
    PaymentStatus,
    PaymentTerminal,
    Product,
    ShippingMethod,
    StockMovement,
    StockMovementKind,
    Store,
)
from app.schemas import CartOut, CheckoutOut
from app.config import get_settings
from app.services import checkout as svc
from app.services import payments, shipping
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from tests.ui_text import es

PROJECT = Path(__file__).resolve().parent.parent
SOLARES = read_store_pack(PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json")
STANDARD = next(m for m in SOLARES["shipping_methods"] if m["code"] == "standard")  # expected values: the pack
CART = "/api/stores/solares/cart"
CHECKOUT = f"{CART}/checkout"
ORDERS = "/api/stores/solares/orders"
BODY = {"shipping_method": "standard", "country": "MX", "terminal": "mock_card"}


@pytest.fixture()
def solares(db_session):
    """Solares as shipped, with its mock terminals enabled again: these tests pay through mock_card /
    mock_transfer and the signed callback (the shipped bank_transfer is tested in test_bank_transfer.py)."""
    import_store_pack(db_session, SOLARES)
    store = db_session.scalar(select(Store).where(Store.slug == "solares"))
    for terminal in db_session.scalars(select(PaymentTerminal).where(PaymentTerminal.store_id == store.id)):
        terminal.is_enabled = True
    db_session.commit()
    return store


def _product(db_session, sku: str) -> Product:
    db_session.expire_all()
    return db_session.scalar(select(Product).where(Product.sku == sku))


def _edit(db_session, sku: str, /, **fields) -> None:
    product = _product(db_session, sku)
    for name, value in fields.items():
        setattr(product, name, value)
    db_session.commit()


def _add(client, sku: str, quantity: int = 1):
    response = client.post(f"{CART}/lines", json={"sku": sku, "quantity": quantity})
    assert response.status_code == 200, response.text
    return response.json()


def _count(db_session, model) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(model))


def _nothing_created(db_session) -> None:
    assert (_count(db_session, Order), _count(db_session, OrderLine), _count(db_session, Payment)) == (0, 0, 0)


def _refusal(response) -> list[tuple]:
    return [(tuple(item["loc"]), item["type"]) for item in response.json()["detail"]]


# ===================================================================== happy path


def test_happy_path_returns_order_and_payment_ids(client, solares, db_session):
    _edit(db_session, "SOL-8T-ALTA", stock_on_hand=3)
    _add(client, "SOL-8T-ALTA", 2)
    cart = _add(client, "GAS-BOIL-40L")
    response = client.post(CHECKOUT, json=BODY)
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"order", "payment"}  # side by side: no combined shipping + pay field
    order, payment = body["order"], body["payment"]
    assert re.fullmatch(r"ord_[A-Za-z0-9_-]{24}", order["id"])
    assert re.fullmatch(r"pay_[A-Za-z0-9_-]{24}", payment["id"])

    # Total = subtotal of the cart's lines + the quote of the chosen method (the shipping service's own answer).
    quote = shipping.quote(db_session, solares, "standard", subtotal_cents=cart["subtotal_cents"], country="MX")
    assert order["subtotal_cents"] == cart["subtotal_cents"] == 2 * 649900 + 519900
    assert order["shipping"] == {"method": "standard", "name": STANDARD["name"], "country": "MX",
                                 "amount_cents": quote.amount_cents, "free_shipping": quote.free_shipping}
    assert order["total_cents"] == order["subtotal_cents"] + quote.amount_cents
    assert (payment["amount_cents"], payment["currency"], payment["status"], payment["terminal"]) == (
        order["total_cents"], "MXN", "pending", "mock_card")
    assert order["status"] == "awaiting_payment"
    assert [(line["sku"], line["name"], line["quantity"], line["unit_price_cents"], line["line_total_cents"])
            for line in order["lines"]] == [(line["sku"], line["name"], line["quantity"], line["unit_price_cents"],
                                             line["line_total_cents"]) for line in cart["lines"]]
    assert order["item_count"] == 3 and order["total_display"].startswith("MXN ")

    # The records behind the ids.
    db_session.expire_all()
    stored = db_session.scalar(select(Order).where(Order.public_id == order["id"]))
    assert stored.payment.public_id == payment["id"] and stored.payment.amount_cents == stored.total_cents
    assert client.get(f"{ORDERS}/{order['id']}").json() == body


def test_the_cart_empties_into_the_order_and_stays_usable(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    client.post(CHECKOUT, json=BODY)
    assert client.get(CART).json()["lines"] == []  # empties, doesn't lock
    again = client.post(f"{CART}/lines", json={"sku": "SOL-20T-ALTA"})  # keep shopping: the same cart
    assert again.status_code == 200 and not any(
        v.startswith("magic_stores_cart=") for v in again.headers.get_list("set-cookie"))
    assert _count(db_session, Cart) == 1


def test_shipping_is_charged_below_the_free_threshold(client, solares, db_session):
    _edit(db_session, "SOL-8T-ALTA", price_cents=100000)  # MXN 1,000.00 < the free-shipping amount
    _add(client, "SOL-8T-ALTA")
    order = client.post(CHECKOUT, json={**BODY, "country": "mx"}).json()["order"]
    assert order["shipping"]["amount_cents"] == STANDARD["rate_cents"]
    assert order["shipping"]["free_shipping"] is False and order["shipping"]["country"] == "MX"
    assert order["total_cents"] == 100000 + STANDARD["rate_cents"]


def test_the_cart_never_holds_shipping_or_payment(client, solares):
    _add(client, "SOL-8T-ALTA")
    client.post("/api/stores/solares/shipping/quote", json={"method": "express", "subtotal_cents": 1, "country": "MX"})
    cart = client.get(CART).json()
    assert set(cart) == set(CartOut.model_fields)
    assert not {"shipping", "payment", "shipping_method", "terminal", "total_cents"} & set(cart)


def test_the_payment_is_an_ordinary_payment(admin_client, solares, db_session):
    """Created by the payments service: the admin payments API (and its terminal) see it as any other."""
    _add(admin_client, "SOL-8T-ALTA")  # (a signed-in admin shops like anyone: the cart is by cookie)
    payment = admin_client.post(CHECKOUT, json={**BODY, "terminal": "mock_transfer"}).json()["payment"]
    seen = admin_client.get(f"/api/stores/solares/payments/{payment['id']}").json()
    assert (seen["amount_cents"], seen["terminal"], seen["status"]) == (payment["amount_cents"], "mock_transfer", "pending")


@pytest.mark.parametrize("status,expected", [(PaymentStatus.succeeded, "paid"), (PaymentStatus.failed, "payment_failed")])
def test_the_orders_status_follows_its_payment(client, solares, db_session, status, expected):
    _add(client, "SOL-8T-ALTA")
    body = client.post(CHECKOUT, json=BODY).json()
    payment = db_session.scalar(select(Payment).where(Payment.public_id == body["payment"]["id"]))
    payments.mark_payment(db_session, payment, status)
    seen = client.get(f"{ORDERS}/{body['order']['id']}").json()
    assert (seen["order"]["status"], seen["payment"]["status"]) == (expected, status.value)


# ===================================================================== refused: unavailable / oversell


@pytest.mark.parametrize("change,issue", [
    ({"is_active": False}, "unavailable"),
    ({"stock_on_hand": 0}, "out_of_stock"),
])
def test_an_unavailable_line_refuses_the_checkout(client, solares, db_session, change, issue):
    _add(client, "SOL-8T-ALTA")
    line_id = _add(client, "GAS-BOIL-40L")["lines"][1]["id"]
    _edit(db_session, "GAS-BOIL-40L", **change)  # right now: no waiting for the cart's 30-minute check
    response = client.post(CHECKOUT, json=BODY)
    assert response.status_code == 409
    assert _refusal(response) == [(("cart", "lines", line_id), issue)]
    _nothing_created(db_session)
    assert _product(db_session, "SOL-8T-ALTA").stock_on_hand == 1  # nothing taken
    assert client.get(CART).json()["line_count"] == 2  # the cart is kept, and now shows the problem
    assert client.get(CART).json()["lines"][1]["issue"] == issue


def test_oversell_refuses_the_checkout(client, solares, db_session):
    _edit(db_session, "SOL-8T-ALTA", stock_on_hand=3)
    line_id = _add(client, "SOL-8T-ALTA", 3)["lines"][0]["id"]
    _edit(db_session, "SOL-8T-ALTA", stock_on_hand=2)  # sold elsewhere since
    response = client.post(CHECKOUT, json=BODY)
    assert response.status_code == 409 and _refusal(response) == [(("cart", "lines", line_id), "insufficient_stock")]
    _nothing_created(db_session)
    assert _product(db_session, "SOL-8T-ALTA").stock_on_hand == 2


def test_every_problem_line_is_listed(client, solares, db_session):
    cart = [_add(client, sku) for sku in ("SOL-8T-ALTA", "GAS-BOIL-40L", "SOL-20T-ALTA")][-1]
    ids = {line["sku"]: line["id"] for line in cart["lines"]}
    _edit(db_session, "SOL-8T-ALTA", stock_on_hand=0)
    _edit(db_session, "SOL-20T-ALTA", is_active=False)
    assert set(_refusal(client.post(CHECKOUT, json=BODY))) == {
        (("cart", "lines", ids["SOL-8T-ALTA"]), "out_of_stock"), (("cart", "lines", ids["SOL-20T-ALTA"]), "unavailable")}


def test_a_changed_price_refuses_until_the_shopper_has_seen_it(client, solares, db_session):
    line_id = _add(client, "SOL-8T-ALTA")["lines"][0]["id"]
    _edit(db_session, "SOL-8T-ALTA", price_cents=700000)
    refused = client.post(CHECKOUT, json=BODY)
    assert refused.status_code == 409 and _refusal(refused) == [(("cart", "lines", line_id), "price_changed")]
    _nothing_created(db_session)
    line = client.get(CART).json()["lines"][0]
    assert (line["unit_price_cents"], line["previous_unit_price_cents"]) == (700000, 649900)  # shown to the shopper
    order = client.post(CHECKOUT, json=BODY).json()["order"]  # confirmed again: the new price
    assert order["subtotal_cents"] == 700000


@pytest.mark.parametrize("setup", ["no_cart", "emptied"])
def test_an_empty_cart_is_refused(client, solares, db_session, setup):
    if setup == "emptied":
        _add(client, "SOL-8T-ALTA")
        client.delete(CART)
    response = client.post(CHECKOUT, json=BODY)
    assert response.status_code == 409 and _refusal(response) == [(("cart",), "cart_empty")]
    _nothing_created(db_session)


# ===================================================================== refused: shipping / terminal / body


@pytest.mark.parametrize("body,loc,kind", [
    ({"shipping_method": "teleport"}, ("body", "shipping_method"), "unknown_method"),
    ({"country": None}, ("body", "country"), "country_required"),
    ({"country": "US"}, ("body", "country"), "country_not_served"),
    ({"terminal": "mock_gold"}, ("body", "terminal"), "unknown_terminal"),
])
def test_shipping_and_terminal_choices_are_checked(client, solares, db_session, body, loc, kind):
    _add(client, "SOL-8T-ALTA")
    response = client.post(CHECKOUT, json={**BODY, **body})
    assert response.status_code == 422 and _refusal(response) == [(loc, kind)]
    _nothing_created(db_session)
    assert client.get(CART).json()["line_count"] == 1


def test_inactive_method_and_disabled_terminal_are_refused(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    db_session.scalar(select(ShippingMethod).where(ShippingMethod.code == "express")).is_active = False
    db_session.scalar(select(PaymentTerminal).where(PaymentTerminal.code == "mock_transfer")).is_enabled = False
    db_session.commit()
    assert _refusal(client.post(CHECKOUT, json={**BODY, "shipping_method": "express"}))[0][1] == "inactive_method"
    assert _refusal(client.post(CHECKOUT, json={**BODY, "terminal": "mock_transfer"}))[0][1] == "terminal_disabled"
    _nothing_created(db_session)


def test_terminal_disabled_at_the_last_moment(client, solares, db_session, monkeypatch):
    _add(client, "SOL-8T-ALTA")

    def disabled(*_args, **_kwargs):
        raise payments.PaymentError("terminal_disabled", "terminal", "Payment terminal 'mock_card' is disabled.")

    monkeypatch.setattr(payments, "create_payment", disabled)
    response = client.post(CHECKOUT, json=BODY)
    assert response.status_code == 422 and _refusal(response) == [(("body", "terminal"), "terminal_disabled")]
    _nothing_created(db_session)


def test_nothing_to_pay_is_refused(client, solares, db_session):
    _edit(db_session, "SOL-8T-ALTA", price_cents=0)
    _add(client, "SOL-8T-ALTA")
    db_session.add(ShippingMethod(store_id=solares.id, code="pickup", name="Pickup", rate_cents=0))
    db_session.commit()
    response = client.post(CHECKOUT, json={**BODY, "shipping_method": "pickup"})
    assert response.status_code == 422 and _refusal(response) == [(("cart",), "nothing_to_pay")]
    _nothing_created(db_session)


@pytest.mark.parametrize("body", [
    {"shipping_method": "standard", "country": "MX"},  # no terminal
    {"terminal": "mock_card", "country": "MX"},  # no method
    {**BODY, "country": "MEX"},
    {**BODY, "shipping": "standard"},  # unknown field (no combined shipping + pay field)
    {**BODY, "total_cents": 1},  # the total is never taken from the client
])
def test_malformed_bodies_are_422(client, solares, db_session, body):
    _add(client, "SOL-8T-ALTA")
    assert client.post(CHECKOUT, json=body).status_code == 422
    _nothing_created(db_session)


def test_non_json_bodies_are_refused(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    for content_type in ("application/x-www-form-urlencoded", "text/plain"):
        assert client.post(CHECKOUT, content=json.dumps(BODY), headers={"Content-Type": content_type}).status_code == 422
    _nothing_created(db_session)


def test_unknown_store_is_404(client):
    assert client.post("/api/stores/nope/cart/checkout", json=BODY).status_code == 404


# ===================================================================== rage clicks on "Place order"


def test_the_same_checkout_key_gives_one_order(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    first = client.post(CHECKOUT, json=BODY, headers={"Idempotency-Key": "checkout-0001"})
    for _ in range(4):
        replay = client.post(CHECKOUT, json=BODY, headers={"Idempotency-Key": "checkout-0001"})
        assert replay.status_code == 201 and replay.headers["idempotent-replayed"] == "true"
        assert replay.json() == first.json()
    assert (_count(db_session, Order), _count(db_session, Payment)) == (1, 1)


def test_a_second_click_without_key_finds_the_cart_empty(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    assert client.post(CHECKOUT, json=BODY).status_code == 201
    assert _refusal(client.post(CHECKOUT, json=BODY)) == [(("cart",), "cart_empty")]
    assert (_count(db_session, Order), _count(db_session, Payment)) == (1, 1)


def test_a_refused_checkout_frees_its_key_and_keys_are_per_request(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    refused = client.post(CHECKOUT, json={**BODY, "country": "US"}, headers={"Idempotency-Key": "checkout-0002"})
    assert refused.status_code == 422
    assert client.post(CHECKOUT, json=BODY, headers={"Idempotency-Key": "checkout-0002"}).status_code == 201
    add_key = client.post(f"{CART}/lines", json={"sku": "SOL-20T-ALTA"}, headers={"Idempotency-Key": "checkout-0002"})
    assert add_key.status_code == 422 and add_key.json()["detail"][0]["type"] == "idempotency_key_reused"


# ===================================================================== orders afterwards


def test_orders_keep_their_copies(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    order_id = client.post(CHECKOUT, json=BODY).json()["order"]["id"]
    _edit(db_session, "SOL-8T-ALTA", name="Renamed", price_cents=1)
    db_session.delete(_product(db_session, "SOL-8T-ALTA"))
    db_session.commit()
    line = client.get(f"{ORDERS}/{order_id}").json()["order"]["lines"][0]
    assert (line["product_id"], line["sku"], line["name"], line["unit_price_cents"]) == (
        None, "SOL-8T-ALTA", "Calentador solar 8 tubos, alta presión", 649900)


def test_unknown_or_foreign_orders_are_404(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    order_id = client.post(CHECKOUT, json=BODY).json()["order"]["id"]
    db_session.add(Store(slug="otra", name="Otra"))
    db_session.commit()
    for path in (f"{ORDERS}/ord_nope", f"/api/stores/otra/orders/{order_id}", f"{ORDERS}/{'x' * 60}"):
        assert client.get(path).status_code == 404, path
    assert client.get(f"{ORDERS}/{order_id}").status_code == 200


def test_deleting_a_store_deletes_its_orders(client, solares, db_session):
    _add(client, "SOL-8T-ALTA")
    client.post(CHECKOUT, json=BODY)
    db_session.delete(db_session.get(Store, solares.id))
    db_session.commit()
    assert (_count(db_session, Order), _count(db_session, OrderLine)) == (0, 0)


def test_documented_answer_is_the_model(client, solares):
    _add(client, "SOL-8T-ALTA")
    body = client.post(CHECKOUT, json=BODY).json()
    assert CheckoutOut.model_validate(body).model_dump() == body


# ===================================================================== the page


def test_cart_page_has_the_checkout_texts_and_apis(default_lang_client, solares):
    html = default_lang_client.get("/stores/solares/cart").text
    config = json.loads(re.search(r'id="cart-config">(.*?)</script>', html, re.S).group(1))
    assert config["store_api"] == "/api/stores/solares" and config["api"] == CART
    assert config["texts"]["checkout.pay"] == es("cart.checkout.pay")
    assert config["texts"]["error.price_changed"] == es("cart.error.price_changed")
    assert "checkout_note" not in config["texts"]


def test_the_stock_log_shows_the_orders_movements(admin_client, solares, db_session):
    """Admin pages list an order's stock movements (their type has a label) with the order id as reason."""
    from tests.ui_text import en

    _add(admin_client, "SOL-8T-ALTA")
    body = admin_client.post(CHECKOUT, json=BODY).json()
    order_id = body["order"]["id"]
    _pay(db_session, body, PaymentStatus.succeeded)
    log = admin_client.get(f"/admin/stores/solares/inventory/log?kind=order")
    assert log.status_code == 200
    assert f"<td>{en('admin.stock.kind.order')}</td>" in log.text and f"<td>{order_id}</td>" in log.text
    product = _product(db_session, "SOL-8T-ALTA")
    assert admin_client.get(f"/admin/stores/solares/products/{product.id}").status_code == 200


@pytest.mark.parametrize("key,kind", [("short", "invalid_idempotency_key"), ("add-key-0001", "idempotency_key_reused")])
def test_checkout_key_problems(client, solares, db_session, key, kind):
    client.post(f"{CART}/lines", json={"sku": "SOL-8T-ALTA"}, headers={"Idempotency-Key": "add-key-0001"})
    response = client.post(CHECKOUT, json=BODY, headers={"Idempotency-Key": key})
    assert response.status_code == 422 and _refusal(response) == [(("header", "Idempotency-Key"), kind)]
    _nothing_created(db_session)



# ===================================================================== stock follows the payment
# Not taken at checkout or while pending; taken when the payment succeeds; never taken when it fails.

SECRET = "c0ffee" * 11  # test-only PAYMENT_CALLBACK_SECRET (66 characters)


@pytest.fixture()
def terminal_secret(monkeypatch):
    monkeypatch.setenv("PAYMENT_CALLBACK_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _terminal_answers(client, payment_id: str, status: str):
    """The terminal's signed callback (app/routers/payments.py), signed from the documented formula."""
    raw = json.dumps({"payment": payment_id, "status": status}).encode()
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(SECRET.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return client.post("/api/stores/solares/payments/callback", content=raw, headers={
        "Content-Type": "application/json", "X-Terminal-Timestamp": timestamp, "X-Terminal-Signature": signature})


def _pay(db_session, body: dict, status: PaymentStatus):
    payment = db_session.scalar(select(Payment).where(Payment.public_id == body["payment"]["id"]))
    return payments.mark_payment(db_session, payment, status)


def _order_movements(db_session) -> list[tuple]:
    db_session.expire_all()
    return [(m.product.sku, m.delta, m.quantity_after, m.reason) for m in db_session.scalars(
        select(StockMovement).where(StockMovement.kind == StockMovementKind.order).order_by(StockMovement.id))]


def _checkout_two(client, db_session) -> dict:
    _edit(db_session, "SOL-8T-ALTA", stock_on_hand=5)
    _edit(db_session, "GAS-BOIL-40L", stock_on_hand=4)
    _add(client, "SOL-8T-ALTA", 2)
    _add(client, "GAS-BOIL-40L", 3)
    return client.post(CHECKOUT, json=BODY).json()


def _stock(db_session) -> tuple[int, int]:
    return (_product(db_session, "SOL-8T-ALTA").stock_on_hand, _product(db_session, "GAS-BOIL-40L").stock_on_hand)


def test_checkout_and_pending_take_no_stock(client, solares, db_session):
    body = _checkout_two(client, db_session)
    assert body["payment"]["status"] == "pending"
    assert _stock(db_session) == (5, 4)  # checked at checkout, not taken
    assert _order_movements(db_session) == []
    assert body["order"]["stock"] == {"taken": False, "taken_at": None, "short": False}


def test_the_terminals_success_takes_each_lines_stock(client, solares, db_session, terminal_secret):
    body = _checkout_two(client, db_session)
    order_id = body["order"]["id"]
    answered = _terminal_answers(client, body["payment"]["id"], "succeeded")
    assert answered.status_code == 200 and answered.json()["status"] == "succeeded"
    assert _stock(db_session) == (3, 1)
    assert _order_movements(db_session) == [("SOL-8T-ALTA", -2, 3, order_id), ("GAS-BOIL-40L", -3, 1, order_id)]
    order = client.get(f"{ORDERS}/{order_id}").json()["order"]
    assert order["status"] == "paid" and order["stock"]["taken"] is True and order["stock"]["short"] is False
    assert re.fullmatch(r"\d{4}-.+Z", order["stock"]["taken_at"])
    assert [line["short_quantity"] for line in order["lines"]] == [0, 0]
    # The terminal repeats its answer (retries): nothing is taken twice.
    assert _terminal_answers(client, body["payment"]["id"], "succeeded").status_code == 200
    assert _stock(db_session) == (3, 1) and len(_order_movements(db_session)) == 2


def test_a_failed_or_cancelled_payment_takes_nothing(client, solares, db_session, terminal_secret):
    body = _checkout_two(client, db_session)
    answered = _terminal_answers(client, body["payment"]["id"], "failed")  # declined / cancelled at the terminal
    assert answered.status_code == 200
    assert _stock(db_session) == (5, 4) and _order_movements(db_session) == []
    order = client.get(f"{ORDERS}/{body['order']['id']}").json()["order"]
    assert order["status"] == "payment_failed" and order["stock"]["taken"] is False
    assert _terminal_answers(client, body["payment"]["id"], "succeeded").status_code == 409  # final: can't succeed later
    assert _stock(db_session) == (5, 4)


def test_the_admin_simulator_takes_the_stock_too(admin_client, solares, db_session):
    body = _checkout_two(admin_client, db_session)
    answered = admin_client.post(f"/admin/stores/solares/try-payment/{body['payment']['id']}/succeed",
                                 data={"mock_reference": "AUTH-1"}, follow_redirects=False)
    assert answered.status_code == 303
    assert _stock(db_session) == (3, 1) and len(_order_movements(db_session)) == 2


def test_sold_out_before_the_payment_takes_what_is_left(client, solares, db_session):
    """Stock isn't held while the payment is pending: if it sold out meanwhile, the (final) success
    takes what is left, never below 0, and the order records the missing units."""
    body = _checkout_two(client, db_session)  # 2 x SOL-8T-ALTA (stock 5), 3 x GAS-BOIL-40L (stock 4)
    _edit(db_session, "SOL-8T-ALTA", stock_on_hand=1)
    _edit(db_session, "GAS-BOIL-40L", stock_on_hand=0)
    _pay(db_session, body, PaymentStatus.succeeded)
    assert _stock(db_session) == (0, 0)
    assert _order_movements(db_session) == [("SOL-8T-ALTA", -1, 0, body["order"]["id"])]  # none for 0 taken
    order = client.get(f"{ORDERS}/{body['order']['id']}").json()["order"]
    assert [line["short_quantity"] for line in order["lines"]] == [1, 3]
    assert order["status"] == "paid" and order["stock"]["short"] is True


def test_a_product_deleted_before_the_payment_is_short(client, solares, db_session):
    body = _checkout_two(client, db_session)
    db_session.delete(_product(db_session, "GAS-BOIL-40L"))
    db_session.commit()
    _pay(db_session, body, PaymentStatus.succeeded)
    order = client.get(f"{ORDERS}/{body['order']['id']}").json()["order"]
    assert [(line["sku"], line["short_quantity"]) for line in order["lines"]] == [("SOL-8T-ALTA", 0), ("GAS-BOIL-40L", 3)]
    assert _product(db_session, "SOL-8T-ALTA").stock_on_hand == 3


def test_take_what_is_left_when_the_stock_changes_meanwhile(solares, db_session, monkeypatch):
    """The compare-and-set loop: the stock changes between reading it and taking it."""
    product = _product(db_session, "SOL-8T-ALTA")
    product.stock_on_hand = 2
    db_session.commit()
    real_scalar = db_session.scalar
    reads = {"n": 0}

    def stock_drops_after_first_read(stmt):
        value = real_scalar(stmt)
        if "stock_on_hand" in str(stmt) and reads["n"] == 0:
            reads["n"] += 1
            db_session.execute(Product.__table__.update().where(Product.__table__.c.id == product.id)
                               .values(stock_on_hand=1))
        return value

    monkeypatch.setattr(db_session, "scalar", stock_drops_after_first_read)
    assert svc._take(db_session, product.id, 5) == (1, 0)


def test_payments_without_an_order_are_unaffected(admin_client, solares, db_session):
    """A payment made in admin (payments API / Try payment) has no order: succeeding it takes nothing."""
    made = admin_client.post("/api/stores/solares/payments", json={"terminal": "mock_card", "amount_cents": 1999}).json()
    payment = db_session.scalar(select(Payment).where(Payment.public_id == made["id"]))
    before = _count(db_session, StockMovement)
    payments.mark_payment(db_session, payment, PaymentStatus.succeeded)
    assert _count(db_session, StockMovement) == before
    assert svc.take_stock_for_payment(db_session, payment) is None


def test_status_and_stock_commit_together(client, solares, db_session, monkeypatch):
    """If taking the stock fails, the payment isn't marked either (one transaction)."""
    body = _checkout_two(client, db_session)

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(svc, "take_stock_for_payment", broken)
    with pytest.raises(RuntimeError):
        _pay(db_session, body, PaymentStatus.succeeded)
    db_session.rollback()
    db_session.expire_all()
    assert db_session.scalar(select(Payment)).status == PaymentStatus.pending
    assert _stock(db_session) == (5, 4)


def test_older_order_tables_get_the_stock_columns(solares):
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import StaticPool

    from app.db import init_db
    from app.schema_upgrades import add_order_stock_columns

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY, public_id VARCHAR(40), store_id INTEGER)"))
        connection.execute(text("CREATE TABLE order_lines (id INTEGER PRIMARY KEY, order_id INTEGER, quantity INTEGER)"))
        connection.execute(text("INSERT INTO order_lines (id, order_id, quantity) VALUES (1, 1, 2)"))
    init_db(bind=engine)
    assert "stock_taken_at" in {c["name"] for c in inspect(engine).get_columns("orders")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT short_quantity FROM order_lines")).scalar() == 0
    assert add_order_stock_columns(engine) == 0
    assert add_order_stock_columns(create_engine("sqlite://")) == 0
