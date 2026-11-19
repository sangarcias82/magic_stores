"""Unknown stores, categories, products, pages and assets answer 404 (storefront, API and admin)."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "url",
    [
        "/stores/nope",
        "/stores/nope/c/shirts",
        "/stores/nope/p/linen-shirt-m",
        "/stores/nope/pages/about",
        "/stores/nope/assets/style.css",
        "/stores/clothing-demo/c/nope",
        "/stores/clothing-demo/pages/nope",
        "/stores/clothing-demo/assets/nope.css",
        "/api/stores/nope",
        "/api/stores/nope/categories",
        "/api/stores/nope/products",
        "/api/stores/nope/products/linen-shirt-m",
        "/api/stores/clothing-demo/products?category=nope",
    ],
)
def test_unknown_things_are_404(client, clothing_fixture, url):
    assert client.get(url).status_code == 404


# Numbers too large for a database INTEGER can't be any row's id. They used to reach the database and
# fail there (OverflowError, a 500); now they are unknown ids like any other.
HUGE = ("99999999999999999999", "-99999999999999999999")


@pytest.mark.parametrize("number", HUGE)
@pytest.mark.parametrize(
    "path, has_post",
    [
        ("/admin/imports/{n}", False),  # read-only page
        ("/admin/stores/demo-widgets/categories/{n}", True),
        ("/admin/stores/demo-widgets/categories/{category}/properties/{n}", True),
        ("/admin/stores/demo-widgets/products/{n}", True),
        ("/admin/stores/demo-widgets/shipping/{n}", True),
        ("/admin/stores/demo-widgets/terminals/{n}", True),
    ],
)
def test_huge_ids_in_admin_urls_are_404(admin_client, widgets_category, path, has_post, number):
    url = path.format(n=number, category=widgets_category.id)
    assert admin_client.get(url).status_code == 404
    if has_post:
        assert admin_client.post(url, data={}).status_code == 404


@pytest.mark.parametrize("number", HUGE)
def test_huge_ids_in_admin_forms_are_invalid_choices(admin_client, widgets_category, number):
    base = "/admin/stores/demo-widgets"
    chooser = admin_client.get(f"{base}/products/new?category_id={number}")
    assert chooser.status_code == 200 and f"{base}/products/new?category_id={widgets_category.id}" in chooser.text
    product = admin_client.post(f"{base}/products", data={"category_id": number, "slug": "p", "name": "P", "price": "1"})
    assert product.status_code == 400  # "choose a category from this store"
    reference = admin_client.post(f"{base}/categories/{widgets_category.id}/properties",
                                  data={"key": "ref", "label": "Ref", "prop_type": "category_ref",
                                        "ref_category_id": number})
    assert reference.status_code == 400
    assert 'id="ref_category_id-error"' in reference.text
    parent = admin_client.post(f"{base}/categories", data={"slug": "x", "name": "X", "parent_id": number})
    assert parent.status_code == 400
