"""Admin: a store's payment terminals at /admin/stores/<slug>/terminals (list, rename, enable / disable),
and what disabling means for payments (API and the try-payment demo)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import func, select

from app.auth import create_user
from app.models import DEFAULT_PAYMENT_TERMINALS, Payment, PaymentTerminal, Store, UserRole
from app.services import payments
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from app.services.terminal_admin import NAME_MAX
from tests.ui_text import en, es, page_text

SOLARES = Path(__file__).resolve().parent.parent / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json"
TERMINALS = "/admin/stores/solares/terminals"
API_TERMINALS = "/api/stores/solares/payment/terminals"
API_PAYMENTS = "/api/stores/solares/payments"
DEMO = "/admin/stores/solares/try-payment"
# The seeded display names (every store gets these terminals), from the code that seeds them.
DEFAULT_NAMES = {code: name for code, name, _ in DEFAULT_PAYMENT_TERMINALS}
CARD, TRANSFER = DEFAULT_NAMES["mock_card"], DEFAULT_NAMES["mock_transfer"]


@pytest.fixture()
def solares(db_session) -> Store:
    """Solares as shipped (MXN, shipping methods standard / express; terminals bank_transfer enabled,
    mock_card + mock_transfer disabled), with the mocks enabled again: these tests are about toggling them."""
    import_store_pack(db_session, read_store_pack(SOLARES))
    store = db_session.scalar(select(Store).where(Store.slug == "solares"))
    for terminal in db_session.scalars(select(PaymentTerminal).where(PaymentTerminal.store_id == store.id)):
        terminal.is_enabled = True
    db_session.commit()
    return store


@pytest.fixture()
def default_lang_admin(default_lang_client):
    response = default_lang_client.post(
        "/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"}, follow_redirects=False
    )
    assert response.status_code == 303
    return default_lang_client


def _text(response) -> str:
    return page_text(response)


def _terminal(db_session, code: str) -> PaymentTerminal:
    db_session.expire_all()
    return db_session.scalar(select(PaymentTerminal).where(PaymentTerminal.code == code))


def _api_terminals(client) -> dict[str, dict]:
    return {t["code"]: t for t in client.get(API_TERMINALS).json()}


def _disable(client, db_session, code: str):
    return client.post(f"{TERMINALS}/{_terminal(db_session, code).id}/disable", follow_redirects=False)


def _payment_count(db_session) -> int:
    return db_session.scalar(select(func.count()).select_from(Payment))


# --- Login required ------------------------------------------------------------------------------------------


def _routes(terminal_id: int) -> list[tuple[str, str]]:
    return [("GET", TERMINALS), ("GET", f"{TERMINALS}/{terminal_id}"), ("POST", f"{TERMINALS}/{terminal_id}"),
            ("POST", f"{TERMINALS}/{terminal_id}/enable"), ("POST", f"{TERMINALS}/{terminal_id}/disable")]


def test_pages_need_login(client, db_session, solares):
    card = _terminal(db_session, "mock_card")
    for method, path in _routes(card.id):
        response = client.request(method, path, data={"name": "Hacked"}, follow_redirects=False)
        assert response.status_code == 303, (method, path)
        assert response.headers["location"] == f"/login?next={quote(path, safe='')}"
    card = _terminal(db_session, "mock_card")
    assert (card.name, card.is_enabled) == (CARD, True)  # nothing changed


def test_pages_are_forbidden_for_customers(client, db_session, solares):
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    card = _terminal(db_session, "mock_card")
    for method, path in _routes(card.id):
        assert client.request(method, path, data={"name": "Hacked"}, follow_redirects=False).status_code == 403
    card = _terminal(db_session, "mock_card")
    assert (card.name, card.is_enabled) == (CARD, True)


# --- List and toggle ------------------------------------------------------------------------------------------


def test_list_shows_every_terminal_with_its_state(admin_client, solares):
    page = _text(admin_client.get(TERMINALS))
    assert '<tr data-code="mock_card" data-enabled="true">' in page
    assert '<tr data-code="mock_transfer" data-enabled="true">' in page
    assert CARD in page and TRANSFER in page and en("admin.terminals.mock") in page
    assert '<tr data-code="bank_transfer" data-enabled="true">' in page
    assert "Transferencia bancaria" in page and en("admin.terminals.bank_transfer") in page
    assert f">{en('admin.terminals.disable')}</button>" in page
    assert f'<a href="{TERMINALS}" aria-current="page">{en("admin.tabs.terminals")}</a>' in page  # the store tab


def test_toggle_a_terminal(admin_client, db_session, solares):
    transfer_id = _terminal(db_session, "mock_transfer").id

    disabled = admin_client.post(f"{TERMINALS}/{transfer_id}/disable", follow_redirects=False)
    assert disabled.status_code == 303 and disabled.headers["location"] == TERMINALS
    page = _text(admin_client.get(TERMINALS))
    assert en("flash.terminal_disabled", name=TRANSFER) in page
    assert '<tr data-code="mock_transfer" data-enabled="false">' in page
    assert f">{en('admin.terminals.enable')}</button>" in page
    assert _terminal(db_session, "mock_transfer").is_enabled is False
    assert _api_terminals(admin_client)["mock_transfer"]["enabled"] is False  # the public list marks it
    assert admin_client.post(f"{TERMINALS}/{transfer_id}/disable").status_code == 200  # twice: harmless

    enabled = admin_client.post(f"{TERMINALS}/{transfer_id}/enable", follow_redirects=False)
    assert enabled.status_code == 303
    assert en("flash.terminal_enabled", name=TRANSFER) in _text(admin_client.get(TERMINALS))
    assert _terminal(db_session, "mock_transfer").is_enabled is True
    assert _terminal(db_session, "mock_card").is_enabled is True  # the other one untouched


def test_all_disabled_is_flagged(admin_client, db_session, solares):
    _disable(admin_client, db_session, "mock_card")
    _disable(admin_client, db_session, "mock_transfer")
    assert en("admin.terminals.none_enabled") not in _text(admin_client.get(TERMINALS))  # bank_transfer left
    _disable(admin_client, db_session, "bank_transfer")
    assert en("admin.terminals.none_enabled") in _text(admin_client.get(TERMINALS))


# --- Rename -------------------------------------------------------------------------------------------------


def test_rename_a_terminal(admin_client, db_session, solares):
    card = _terminal(db_session, "mock_card")
    form = _text(admin_client.get(f"{TERMINALS}/{card.id}"))
    assert f'name="name" value="{CARD}"' in form and 'name="is_enabled" value="1" checked' in form
    assert "<code>mock_card</code>" in form  # shown, not editable

    saved = admin_client.post(f"{TERMINALS}/{card.id}",
                              data={"name": "  Tarjeta   (simulada) ", "is_enabled": "1", "code": "visa_live"},
                              follow_redirects=False)

    assert saved.status_code == 303
    card = _terminal(db_session, "mock_card")
    assert (card.name, card.code, card.is_enabled) == ("Tarjeta (simulada)", "mock_card", True)  # code kept
    assert en("flash.terminal_saved", name="Tarjeta (simulada)") in _text(admin_client.get(TERMINALS))
    assert _api_terminals(admin_client)["mock_card"]["name"] == "Tarjeta (simulada)"


def test_the_form_can_disable_too(admin_client, db_session, solares):
    card = _terminal(db_session, "mock_card")
    admin_client.post(f"{TERMINALS}/{card.id}", data={"name": CARD})  # checkbox unticked
    assert _terminal(db_session, "mock_card").is_enabled is False


@pytest.mark.parametrize("name, key, params", [("", "required", {}), ("   ", "required", {}),
                                               ("x" * (NAME_MAX + 1), "too_long", {"max_len": NAME_MAX})])
def test_invalid_name_is_rejected(admin_client, db_session, solares, name, key, params):
    card = _terminal(db_session, "mock_card")
    response = admin_client.post(f"{TERMINALS}/{card.id}", data={"name": name, "is_enabled": ""})
    assert response.status_code == 400
    assert f'id="name-error">{en(f"admin.shipping.error.{key}", **params)}</p>' in _text(response)
    card = _terminal(db_session, "mock_card")
    assert (card.name, card.is_enabled) == (CARD, True)  # nothing saved, not even the checkbox


def test_another_stores_terminal_is_404(admin_client, db_session, solares):
    other = Store(slug="other-shop", name="Other", currency="EUR")
    db_session.add(other)
    db_session.commit()
    theirs = db_session.scalar(select(PaymentTerminal).where(PaymentTerminal.store_id == other.id,
                                                             PaymentTerminal.code == "mock_card"))
    for method, path in _routes(theirs.id)[1:]:
        assert admin_client.request(method, path, data={"name": "Hacked"}).status_code == 404, path
    assert admin_client.get(f"{TERMINALS}/99999").status_code == 404
    db_session.refresh(theirs)
    assert (theirs.name, theirs.is_enabled) == (CARD, True)  # untouched


# --- A disabled terminal can't take new payments ----------------------------------------------------------------


def test_pay_with_a_disabled_terminal_is_rejected_by_the_api(admin_client, db_session, solares):
    assert _disable(admin_client, db_session, "mock_transfer").status_code == 303

    refused = admin_client.post(API_PAYMENTS, json={"terminal": "mock_transfer", "amount_cents": 1999})

    assert refused.status_code == 422
    assert refused.json()["detail"][0]["type"] == "terminal_disabled"
    assert _payment_count(db_session) == 0
    assert admin_client.post(API_PAYMENTS, json={"terminal": "mock_card", "amount_cents": 1999}).status_code == 201


def test_pay_with_a_disabled_terminal_is_rejected_by_the_demo(admin_client, db_session, solares):
    _disable(admin_client, db_session, "mock_transfer")

    quote_page = admin_client.get(DEMO, params={"subtotal": "5199", "method": "standard", "country": "MX"}).text
    assert 'value="mock_card"' in quote_page and 'value="mock_transfer"' not in quote_page  # not offered

    refused = admin_client.post(f"{DEMO}/pay", data={"subtotal": "5199", "method": "standard", "country": "MX",
                                                     "terminal": "mock_transfer"})  # a page opened before
    assert refused.status_code == 400
    assert en("admin.try_payment.error.terminal_disabled") in _text(refused)
    assert _payment_count(db_session) == 0


def test_with_every_terminal_disabled_the_demo_says_so(admin_client, db_session, solares):
    _disable(admin_client, db_session, "mock_card")
    _disable(admin_client, db_session, "mock_transfer")
    page = _text(admin_client.get(DEMO, params={"subtotal": "5199", "method": "standard", "country": "MX"}))
    assert en("admin.try_payment.no_terminals") in page
    assert 'name="terminal"' not in page


def test_reenabled_terminal_takes_payments_again(admin_client, db_session, solares):
    _disable(admin_client, db_session, "mock_transfer")
    admin_client.post(f"{TERMINALS}/{_terminal(db_session, 'mock_transfer').id}/enable")
    created = admin_client.post(API_PAYMENTS, json={"terminal": "mock_transfer", "amount_cents": 1999})
    assert created.status_code == 201


def test_payments_pending_on_a_disabled_terminal_can_still_be_answered(admin_client, db_session, solares):
    """Disabling stops *new* payments; one already started gets its answer (here the admin simulator)."""
    payment = payments.create_payment(db_session, solares, "mock_transfer", 1999)
    _disable(admin_client, db_session, "mock_transfer")

    answered = admin_client.post(f"{DEMO}/{payment.public_id}/succeed", follow_redirects=False)

    assert answered.status_code == 303
    assert admin_client.get(f"{API_PAYMENTS}/{payment.public_id}").json()["status"] == "succeeded"


# --- Spanish first ----------------------------------------------------------------------------------------------


def test_pages_are_spanish_by_default(default_lang_admin, db_session, solares):
    page = _text(default_lang_admin.get(TERMINALS))
    assert f"<h1>{es('admin.tabs.terminals')}</h1>" in page
    assert f">{es('admin.tabs.terminals')}</a>" in page  # the store tab
    for text in (es("admin.terminals.enabled"), f">{es('admin.terminals.disable')}</button>", es("admin.terminals.mock")):
        assert text in page, text

    transfer_id = _terminal(db_session, "mock_transfer").id
    assert default_lang_admin.post(f"{TERMINALS}/{transfer_id}/disable", follow_redirects=False).status_code == 303
    listed = _text(default_lang_admin.get(TERMINALS))
    assert es("flash.terminal_disabled", name=TRANSFER) in listed
    assert es("admin.terminals.disabled") in listed and f">{es('admin.terminals.enable')}</button>" in listed

    form = _text(default_lang_admin.get(f"{TERMINALS}/{transfer_id}"))
    for text in (es("admin.terminals.name"), es("admin.terminals.save"), es("admin.terminals.code_hint")):
        assert text in form, text
    error = default_lang_admin.post(f"{TERMINALS}/{transfer_id}", data={"name": ""})
    assert error.status_code == 400 and es("admin.shipping.error.required") in _text(error)
