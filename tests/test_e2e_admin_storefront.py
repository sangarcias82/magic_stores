"""End to end: an anonymous visitor is kept out of /admin, the dummy admin signs in, builds a
store (category, property definitions, product) through the admin forms only, signs out, and
the product is visible on the public storefront (HTML and API)."""

from __future__ import annotations

import re

from tests.ui_text import en, page_text

ADMIN_EMAIL = "admin@example.com"  # the dummy admin seeded on startup
ADMIN_PASSWORD = "ChangeMeAdmin123!"


def _location(response) -> str:
    assert response.status_code == 303, (response.status_code, response.text[:500])
    return response.headers["location"]


def _id_from(location: str) -> int:
    return int(location.rstrip("/").rsplit("/", 1)[1])


def _dd(html: str, key: str) -> str:
    """Rendered value of one product property on the public product page."""
    match = re.search(rf'data-key="{key}">(.*?)</dd>', html, re.S)
    assert match, key
    return " ".join(match.group(1).split())


def test_admin_builds_catalog_that_shows_on_storefront(client):
    # 1. Anonymous: admin is blocked, login page is offered.
    blocked = client.get("/admin", follow_redirects=False)
    assert _location(blocked) == "/login?next=%2Fadmin"
    assert f"<h1>{en('login.title')}</h1>" in page_text(client.get("/login?next=%2Fadmin"))

    # 2. Sign in with the dummy admin credentials -> back to /admin.
    login = client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "next": "/admin"},
                        follow_redirects=False)
    assert _location(login) == "/admin"
    assert f"{en('admin.dashboard.signed_in_as')} <strong>{ADMIN_EMAIL}</strong>" in page_text(client.get("/admin"))

    # 3. Store.
    store = client.post("/admin/stores", data={"slug": "demo-widgets", "name": "Demo Widgets", "currency": "USD"},
                        follow_redirects=False)
    assert _location(store) == "/admin/stores"
    assert en("flash.store_created", name="Demo Widgets", slug="demo-widgets") in page_text(client.get("/admin/stores"))
    base = "/admin/stores/demo-widgets"

    # 4. Category.
    category_url = _location(client.post(f"{base}/categories", data={"slug": "widgets", "name": "Widgets"},
                                         follow_redirects=False))
    category_id = _id_from(category_url)
    assert en("flash.category_created", name="Widgets") in page_text(client.get(category_url))

    # 5. Property definitions: text (required), enum with options, number, boolean.
    for definition in (
        {"key": "model", "label": "Model", "prop_type": "text", "required": "1", "sort_order": "1"},
        {"key": "color", "label": "Color", "prop_type": "enum", "options": "Red\nBlue\nGreen", "sort_order": "2"},
        {"key": "weight_g", "label": "Weight (g)", "prop_type": "number", "sort_order": "3"},
        {"key": "waterproof", "label": "Waterproof", "prop_type": "boolean", "sort_order": "4"},
    ):
        assert _location(client.post(f"{category_url}/properties", data=definition,
                                     follow_redirects=False)) == category_url

    # 6. Product form is driven by those definitions; create the product.
    form = client.get(f"{base}/products/new?category_id={category_id}").text
    for key in ("model", "color", "weight_g", "waterproof"):
        assert f'name="prop_{key}"' in form
    product_url = _location(client.post(f"{base}/products", data={
        "category_id": str(category_id),
        "slug": "widget-blue-m",
        "sku": "WB-100-M", "stock": "12",
        "name": "Widget Blue M",
        "price": "19.99",
        "description": "A blue widget.",
        "prop_model": "WB-100",
        "prop_color": "Blue",
        "prop_weight_g": "250",
        "prop_waterproof": "true",
    }, follow_redirects=False))
    assert en("flash.product_created", name="Widget Blue M") in page_text(client.get(product_url))
    assert "Widget Blue M" in client.get(f"{base}/products").text

    # 7. Sign out: admin is blocked again.
    assert _location(client.post("/logout", follow_redirects=False)) == "/"
    assert _location(client.get(product_url, follow_redirects=False)).startswith("/login")

    # 8. Public storefront (no session) shows the catalog -- HTML ...
    store_page = client.get("/stores/demo-widgets")
    assert store_page.status_code == 200
    assert '<a href="/stores/demo-widgets/c/widgets">Widgets</a>' in store_page.text
    assert '<a href="/stores/demo-widgets/p/widget-blue-m">Widget Blue M</a>' in store_page.text
    assert "USD 19.99" in store_page.text

    category_page = client.get("/stores/demo-widgets/c/widgets").text
    assert "Widget Blue M" in category_page
    for label in ("Model", "Color", "Weight (g)", "Waterproof"):
        assert f"<strong>{label}</strong>" in category_page  # property schema

    product_page = client.get("/stores/demo-widgets/p/widget-blue-m")
    assert product_page.status_code == 200
    html = product_page.text
    assert "<h1>Widget Blue M</h1>" in html and "USD 19.99" in html and "A blue widget." in html
    assert _dd(html, "model") == "WB-100"
    assert _dd(html, "color") == "Blue"
    assert _dd(html, "weight_g") == "250"
    assert _dd(html, "waterproof") == en("value.yes")

    # ... and API.
    assert [s["slug"] for s in client.get("/api/stores").json()] == ["demo-widgets"]
    categories = client.get("/api/stores/demo-widgets/categories").json()
    assert [c["slug"] for c in categories] == ["widgets"]
    defs = {d["key"]: d for d in categories[0]["property_definitions"]}
    assert {k: d["prop_type"] for k, d in defs.items()} == {
        "model": "text", "color": "enum", "weight_g": "number", "waterproof": "boolean",
    }
    assert defs["color"]["config"] == {"options": ["Red", "Blue", "Green"]}
    assert defs["model"]["required"] is True

    listed = client.get("/api/stores/demo-widgets/products?category=widgets").json()
    assert [p["slug"] for p in listed] == ["widget-blue-m"]
    detail = client.get("/api/stores/demo-widgets/products/widget-blue-m").json()
    assert detail["price_cents"] == 1999
    assert detail["category_id"] == category_id
    assert {p["key"]: p["value"] for p in detail["properties"]} == {
        "model": {"value": "WB-100"},
        "color": {"value": "Blue"},
        "weight_g": {"value": 250},
        "waterproof": {"value": True},
    }
