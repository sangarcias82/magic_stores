"""Admin: a store's shipping methods (list, create, edit) at /admin/stores/<slug>/shipping."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from sqlalchemy import select

from app.auth import create_user
from app.models import ShippingMethod, Store, UserRole
from app.services.category_admin import SORT_MAX, SORT_MIN
from app.services.shipping_admin import CODE_MAX, COUNTRIES_MAX_CHARS, NAME_MAX
from tests.ui_text import en, es, page_text

BASE = "/admin/stores/demo-widgets/shipping"  # demo_store: demo-widgets, currency USD
QUOTE = "/api/stores/demo-widgets/shipping/quote"
FORM = {
    "name": "Standard",
    "code": "standard",
    "rate": "4.95",
    "free_from": "50",
    "countries": "es, pt ES",
    "sort_order": "2",
    "is_active": "1",
}


def _methods(db_session) -> list[ShippingMethod]:
    db_session.expire_all()
    return list(db_session.scalars(select(ShippingMethod).order_by(ShippingMethod.id)))


def _create(client, **fields):
    return client.post(BASE, data={**FORM, **fields}, follow_redirects=False)


def _text(response) -> str:
    """The page as the admin reads it (HTML entities such as &#34; decoded)."""
    return page_text(response)


@pytest.fixture()
def default_lang_admin(default_lang_client):
    """Admin session with no language chosen: admin pages are in Spanish."""
    response = default_lang_client.post(
        "/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"}, follow_redirects=False
    )
    assert response.status_code == 303
    return default_lang_client


# --- Pages need login -------------------------------------------------------------------------


def _pages(method_id: int) -> list[tuple[str, str]]:
    return [
        ("GET", BASE),
        ("GET", f"{BASE}/new"),
        ("POST", BASE),
        ("GET", f"{BASE}/{method_id}"),
        ("POST", f"{BASE}/{method_id}"),
    ]


def test_pages_need_login(client, db_session):
    store = Store(slug="demo-widgets", name="Demo", currency="USD")
    db_session.add(store)
    db_session.flush()
    method = ShippingMethod(store_id=store.id, code="standard", name="Standard", rate_cents=495)
    db_session.add(method)
    db_session.commit()

    for http_method, path in _pages(method.id):
        response = client.request(http_method, path, data=FORM, follow_redirects=False)
        assert response.status_code == 303, (http_method, path)
        assert response.headers["location"] == f"/login?next={quote(path, safe='')}"
    assert [(m.name, m.rate_cents) for m in _methods(db_session)] == [("Standard", 495)]  # nothing changed


def test_pages_are_forbidden_for_customers(client, db_session):
    store = Store(slug="demo-widgets", name="Demo", currency="USD")
    db_session.add(store)
    db_session.flush()
    method = ShippingMethod(store_id=store.id, code="standard", name="Standard", rate_cents=495)
    db_session.add(method)
    db_session.commit()
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})

    for http_method, path in _pages(method.id):
        assert client.request(http_method, path, data=FORM, follow_redirects=False).status_code == 403
    assert [(m.name, m.rate_cents) for m in _methods(db_session)] == [("Standard", 495)]


# --- Create -----------------------------------------------------------------------------------


def test_create_method(admin_client, demo_store, db_session):
    form = admin_client.get(f"{BASE}/new")
    assert form.status_code == 200
    assert 'name="rate"' in form.text and 'name="is_active" value="1" checked' in form.text  # active by default

    response = _create(admin_client)

    assert response.status_code == 303
    assert response.headers["location"] == BASE
    [method] = _methods(db_session)
    assert (method.name, method.code, method.rate_cents, method.free_shipping_threshold_cents) == (
        "Standard", "standard", 495, 5000)
    assert (method.countries, method.sort_order, method.is_active) == (["ES", "PT"], 2, True)

    page = _text(admin_client.get(BASE))
    assert en("flash.shipping_created", name="Standard") in page  # flash
    assert f'<a href="{BASE}/{method.id}">Standard</a>' in page
    assert "USD 4.95" in page and "USD 50.00" in page and "ES, PT" in page

    # The quote API uses it at once.
    quote_ = admin_client.post(QUOTE, json={"method": "standard", "subtotal_cents": 1000, "country": "ES"})
    assert quote_.json()["amount_cents"] == 495


