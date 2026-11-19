"""Public product API: the ``in_stock`` flag (stock on hand > 0) on the list and the detail, without
exposing the count or the SKU, and the README's field table kept equal to the response models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Product
from app.schemas import ProductListItem, ProductOut
from tests.markdown_doc import section, table_rows

PROJECT = Path(__file__).resolve().parent.parent
README = (PROJECT / "README.md").read_text(encoding="utf-8")
ADMIN_PRODUCTS = "/admin/stores/demo-widgets/products"
API = "/api/stores/demo-widgets/products"
PRIVATE_FIELDS = {"stock_on_hand", "stock", "sku"}  # admin-only


def _create(admin_client, category_id: int, slug: str, stock: str, **extra) -> int:
    data = {"category_id": str(category_id), "slug": slug, "sku": slug.upper(), "name": slug.title(),
            "price": "10", "stock": stock, "prop_size": "M", **extra}
    response = admin_client.post(ADMIN_PRODUCTS, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _edit_stock(admin_client, category_id: int, product_id: int, slug: str, stock: str) -> None:
    data = {"category_id": str(category_id), "slug": slug, "sku": slug.upper(), "name": slug.title(),
            "price": "10", "stock": stock, "prop_size": "M"}
    response = admin_client.post(f"{ADMIN_PRODUCTS}/{product_id}", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text


def _listed(client) -> dict[str, dict]:
    return {item["slug"]: item for item in client.get(API).json()}


def test_in_stock_follows_the_stock_on_list_and_detail(admin_client, widgets_category):
    _create(admin_client, widgets_category.id, "plenty", "25")
    _create(admin_client, widgets_category.id, "last-one", "1")
    _create(admin_client, widgets_category.id, "sold-out", "0")
    admin_client.post("/logout")  # public: no session needed

    expected = {"plenty": True, "last-one": True, "sold-out": False}
    listed = _listed(admin_client)
    assert {slug: item["in_stock"] for slug, item in listed.items()} == expected
    for slug, in_stock in expected.items():
        detail = admin_client.get(f"{API}/{slug}")
        assert detail.status_code == 200
        assert detail.json()["in_stock"] is in_stock, slug  # a real JSON boolean


def test_in_stock_changes_when_admin_edits_the_stock(admin_client, widgets_category):
    product_id = _create(admin_client, widgets_category.id, "widget", "5")
    assert admin_client.get(f"{API}/widget").json()["in_stock"] is True
    _edit_stock(admin_client, widgets_category.id, product_id, "widget", "0")
    assert admin_client.get(f"{API}/widget").json()["in_stock"] is False
    assert _listed(admin_client)["widget"]["in_stock"] is False
    _edit_stock(admin_client, widgets_category.id, product_id, "widget", "1")
    assert admin_client.get(f"{API}/widget").json()["in_stock"] is True


def test_the_count_and_the_sku_stay_private(admin_client, widgets_category):
    _create(admin_client, widgets_category.id, "widget", "4321")
    admin_client.post("/logout")
    detail = admin_client.get(f"{API}/widget")
    listing = admin_client.get(API)
    for body in (detail.json(), listing.json()[0]):
        assert not PRIVATE_FIELDS & set(body), sorted(body)
    for response in (detail, listing):
        assert "4321" not in response.text and "WIDGET" not in response.text


def test_response_fields_are_exactly_the_models(admin_client, widgets_category):
    _create(admin_client, widgets_category.id, "widget", "2")
    assert set(admin_client.get(f"{API}/widget").json()) == set(ProductOut.model_fields)
    assert set(admin_client.get(API).json()[0]) == set(ProductListItem.model_fields)


def test_hidden_products_stay_private_whatever_their_stock(admin_client, widgets_category):
    _create(admin_client, widgets_category.id, "secret", "9", status="hidden")
    assert admin_client.get(f"{API}/secret").status_code == 404
    assert "secret" not in _listed(admin_client)


def test_products_without_stock_are_not_in_stock(client, db_session):
    """Pack products (and older databases) have stock 0 until an admin sets it: the API says so."""
    from app.services.store_pack_import import import_store_pack

    pack = json.loads((PROJECT / "fixtures" / "packs" / "clothing-demo.store-pack.v1.json").read_text(encoding="utf-8"))
    import_store_pack(db_session, pack)
    slug, product_slug = pack["store"]["slug"], pack["products"][0]["slug"]
    assert client.get(f"/api/stores/{slug}/products/{product_slug}").json()["in_stock"] is False
    assert all(item["in_stock"] is False for item in client.get(f"/api/stores/{slug}/products").json())

    product = db_session.scalar(select(Product).where(Product.slug == product_slug))
    product.stock_on_hand = 3
    db_session.commit()
    assert client.get(f"/api/stores/{slug}/products/{product_slug}").json()["in_stock"] is True


@pytest.mark.parametrize("stock,expected", [(0, False), (1, True), (2_147_483_647, True), (None, False)])
def test_in_stock_property(stock, expected):
    assert Product(stock_on_hand=stock).in_stock is expected  # None: a new object before its default


# --- The README documents exactly these fields ---------------------------------------------------------------------


def _documented_fields() -> dict[str, tuple[str, str]]:
    """README "Product API (public)" table: field -> (list column, detail column)."""
    rows = table_rows(section(README, "Product fields"))
    return {row[0].strip("`"): (row[1], row[2]) for row in rows}


def test_readme_field_table_is_the_models():
    documented = _documented_fields()
    assert {f for f, (in_list, _) in documented.items() if in_list == "yes"} == set(ProductListItem.model_fields)
    assert {f for f, (_, in_detail) in documented.items() if in_detail == "yes"} == set(ProductOut.model_fields)
    assert "in_stock" in documented
    api_section = section(README, "Product API (public)")
    for private in ("stock_on_hand", "sku"):
        assert f"`{private}`" in api_section  # says they are not exposed
