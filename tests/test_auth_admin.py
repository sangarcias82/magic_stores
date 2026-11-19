"""Admin access via session auth: /login, /logout, require_admin on every /admin route,
session cookie integrity, public storefront."""

from __future__ import annotations

import json
import re
from base64 import b64encode
from urllib.parse import quote

import itsdangerous
import pytest
from sqlalchemy import select

from app.auth import create_user
from app.config import get_settings
from app.models import User, UserRole
from tests.ui_text import en, page_text

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "ChangeMeAdmin123!"

SESSION_COOKIE = "magic_stores_session"
# Values for path parameters when calling every admin route (none of these need to exist:
# the admin guard must answer before any lookup).
PATH_PARAM_SAMPLES = {"store_slug": "demo-widgets", "category_id": "1", "definition_id": "1", "product_id": "1",
                      "record_id": "1", "method_id": "1",
                      "payment_id": "1", "terminal_id": "1"}


def _login(client, email, password, next_path=""):
    return client.post(
        "/login",
        data={"email": email, "password": password, "next": next_path},
        follow_redirects=False,
    )


def _session_cookie(data: dict, secret: str) -> str:
    """A session cookie exactly as Starlette's SessionMiddleware builds it."""
    return itsdangerous.TimestampSigner(secret).sign(b64encode(json.dumps(data).encode())).decode()


def _admin_routes(client) -> list[tuple[str, str]]:
    """(method, concrete path) for every route under /admin. Read from the OpenAPI schema, a
    public API (newer FastAPI versions no longer flatten included routers into ``app.routes``).
    Admin routes must therefore stay in the schema (no ``include_in_schema=False``)."""
    calls = []
    for template, operations in client.app.openapi()["paths"].items():
        if template.startswith("/admin"):
            path = re.sub(r"\{(\w+)\}", lambda m: PATH_PARAM_SAMPLES[m.group(1)], template)
            calls.extend((method.upper(), path) for method in operations)
    return calls


# --- login / admin guard / logout ----------------------------------------------