def test_optional_fields_can_be_empty(admin_client, demo_store, db_session):
    assert _create(admin_client, free_from="", countries="", sort_order="", is_active="").status_code == 303
    [method] = _methods(db_session)
    assert method.free_shipping_threshold_cents is None
    assert (method.countries, method.sort_order, method.is_active) == ([], 0, False)  # unticked = inactive
    row = _text(admin_client.get(BASE))
    assert en("admin.shipping.all_countries") in row and en("status.inactive") in row


# --- Edit -------------------------------------------------------------------------------------


def test_edit_method(admin_client, demo_store, db_session):
    _create(admin_client)
    [method] = _methods(db_session)
    edit_url = f"{BASE}/{method.id}"

    page = admin_client.get(edit_url)
    assert page.status_code == 200
    for field in ('name="name" value="Standard"', 'name="rate" value="4.95"', 'name="free_from" value="50.00"',
                  'name="countries" value="ES, PT"', 'name="sort_order" value="2"', 'value="1" checked'):
        assert field in page.text, field

    response = admin_client.post(edit_url, data={
        **FORM, "name": "Standard 48h", "rate": "6", "free_from": "", "countries": "", "sort_order": "-1",
        "is_active": "",  # unticked: deactivate
    }, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == BASE
    [edited] = _methods(db_session)
    assert edited.id == method.id
    assert (edited.name, edited.code, edited.rate_cents, edited.free_shipping_threshold_cents) == (
        "Standard 48h", "standard", 600, None)
    assert (edited.countries, edited.sort_order, edited.is_active) == ([], -1, False)
    assert en("flash.shipping_saved", name="Standard 48h") in _text(admin_client.get(BASE))

    # Inactive now: the quote API refuses it.
    assert admin_client.post(QUOTE, json={"method": "standard"}).json()["detail"][0]["type"] == "inactive_method"


def test_edit_can_change_the_code_but_not_to_a_taken_one(admin_client, demo_store, db_session):
    _create(admin_client)
    _create(admin_client, name="Express", code="express")
    standard, express = _methods(db_session)

    taken = admin_client.post(f"{BASE}/{standard.id}", data={**FORM, "code": "express"})
    assert taken.status_code == 400
    assert en("admin.shipping.error.code_taken", code="express") in _text(taken)
    assert f"<h1>{en('admin.shipping.heading', name='Standard')}</h1>" in _text(taken)  # stored name, not the input

    renamed = admin_client.post(f"{BASE}/{standard.id}", data={**FORM, "code": "normal"}, follow_redirects=False)
    assert renamed.status_code == 303
    assert [m.code for m in _methods(db_session)] == ["normal", "express"]


def test_another_stores_method_is_404(admin_client, demo_store, db_session):
    other = Store(slug="other-shop", name="Other", currency="EUR")
    db_session.add(other)
    db_session.flush()
    theirs = ShippingMethod(store_id=other.id, code="pickup", name="Pickup", rate_cents=0)
    db_session.add(theirs)
    db_session.commit()

    assert admin_client.get(f"{BASE}/{theirs.id}").status_code == 404
    assert admin_client.post(f"{BASE}/{theirs.id}", data=FORM).status_code == 404
    assert admin_client.get(f"{BASE}/999").status_code == 404
    assert admin_client.get("/admin/stores/nope/shipping").status_code == 404
    assert [(m.code, m.name) for m in _methods(db_session)] == [("pickup", "Pickup")]  # untouched


# --- Validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, value, key, params",
    [
        ("name", "", "required", {}),
        ("name", "x" * (NAME_MAX + 1), "too_long", {"max_len": NAME_MAX}),
        ("code", "", "required", {}),
        ("code", "Express Delivery", "code_format", {"max_len": CODE_MAX}),
        ("rate", "", "required", {}),
        ("rate", "4,95", "money_comma", {}),
        ("rate", "abc", "money_format", {}),
        ("rate", "4.955", "money_decimals", {}),
        ("rate", "-1", "money_negative", {}),
        ("rate", "99999999999", "money_too_large", {}),
        ("free_from", "1e3", "money_format", {}),
        ("countries", "ES, Spain", "country_format", {"value": "Spain"}),
        ("countries", "ES," * 400, "too_long", {"max_len": COUNTRIES_MAX_CHARS}),
        ("sort_order", "first", "sort_format", {}),
        ("sort_order", "9999999", "sort_range", {"min": f"{SORT_MIN:,}", "max": f"{SORT_MAX:,}"}),
    ],
)
def test_invalid_input_is_rejected_and_nothing_saved(admin_client, demo_store, db_session, field, value, key, params):
    response = _create(admin_client, **{field: value})

    assert response.status_code == 400
    assert en("form.fix_errors") in _text(response)
    assert f'id="{field}-error">{en(f"admin.shipping.error.{key}", **params)}</p>' in _text(response)
    assert _methods(db_session) == []


