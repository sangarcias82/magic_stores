"""Payments: create (admin), outcome only through the terminal's signed callback, unguessable ids.
(app/services/payments.py, app/services/payment_callbacks.py, app/routers/payments.py)"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, select, text, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import create_user
from app.config import Settings, get_settings
from app.db import init_db
from app.models import MAX_AMOUNT_CENTS, Payment, PaymentStatus, PaymentTerminal, Store, UserRole
from app.services import payment_callbacks, payments

STORE = "/api/stores/clothing-demo"  # clothing_fixture: currency USD, terminals mock_card + mock_transfer
BASE = f"{STORE}/payments"
CALLBACK = f"{BASE}/callback"
SECRET = "9f2c0e7d4b1a6c3e8f5d2a7b0c9e4f1a3d6b8c2e5f7a0d9c4b1e6f3a8d2c7b05"  # test-only
PUBLIC_ID = re.compile(r"^pay_[A-Za-z0-9_-]{24}$")


@pytest.fixture()
def secret(monkeypatch) -> str:
    """PAYMENT_CALLBACK_SECRET set: terminal callbacks are accepted when signed with it."""
    monkeypatch.setenv("PAYMENT_CALLBACK_SECRET", SECRET)
    get_settings.cache_clear()
    yield SECRET
    get_settings.cache_clear()


def _create(client, terminal: str = "mock_card", amount_cents: int = 1999):
    return client.post(BASE, json={"terminal": terminal, "amount_cents": amount_cents})


def _created(client, **kwargs) -> dict:
    response = _create(client, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


def _signature(key: str, timestamp: str, raw: bytes) -> str:
    """Computed here from the documented formula (not with the app's own function)."""
    return "sha256=" + hmac.new(key.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest()


def _callback(client, payment_id: str, status: str, *, reference: str | None = None, key: str = SECRET,
              timestamp: str | None = None, raw: bytes | None = None, signature: str | None = None,
              url: str = CALLBACK):
    """POST a terminal callback as the terminal would: signed body + timestamp header."""
    payload = {"payment": payment_id, "status": status}
    if reference is not None:
        payload["mock_reference"] = reference
    raw = json.dumps(payload).encode() if raw is None else raw
    timestamp = str(int(time.time())) if timestamp is None else timestamp
    headers = {"Content-Type": "application/json", payment_callbacks.TIMESTAMP_HEADER: timestamp,
               payment_callbacks.SIGNATURE_HEADER: signature or _signature(key, timestamp, raw)}
    return client.post(url, content=raw, headers=headers)


def _error(response, status: int) -> dict:
    assert response.status_code == status, response.text
    [error] = response.json()["detail"]
    return error


def _status(admin_client, payment_id: str) -> str:
    return admin_client.get(f"{BASE}/{payment_id}").json()["status"]


def _payment_count(db_session) -> int:
    return db_session.scalar(select(func.count()).select_from(Payment))


def _time(value: str) -> datetime:
    assert value.endswith("Z")  # UTC
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- The four required cases (create / succeed / fail / unknown terminal) ------------------------------


def test_create_payment_is_pending(admin_client, clothing_fixture):
    payment = _created(admin_client, terminal="mock_card", amount_cents=1999)

    assert PUBLIC_ID.match(payment["id"]), payment["id"]
    assert {k: v for k, v in payment.items() if k not in ("id", "created_at", "updated_at")} == {
        "store": "clothing-demo",
        "terminal": "mock_card",
        "amount_cents": 1999,
        "currency": "USD",  # the store's
        "status": "pending",
        "mock_reference": None,
        "transfer_reference": None,  # only bank transfer payments have one
    }
    assert _time(payment["created_at"]) == _time(payment["updated_at"])
    assert admin_client.get(f"{BASE}/{payment['id']}").json() == payment


def test_succeed_payment_through_the_signed_callback(admin_client, clothing_fixture, secret):
    payment = _created(admin_client)

    response = _callback(admin_client, payment["id"], "succeeded", reference="AUTH-123")

    assert response.status_code == 200, response.text
    done = response.json()
    assert (done["id"], done["status"], done["mock_reference"]) == (payment["id"], "succeeded", "AUTH-123")
    assert _time(done["updated_at"]) >= _time(done["created_at"]) == _time(payment["created_at"])
    assert _status(admin_client, payment["id"]) == "succeeded"  # stored


def test_fail_payment_through_the_signed_callback(admin_client, clothing_fixture, secret):
    payment = _created(admin_client, terminal="mock_transfer")

    response = _callback(admin_client, payment["id"], "failed")  # the reference is optional

    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["mock_reference"]) == ("failed", None)
    assert _status(admin_client, payment["id"]) == "failed"


def test_unknown_terminal_rejected(admin_client, db_session, clothing_fixture):
    error = _error(_create(admin_client, terminal="visa_live"), 422)

    assert (error["type"], error["loc"]) == ("unknown_terminal", ["body", "terminal"])
    assert _payment_count(db_session) == 0  # nothing recorded


# --- Lockdown: who may create and read --------------------------------------------------------------------


def test_anonymous_callers_can_neither_create_nor_read(client, admin_client, db_session, clothing_fixture):
    payment = _created(admin_client)
    client.post("/logout")

    assert _create(client).status_code == 401
    assert client.get(f"{BASE}/{payment['id']}").status_code == 401  # even with the right id
    assert client.get("/api/stores/nope/payments/pay_x").status_code == 401  # checked before any lookup
    assert client.post("/api/stores/nope/payments", json={}).status_code == 401
    assert _payment_count(db_session) == 1


def test_customers_are_forbidden(client, db_session, clothing_fixture):
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert _create(client).status_code == 403
    assert client.get(f"{BASE}/pay_whatever").status_code == 403
    assert _payment_count(db_session) == 0


def test_json_routes_answer_json_not_a_login_redirect(client, clothing_fixture):
    response = client.post(BASE, json={"terminal": "mock_card", "amount_cents": 1}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")


def test_another_site_cannot_post_a_payment_through_an_admins_browser(admin_client, db_session, clothing_fixture):
    """A cross-site form can only send form or text/plain bodies (no CORS preflight): refused."""
    for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x"):
        response = admin_client.post(BASE, content=b'{"terminal": "mock_card", "amount_cents": 1}',
                                     headers={"Content-Type": content_type})
        assert response.status_code == 422, content_type
    assert _payment_count(db_session) == 0


# --- Lockdown: ids can't be guessed ----------------------------------------------------------------------------


def test_ids_are_random_and_the_database_id_is_never_shown(admin_client, db_session, clothing_fixture):
    ids = [_created(admin_client, amount_cents=100 + n)["id"] for n in range(30)]

    assert len(set(ids)) == 30 and all(PUBLIC_ID.match(i) for i in ids)
    internal = db_session.scalars(select(Payment.id)).all()
    for number in internal:  # what a caller walking through 1, 2, 3… would try
        assert admin_client.get(f"{BASE}/{number}").status_code == 404
    assert all(str(n) not in ids for n in internal)


# --- Lockdown: only the terminal sets the outcome ---------------------------------------------------------------


def test_an_admin_session_alone_cannot_mark_a_payment(admin_client, clothing_fixture, secret):
    payment = _created(admin_client)
    unsigned = admin_client.post(CALLBACK, json={"payment": payment["id"], "status": "succeeded"})
    assert unsigned.status_code == 401
    for old in ("succeed", "fail"):  # the former browser-callable routes are gone
        assert admin_client.post(f"{BASE}/{payment['id']}/{old}").status_code in (404, 405)
    assert _status(admin_client, payment["id"]) == "pending"


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"key": "another-secret-another-secret-123456"}, "invalid signature"),
        ({"signature": "sha256=" + "0" * 64}, "invalid signature"),
        ({"signature": "not-a-signature"}, "invalid signature"),
        ({"age": 400}, "seconds away from now"),  # captured, replayed later
        ({"age": -400}, "seconds away from now"),  # dated in the future
        ({"timestamp": "yesterday"}, "must be a unix time"),
        ({"timestamp": ""}, "missing"),
    ],
)
def test_unproven_callbacks_are_rejected(admin_client, clothing_fixture, secret, change, reason):
    payment = _created(admin_client)
    change = dict(change)
    if "age" in change:  # computed now, not at collection time
        change["timestamp"] = str(int(time.time()) - change.pop("age"))
    response = _callback(admin_client, payment["id"], "succeeded", **change)
    assert response.status_code == 401
    assert reason in response.json()["detail"]
    assert _status(admin_client, payment["id"]) == "pending"


