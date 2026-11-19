"""Admin: store theme editor (primary / secondary / accent) and the storefront CSS variables."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth import create_user
from app.models import Store, UserRole
from app.services.storefront import DEFAULT_THEME
from tests.ui_text import en, en_html

THEME = "/admin/stores/demo-widgets/theme"
API_STORE = "/api/stores/demo-widgets"
COLORS = {"primary": "#aa0000", "secondary": "#fff8e1", "accent": "#0044cc"}


def _save(client, **colors):
    return client.post(THEME, data={**COLORS, **colors}, follow_redirects=False)


def _db_theme(db_session) -> dict:
    db_session.expire_all()
    return db_session.scalar(select(Store).where(Store.slug == "demo-widgets")).theme


# ------------------------------------------------------------------ requested scenario


def test_theme_primary_persists_after_update(admin_client, demo_store, db_session):
    assert admin_client.get(API_STORE).json()["theme"]["primary"] == DEFAULT_THEME["primary"]

    response = _save(admin_client, primary="#aa0000")
    assert response.status_code == 303
    assert response.headers["location"] == THEME

    # Persisted: API, database, and the editor all show the new primary.
    assert admin_client.get(API_STORE).json()["theme"]["primary"] == "#aa0000"
    assert _db_theme(db_session)["primary"] == "#aa0000"
    page = admin_client.get(THEME).text
    assert 'id="primary" name="primary" value="#aa0000"' in page
    assert en("flash.theme_saved") in page

    # A later update of another color keeps primary.
    assert _save(admin_client, accent="#228833").status_code == 303
    theme = admin_client.get(API_STORE).json()["theme"]
    assert theme["primary"] == "#aa0000"
    assert theme["accent"] == "#228833"
    assert theme["secondary"] == COLORS["secondary"]
    assert theme["logo_url"] is None  # other theme keys are kept

    # And the storefront picks it up.
    assert "--color-primary: #aa0000;" in admin_client.get("/stores/demo-widgets").text


# ---------------------------------------------------------------------------- editor


def test_editor_shows_current_colors_and_defaults(admin_client, demo_store):
    page = admin_client.get(THEME)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    for key in ("primary", "secondary", "accent"):
        assert f'type="color" id="{key}" name="{key}" value="{DEFAULT_THEME[key]}"' in page.text
        assert en("admin.theme.default", color=DEFAULT_THEME[key]) in page.text
    assert 'href="/admin/stores/demo-widgets/theme" aria-current="page"' in page.text  # store tab


def test_hex_colors_are_normalized(admin_client, demo_store):
    assert _save(admin_client, primary="#ABC", secondary=" #FFFFFF ", accent="#0044CC").status_code == 303
    theme = admin_client.get(API_STORE).json()["theme"]
    assert theme["primary"] == "#aabbcc"
    assert theme["secondary"] == "#ffffff"
    assert theme["accent"] == "#0044cc"


@pytest.mark.parametrize(
    "field,value",
    [
        ("primary", ""),
        ("primary", "red"),
        ("secondary", "#12345"),
        ("accent", "#gggggg"),
        ("primary", "#aa0000; } body { display: none"),
        ("accent", "</style><script>alert(1)</script>"),
    ],
)
def test_invalid_colors_are_rejected(admin_client, demo_store, db_session, field, value):
    response = _save(admin_client, **{field: value})
    assert response.status_code == 400
    # Named with the theme page's own label, under its field.
    message = en_html("admin.form.error.color_format", label=en(f"admin.theme.label.{field}"))
    assert f'id="{field}-error">{message}</p>' in response.text
    assert "<script>alert(1)" not in response.text
    assert _db_theme(db_session) == DEFAULT_THEME  # nothing saved


def test_invalid_color_does_not_leak_into_preview(admin_client, demo_store):
    response = _save(admin_client, primary="red;}")
    preview = response.text[response.text.index('class="theme-preview"'):]
    assert "red;}" not in preview.split(">", 1)[0]
    assert f"--p: {DEFAULT_THEME['primary']}" in preview


def test_theme_of_other_stores_untouched(admin_client, demo_store, db_session):
    db_session.add(Store(slug="other-store", name="Other", theme={"primary": "#111111"}))
    db_session.commit()
    _save(admin_client, primary="#aa0000")
    assert admin_client.get("/api/stores/other-store").json()["theme"] == {"primary": "#111111"}


def test_unknown_store_is_404(admin_client):
    assert admin_client.get("/admin/stores/nope/theme").status_code == 404
    assert admin_client.post("/admin/stores/nope/theme", data=COLORS).status_code == 404


def test_theme_requires_admin(client, demo_store, db_session):
    client.post("/logout")
    for method in ("get", "post"):
        response = getattr(client, method)(THEME, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")

    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert client.get(THEME).status_code == 403
    assert client.post(THEME, data=COLORS).status_code == 403
    assert _db_theme(db_session) == DEFAULT_THEME


# ------------------------------------------------------------ storefront CSS variables


def test_storefront_pages_use_theme_variables(admin_client, widgets_category):
    _save(admin_client)
    admin_client.post("/admin/stores/demo-widgets/products",
                      data={"category_id": str(widgets_category.id), "slug": "widget-blue-m",
                            "sku": "WB-100-M", "stock": "12", "name": "Widget Blue M", "price": "19.99", "prop_size": "M"})
    admin_client.post("/logout")  # public pages, no login
    for path in ("/stores/demo-widgets", "/stores/demo-widgets/c/widgets", "/stores/demo-widgets/p/widget-blue-m"):
        text = admin_client.get(path).text
        assert "--color-primary: #aa0000;" in text, path
        assert "--color-secondary: #fff8e1;" in text, path
        assert "--color-accent: #0044cc;" in text, path
        assert '<link rel="stylesheet" href="/stores/demo-widgets/assets/style.css">' in text
    # The stylesheet consumes the variables.
    css = admin_client.get("/stores/demo-widgets/assets/style.css").text
    assert "var(--color-primary)" in css and "var(--color-accent)" in css


@pytest.mark.parametrize(
    "stored,rendered",
    [
        ("teal", "teal"),  # named colors from packs are fine
        ("rgb(10, 20, 30)", "rgb(10, 20, 30)"),
        ("red; } body { display: none", DEFAULT_THEME["primary"]),  # CSS injection -> default
        ("</style><script>alert(1)</script>", DEFAULT_THEME["primary"]),
        (123, DEFAULT_THEME["primary"]),
    ],
)
def test_storefront_sanitizes_stored_theme(client, db_session, stored, rendered):
    db_session.add(Store(slug="packed", name="Packed", theme={"primary": stored}))
    db_session.commit()
    text = client.get("/stores/packed").text
    assert f"--color-primary: {rendered};" in text
    assert "<script>alert(1)" not in text


def test_editor_shows_default_for_non_hex_stored_color(admin_client, db_session):
    db_session.add(Store(slug="packed", name="Packed", theme={"primary": "teal"}))
    db_session.commit()
    page = admin_client.get("/admin/stores/packed/theme").text
    assert f'id="primary" name="primary" value="{DEFAULT_THEME["primary"]}"' in page


@pytest.mark.parametrize(
    "value,expected",
    [("#ABC", "#aabbcc"), (" #A1B2C3 ", "#a1b2c3"), ("#abcd", None), ("teal", None), (None, None), (0x123456, None)],
)
def test_normalize_hex_color(value, expected):
    from app.services.storefront import normalize_hex_color

    assert normalize_hex_color(value) == expected
