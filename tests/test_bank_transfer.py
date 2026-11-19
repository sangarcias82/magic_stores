"""Offline bank transfer terminal (solares' "Transferencia bancaria", code ``bank_transfer``):
deposit details in the terminal's config (app/services/bank_transfer.py), checkout creates a pending
payment with a reference and a copy of the instructions, the shopper sees them (checkout answer,
cart page texts, order page), and only an admin confirms or rejects the payment
(app/routers/admin_payments.py); the signed callback and Try payment are refused for it. Also: the
pack section ``payment_terminals`` (validation, import, re-import), the startup catch-up for older
databases and the schema upgrade."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import create_user
from app.config import get_settings
from app.db import get_db, init_db
from app.main import create_app
from app.models import (
    Order,
    Payment,
    PaymentStatus,
    PaymentTerminal,
    Product,
    StockMovement,
    StockMovementKind,
    Store,
    TerminalKind,
    User,
    UserRole,
)
from app.routers.admin_payments import NOTE_MAX
from app.schema_upgrades import add_bank_transfer_columns
from app.services import bank_transfer, payments
from app.services.pack_seed import catch_up_terminals
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack, validate_store_pack
from tests.ui_text import en, es, page_text

PROJECT = Path(__file__).resolve().parent.parent
SOLARES_FILE = PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json"
SOLARES = read_store_pack(SOLARES_FILE)
SOLARES_SEED = "fixtures/packs/solares/solares.store-pack.v1.json"
PACK_BANK = next(t for t in SOLARES["payment_terminals"] if t["code"] == "bank_transfer")
API = "/api/stores/solares"
CHECKOUT = f"{API}/cart/checkout"
BODY = {"shipping_method": "standard", "country": "MX", "terminal": "bank_transfer"}
PAYMENTS_ADMIN = "/admin/stores/solares/payments"
TERMINALS_ADMIN = "/admin/stores/solares/terminals"
SECRET = "b4nk" * 16  # test-only PAYMENT_CALLBACK_SECRET
VALID_CLABE = "032180000118359719"  # the published example CLABE (control digit 9)
OTHER_CLABE = "012180015031234560"  # another valid one (control digit computed: 0)


@pytest.fixture()
def solares(db_session) -> Store:
    """Solares as shipped: bank_transfer enabled (example details), mock_card / mock_transfer disabled."""
    import_store_pack(db_session, SOLARES)
    return db_session.scalar(select(Store).where(Store.slug == "solares"))


@pytest.fixture()
def terminal_secret(monkeypatch):
    monkeypatch.setenv("PAYMENT_CALLBACK_SECRET", SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login(client) -> None:
    response = client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"},
                           follow_redirects=False)
    assert response.status_code == 303


def _terminal(db_session, code: str, store_slug: str = "solares") -> PaymentTerminal:
    db_session.expire_all()
    return db_session.scalar(select(PaymentTerminal).join(Store).where(Store.slug == store_slug,
                                                                       PaymentTerminal.code == code))


def _payment(db_session, public_id: str) -> Payment:
    db_session.expire_all()
    return db_session.scalar(select(Payment).where(Payment.public_id == public_id))


def _stock(db_session, sku: str) -> int:
    db_session.expire_all()
    return db_session.scalar(select(Product.stock_on_hand).where(Product.sku == sku))


def _checkout(client, sku: str = "SOL-8T-ALTA", quantity: int = 1, body: dict = BODY):
    added = client.post(f"{API}/cart/lines", json={"sku": sku, "quantity": quantity})
    assert added.status_code == 200, added.text
    return client.post(CHECKOUT, json=body)


def _placed(client) -> dict:
    response = _checkout(client)
    assert response.status_code == 201, response.text
    return response.json()


def _signed_callback(client, payment_id: str, status: str):
    raw = json.dumps({"payment": payment_id, "status": status}).encode()
    timestamp = str(int(time.time()))
    signature = "sha256=" + hmac.new(SECRET.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return client.post(f"{API}/payments/callback", content=raw, headers={
        "Content-Type": "application/json", "X-Terminal-Timestamp": timestamp, "X-Terminal-Signature": signature})


def _order_stock_movements(db_session) -> list[tuple]:
    db_session.expire_all()
    return [(m.product.sku, m.delta) for m in db_session.scalars(
        select(StockMovement).where(StockMovement.kind == StockMovementKind.order))]


# ============================================================ deposit details (service)


@pytest.mark.parametrize("clabe", [VALID_CLABE, OTHER_CLABE, PACK_BANK["config"]["clabe"]])
def test_valid_clabes(clabe):
    assert bank_transfer.is_valid_clabe(clabe)
    assert bank_transfer.clabe_control_digit(clabe[:17]) == int(clabe[17])


@pytest.mark.parametrize("clabe", [VALID_CLABE[:17] + "0", "1" + VALID_CLABE[1:], VALID_CLABE[:17], VALID_CLABE + "0",
                                   "03218000011835971x", "０３２１８００００１１８３５９７１９"])
def test_invalid_clabes(clabe):
    assert not bank_transfer.is_valid_clabe(clabe)


def test_config_is_cleaned():
    config, errors = bank_transfer.validate_config({
        "bank": "  Banco   Ejemplo ", "beneficiary": "Solares\tSA", "clabe": "032 180 0001-1835 9719",
        "account": " 12-34-56-78 ", "note": "  Pay today.  "})
    assert errors == {}
    assert config == {"bank": "Banco Ejemplo", "beneficiary": "Solares SA", "clabe": VALID_CLABE,
                      "account": "12345678", "note": "Pay today."}


GOOD = {"bank": "Banco", "beneficiary": "Solares", "clabe": VALID_CLABE, "account": "", "note": ""}


@pytest.mark.parametrize("field, value, error", [
    ("bank", "", ("required", {})),
    ("bank", "b" * (bank_transfer.BANK_MAX + 1), ("too_long", {"max_len": bank_transfer.BANK_MAX})),
    ("beneficiary", "   ", ("required", {})),
    ("beneficiary", "b" * (bank_transfer.BENEFICIARY_MAX + 1), ("too_long", {"max_len": bank_transfer.BENEFICIARY_MAX})),
    ("clabe", "", ("required", {})),
    ("clabe", "12345", ("clabe_format", {})),
    ("clabe", "03218000011835971A", ("clabe_format", {})),
    ("clabe", "０３２１８００００１１８３５９７１９", ("clabe_format", {})),  # full-width digits
    ("clabe", VALID_CLABE[:17] + "1", ("clabe_check", {})),
    ("account", "12345", ("account_format", {})),
    ("account", "1" * 21, ("account_format", {})),
    ("account", "12345a", ("account_format", {})),
    ("note", "n" * (bank_transfer.NOTE_MAX + 1), ("too_long", {"max_len": bank_transfer.NOTE_MAX})),
])
def test_config_errors(field, value, error):
    _, errors = bank_transfer.validate_config({**GOOD, field: value})
    assert errors == {field: error}


def test_is_configured():
    assert bank_transfer.is_configured(PaymentTerminal(code="bank_transfer", config=dict(GOOD)))
    assert not bank_transfer.is_configured(PaymentTerminal(code="bank_transfer", config=None))
    assert not bank_transfer.is_configured(PaymentTerminal(code="bank_transfer", config={**GOOD, "clabe": "1"}))


def test_references_are_seven_digits_and_unique_in_the_store(db_session, solares, monkeypatch):
    taken = payments.create_payment(db_session, solares, "bank_transfer", 1000).transfer_reference
    assert re.fullmatch(r"[1-9][0-9]{6}", taken)
    # The random source first offers the taken reference, then another: the taken one is skipped.
    offers = iter([int(taken) - 1_000_000, 1_234_567 - 1_000_000])
    monkeypatch.setattr(bank_transfer.secrets, "randbelow", lambda _n: next(offers))
    assert bank_transfer.new_reference(db_session, solares.id) == "1234567"


def test_the_database_refuses_a_reference_twice_in_a_store(db_session, solares):
    first = payments.create_payment(db_session, solares, "bank_transfer", 1000)
    other = Store(slug="other-shop", name="Other", currency="MXN")
    db_session.add(other)
    db_session.commit()
    other_card = _terminal(db_session, "mock_card", "other-shop")
    db_session.add(Payment(store_id=other.id, terminal_id=other_card.id, amount_cents=1, currency="MXN",
                           transfer_reference=first.transfer_reference))
    db_session.commit()  # the same reference in another store: fine
    db_session.add(Payment(store_id=solares.id, terminal_id=first.terminal_id, amount_cents=1, currency="MXN",
                           transfer_reference=first.transfer_reference))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ============================================================ solares as shipped


def test_solares_takes_bank_transfers_only(client, solares):
    terminals = client.get(f"{API}/payment/terminals").json()
    assert terminals == [
        {"code": "bank_transfer", "name": "Transferencia bancaria", "kind": "bank_transfer", "enabled": True,
         "sort_order": 0},
        {"code": "mock_card", "name": "Mock card", "kind": "mock", "enabled": False, "sort_order": 10},
        {"code": "mock_transfer", "name": "Mock bank transfer", "kind": "mock", "enabled": False, "sort_order": 11},
    ]
    assert "config" not in json.dumps(terminals) and PACK_BANK["config"]["clabe"] not in json.dumps(terminals)
    refused = _checkout(client, body={**BODY, "terminal": "mock_card"})  # the mocks are unlinked
    assert refused.status_code == 422 and refused.json()["detail"][0]["type"] == "terminal_disabled"


def test_the_terminal_keeps_the_pack_details(db_session, solares):
    terminal = _terminal(db_session, "bank_transfer")
    assert terminal.kind == TerminalKind.bank_transfer and terminal.is_bank_transfer and not terminal.is_mock
    assert terminal.config == PACK_BANK["config"]


# ============================================================ checkout


def test_checkout_gives_a_pending_payment_with_deposit_instructions(client, db_session, solares):
    body = _placed(client)
    order, payment = body["order"], body["payment"]
    assert (payment["status"], payment["terminal"], payment["kind"]) == ("pending", "bank_transfer", "bank_transfer")
    assert order["status"] == "awaiting_payment" and payment["amount_cents"] == order["total_cents"] == 649900 + 45000
    reference = payment["transfer_reference"]
    assert re.fullmatch(r"[1-9][0-9]{6}", reference)
    assert payment["instructions"] == {
        **PACK_BANK["config"], "reference": reference, "concept": payment["id"],
        "note_translations": {"en": PACK_BANK["i18n"]["en"]["note"]},  # the pack's English note, copied too
        "amount_cents": order["total_cents"], "currency": "MXN", "amount_display": order["total_display"]}
    assert client.get(f"{API}/orders/{order['id']}").json() == body  # the order API says the same
    stored = _payment(db_session, payment["id"])
    assert stored.transfer_reference == reference and stored.instructions["clabe"] == PACK_BANK["config"]["clabe"]
    assert _stock(db_session, "SOL-8T-ALTA") == 1 and _order_stock_movements(db_session) == []  # nothing taken yet


def test_every_payment_gets_its_own_reference(client, db_session, solares):
    _edit_stock(db_session, "SOL-8T-ALTA", 5)
    references = {_placed(client)["payment"]["transfer_reference"] for _ in range(3)}
    assert len(references) == 3


def _edit_stock(db_session, sku: str, stock: int) -> None:
    db_session.expire_all()
    db_session.scalar(select(Product).where(Product.sku == sku)).stock_on_hand = stock
    db_session.commit()


def test_mock_payments_have_no_reference_or_instructions(client, db_session, solares):
    _terminal(db_session, "mock_card").is_enabled = True
    db_session.commit()
    payment = _checkout(client, body={**BODY, "terminal": "mock_card"}).json()["payment"]
    assert (payment["kind"], payment["transfer_reference"], payment["instructions"]) == ("mock", None, None)


def test_the_instructions_are_a_copy(client, db_session, solares):
    body = _placed(client)
    terminal = _terminal(db_session, "bank_transfer")
    terminal.config = {**terminal.config, "bank": "Otro Banco", "clabe": OTHER_CLABE}
    db_session.commit()
    seen = client.get(f"{API}/orders/{body['order']['id']}").json()["payment"]["instructions"]
    assert (seen["bank"], seen["clabe"]) == (PACK_BANK["config"]["bank"], PACK_BANK["config"]["clabe"])
    _edit_stock(db_session, "SOL-8T-ALTA", 1)
    later = _placed(client)["payment"]["instructions"]
    assert (later["bank"], later["clabe"]) == ("Otro Banco", OTHER_CLABE)  # new shoppers get the new details


@pytest.mark.parametrize("config", [None, {}, {**PACK_BANK["config"], "clabe": "123"}])
def test_incomplete_details_refuse_the_checkout(client, db_session, solares, config):
    _terminal(db_session, "bank_transfer").config = config
    db_session.commit()
    refused = _checkout(client)
    assert refused.status_code == 422
    assert [(d["loc"], d["type"]) for d in refused.json()["detail"]] == [(["body", "terminal"], "terminal_not_configured")]
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0
    assert db_session.scalar(select(func.count()).select_from(Order)) == 0
    assert len(client.get(f"{API}/cart").json()["lines"]) == 1  # the cart is kept


def test_incomplete_details_refuse_the_payments_api_too(admin_client, db_session, solares):
    _terminal(db_session, "bank_transfer").config = None
    db_session.commit()
    refused = admin_client.post(f"{API}/payments", json={"terminal": "bank_transfer", "amount_cents": 1999})
    assert refused.status_code == 422 and refused.json()["detail"][0]["type"] == "terminal_not_configured"


def test_the_payments_api_shows_the_reference(admin_client, solares):
    created = admin_client.post(f"{API}/payments", json={"terminal": "bank_transfer", "amount_cents": 1999}).json()
    assert re.fullmatch(r"[1-9][0-9]{6}", created["transfer_reference"]) and created["status"] == "pending"


# ============================================================ only an admin decides


def test_the_signed_callback_is_refused(client, db_session, solares, terminal_secret):
    payment_id = _placed(client)["payment"]["id"]
    for status in ("succeeded", "failed"):
        refused = _signed_callback(client, payment_id, status)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"][0]["type"] == "operator_confirmation_required"
    assert _payment(db_session, payment_id).status == PaymentStatus.pending
    assert _stock(db_session, "SOL-8T-ALTA") == 1


def test_mark_payment_needs_an_operator(db_session, solares):
    payment = payments.create_payment(db_session, solares, "bank_transfer", 1999)
    with pytest.raises(payments.PaymentError) as refused:
        payments.mark_payment(db_session, payment, PaymentStatus.succeeded)
    assert refused.value.kind == "operator_confirmation_required"
    admin = create_user(db_session, "operator@example.com", "operator-pass", role=UserRole.admin)
    done = payments.mark_payment(db_session, payment, PaymentStatus.succeeded, operator=admin, note="SPEI 1")
    assert (done.status, done.decided_by_id, done.decision_note) == (PaymentStatus.succeeded, admin.id, "SPEI 1")


def test_try_payment_never_answers_it(admin_client, db_session, solares):
    payment = payments.create_payment(db_session, solares, "bank_transfer", 1999)
    page = admin_client.post(f"/admin/stores/solares/try-payment/{payment.public_id}/succeed")
    assert page.status_code == 409
    assert _payment(db_session, payment.public_id).status == PaymentStatus.pending


def test_try_payment_on_the_shipped_solares_says_why(admin_client, db_session, solares):
    """Mocks disabled (as shipped): the demo offers nothing, and a bank_transfer sent anyway is refused with
    its reason next to the "no mock terminal" message."""
    refused = admin_client.post("/admin/stores/solares/try-payment/pay",
                                data={"subtotal": "5199", "method": "standard", "country": "MX", "terminal": "bank_transfer"})
    assert refused.status_code == 400
    page = page_text(refused)
    assert en("admin.try_payment.error.not_mock_pay", terminal="Transferencia bancaria") in page
    assert en("admin.try_payment.no_terminals") in page
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(Payment)) == 0


def test_admin_confirms_the_deposit(client, db_session, solares):
    body = _placed(client)
    payment_id, order_id = body["payment"]["id"], body["order"]["id"]
    _login(client)

    page = page_text(client.get(f"{PAYMENTS_ADMIN}/{payment_id}"))
    reference = body["payment"]["transfer_reference"]
    for expected in (reference, PACK_BANK["config"]["clabe"], "MXN 6,949.00", en("admin.payment.confirm"),
                     en("admin.payment.reject"), "SOL-8T-ALTA", order_id,
                     en("admin.payment.decide_intro", amount="MXN 6,949.00", reference=reference)):
        assert expected in page, expected

    confirmed = client.post(f"{PAYMENTS_ADMIN}/{payment_id}/confirm", data={"note": "  SPEI   clave 0123 "},
                            follow_redirects=False)
    assert confirmed.status_code == 303 and confirmed.headers["location"] == f"{PAYMENTS_ADMIN}/{payment_id}"

    stored = _payment(db_session, payment_id)
    assert stored.status == PaymentStatus.succeeded
    assert (stored.decided_by.email, stored.decision_note) == ("admin@example.com", "SPEI clave 0123")
    assert _stock(db_session, "SOL-8T-ALTA") == 0 and _order_stock_movements(db_session) == [("SOL-8T-ALTA", -1)]
    order = client.get(f"{API}/orders/{order_id}").json()
    assert order["order"]["status"] == "paid" and order["order"]["stock"]["taken"] is True

    after = page_text(client.get(f"{PAYMENTS_ADMIN}/{payment_id}"))
    assert en("flash.payment_confirmed", id=payment_id) in after
    assert "admin@example.com" in after and "SPEI clave 0123" in after
    assert en("admin.payment.confirm") not in after  # decided: no form anymore


def test_confirming_twice_is_harmless_and_the_other_answer_is_refused(admin_client, db_session, solares):
    payment = payments.create_payment(db_session, solares, "bank_transfer", 1999)
    url = f"{PAYMENTS_ADMIN}/{payment.public_id}"
    assert admin_client.post(f"{url}/confirm", data={"note": "first"}, follow_redirects=False).status_code == 303
    again = admin_client.post(f"{url}/confirm", data={"note": "second"}, follow_redirects=False)
    assert again.status_code == 303
    assert _payment(db_session, payment.public_id).decision_note == "first"  # unchanged

    refused = admin_client.post(f"{url}/reject", data={"note": "too late"})
    assert refused.status_code == 409
    assert en("admin.payment.error.already", status=en("admin.try_payment.status.succeeded")) in page_text(refused)
    assert _payment(db_session, payment.public_id).status == PaymentStatus.succeeded


def test_admin_rejects_the_payment(client, db_session, solares):
    body = _placed(client)
    payment_id, order_id = body["payment"]["id"], body["order"]["id"]
    _login(client)
    rejected = client.post(f"{PAYMENTS_ADMIN}/{payment_id}/reject", data={"note": "No llegó"}, follow_redirects=False)
    assert rejected.status_code == 303
    stored = _payment(db_session, payment_id)
    assert (stored.status, stored.decision_note) == (PaymentStatus.failed, "No llegó")
    assert _stock(db_session, "SOL-8T-ALTA") == 1 and _order_stock_movements(db_session) == []  # nothing taken
    assert client.get(f"{API}/orders/{order_id}").json()["order"]["status"] == "payment_failed"
    assert en("flash.payment_rejected", id=payment_id) in page_text(client.get(f"{PAYMENTS_ADMIN}/{payment_id}"))


def test_an_empty_note_is_fine_and_a_long_one_is_refused(admin_client, db_session, solares):
    payment = payments.create_payment(db_session, solares, "bank_transfer", 1999)
    url = f"{PAYMENTS_ADMIN}/{payment.public_id}"
    too_long = admin_client.post(f"{url}/confirm", data={"note": "n" * (NOTE_MAX + 1)})
    assert too_long.status_code == 400
    assert en("admin.payment.error.note_too_long", max_len=NOTE_MAX) in page_text(too_long)
    assert _payment(db_session, payment.public_id).status == PaymentStatus.pending
    assert admin_client.post(f"{url}/confirm", follow_redirects=False).status_code == 303
    stored = _payment(db_session, payment.public_id)
    assert (stored.status, stored.decision_note) == (PaymentStatus.succeeded, None)


def test_a_mock_payment_is_not_decided_here(admin_client, db_session, solares):
    _terminal(db_session, "mock_card").is_enabled = True
    db_session.commit()
    payment = payments.create_payment(db_session, solares, "mock_card", 1999)
    url = f"{PAYMENTS_ADMIN}/{payment.public_id}"
    page = page_text(admin_client.get(url))
    assert en("admin.payment.answered_by_terminal") in page and en("admin.payment.confirm") not in page
    assert f"/admin/stores/solares/try-payment/{payment.public_id}" in page
    refused = admin_client.post(f"{url}/confirm")
    assert refused.status_code == 409
    assert en("admin.payment.error.not_offline", terminal="Mock card") in page_text(refused)
    assert _payment(db_session, payment.public_id).status == PaymentStatus.pending


def test_unknown_or_foreign_payments_are_404(admin_client, db_session, solares):
    other = Store(slug="other-shop", name="Other", currency="MXN")
    db_session.add(other)
    db_session.commit()
    theirs = payments.create_payment(db_session, other, "mock_card", 1999)
    for path in (f"{PAYMENTS_ADMIN}/{theirs.public_id}", f"{PAYMENTS_ADMIN}/pay_nope/confirm", f"{PAYMENTS_ADMIN}/1"):
        method = "POST" if path.endswith("confirm") else "GET"
        assert admin_client.request(method, path).status_code == 404, path
    assert admin_client.post(f"{PAYMENTS_ADMIN}/{theirs.public_id}/confirm").status_code == 404


def test_only_admins_decide(client, db_session, solares):
    payment = payments.create_payment(db_session, solares, "bank_transfer", 1999)
    routes = [("GET", f"{PAYMENTS_ADMIN}/{payment.public_id}"), ("POST", f"{PAYMENTS_ADMIN}/{payment.public_id}/confirm"),
              ("POST", f"{PAYMENTS_ADMIN}/{payment.public_id}/reject")]
    for method, path in routes:  # anonymous: to the login page
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == f"/login?next={quote(path, safe='')}"
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    for method, path in routes:
        assert client.request(method, path, follow_redirects=False).status_code == 403, path
    assert _payment(db_session, payment.public_id).status == PaymentStatus.pending


def test_the_payments_list_shows_the_reference(admin_client, db_session, solares):
    payment = payments.create_payment(db_session, solares, "bank_transfer", 1999)
    page = page_text(admin_client.get(PAYMENTS_ADMIN))
    assert f'<a href="{PAYMENTS_ADMIN}/{payment.public_id}"><code>{payment.public_id}</code></a>' in page
    assert f"{en('admin.payment.reference')} <code>{payment.transfer_reference}</code>" in page
    assert en("admin.payment.to_confirm") in page


def test_admin_payment_page_is_spanish_by_default(default_lang_client, db_session, solares):
    _login(default_lang_client)
    payment = payments.create_payment(db_session, solares, "bank_transfer", 1999)
    page = page_text(default_lang_client.get(f"{PAYMENTS_ADMIN}/{payment.public_id}"))
    for key in ("admin.payment.confirm", "admin.payment.reject", "admin.payment.instructions_heading",
                "admin.payment.note_label"):
        assert es(key) in page, key


# ============================================================ what the shopper sees


def test_order_page_shows_the_instructions_in_spanish(default_lang_client, solares):
    body = _placed(default_lang_client)
    page = default_lang_client.get(f"/stores/solares/orders/{body['order']['id']}")
    assert page.status_code == 200
    html = page_text(page)
    instructions = body["payment"]["instructions"]
    assert '<meta name="robots" content="noindex">' in html
    assert es("order.transfer_heading") in html
    assert es("order.transfer_intro", amount=instructions["amount_display"]) in html
    assert es("order.status.awaiting_payment") in html
    for value in (instructions["bank"], instructions["beneficiary"], instructions["clabe"], instructions["account"],
                  instructions["reference"], instructions["concept"], instructions["note"], "SOL-8T-ALTA"):
        assert value in html, value


def test_order_page_after_the_decision(client, db_session, solares):
    body = _placed(client)
    admin = db_session.scalar(select(User).where(User.email == "admin@example.com"))
    payments.mark_payment(db_session, _payment(db_session, body["payment"]["id"]), PaymentStatus.succeeded,
                          operator=admin)
    html = page_text(client.get(f"/stores/solares/orders/{body['order']['id']}"))
    assert en("order.status.paid") in html
    assert 'class="order-transfer"' not in html and body["payment"]["instructions"]["clabe"] not in html  # nothing to pay


def test_unknown_or_foreign_orders_have_no_page(client, db_session, solares):
    body = _placed(client)
    assert client.get("/stores/solares/orders/ord_nope").status_code == 404
    db_session.add(Store(slug="other-shop", name="Other", currency="MXN"))
    db_session.commit()
    assert client.get(f"/stores/other-shop/orders/{body['order']['id']}").status_code == 404
    assert client.get(f"/stores/nope/orders/{body['order']['id']}").status_code == 404


def test_cart_page_has_the_transfer_texts(default_lang_client, solares):
    html = default_lang_client.get("/stores/solares/cart").text
    texts = json.loads(re.search(r'id="cart-config">(.*?)</script>', html, re.S).group(1))["texts"]
    for name, key in (("checkout.transfer_hint", "cart.checkout.transfer_hint"),
                      ("checkout.pay_transfer", "cart.checkout.pay_transfer"),
                      ("checkout.order_link", "cart.checkout.order_link"), ("order.transfer_heading", "order.transfer_heading"),
                      ("order.transfer_intro", "order.transfer_intro"), ("bank.clabe", "bank.clabe"),
                      ("bank.reference", "bank.reference"),
                      ("error.terminal_not_configured", "cart.error.terminal_not_configured")):
        assert texts[name] == es(key), name


# ============================================================ admin: the terminal's details


def test_the_form_shows_and_saves_the_details(admin_client, db_session, solares):
    terminal = _terminal(db_session, "bank_transfer")
    form = page_text(admin_client.get(f"{TERMINALS_ADMIN}/{terminal.id}"))
    assert en("admin.terminals.bank_heading") in form and en("admin.terminals.bank_transfer") in form
    assert f'name="clabe" value="{PACK_BANK["config"]["clabe"]}"' in form

    saved = admin_client.post(f"{TERMINALS_ADMIN}/{terminal.id}", data={
        "name": "Transferencia SPEI", "is_enabled": "1", "bank": "BBVA México", "beneficiary": "Solares SA de CV",
        "clabe": "0121 8001 5031 2345 60", "account": "", "note": "Envía tu comprobante."}, follow_redirects=False)

    assert saved.status_code == 303
    terminal = _terminal(db_session, "bank_transfer")
    assert terminal.name == "Transferencia SPEI" and terminal.config == {
        "bank": "BBVA México", "beneficiary": "Solares SA de CV", "clabe": OTHER_CLABE, "account": "",
        "note": "Envía tu comprobante."}


@pytest.mark.parametrize("field, value, key, params", [
    ("bank", "", "required", {}),
    ("beneficiary", "x" * 201, "too_long", {"max_len": 200}),
    ("clabe", "1234", "clabe_format", {}),
    ("clabe", VALID_CLABE[:17] + "0", "clabe_check", {}),
    ("account", "12", "account_format", {}),
    ("note", "x" * 501, "too_long", {"max_len": 500}),
])
def test_bad_details_are_refused_with_nothing_saved(admin_client, db_session, solares, field, value, key, params):
    terminal = _terminal(db_session, "bank_transfer")
    data = {"name": "Renamed", "is_enabled": "", **PACK_BANK["config"], field: value}
    refused = admin_client.post(f"{TERMINALS_ADMIN}/{terminal.id}", data=data)
    assert refused.status_code == 400
    assert f'id="{field}-error">{en(f"admin.terminals.error.{key}", **params)}</p>' in page_text(refused)
    terminal = _terminal(db_session, "bank_transfer")
    assert (terminal.name, terminal.is_enabled, terminal.config) == ("Transferencia bancaria", True, PACK_BANK["config"])


def test_a_mock_terminal_has_no_details(admin_client, db_session, solares):
    card = _terminal(db_session, "mock_card")
    assert en("admin.terminals.bank_heading") not in page_text(admin_client.get(f"{TERMINALS_ADMIN}/{card.id}"))
    admin_client.post(f"{TERMINALS_ADMIN}/{card.id}", data={"name": "Mock card", "clabe": VALID_CLABE, "bank": "X"})
    assert _terminal(db_session, "mock_card").config is None  # ignored


def test_terminal_form_is_spanish_by_default(default_lang_client, db_session, solares):
    _login(default_lang_client)
    terminal = _terminal(db_session, "bank_transfer")
    form = page_text(default_lang_client.get(f"{TERMINALS_ADMIN}/{terminal.id}"))
    for key in ("admin.terminals.bank_heading", "admin.terminals.bank.clabe", "admin.terminals.bank.beneficiary"):
        assert es(key) in form, key
    refused = default_lang_client.post(f"{TERMINALS_ADMIN}/{terminal.id}",
                                       data={"name": "X", **PACK_BANK["config"], "clabe": VALID_CLABE[:17] + "0"})
    assert es("admin.terminals.error.clabe_check") in page_text(refused)


# ============================================================ the pack section


def _terminal_issues(terminals) -> list[tuple[str, str]]:
    pack = copy.deepcopy(SOLARES)
    pack["payment_terminals"] = terminals
    return [(i.path, i.message) for i in validate_store_pack(pack)]


BANK = {"code": "bank_transfer", "name": "Transferencia", "kind": "bank_transfer", "config": dict(GOOD)}


@pytest.mark.parametrize("terminals, issue", [
    ("x", ("payment_terminals", "must be an array, got string")),
    (["x"], ("payment_terminals[0]", "must be an object, got string")),
    ([{"code": "bank_transfer", "name": "T", "kind": "bank_transfer"}],
     ("payment_terminals[0].config", "required for a bank_transfer terminal: its deposit instructions")),
    ([{"code": "mock_card", "name": "T", "kind": "mock", "config": GOOD}],
     ("payment_terminals[0].config", "a mock terminal has no config")),
    ([{"code": "card", "name": "T", "kind": "mock"}],
     ("payment_terminals[0].code", 'a "mock" terminal\'s code must start with "mock_"')),
    ([{**BANK, "code": "mock_bank"}],
     ("payment_terminals[0].code", 'a "bank_transfer" terminal\'s code must not start with "mock_"')),
    ([{**BANK, "code": "Bank-Transfer"}],
     ("payment_terminals[0].code", 'must be lower-case letters, digits and underscores, starting with a letter '
                                   '(max 64), got "Bank-Transfer"')),
    ([BANK, BANK], ("payment_terminals[1].code", 'duplicate code "bank_transfer" (also payment_terminals[0])')),
    ([{**BANK, "kind": "card"}], ("payment_terminals[0].kind", 'must be one of mock, bank_transfer, got "card"')),
    ([{"name": "T", "kind": "mock"}], ("payment_terminals[0].code", "required")),
    ([{"code": "mock_x", "name": "T"}], ("payment_terminals[0].kind", "required")),
    ([{**BANK, "colour": "red"}],
     ("payment_terminals[0].colour", "unknown field (allowed: code, config, enabled, i18n, kind, name, sort_order)")),
    ([{**BANK, "config": {**GOOD, "swift": "X"}}], ("payment_terminals[0].config.swift", "unknown field")),
    ([{**BANK, "config": {**GOOD, "clabe": VALID_CLABE[:17] + "0"}}],
     ("payment_terminals[0].config.clabe", "is not a valid CLABE (its control digit doesn't match)")),
    ([{**BANK, "config": {**GOOD, "clabe": "12"}}], ("payment_terminals[0].config.clabe", "must be 18 digits")),
    ([{**BANK, "config": {**GOOD, "bank": ""}}], ("payment_terminals[0].config.bank", "required")),
    ([{**BANK, "config": {**GOOD, "account": "1"}}], ("payment_terminals[0].config.account", "must be 6 to 20 digits")),
    ([{**BANK, "config": {**GOOD, "note": "n" * 501}}],
     ("payment_terminals[0].config.note", "must be at most 500 characters")),
    ([{**BANK, "enabled": "yes"}], ("payment_terminals[0].enabled", "must be true or false, got string")),
])
def test_pack_terminal_errors(terminals, issue):
    assert issue in _terminal_issues(terminals)


def test_the_shipped_pack_section_is_valid():
    assert _terminal_issues(SOLARES["payment_terminals"]) == []


def test_import_reports_the_terminals(db_session):
    report = import_store_pack(db_session, SOLARES)
    assert (report.payment_terminals.created, report.payment_terminals.updated) == (1, 2)  # mocks came with the store
    assert "payment terminals: 1 created, 2 updated" in report.summary()
    again = import_store_pack(db_session, SOLARES)
    assert (again.payment_terminals.created, again.payment_terminals.updated) == (0, 3)


def test_packs_without_the_section_leave_terminals_alone(db_session):
    pack = copy.deepcopy(SOLARES)
    del pack["payment_terminals"]
    report = import_store_pack(db_session, pack)
    assert "payment terminals" not in report.summary()
    codes = set(db_session.scalars(select(PaymentTerminal.code)))
    assert codes == {"mock_card", "mock_transfer"}


def test_reimport_never_overwrites_the_bank_details(admin_client, db_session, solares):
    terminal = _terminal(db_session, "bank_transfer")
    admin_client.post(f"{TERMINALS_ADMIN}/{terminal.id}", data={
        "name": "Transferencia", "is_enabled": "1", "bank": "BBVA", "beneficiary": "Solares SA", "clabe": OTHER_CLABE})
    admin_client.post(f"{TERMINALS_ADMIN}/{_terminal(db_session, 'mock_card').id}/enable")

    import_store_pack(db_session, SOLARES)

    terminal = _terminal(db_session, "bank_transfer")
    assert (terminal.config["bank"], terminal.config["clabe"]) == ("BBVA", OTHER_CLABE)  # the real details stay
    assert terminal.name == "Transferencia bancaria"  # the name follows the pack, like other named rows
    assert _terminal(db_session, "mock_card").is_enabled is False  # and so does the enabled flag


# ============================================================ older databases


def test_startup_gives_an_older_solares_its_bank_transfer_once(engine, db_session, monkeypatch, caplog):
    # A database seeded before solares had payment_terminals: mock terminals only, both enabled.
    pack = copy.deepcopy(SOLARES)
    del pack["payment_terminals"]
    import_store_pack(db_session, pack)
    assert {t.code: t.is_enabled for t in db_session.scalars(select(PaymentTerminal))} == {
        "mock_card": True, "mock_transfer": True}
    monkeypatch.setenv("SEED_PACKS", SOLARES_SEED)
    get_settings.cache_clear()
    try:
        with caplog.at_level(logging.INFO, logger="app.services.pack_seed"), _started(engine) as started:
            terminals = {t["code"]: (t["kind"], t["enabled"]) for t in started.get(f"{API}/payment/terminals").json()}
        assert terminals == {"bank_transfer": ("bank_transfer", True), "mock_card": ("mock", False),
                             "mock_transfer": ("mock", False)}
        assert "SEED_PACKS: store solares got the payment terminals of" in caplog.text
        assert _terminal(db_session, "bank_transfer").config == PACK_BANK["config"]

        # The admin enables a mock again for a demo: later starts leave it (the catch-up ran once).
        _terminal(db_session, "mock_card").is_enabled = True
        db_session.commit()
        with _started(engine):
            pass
        assert _terminal(db_session, "mock_card").is_enabled is True
    finally:
        get_settings.cache_clear()


def _started(engine) -> TestClient:
    sessions = sessionmaker(bind=engine, autoflush=False)
    app = create_app()

    def override():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def test_catch_up_does_nothing_when_every_terminal_exists(db_session, solares):
    assert catch_up_terminals(db_session, solares, SOLARES) == []
    pack = copy.deepcopy(SOLARES)
    del pack["payment_terminals"]
    assert catch_up_terminals(db_session, solares, pack) == []


def test_older_tables_get_the_bank_transfer_columns():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE payment_terminals (id INTEGER PRIMARY KEY, store_id INTEGER, "
                                "code VARCHAR(64), name VARCHAR(200))"))
        connection.execute(text("INSERT INTO payment_terminals (id, store_id, code, name) VALUES (1, 1, 'mock_card', 'C')"))
        connection.execute(text("CREATE TABLE payments (id INTEGER PRIMARY KEY, public_id VARCHAR(40), store_id INTEGER)"))
    init_db(bind=engine)
    columns = {table: {c["name"] for c in inspect(engine).get_columns(table)} for table in ("payment_terminals", "payments")}
    assert {"kind", "config"} <= columns["payment_terminals"]
    assert {"transfer_reference", "instructions", "decided_by_id", "decision_note"} <= columns["payments"]
    with engine.connect() as connection:
        assert connection.execute(text("SELECT kind FROM payment_terminals")).scalar() == "mock"
    indexes = {i["name"]: i for i in inspect(engine).get_indexes("payments")}
    assert indexes["uq_payment_store_transfer_reference"]["unique"]
    assert add_bank_transfer_columns(engine) == 0  # once
    assert add_bank_transfer_columns(create_engine("sqlite://")) == 0  # no tables: nothing to do