def test_a_signed_callback_cannot_be_pointed_at_another_payment(admin_client, clothing_fixture, secret):
    mine, other = _created(admin_client), _created(admin_client)
    timestamp = str(int(time.time()))
    signed = json.dumps({"payment": mine["id"], "status": "succeeded"}).encode()
    tampered = signed.replace(mine["id"].encode(), other["id"].encode())  # same signature, other payment
    response = _callback(admin_client, other["id"], "succeeded", raw=tampered, timestamp=timestamp,
                         signature=_signature(SECRET, timestamp, signed))
    assert response.status_code == 401
    assert (_status(admin_client, mine["id"]), _status(admin_client, other["id"])) == ("pending", "pending")


def test_callbacks_are_refused_without_a_configured_secret(admin_client, clothing_fixture):
    payment = _created(admin_client)  # PAYMENT_CALLBACK_SECRET is empty in the test settings
    response = _callback(admin_client, payment["id"], "succeeded", key="")
    assert response.status_code == 503
    assert _status(admin_client, payment["id"]) == "pending"


def test_a_short_secret_stops_the_app_from_starting():
    with pytest.raises(ValidationError, match="at least 32 characters"):
        Settings(_env_file=None, payment_callback_secret="too-short")
    assert Settings(_env_file=None, payment_callback_secret=f"  {SECRET}  ").payment_callback_secret == SECRET


