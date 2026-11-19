"""Admin: a store's recent payments at /admin/stores/<slug>/payments (read-only list)."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.auth import create_user
from app.models import PaymentTerminal, Payment, PaymentStatus, Store, UserRole
from app.routers.admin import MAX_PAGE
from app.routers.admin_payments import PAGE_SIZE
from app.services import payments
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from tests.ui_text import en, es, page_text

SOLARES = Path(__file__).resolve().parent.parent / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json"
LIST = "/admin/stores/solares/payments"
API = "/api/stores/solares/payments"


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


def _rows(html: str) -> list[str]:
    """Payment ids in the order the table lists them."""
    return re.findall(r'<tr data-payment="(pay_[\w-]+)"', html)


def _row(html: str, payment_id: str) -> str:
    return re.search(rf'<tr data-payment="{payment_id}".*?</tr>', html, re.S).group(0)


def _stamp(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


# --- Requested: shows a payment you created -----------------------------------------------------------------


def test_shows_a_payment_you_created(admin_client, db_session, solares):
    created = admin_client.post(API, json={"terminal": "mock_card", "amount_cents": 564900})
    assert created.status_code == 201, created.text
    payment_id = created.json()["id"]

    page = admin_client.get(LIST)

    assert page.status_code == 200
    html = page_text(page)
    assert _rows(html) == [payment_id]
    row = _row(html, payment_id)
    stored = db_session.scalar(select(Payment).where(Payment.public_id == payment_id))
    assert f"<code>{payment_id}</code>" in row  # id (the public one)
    assert '<td class="number">5,649.00</td>' in row  # amount
    assert f"<td>{solares.currency}</td>" in row  # currency: the store's
    assert "Mock card <code>mock_card</code>" in row  # terminal
    assert f"<td>{en('admin.try_payment.status.pending')}</td>" in row  # status
    assert f'<time datetime="{stored.created_at.isoformat()}Z">{_stamp(stored.created_at)}</time>' in row  # created
    assert f'<time datetime="{stored.updated_at.isoformat()}Z">{_stamp(stored.updated_at)}</time>' in row  # updated
    assert f'href="/admin/stores/solares/try-payment/{payment_id}"' in row  # its detail page


def test_status_and_updated_time_follow_the_answer(admin_client, db_session, solares):
    approved = payments.create_payment(db_session, solares, "mock_card", 1000)
    declined = payments.create_payment(db_session, solares, "mock_transfer", 2000)
    payments.mark_payment(db_session, approved, PaymentStatus.succeeded, "AUTH-1")
    payments.mark_payment(db_session, declined, PaymentStatus.failed)

    html = page_text(admin_client.get(LIST))

    assert '<tr data-payment="%s" data-status="succeeded">' % approved.public_id in html
    assert en("admin.try_payment.status.succeeded") in _row(html, approved.public_id)
    assert en("admin.try_payment.status.failed") in _row(html, declined.public_id)
    db_session.refresh(approved)
    assert _stamp(approved.updated_at) in _row(html, approved.public_id)


# --- Recent first, pages, scoping -----------------------------------------------------------------------------


def test_most_recent_first_and_paged(admin_client, db_session, solares):
    made = [payments.create_payment(db_session, solares, "mock_card", 100 + n).public_id for n in range(PAGE_SIZE + 1)]

    first = page_text(admin_client.get(LIST))
    assert _rows(first) == list(reversed(made))[:PAGE_SIZE]  # newest first, one page
    assert f'href="{LIST}?page=2">{en("admin.imports.older")}</a>' in first
    assert en("admin.imports.newer") not in first

    second = page_text(admin_client.get(f"{LIST}?page=2"))
    assert _rows(second) == [made[0]]  # the oldest one
    assert f'href="{LIST}?page=1">{en("admin.imports.newer")}</a>' in second
    assert en("admin.imports.older") not in second

    past_the_end = page_text(admin_client.get(f"{LIST}?page=3"))
    assert _rows(past_the_end) == [] and en("admin.payments.no_more") in past_the_end


@pytest.mark.parametrize("page, rows", [
    ("abc", 1), ("0", 1), ("-1", 1), ("1.5", 1), ("", 1),  # not a page number: page 1
    (str(MAX_PAGE + 1), 0), ("99999999999999999999999", 0),  # huge: the last allowed page (empty), never a 500
])
def test_odd_page_numbers_never_fail(admin_client, db_session, solares, page, rows):
    payments.create_payment(db_session, solares, "mock_card", 100)
    response = admin_client.get(f"{LIST}?page={quote(page)}")
    assert response.status_code == 200
    assert len(_rows(page_text(response))) == rows


def test_only_this_stores_payments(admin_client, db_session, solares):
    other = Store(slug="other-shop", name="Other", currency="EUR")
    db_session.add(other)
    db_session.commit()
    theirs = payments.create_payment(db_session, other, "mock_card", 700)
    mine = payments.create_payment(db_session, solares, "mock_card", 500)

    assert _rows(page_text(admin_client.get(LIST))) == [mine.public_id]
    assert _rows(page_text(admin_client.get("/admin/stores/other-shop/payments"))) == [theirs.public_id]
    assert admin_client.get("/admin/stores/nope/payments").status_code == 404


def test_empty_list_points_to_a_test_payment(admin_client, solares):
    html = page_text(admin_client.get(LIST))
    assert en("admin.payments.empty") in html
    assert f'<a href="/admin/stores/solares/try-payment">{en("admin.payments.try_link")}</a>' in html


def test_it_is_read_only(admin_client, solares):
    operations = admin_client.app.openapi()["paths"]["/admin/stores/{store_slug}/payments"]
    assert set(operations) == {"get"}
    assert admin_client.post(LIST).status_code == 405
    assert "<form" not in admin_client.get(LIST).text.split("<main", 1)[1].split("</main>", 1)[0]


# --- Login required, Spanish first ------------------------------------------------------------------------------


def test_needs_login(client, db_session, solares):
    payments.create_payment(db_session, solares, "mock_card", 100)
    response = client.get(LIST, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next={quote(LIST, safe='')}"

    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert client.get(LIST, follow_redirects=False).status_code == 403


def test_spanish_by_default(default_lang_client, db_session, solares):
    default_lang_client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
    payment = payments.create_payment(db_session, solares, "mock_card", 100)

    html = page_text(default_lang_client.get(LIST))

    assert f"<h1>{es('admin.tabs.payments')}</h1>" in html
    assert f'<a href="{LIST}" aria-current="page">{es("admin.tabs.payments")}</a>' in html  # the store tab
    for key in ("admin.payments.col.id", "admin.payments.col.amount", "admin.payments.col.terminal",
                "admin.payments.col.created", "admin.payments.col.updated"):
        assert f"<th>{es(key)}</th>" in html, key
    assert es("admin.try_payment.status.pending") in _row(html, payment.public_id)
