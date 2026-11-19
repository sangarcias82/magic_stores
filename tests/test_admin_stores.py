"""Admin shell: list and create stores under /admin (admin session required)."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

import app.routers.admin as admin_router
from app.auth import create_user
from app.models import Store, UserRole
from app.services.storefront import DEFAULT_THEME
from tests.ui_text import en, en_html, page_text, pattern

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "ChangeMeAdmin123!"

DEMO = {"slug": "demo-widgets", "name": "Demo Widgets", "currency": "USD"}


def _login(client, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
    response = client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert response.status_code == 303
    return response


def _create(client, **fields):
    return client.post("/admin/stores", data={**DEMO, **fields}, follow_redirects=False)


def _store_count(db_session) -> int:
    return db_session.scalar(select(func.count()).select_from(Store))


def test_admin_creates_store_and_it_shows_in_api(client):
    _login(client)

    response = _create(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/stores"

    stores = client.get("/api/stores").json()
    created = next(s for s in stores if s["slug"] == "demo-widgets")
    assert created["name"] == "Demo Widgets"
    assert created["currency"] == "USD"
    assert created["theme"] == DEFAULT_THEME

    listing = client.get("/admin/stores")
    assert listing.status_code == 200
    assert "demo-widgets" in listing.text
    assert en("flash.store_created", name="Demo Widgets", slug="demo-widgets") in page_text(listing)
    # Flash is shown once.
    assert not pattern("en", "flash.store_created").search(page_text(client.get("/admin/stores")))

    # The new store is immediately browsable on the public storefront, without login.
    client.post("/logout")
    assert client.get("/stores/demo-widgets").status_code == 200


def test_admin_store_pages_render(client):
    _login(client)
    dashboard = client.get("/admin")
    assert dashboard.status_code == 200
    assert 'href="/admin/stores"' in dashboard.text

    empty = client.get("/admin/stores")
    assert empty.status_code == 200
    assert en("admin.stores.empty") in page_text(empty)
    assert empty.headers["cache-control"] == "no-store"

    form = client.get("/admin/stores/new")
    assert form.status_code == 200
    for field in ("slug", "name", "currency"):
        assert f'name="{field}"' in form.text
    assert 'value="USD"' in form.text
    assert en("admin.store_form.theme_hint") in page_text(form)


@pytest.mark.parametrize("path", ["/admin/stores", "/admin/stores/new"])
def test_store_pages_require_login(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=")


def test_create_store_blocked_when_logged_out(client, db_session):
    response = _create(client)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert _store_count(db_session) == 0


def test_create_store_forbidden_for_customer(client, db_session):
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    _login(client, "shopper@example.com", "shopper-pass")
    assert client.get("/admin/stores").status_code == 403
    assert _create(client).status_code == 403
    assert _store_count(db_session) == 0


@pytest.mark.parametrize(
    "fields,field,code,params",
    [
        ({"slug": ""}, "slug", "slug_required", {}),
        ({"slug": "Demo Widgets!"}, "slug", "store_slug_format", {"max_len": 64}),
        ({"slug": "_default"}, "slug", "store_slug_format", {"max_len": 64}),
        ({"slug": "demo--widgets"}, "slug", "store_slug_format", {"max_len": 64}),
        ({"slug": "new"}, "slug", "slug_reserved", {"slug": "new"}),
        ({"name": "   "}, "name", "name_required", {}),
        ({"name": "N" * 201}, "name", "name_too_long", {"max_len": 200}),
        ({"currency": "US"}, "currency", "currency_format", {}),
        ({"currency": "dollars"}, "currency", "currency_format", {}),
    ],
)
def test_create_store_validation(client, db_session, fields, field, code, params):
    _login(client)
    response = _create(client, **fields)
    assert response.status_code == 400
    assert f'id="{field}-error">{en_html(f"admin.form.error.{code}", **params)}</p>' in response.text
    assert _store_count(db_session) == 0


def test_create_store_rejects_duplicate_slug(client, db_session):
    _login(client)
    assert _create(client).status_code == 303
    response = _create(client, name="Another name")
    assert response.status_code == 400
    assert en("admin.form.error.store_slug_taken") in page_text(response)
    assert 'value="Another name"' in response.text  # input kept
    assert _store_count(db_session) == 1


def test_create_store_normalizes_input(client):
    _login(client)
    response = _create(client, slug="  Demo-Widgets ", name="  Demo   Widgets ", currency="eur")
    assert response.status_code == 303
    created = next(s for s in client.get("/api/stores").json() if s["slug"] == "demo-widgets")
    assert created["name"] == "Demo Widgets"
    assert created["currency"] == "EUR"


def test_reserved_slug_keeps_new_store_form(client):
    _login(client)
    # /admin/stores/new must stay the create form (a store slugged "new" would shadow it).
    assert en("admin.store_form.submit") in page_text(client.get("/admin/stores/new"))


def test_create_store_insert_race_is_reported(client, db_session, monkeypatch):
    _login(client)
    assert _create(client).status_code == 303
    real_clean = admin_router.clean_store_input

    def clean_without_errors(*args, **kwargs):
        data = real_clean(*args, **kwargs)
        data.errors.clear()  # pretend the duplicate was not seen yet
        return data

    monkeypatch.setattr(admin_router, "clean_store_input", clean_without_errors)
    response = _create(client, name="Second")
    assert response.status_code == 400
    assert en("admin.form.error.store_slug_taken") in page_text(response)
    assert _store_count(db_session) == 1