def test_the_timestamp_window_is_300_seconds_either_way():
    raw = b"{}"
    signature = _signature(SECRET, "1700000000", raw)
    for now in (1700000000 - 300, 1700000000, 1700000000 + 300):
        payment_callbacks.verify(SECRET, "1700000000", signature, raw, now=now)  # accepted
    for now in (1700000000 - 301, 1700000000 + 301):
        with pytest.raises(payment_callbacks.CallbackRejected):
            payment_callbacks.verify(SECRET, "1700000000", signature, raw, now=now)


def test_verify_needs_a_secret():
    with pytest.raises(ValueError):
        payment_callbacks.verify("", "1700000000", "sha256=x", b"{}")


def test_the_documented_signature_is_what_the_app_computes():
    raw = b'{"payment": "pay_x", "status": "failed"}'
    assert payment_callbacks.sign(SECRET, "1700000000", raw) == _signature(SECRET, "1700000000", raw)


# --- Statuses: one final answer -----------------------------------------------------------------------------------


def test_statuses_are_spelled_out():
    assert [s.value for s in PaymentStatus] == ["pending", "succeeded", "failed"]


@pytest.mark.parametrize("first, then", [("succeeded", "failed"), ("failed", "succeeded")])
def test_a_final_payment_cannot_change(admin_client, clothing_fixture, secret, first, then):
    payment = _created(admin_client)
    assert _callback(admin_client, payment["id"], first).status_code == 200

    error = _error(_callback(admin_client, payment["id"], then), 409)

    assert (error["type"], error["loc"]) == ("invalid_transition", ["body", "payment"])
    assert _status(admin_client, payment["id"]) == first


def test_a_replayed_callback_changes_nothing(admin_client, clothing_fixture, secret):
    payment = _created(admin_client)
    first = _callback(admin_client, payment["id"], "succeeded", reference="AUTH-1").json()

    again = _callback(admin_client, payment["id"], "succeeded", reference="AUTH-2")  # e.g. the terminal retries

    assert again.status_code == 200
    assert again.json() == first  # unchanged: a retried answer is safe


def test_concurrent_answers_only_one_wins(engine, clothing_fixture, monkeypatch):
    sessions = sessionmaker(bind=engine, autoflush=False)
    first, second = sessions(), sessions()
    store = second.scalar(select(Store).where(Store.slug == "clothing-demo"))
    payment = payments.create_payment(first, first.merge(store), "mock_card", 500)
    stale = second.get(Payment, payment.id)  # this request read it while still pending
    payments.mark_payment(first, payment, PaymentStatus.failed)  # the other request answers first

    # Let the stale request skip its re-read once, as if both had read the row at the same moment.
    real_refresh = second.refresh
    calls = []
    monkeypatch.setattr(second, "refresh", lambda obj: calls.append(obj) if not calls else real_refresh(obj))

    with pytest.raises(payments.PaymentError) as raised:
        payments.mark_payment(second, stale, PaymentStatus.succeeded)

    assert raised.value.kind == "invalid_transition"  # its conditional UPDATE matched no row
    assert sessions().get(Payment, payment.id).status == PaymentStatus.failed
    first.close()
    second.close()


# --- Callback input -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'{"payment": "pay_x"}',  # no status
        b'{"payment": "pay_x", "status": "pending"}',  # a terminal answers succeeded or failed
        b'{"payment": "pay_x", "status": "succeeded", "amount_cents": 1}',  # unknown field
        b'{"payment": "pay_x", "status": "succeeded", "mock_reference": "' + b"x" * 65 + b'"}',
    ],
)
def test_malformed_signed_callbacks_are_422(admin_client, clothing_fixture, secret, raw):
    assert _callback(admin_client, "pay_x", "succeeded", raw=raw).status_code == 422


def test_oversized_callbacks_are_refused_before_anything_else(admin_client, clothing_fixture, secret):
    raw = b'{"payment": "pay_x", "status": "succeeded", "mock_reference": "' + b"x" * 9000 + b'"}'
    assert _callback(admin_client, "pay_x", "succeeded", raw=raw).status_code == 413