def test_duplicate_code_is_rejected(admin_client, demo_store, db_session):
    _create(admin_client)
    response = _create(admin_client, name="Another")
    assert response.status_code == 400
    assert en("admin.shipping.error.code_taken", code="standard") in _text(response)
    assert len(_methods(db_session)) == 1


# --- Spanish first ------------------------------------------------------------------------------


def test_pages_are_spanish_by_default(default_lang_admin, db_session):
    default_lang_admin.post("/admin/stores", data={"slug": "demo-widgets", "name": "Demo", "currency": "USD"})

    page = _text(default_lang_admin.get(BASE))
    assert f"<h1>{es('admin.shipping.title')}</h1>" in page
    assert es("admin.shipping.empty") in page
    assert f">{es('admin.tabs.shipping')}</a>" in page  # the store tab

    form = _text(default_lang_admin.get(f"{BASE}/new"))
    for text in (es("admin.shipping.field.rate", currency="USD"), es("admin.shipping.field.free_from", currency="USD"),
                 es("admin.shipping.create")):
        assert text in form, text

    error = default_lang_admin.post(BASE, data={**FORM, "rate": "4,95"})
    assert error.status_code == 400
    assert es("admin.shipping.error.money_comma") in _text(error)

    assert default_lang_admin.post(BASE, data=FORM, follow_redirects=False).status_code == 303
    listed = _text(default_lang_admin.get(BASE))
    assert es("flash.shipping_created", name="Standard") in listed
    assert es("status.active") in listed


def test_store_tabs_link_to_shipping_on_every_store_page(admin_client, demo_store):
    for path in ("/admin/stores/demo-widgets", "/admin/stores/demo-widgets/products",
                 "/admin/stores/demo-widgets/theme", BASE):
        assert f'<a href="{BASE}"' in admin_client.get(path).text, path
    assert f'<a href="{BASE}" aria-current="page">{en("admin.tabs.shipping")}</a>' in _text(admin_client.get(BASE))


def test_code_taken_at_the_same_moment_is_reported_not_a_crash(admin_client, demo_store, db_session, monkeypatch):
    """Two admins saving the same code at once: the second one passes the check, the database's unique
    constraint refuses it, and the form says the code is taken (nothing half-saved)."""
    from app.services import shipping_admin

    _create(admin_client)
    real_validate = shipping_admin.validate_method

    def validate_without_the_check(db, store, form, method=None):
        values, errors = real_validate(db, store, form, method)
        errors.pop("code", None)  # as if the other save happened right after this check
        return values, errors

    monkeypatch.setattr(shipping_admin, "validate_method", validate_without_the_check)
    response = _create(admin_client, name="Second")

    assert response.status_code == 400
    assert en("admin.shipping.error.code_taken", code="standard") in _text(response)
    assert [m.name for m in _methods(db_session)] == ["Standard"]


def test_countries_accept_commas_spaces_and_semicolons(admin_client, demo_store, db_session):
    assert _create(admin_client, countries=" ,es;;PT  mx, ").status_code == 303
    assert _methods(db_session)[0].countries == ["ES", "PT", "MX"]
