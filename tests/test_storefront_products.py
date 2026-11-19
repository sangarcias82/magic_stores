"""Public storefront: product pages show typed property values; hidden products stay private."""

from __future__ import annotations

import re

from tests.ui_text import en

PRODUCTS = "/admin/stores/demo-widgets/products"
PUBLIC = "/stores/demo-widgets/p/widget-blue-m"


def _create(client, category, **overrides):
    data = {
        "category_id": str(category.id),
        "slug": "widget-blue-m",
        "sku": "WB-100-M", "stock": "12",
        "name": "Widget Blue M",
        "price": "19.99",
        "prop_color": "Blue",
        "prop_size": "M",
        "prop_weight_g": "250",
        "prop_waterproof": "true",
        "prop_photo": "/static/widget-blue.png",
        "prop_brand": str(category.acme_id),
        "prop_notes": "",
        **overrides,
    }
    response = client.post(PRODUCTS, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _prop(html: str, key: str) -> str:
    match = re.search(rf'data-key="{key}">(.*?)</dd>', html, re.S)
    assert match, key
    return " ".join(match.group(1).split())


def test_public_product_page_shows_properties(admin_client, widgets_category):
    _create(admin_client, widgets_category)
    admin_client.post("/logout")  # the storefront is public

    page = admin_client.get(PUBLIC)
    assert page.status_code == 200
    html = page.text
    assert "<h1>Widget Blue M</h1>" in html
    assert "USD 19.99" in html
    assert '<a href="/stores/demo-widgets/c/widgets">Widgets</a>' in html  # breadcrumb

    assert _prop(html, "color") == "Blue"
    assert _prop(html, "size") == "M"
    assert _prop(html, "weight_g") == "250"
    assert _prop(html, "waterproof") == en("value.yes")
    assert _prop(html, "photo") == '<img src="/static/widget-blue.png" alt="Photo" class="prop-image">'
    assert _prop(html, "brand") == '<a href="/stores/demo-widgets/c/acme">Acme</a>'
    assert _prop(html, "notes") == '<span class="meta">—</span>'
    for label in ("Color", "Size", "Weight (g)", "Waterproof", "Photo", "Brand", "Notes"):
        assert f"<dt>{label}</dt>" in html


def test_public_product_page_empty_and_false_values(admin_client, widgets_category):
    _create(admin_client, widgets_category, prop_waterproof="false", prop_photo="", prop_brand="", prop_color="")
    html = admin_client.get(PUBLIC).text
    assert _prop(html, "waterproof") == en("value.no")
    assert _prop(html, "photo") == f'<span class="meta">{en("store.no_image")}</span>'
    assert _prop(html, "brand") == '<span class="meta">—</span>'
    assert _prop(html, "color") == '<span class="meta">—</span>'


def test_public_values_are_escaped(admin_client, widgets_category):
    _create(admin_client, widgets_category, prop_notes="<b>bold</b>")
    html = admin_client.get(PUBLIC).text
    assert _prop(html, "notes") == "&lt;b&gt;bold&lt;/b&gt;"


def test_category_ref_to_deleted_category_falls_back_to_id(admin_client, widgets_category, db_session):
    from sqlalchemy import select

    from app.models import Category, PropertyDefinition, PropertyValue

    _create(admin_client, widgets_category)
    # Point the stored value at a category id that no longer exists (e.g. stale pack data).
    brand = db_session.scalar(select(PropertyDefinition).where(PropertyDefinition.key == "brand"))
    value = db_session.scalar(select(PropertyValue).where(PropertyValue.definition_id == brand.id))
    value.value = {"category_id": 999999}
    db_session.commit()
    assert db_session.get(Category, 999999) is None
    assert _prop(admin_client.get(PUBLIC).text, "brand") == f"<span>{en('store.category_ref', id=999999)}</span>"


def test_product_listed_on_store_and_category_pages(admin_client, widgets_category):
    _create(admin_client, widgets_category)
    admin_client.post("/logout")
    for path in ("/stores/demo-widgets", "/stores/demo-widgets/c/widgets"):
        assert f'href="{PUBLIC}">Widget Blue M</a>' in admin_client.get(path).text, path
    assert [p["slug"] for p in admin_client.get("/api/stores/demo-widgets/products").json()] == ["widget-blue-m"]


def test_hidden_product_is_not_public(admin_client, widgets_category):
    _create(admin_client, widgets_category, status="hidden")
    admin_client.post("/logout")
    assert admin_client.get(PUBLIC).status_code == 404
    assert admin_client.get("/api/stores/demo-widgets/products/widget-blue-m").status_code == 404
    assert admin_client.get("/api/stores/demo-widgets/products").json() == []
    for path in ("/stores/demo-widgets", "/stores/demo-widgets/c/widgets"):
        assert "Widget Blue M" not in admin_client.get(path).text, path
