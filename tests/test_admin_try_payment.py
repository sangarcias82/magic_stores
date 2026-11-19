"""Operator demo "Try payment" on solares: shipping method -> quote -> mock payment -> succeed / fail.

Driven through the admin pages like an operator, and checked against the public JSON API (same services)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote as urlquote

import pytest
from sqlalchemy import func, select, update

from app.auth import create_user
from app.models import Payment, PaymentTerminal, Store, UserRole
from app.routers.admin_try_payment import REFERENCE_MAX
from app.services import payments
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from tests.ui_text import en, es, page_text

SOLARES = Path(__file__).resolve().parent.parent / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json"
DEMO = "/admin/stores/solares/try-payment"
SHIPPING = "/admin/stores/solares/shipping"
API = "/api/stores/solares"
# An operator's own test method, next to the pack's standard / express: 150 MXN, free from 5,000 MXN, Mexico only.
METHOD = {"name": "Test shipping", "code": "test-rate", "rate": "150", "free_from": "5000", "countries": "MX",
          "sort_order": "0", "is_active": "1"}


@pytest.fixture()
def solares(db_session) -> Store:
    """The solares store as shipped (pack catalog only: no files copied), currency MXN, with its mock
    terminals enabled again: solares ships with bank_transfer only, and Try payment uses mocks (the
    README's demo on solares starts by enabling them in Payment terminals)."""
    import_store_pack(db_session, read_store_pack(SOLARES))
    store = db_session.scalar(select(Store).where(Store.slug == "solares"))
    for terminal in db_session.scalars(select(PaymentTerminal).where(PaymentTerminal.store_id == store.id)):
        if terminal.is_mock:
            terminal.is_enabled = True
    db_session.commit()
    return store


@pytest.fixture()
def operator(admin_client, solares):
    """Admin session on solares with a test method created through the admin Shipping form."""
    response = admin_client.post(SHIPPING, data=METHOD, follow_redirects=False)
    assert response.status_code == 303, response.text
    return admin_client


def _text(response) -> str:
    return page_text(response)


def _cents(html: str, element_id: str) -> int:
    return int(re.search(rf'id="{element_id}" data-cents="(\d+)"', html).group(1))


def _quote(client, subtotal="1000", method="test-rate", country="mx"):
    return client.get(DEMO, params={"subtotal": subtotal, "method": method, "country": country})


def _pay(client, terminal="mock_card", subtotal="1000", method="test-rate", country="mx"):
    return client.post(f"{DEMO}/pay", data={"subtotal": subtotal, "method": method, "country": country,
                                            "terminal": terminal}, follow_redirects=False)


def _payment_id(response) -> str:
    assert response.status_code == 303, response.text
    match = re.fullmatch(rf"{DEMO}/(pay_[A-Za-z0-9_-]{{24}})", response.headers["location"])  # public id
    assert match, response.headers["location"]
    return match.group(1)


def _edit_url(client, code: str) -> str:
    """The edit page of the method with this code (found by its row, not its position in the list)."""
    page = client.get(SHIPPING).text
    return re.search(rf'<tr data-code="{code}">\s*<td><a href="({SHIPPING}/\d+)"', page).group(1)


def _payment_count(db_session) -> int:
    return db_session.scalar(select(func.count()).select_from(Payment))


# --- The integrated path ------------------------------------------------------------------------


def test_quote_pay_succeed_on_solares_in_mxn(operator):
    # 1. Quote: 1,000 MXN subtotal + 150 MXN shipping.
    page = _quote(operator)
    assert page.status_code == 200, page.text
    html = _text(page)
    assert "MXN 1,000.00" in html and "MXN 150.00" in html and "MXN 1,150.00" in html
    assert _cents(html, "total") == 115000
    assert en("admin.try_payment.pay", amount="MXN 1,150.00") in html
    # Same number as the public quote API.
    api_quote = operator.post(f"{API}/shipping/quote", json={"method": "test-rate", "subtotal_cents": 100000,
                                                              "country": "MX"}).json()
    assert (api_quote["amount_cents"], api_quote["currency"]) == (15000, "MXN")

    # 2. Pay: a pending mock payment of the total, in the store's currency.
    payment_id = _payment_id(_pay(operator))
    payment_page = _text(operator.get(f"{DEMO}/{payment_id}"))
    assert en("flash.try_payment_created", id=payment_id, amount="MXN 1,150.00") in payment_page
    assert 'data-status="pending"' in payment_page and en("admin.try_payment.succeed") in payment_page
    assert operator.get(f"{API}/payments/{payment_id}").json() | {"created_at": None, "updated_at": None} == {
        "id": payment_id, "store": "solares", "terminal": "mock_card", "amount_cents": 115000, "currency": "MXN",
        "status": "pending", "mock_reference": None, "transfer_reference": None, "created_at": None, "updated_at": None,
    }

    # 3. Succeed, with a mock reference.
    answered = operator.post(f"{DEMO}/{payment_id}/succeed", data={"mock_reference": "AUTH-123"},
                             follow_redirects=False)
    assert answered.status_code == 303 and answered.headers["location"] == f"{DEMO}/{payment_id}"
    final_page = _text(operator.get(f"{DEMO}/{payment_id}"))
    assert en("flash.try_payment_succeeded", id=payment_id) in final_page
    assert 'data-status="succeeded"' in final_page and "AUTH-123" in final_page
    assert en("admin.try_payment.succeed") not in final_page  # answered: no more buttons
    api = operator.get(f"{API}/payments/{payment_id}").json()
    assert (api["status"], api["mock_reference"], api["amount_cents"], api["currency"]) == (
        "succeeded", "AUTH-123", 115000, "MXN")


def test_quote_pay_fail_and_then_it_stays_failed(operator):
    payment_id = _payment_id(_pay(operator, terminal="mock_transfer", subtotal="250.50"))
    assert operator.get(f"{API}/payments/{payment_id}").json()["amount_cents"] == 25050 + 15000

    answered = operator.post(f"{DEMO}/{payment_id}/fail", follow_redirects=False)  # no reference
    assert answered.status_code == 303
    assert en("flash.try_payment_failed", id=payment_id) in _text(operator.get(f"{DEMO}/{payment_id}"))
    api = operator.get(f"{API}/payments/{payment_id}").json()
    assert (api["status"], api["terminal"], api["mock_reference"]) == ("failed", "mock_transfer", None)

    late = operator.post(f"{DEMO}/{payment_id}/succeed")  # e.g. a second tab
    assert late.status_code == 409
    assert en("admin.try_payment.error.already_final", status=en("admin.try_payment.status.failed")) in _text(late)
    assert operator.get(f"{API}/payments/{payment_id}").json()["status"] == "failed"


def test_free_shipping_pays_only_the_subtotal(operator):
    html = _text(_quote(operator, subtotal="5000"))
    assert en("admin.try_payment.free_shipping") in html and _cents(html, "total") == 500000
    payment_id = _payment_id(_pay(operator, subtotal="5000"))
    assert operator.get(f"{API}/payments/{payment_id}").json()["amount_cents"] == 500000


def test_payment_is_quoted_again_not_taken_from_the_page(operator, db_session):
    assert _cents(_text(_quote(operator)), "total") == 115000  # the operator saw 1,150
    assert _payment_count(db_session) == 0  # seeing a quote creates nothing
    # Meanwhile the rate changes to 200 MXN in the Shipping tab.
    edit_url = _edit_url(operator, "test-rate")
    assert operator.post(edit_url, data={**METHOD, "rate": "200"}, follow_redirects=False).status_code == 303

    payment_id = _payment_id(_pay(operator))
    assert operator.get(f"{API}/payments/{payment_id}").json()["amount_cents"] == 120000  # current rules


@pytest.mark.parametrize("slug, currency", [("solares", "MXN"), ("euro-shop", "EUR")])
def test_currency_follows_the_store(admin_client, solares, db_session, slug, currency):
    if slug != "solares":
        db_session.add(Store(slug=slug, name="Euro Shop", currency=currency))
        db_session.commit()
    admin_client.post(f"/admin/stores/{slug}/shipping", data={**METHOD, "countries": ""})
    demo = f"/admin/stores/{slug}/try-payment"

    assert f"{currency} 1,150.00" in _text(admin_client.get(demo, params={"subtotal": "1000", "method": "test-rate"}))
    response = admin_client.post(f"{demo}/pay", data={"subtotal": "1000", "method": "test-rate",
                                                      "terminal": "mock_card"}, follow_redirects=False)
    payment_id = response.headers["location"].rsplit("/", 1)[1]
    assert admin_client.get(f"/api/stores/{slug}/payments/{payment_id}").json()["currency"] == currency


# --- Refusals: shown on the page, nothing paid --------------------------------------------------------


@pytest.mark.parametrize(
    "params, field, key, key_params",
    [
        ({"subtotal": "1000", "method": "test-rate", "country": "US"}, "country",
         "admin.try_payment.error.country_not_served", {}),
        ({"subtotal": "1000", "method": "test-rate", "country": ""}, "country",
         "admin.try_payment.error.country_required", {}),
        ({"subtotal": "1000", "method": "test-rate", "country": "Mexico"}, "country",
         "admin.shipping.error.country_format", {"value": "Mexico"}),
        ({"subtotal": "1000", "method": "teleport", "country": "MX"}, "method", "admin.try_payment.error.unknown_method", {}),
        ({"subtotal": "1000", "method": "", "country": "MX"}, "method", "admin.try_payment.error.choose_method", {}),
        ({"subtotal": "1000,50", "method": "test-rate", "country": "MX"}, "subtotal", "admin.shipping.error.money_comma", {}),
        ({"subtotal": "", "method": "test-rate", "country": "MX"}, "subtotal", "admin.shipping.error.required", {}),
    ],
)
def test_bad_quote_input_is_explained(operator, db_session, params, field, key, key_params):
    page = operator.get(DEMO, params=params)
    assert page.status_code == 400
    assert f'id="{field}-error">{en(key, **key_params)}</p>' in _text(page)
    assert 'id="total"' not in page.text  # no quote, no pay button
    bad_pay = _pay(operator, **{k: v for k, v in params.items()})
    assert bad_pay.status_code == 400
    assert _payment_count(db_session) == 0


def test_nothing_to_pay(admin_client, solares, db_session):
    admin_client.post(SHIPPING, data={**METHOD, "code": "free", "rate": "0", "countries": ""})
    page = _text(admin_client.get(DEMO, params={"subtotal": "0", "method": "free"}))
    assert en("admin.try_payment.error.nothing_to_pay") in page
    assert _pay(admin_client, subtotal="0", method="free", country="").status_code == 400
    assert _payment_count(db_session) == 0


def test_total_over_the_payment_limit(admin_client, solares, db_session):
    # No free-shipping amount: the largest subtotal a payment can hold + 1.00 shipping is over the limit.
    admin_client.post(SHIPPING, data={**METHOD, "code": "no-threshold", "rate": "1", "free_from": "", "countries": ""})
    page = admin_client.get(DEMO, params={"subtotal": "21474836.47", "method": "no-threshold"})
    assert page.status_code == 400 and en("admin.try_payment.error.total_too_large") in _text(page)
    assert _pay(admin_client, subtotal="21474836.47", method="no-threshold", country="").status_code == 400
    assert _payment_count(db_session) == 0


def test_inactive_method_or_disabled_terminal_at_pay_time(operator, db_session):
    db_session.execute(update(PaymentTerminal).where(PaymentTerminal.code == "mock_transfer").values(is_enabled=False))
    db_session.commit()
    html = _quote(operator).text
    assert 'value="mock_card"' in html and 'value="mock_transfer"' not in html  # only enabled ones offered

    refused = _pay(operator, terminal="mock_transfer")  # a page opened before it was disabled
    assert refused.status_code == 400
    assert en("admin.try_payment.error.terminal_disabled") in _text(refused)
    assert _cents(refused.text, "total") == 115000  # the quote is still shown
    assert _pay(operator, terminal="").status_code == 400

    edit_url = _edit_url(operator, "test-rate")
    operator.post(edit_url, data={**METHOD, "is_active": ""})  # deactivated meanwhile
    refused = _pay(operator)
    assert refused.status_code == 400 and en("admin.try_payment.error.inactive_method") in _text(refused)
    assert _payment_count(db_session) == 0


def test_reference_too_long_and_other_stores_payments(operator, db_session):
    payment_id = _payment_id(_pay(operator))
    too_long = operator.post(f"{DEMO}/{payment_id}/succeed", data={"mock_reference": "x" * (REFERENCE_MAX + 1)})
    assert too_long.status_code == 400
    assert en("admin.try_payment.error.reference_too_long", max_len=REFERENCE_MAX) in _text(too_long)
    assert operator.get(f"{API}/payments/{payment_id}").json()["status"] == "pending"

    other = Store(slug="other-shop", name="Other Shop", currency="EUR")
    db_session.add(other)
    db_session.commit()
    for method, path in (("GET", f"/admin/stores/other-shop/try-payment/{payment_id}"),
                         ("POST", f"/admin/stores/other-shop/try-payment/{payment_id}/succeed"),
                         ("GET", f"{DEMO}/999")):
        assert operator.request(method, path).status_code == 404
    assert operator.get(f"{API}/payments/{payment_id}").json()["status"] == "pending"


def test_the_simulator_only_answers_mock_terminals(operator, db_session, solares):
    """A real terminal's payment (made elsewhere: checkout, the payments API) is never answered here."""
    db_session.add(PaymentTerminal(store_id=solares.id, code="bank_live", name="Bank (live)", sort_order=5))
    db_session.commit()
    payment_id = payments.create_payment(db_session, solares, "bank_live", 1000).public_id

    page = _text(operator.get(f"{DEMO}/{payment_id}"))
    assert en("admin.try_payment.real_terminal") in page
    assert en("admin.try_payment.succeed") not in page
    refused = operator.post(f"{DEMO}/{payment_id}/succeed")
    assert refused.status_code == 409
    assert en("admin.try_payment.error.not_mock", terminal="Bank (live)") in _text(refused)
    assert operator.get(f"{API}/payments/{payment_id}").json()["status"] == "pending"


def test_try_payment_keeps_refusing_the_bank_transfer_terminal(operator, db_session, solares):
    """solares' real terminal (bank_transfer): not offered, not payable here, not answerable here."""
    page = operator.get(f"{DEMO}?subtotal=1000&method=test-rate&country=mx").text
    assert 'value="bank_transfer"' not in page and 'value="mock_card"' in page
    created = _pay(operator, terminal="bank_transfer")
    assert created.status_code == 400
    assert en("admin.try_payment.error.not_mock_pay", terminal="Transferencia bancaria") in _text(created)
    assert db_session.scalar(select(Payment).where(Payment.terminal.has(code="bank_transfer"))) is None

    transfer = payments.create_payment(db_session, solares, "bank_transfer", 5000)  # e.g. from a checkout
    for answer in ("succeed", "fail"):
        refused = operator.post(f"{DEMO}/{transfer.public_id}/{answer}")
        assert refused.status_code == 409
        assert en("admin.try_payment.error.not_mock", terminal="Transferencia bancaria") in _text(refused)
    db_session.refresh(transfer)
    assert transfer.status.value == "pending"


def test_try_payment_without_mock_terminals_says_so(admin_client, db_session):
    """solares as shipped (mocks off): Try payment offers nothing and says how to get a mock."""
    import_store_pack(db_session, read_store_pack(SOLARES))
    html = _text(admin_client.get(f"{DEMO}?subtotal=1000&method=standard&country=mx"))
    assert en("admin.try_payment.no_terminals") in html


def test_store_without_methods_points_to_the_shipping_tab(admin_client, db_session):
    db_session.add(Store(slug="empty-shop", name="Empty Shop", currency="MXN"))  # solares ships its own methods
    db_session.commit()
    html = _text(admin_client.get("/admin/stores/empty-shop/try-payment"))
    assert en("admin.try_payment.no_methods") in html
    assert 'href="/admin/stores/empty-shop/shipping/new"' in html


# --- Admin only, Spanish first -----------------------------------------------------------------------


def _demo_routes(payment_id: int) -> list[tuple[str, str]]:
    return [("GET", DEMO), ("POST", f"{DEMO}/pay"), ("GET", f"{DEMO}/{payment_id}"),
            ("POST", f"{DEMO}/{payment_id}/succeed"), ("POST", f"{DEMO}/{payment_id}/fail")]


def test_pages_need_login_and_admin(client, db_session, solares):
    for method, path in _demo_routes(1):
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 303, (method, path)
        assert response.headers["location"] == f"/login?next={urlquote(path, safe='')}"

    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    for method, path in _demo_routes(1):
        assert client.request(method, path, follow_redirects=False).status_code == 403, (method, path)
    assert _payment_count(db_session) == 0


def test_demo_is_spanish_by_default(default_lang_client, solares):
    client = default_lang_client
    client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
    client.post(SHIPPING, data=METHOD)

    html = _text(client.get(DEMO, params={"subtotal": "1000", "method": "test-rate", "country": "MX"}))
    assert f">{es('admin.tabs.try_payment')}</a>" in html  # the store tab
    for text in (es("admin.try_payment.step_quote"), es("admin.try_payment.subtotal", currency="MXN"),
                 es("admin.try_payment.shipping_row", method="Test shipping"), es("admin.try_payment.total_row"),
                 es("admin.try_payment.pay", amount="MXN 1,150.00"), es("admin.try_payment.no_network")):
        assert text in html, text

    payment_id = _payment_id(client.post(f"{DEMO}/pay", data={"subtotal": "1000", "method": "test-rate",
                                                               "country": "MX", "terminal": "mock_card"},
                                         follow_redirects=False))
    page = _text(client.get(f"{DEMO}/{payment_id}"))
    assert es("flash.try_payment_created", id=payment_id, amount="MXN 1,150.00") in page
    for text in (es("admin.try_payment.status.pending"), es("admin.try_payment.succeed"), es("admin.try_payment.fail")):
        assert text in page, text
    client.post(f"{DEMO}/{payment_id}/succeed", follow_redirects=False)
    assert es("admin.try_payment.status.succeeded") in _text(client.get(f"{DEMO}/{payment_id}"))