def test_callback_for_a_payment_of_another_store_is_404(admin_client, db_session, clothing_fixture, secret):
    db_session.add(Store(slug="other-shop", name="Other", currency="EUR"))
    db_session.commit()
    theirs = admin_client.post("/api/stores/other-shop/payments", json={"terminal": "mock_card", "amount_cents": 700})
    their_id = theirs.json()["id"]

    assert _callback(admin_client, their_id, "succeeded").status_code == 404  # sent to clothing-demo
    assert admin_client.get(f"{BASE}/{their_id}").status_code == 404
    assert admin_client.get(f"/api/stores/other-shop/payments/{their_id}").json()["status"] == "pending"
    assert _callback(admin_client, "pay_unknown", "succeeded").status_code == 404
    assert _callback(admin_client, their_id, "succeeded",
                     url="/api/stores/nope/payments/callback").status_code == 404


# --- Create input -------------------------------------------------------------------------------------------------


def test_disabled_terminal_rejected(admin_client, db_session, clothing_fixture):
    db_session.execute(update(PaymentTerminal).where(PaymentTerminal.code == "mock_transfer").values(is_enabled=False))
    db_session.commit()

    assert _error(_create(admin_client, terminal="mock_transfer"), 422)["type"] == "terminal_disabled"
    assert _payment_count(db_session) == 0


@pytest.mark.parametrize(
    "body",
    [
        {"terminal": "mock_card"},  # no amount
        {"terminal": "mock_card", "amount_cents": 0},
        {"terminal": "mock_card", "amount_cents": -5},
        {"terminal": "mock_card", "amount_cents": 19.99},
        {"terminal": "mock_card", "amount_cents": MAX_AMOUNT_CENTS + 1},
        {"amount_cents": 100},  # no terminal
        {"terminal": "", "amount_cents": 100},
    ],
)
def test_malformed_create_is_422(admin_client, db_session, clothing_fixture, body):
    assert admin_client.post(BASE, json=body).status_code == 422
    assert _payment_count(db_session) == 0


def test_service_checks_the_amount_too(db_session, clothing_fixture):
    for amount in (0, -1, MAX_AMOUNT_CENTS + 1, True, 1.5):
        with pytest.raises(payments.PaymentError) as raised:
            payments.create_payment(db_session, clothing_fixture, "mock_card", amount)
        assert raised.value.kind == "invalid_amount"


def test_unknown_store_is_404_for_admins(admin_client, clothing_fixture):
    assert admin_client.post("/api/stores/nope/payments", json={"terminal": "mock_card", "amount_cents": 1}).status_code == 404
    assert admin_client.get(f"{BASE}/pay_missing").status_code == 404


def test_deleting_a_store_deletes_its_payments(admin_client, db_session, clothing_fixture):
    _created(admin_client)
    db_session.delete(db_session.scalar(select(Store).where(Store.slug == "clothing-demo")))
    db_session.commit()
    assert _payment_count(db_session) == 0


# --- Databases from before public ids ------------------------------------------------------------------------------


def test_older_databases_get_random_ids_at_startup():
    """A payments table created before ``public_id`` existed: init_db (run at every start) adds the
    column, gives each payment its own random id, and makes it unique; running it again changes nothing."""
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:  # the table as the previous version created it
        connection.execute(text(
            "CREATE TABLE payments (id INTEGER PRIMARY KEY, store_id INTEGER, terminal_id INTEGER, "
            "amount_cents INTEGER, currency VARCHAR(8), status VARCHAR(16), mock_reference VARCHAR(64), "
            "created_at DATETIME, updated_at DATETIME)"))
        connection.execute(text("INSERT INTO payments (id, amount_cents, currency, status) VALUES "
                                "(1, 100, 'USD', 'pending'), (2, 200, 'USD', 'succeeded')"))

    init_db(bind=engine)

    with engine.connect() as connection:
        ids = dict(connection.execute(text("SELECT id, public_id FROM payments")).all())
    assert set(ids) == {1, 2} and len(set(ids.values())) == 2 and all(PUBLIC_ID.match(v) for v in ids.values())
    indexes = {i["name"]: i for i in inspect(engine).get_indexes("payments")}
    assert indexes["ix_payments_public_id"]["unique"]
    init_db(bind=engine)  # already upgraded: no-op
    with engine.connect() as connection:
        assert dict(connection.execute(text("SELECT id, public_id FROM payments")).all()) == ids


def test_oversized_callbacks_without_a_declared_length_are_refused_too(admin_client, clothing_fixture, secret):
    """Sent in chunks (no Content-Length): the size is checked while reading."""
    def chunks():
        yield b'{"payment": "pay_x", "status": "succeeded", "mock_reference": "'
        for _ in range(10):
            yield b"x" * 1024
        yield b'"}'

    response = admin_client.post(CALLBACK, content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_the_upgrade_skips_databases_without_a_payments_table():
    from app.schema_upgrades import add_payment_public_ids

    assert add_payment_public_ids(create_engine("sqlite://")) == 0