def test_login_page_renders(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert f"<h1>{en('login.title')}</h1>" in page_text(response)
    assert 'name="password"' in response.text


def test_admin_blocked_when_logged_out(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fadmin"


def test_login_success_opens_admin_then_logout_blocks_again(client):
    response = _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    # Signed session cookie: HttpOnly, SameSite=Lax, 14-day lifetime.
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()
    assert "Max-Age=1209600" in set_cookie
    assert ADMIN_PASSWORD not in set_cookie

    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    assert f"<h1>{en('admin.dashboard.title')}</h1>" in page_text(admin_page)
    assert f"{en('admin.dashboard.signed_in_as')} <strong>{ADMIN_EMAIL}</strong> (admin)" in page_text(admin_page)
    assert en("nav.sign_out") in page_text(admin_page)
    assert admin_page.headers["cache-control"] == "no-store"

    logout = client.post("/logout", follow_redirects=False)
    assert logout.status_code == 303
    assert logout.headers["location"] == "/"
    assert SESSION_COOKIE not in client.cookies  # cookie cleared

    blocked = client.get("/admin", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"].startswith("/login")
    assert f'<a href="/login">{en("nav.sign_in")}</a>' in page_text(client.get("/"))


def test_get_logout_also_works(client):
    _login(client, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert client.get("/admin").status_code == 200
    client.get("/logout")
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_login_email_is_case_insensitive(client):
    response = _login(client, "  Admin@Example.COM ", ADMIN_PASSWORD)
    assert response.status_code == 303
    assert client.get("/admin").status_code == 200


@pytest.mark.parametrize(
    "email,password",
    [
        (ADMIN_EMAIL, "wrong-password"),
        ("nobody@example.com", ADMIN_PASSWORD),
        (ADMIN_EMAIL, ""),
    ],
)
def test_login_failure(client, email, password):
    response = _login(client, email, password)
    assert response.status_code == 401
    assert en("login.invalid") in page_text(response)  # same message: no user enumeration
    assert "set-cookie" not in response.headers  # no session is created
    assert SESSION_COOKIE not in client.cookies
    assert f'value="{email}"' in response.text  # email kept in the form ...
    if password:
        assert password not in response.text  # ... the password never echoed back
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_login_with_missing_fields_fails(client):
    response = client.post("/login", data={}, follow_redirects=False)
    assert response.status_code == 401
    assert en("login.invalid") in page_text(response)


def test_customer_cannot_open_admin(client, db_session):
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    response = _login(client, "shopper@example.com", "shopper-pass")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert client.get("/admin", follow_redirects=False).status_code == 403


def test_login_redirects_to_safe_next(client):
    response = _login(client, ADMIN_EMAIL, ADMIN_PASSWORD, next_path="/admin")
    assert response.headers["location"] == "/admin"


@pytest.mark.parametrize("evil", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
def test_login_rejects_open_redirect(client, evil):
    response = _login(client, ADMIN_EMAIL, ADMIN_PASSWORD, next_path=evil)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"


@pytest.mark.parametrize("evil", ["/admin\r\nSet-Cookie: x=1", "/admin\nLocation: https://evil.example", "/ad\tmin"])
def test_login_rejects_next_with_control_characters(client, evil):
    # Would allow HTTP header injection through the Location header.
    response = _login(client, ADMIN_EMAIL, ADMIN_PASSWORD, next_path=evil)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert "set-cookie: x=1" not in str(response.headers).lower()


def test_logout_has_distinct_openapi_operations(client):
    operations = client.app.openapi()["paths"]["/logout"]
    assert set(operations) == {"get", "post"}
    assert operations["get"]["operationId"] != operations["post"]["operationId"]


def test_placeholder_secret_key_logs_warning(monkeypatch, caplog):
    from app.config import get_settings
    from app.main import PLACEHOLDER_SECRET, create_app

    monkeypatch.setenv("SECRET_KEY", PLACEHOLDER_SECRET)
    get_settings.cache_clear()
    try:
        with caplog.at_level("WARNING", logger="app.main"):
            create_app()
        assert "SECRET_KEY is the placeholder value" in caplog.text

        caplog.clear()
        monkeypatch.setenv("SECRET_KEY", "a-real-secret")
        get_settings.cache_clear()
        with caplog.at_level("WARNING", logger="app.main"):
            create_app()
        assert "SECRET_KEY" not in caplog.text
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()  # back to the pinned test settings


def test_tampered_session_cookie_is_ignored(client):
    client.cookies.set("magic_stores_session", "forged-value")
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_blocked_admin_page_returns_there_after_login(client):
    blocked = client.get("/admin/stores", follow_redirects=False)
    assert blocked.status_code == 303
    assert blocked.headers["location"] == "/login?next=%2Fadmin%2Fstores"

    form = client.get(blocked.headers["location"])
    assert 'name="next" value="/admin/stores"' in form.text
    response = _login(client, ADMIN_EMAIL, ADMIN_PASSWORD, next_path="/admin/stores")
    assert response.headers["location"] == "/admin/stores"
    assert client.get("/admin/stores").status_code == 200


def test_every_admin_route_is_blocked_without_session(client):
    calls = _admin_routes(client)
    assert len(calls) >= 25  # guards against the route discovery silently finding nothing
    for method, path in calls:
        response = client.request(method, path, follow_redirects=False)
        # Redirect to login before any lookup: no 404 reveals whether a store/category exists.
        assert response.status_code == 303, (method, path, response.status_code)
        assert response.headers["location"] == f"/login?next={quote(path, safe='')}", (method, path)


def test_every_admin_route_is_forbidden_for_customers(client, db_session):
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    _login(client, "shopper@example.com", "shopper-pass")
    for method, path in _admin_routes(client):
        response = client.request(method, path, follow_redirects=False)
        assert response.status_code == 403, (method, path, response.status_code)


def test_session_cookie_forged_without_secret_is_rejected(client, db_session):
    admin_id = db_session.scalar(select(User.id).where(User.email == ADMIN_EMAIL))

    client.cookies.set(SESSION_COOKIE, _session_cookie({"user_id": admin_id}, "not-the-secret"))
    assert client.get("/admin", follow_redirects=False).status_code == 303

    # Control: the same cookie signed with the real SECRET_KEY is accepted, so the rejection
    # above is due to the signature and not to a malformed test cookie.
    client.cookies.set(SESSION_COOKIE, _session_cookie({"user_id": admin_id}, get_settings().secret_key))
    assert client.get("/admin", follow_redirects=False).status_code == 200


def test_session_of_deleted_user_is_dropped(client, db_session):
    create_user(db_session, "temp-admin@example.com", "temp-pass-123", role=UserRole.admin)
    _login(client, "temp-admin@example.com", "temp-pass-123")
    assert client.get("/admin").status_code == 200

    db_session.delete(db_session.scalar(select(User).where(User.email == "temp-admin@example.com")))
    db_session.commit()
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# --- public storefront stays open -----------------------------------------------

def test_storefront_public_without_login(client, clothing_fixture):
    assert client.get("/").status_code == 200
    assert client.get("/stores/clothing-demo").status_code == 200
    assert client.get("/stores/clothing-demo/p/linen-shirt-m").status_code == 200
    assert client.get("/api/stores").status_code == 200
