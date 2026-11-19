"""Admin: products on demo-widgets (create / edit / delete), typed property values."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.auth import create_user
from app.models import Category, Product, PropertyDefinition, PropertyType, PropertyValue, Store, UserRole
from app.services import product_admin as svc
from tests.ui_text import en, en_html, es, page_text, pattern

STORE = "/admin/stores/demo-widgets"
PRODUCTS = f"{STORE}/products"
API_PRODUCT = "/api/stores/demo-widgets/products/widget-blue-m"


def _product_data(category_id, **overrides):
    data = {
        "category_id": str(category_id),
        "slug": "widget-blue-m",
        "sku": "WB-100-M", "stock": "12",
        "name": "Widget Blue M",
        "price": "19.99",
        "description": "A blue widget, size M.",
        "prop_color": "Blue",
        "prop_size": "M",
        "prop_weight_g": "250",
        "prop_waterproof": "true",
        "prop_photo": "/static/widget-blue.png",
        "prop_brand": "",
        "prop_notes": "",
    }
    data.update(overrides)
    return data


def _create(client, category_id, **overrides):
    return client.post(PRODUCTS, data=_product_data(category_id, **overrides), follow_redirects=False)


def _create_id(client, category_id, **overrides) -> int:
    response = _create(client, category_id, **overrides)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _props(client, url=API_PRODUCT) -> dict[str, dict]:
    return {p["key"]: p for p in client.get(url).json()["properties"]}


def _product_count(db_session) -> int:
    return db_session.scalar(select(func.count()).select_from(Product))


# ------------------------------------------------------------------ requested scenario


def test_create_product_with_values_shows_in_api_detail(admin_client, widgets_category, db_session):
    response = _create(admin_client, widgets_category.id, prop_brand=str(widgets_category.acme_id),
                       prop_notes="Ships in a box")
    assert response.status_code == 303
    product_id = int(response.headers["location"].rsplit("/", 1)[1])
    assert response.headers["location"] == f"{PRODUCTS}/{product_id}"

    detail = admin_client.get(API_PRODUCT)
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == product_id
    assert body["slug"] == "widget-blue-m"
    assert body["name"] == "Widget Blue M"
    assert body["description"] == "A blue widget, size M."
    assert body["price_cents"] == 1999
    assert body["is_active"] is True
    assert body["category_id"] == widgets_category.id

    props = {p["key"]: p for p in body["properties"]}
    # Every definition of the category, in definition order.
    assert [p["key"] for p in body["properties"]] == [
        "color", "size", "weight_g", "waterproof", "photo", "brand", "notes"
    ]
    expected = {
        "color": ("enum", {"value": "Blue"}),
        "size": ("enum", {"value": "M"}),
        "weight_g": ("number", {"value": 250}),
        "waterproof": ("boolean", {"value": True}),
        "photo": ("image", {"media_path": "/static/widget-blue.png"}),
        "brand": ("category_ref", {"category_id": widgets_category.acme_id}),
        "notes": ("text", {"value": "Ships in a box"}),
    }
    for key, (prop_type, value) in expected.items():
        assert props[key]["prop_type"] == prop_type, key
        assert props[key]["value"] == value, key
    # JSON types are real types, not strings.
    assert isinstance(props["weight_g"]["value"]["value"], int)
    assert props["waterproof"]["value"]["value"] is True

    # Stored as one typed PropertyValue row per filled definition.
    rows = db_session.scalars(select(PropertyValue).where(PropertyValue.product_id == product_id)).all()
    assert len(rows) == 7

    assert en("flash.product_created", name="Widget Blue M") in page_text(admin_client.get(f"{PRODUCTS}/{product_id}"))


def test_empty_optional_values_are_not_stored(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id, prop_color="", prop_weight_g="", prop_waterproof="",
                            prop_photo="")
    props = _props(admin_client)
    for key in ("color", "weight_g", "waterproof", "photo", "brand", "notes"):
        assert props[key]["value"] == {}, key
    assert props["size"]["value"] == {"value": "M"}
    rows = db_session.scalars(select(PropertyValue).where(PropertyValue.product_id == product_id)).all()
    assert len(rows) == 1


def test_omitted_status_defaults_to_active(admin_client, widgets_category):
    data = _product_data(widgets_category.id)
    assert "status" not in data  # e.g. a curl call that doesn't send it
    response = admin_client.post(PRODUCTS, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert admin_client.get(API_PRODUCT).json()["is_active"] is True


@pytest.mark.parametrize(
    "raw,expected",
    [("250", 250), ("-3", -3), ("2.5", 2.5), ("0", 0), (" 7 ", 7)],
)
def test_number_values_are_typed(admin_client, widgets_category, raw, expected):
    _create_id(admin_client, widgets_category.id, prop_weight_g=raw)
    value = _props(admin_client)["weight_g"]["value"]["value"]
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize("raw,expected", [("true", True), ("yes", True), ("1", True), ("false", False),
                                          ("No", False), ("0", False)])
def test_boolean_values(admin_client, widgets_category, raw, expected):
    _create_id(admin_client, widgets_category.id, prop_waterproof=raw)
    assert _props(admin_client)["waterproof"]["value"] == {"value": expected}


def test_image_accepts_https_url(admin_client, widgets_category):
    _create_id(admin_client, widgets_category.id, prop_photo="https://cdn.example.com/w.png")
    assert _props(admin_client)["photo"]["value"] == {"media_path": "https://cdn.example.com/w.png"}


def test_category_ref_accepts_referenced_category_itself(admin_client, widgets_category):
    _create_id(admin_client, widgets_category.id, prop_brand=str(widgets_category.brands_id))
    assert _props(admin_client)["brand"]["value"] == {"category_id": widgets_category.brands_id}


# ---------------------------------------------------------------------------- pages


def test_products_page_without_categories(admin_client, demo_store):
    page = admin_client.get(PRODUCTS)
    assert page.status_code == 200
    assert f'{en("admin.products.create_category")}</a> {en("admin.products.create_category_end")}' in page_text(page)
    pick = admin_client.get(f"{PRODUCTS}/new")
    assert en("admin.pick_category.no_categories") in page_text(pick)


def test_new_product_starts_with_category_choice(admin_client, widgets_category):
    pick = admin_client.get(f"{PRODUCTS}/new")
    assert pick.status_code == 200
    assert f'href="{PRODUCTS}/new?category_id={widgets_category.id}"' in pick.text
    # Unknown / foreign / garbage category ids fall back to the chooser.
    for bad in ("999999", "abc", ""):
        assert en("admin.pick_category.intro") in page_text(admin_client.get(f"{PRODUCTS}/new?category_id={bad}"))


def test_product_form_is_driven_by_category_definitions(admin_client, widgets_category):
    form = admin_client.get(f"{PRODUCTS}/new?category_id={widgets_category.id}")
    assert form.status_code == 200
    text = form.text
    assert f'name="category_id" value="{widgets_category.id}"' in text
    for key in ("color", "size", "weight_g", "waterproof", "photo", "brand", "notes"):
        assert f'name="prop_{key}"' in text, key
    # Widgets per type.
    assert '<select id="prop_color" name="prop_color"' in text
    assert '<option value="Green"' in text
    assert '<option value="true"' in text and '<option value="false"' in text
    assert f'placeholder="{en("admin.product.image_placeholder")}"' in text
    assert "Size <span aria-hidden=\"true\">*</span>" in text  # required marker
    # category_ref offers only the referenced category and its subcategories.
    brand_select = text[text.index('name="prop_brand"'):text.index("</select>", text.index('name="prop_brand"'))]
    assert f'value="{widgets_category.brands_id}"' in brand_select
    assert f'value="{widgets_category.acme_id}"' in brand_select
    assert f'value="{widgets_category.id}"' not in brand_select


def test_category_without_properties_gets_a_plain_form(admin_client, demo_store):
    response = admin_client.post(f"{STORE}/categories", data={"slug": "misc", "name": "Misc"}, follow_redirects=False)
    category_id = int(response.headers["location"].rsplit("/", 1)[1])
    form = admin_client.get(f"{PRODUCTS}/new?category_id={category_id}")
    assert en("admin.product.no_definitions") in page_text(form)
    response = admin_client.post(PRODUCTS, data={"category_id": str(category_id), "slug": "thing", "sku": "THING-1", "stock": "1",
                                                 "name": "Thing", "price": "1"}, follow_redirects=False)
    assert response.status_code == 303
    assert admin_client.get("/api/stores/demo-widgets/products/thing").json()["properties"] == []


def test_product_list_and_category_filter(admin_client, widgets_category):
    _create_id(admin_client, widgets_category.id)
    misc = admin_client.post(f"{STORE}/categories", data={"slug": "misc", "name": "Misc"}, follow_redirects=False)
    misc_id = int(misc.headers["location"].rsplit("/", 1)[1])
    admin_client.post(PRODUCTS, data={"category_id": str(misc_id), "slug": "thing", "sku": "THING-1", "stock": "1", "name": "Thing",
                                      "price": "1", "status": "hidden"})

    listing = admin_client.get(PRODUCTS).text
    assert "Widget Blue M" in listing and "Thing" in listing
    assert "USD 19.99" in listing
    assert en("status.hidden") in listing

    filtered = admin_client.get(f"{PRODUCTS}?category_id={widgets_category.id}").text
    assert "Widget Blue M" in filtered and "Thing" not in filtered
    assert f'<option value="{widgets_category.id}" selected>' in filtered
    empty = admin_client.get(f"{PRODUCTS}?category_id={widgets_category.acme_id}").text
    assert en("admin.products.empty_in_category") in empty
    # Unknown filter shows everything.
    assert "Thing" in admin_client.get(f"{PRODUCTS}?category_id=abc").text

    # Category pages link to products.
    store_page = admin_client.get(STORE).text
    assert f'href="{PRODUCTS}?category_id={widgets_category.id}">1</a>' in store_page
    category_page = admin_client.get(f"{STORE}/categories/{widgets_category.id}").text
    assert en("admin.category.product_count", count=1) in category_page
    assert f'href="{PRODUCTS}/new?category_id={widgets_category.id}"' in category_page


# ----------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "overrides,field,code,params",
    [
        ({"slug": ""}, "slug", "slug_required", {}),
        ({"slug": "Widget Blue"}, "slug", "slug_format", {"max_len": 64}),
        ({"name": ""}, "name", "name_required", {}),
        ({"name": "n" * 201}, "name", "name_too_long", {"max_len": 200}),
        ({"description": "d" * 5001}, "description", "description_too_long", {"max_len": 5000}),
        ({"price": ""}, "price", "price_required", {}),
        ({"price": "abc"}, "price", "price_format", {}),
        ({"price": "19.999"}, "price", "price_decimals", {}),
        ({"price": "-1"}, "price", "price_negative", {}),
        ({"price": "1000000000"}, "price", "price_too_large", {}),
        ({"price": "1" * 21}, "price", "price_too_large", {}),
        ({"status": "archived"}, "status", "status_invalid", {}),
        ({"prop_size": ""}, "prop_size", "value_required", {"label": "Size"}),
        ({"prop_color": "Purple"}, "prop_color", "enum_choice", {}),
        ({"prop_weight_g": "heavy"}, "prop_weight_g", "number_format", {}),
        ({"prop_weight_g": "1e5"}, "prop_weight_g", "number_format", {}),
        ({"prop_weight_g": "9" * 31}, "prop_weight_g", "number_format", {}),
        ({"prop_waterproof": "maybe"}, "prop_waterproof", "boolean_choice", {}),
        ({"prop_photo": "javascript:alert(1)"}, "prop_photo", "image_url", {}),
        ({"prop_photo": "data:image/png;base64,AAAA"}, "prop_photo", "image_url", {}),
        ({"prop_photo": "//evil.example/x.png"}, "prop_photo", "image_url", {}),
        ({"prop_photo": "/\\evil.example/x.png"}, "prop_photo", "image_url", {}),
        ({"prop_photo": "https://"}, "prop_photo", "image_url", {}),
        ({"prop_photo": "/x" * 501}, "prop_photo", "image_url", {}),
        ({"prop_brand": "abc"}, "prop_brand", "category_choice", {}),
        ({"prop_notes": "n" * 2001}, "prop_notes", "value_too_long", {"max_len": 2000}),
    ],
)
def test_product_validation(admin_client, widgets_category, db_session, overrides, field, code, params):
    response = _create(admin_client, widgets_category.id, **overrides)
    assert response.status_code == 400
    # The message is the catalog's, shown under its own field.
    assert f'id="{field}-error">{en_html(f"admin.product.error.{code}", **params)}</p>' in response.text
    assert _product_count(db_session) == 0


def test_category_ref_outside_referenced_tree_is_rejected(admin_client, widgets_category):
    response = _create(admin_client, widgets_category.id, prop_brand=str(widgets_category.id))
    assert response.status_code == 400
    assert en("admin.product.error.category_choice") in response.text


def test_invalid_form_keeps_input(admin_client, widgets_category):
    response = _create(admin_client, widgets_category.id, price="cheap", prop_notes="Keep me")
    assert response.status_code == 400
    text = response.text
    assert 'value="widget-blue-m"' in text
    assert 'value="cheap"' in text
    assert 'value="Keep me"' in text
    assert '<option value="Blue" selected>' in text
    assert '<option value="true" selected>' in text


@pytest.mark.parametrize("category_id", ["", "abc", "999999"])
def test_create_requires_category_of_this_store(admin_client, widgets_category, db_session, category_id):
    response = admin_client.post(PRODUCTS, data=_product_data(category_id))
    assert response.status_code == 400
    assert en("admin.pick_category.invalid") in page_text(response)
    assert _product_count(db_session) == 0


def test_create_with_foreign_category_is_rejected(admin_client, widgets_category, db_session):
    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.flush()
    foreign = Category(store_id=other.id, slug="foreign", name="Foreign")
    db_session.add(foreign)
    db_session.commit()
    response = admin_client.post(PRODUCTS, data=_product_data(foreign.id))
    assert response.status_code == 400
    assert _product_count(db_session) == 0


def test_slug_unique_per_store(admin_client, widgets_category, db_session):
    _create_id(admin_client, widgets_category.id)
    duplicate = _create(admin_client, widgets_category.id, name="Another")
    assert duplicate.status_code == 400
    assert en("admin.product.error.slug_taken") in duplicate.text

    # Same slug in another store is fine.
    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.commit()
    response = admin_client.post("/admin/stores/other-store/categories", data={"slug": "c", "name": "C"},
                                 follow_redirects=False)
    other_category = int(response.headers["location"].rsplit("/", 1)[1])
    response = admin_client.post("/admin/stores/other-store/products",
                                 data={"category_id": str(other_category), "slug": "widget-blue-m", "sku": "W-1", "stock": "1",
                                       "name": "W", "price": "1"}, follow_redirects=False)
    assert response.status_code == 303


def test_insert_race_on_duplicate_slug_is_reported(admin_client, widgets_category, monkeypatch):
    first = _create_id(admin_client, widgets_category.id)
    second = _create_id(admin_client, widgets_category.id, slug="widget-red-s", sku="WR-100-S", name="Widget Red S")
    real_validate = svc.validate_product

    def validate_without_errors(*args, **kwargs):
        result = real_validate(*args, **kwargs)
        args[3].errors.clear()  # pretend the duplicate was not seen yet
        return result

    monkeypatch.setattr(svc, "validate_product", validate_without_errors)
    created = _create(admin_client, widgets_category.id)
    assert created.status_code == 400
    assert en("admin.product.error.slug_taken") in created.text

    # Same race on update: renaming the second product to the taken slug.
    updated = admin_client.post(f"{PRODUCTS}/{second}", data=_product_data(widgets_category.id))
    assert updated.status_code == 400
    assert en("admin.product.error.slug_taken") in updated.text
    assert admin_client.get(API_PRODUCT).json()["id"] == first


# ----------------------------------------------------------------------------- edit


def test_edit_form_is_prefilled(admin_client, widgets_category):
    product_id = _create_id(admin_client, widgets_category.id, prop_brand=str(widgets_category.acme_id),
                            prop_notes="Fragile", status="hidden", price="5")
    text = admin_client.get(f"{PRODUCTS}/{product_id}").text
    assert 'value="widget-blue-m"' in text
    assert 'value="5.00"' in text
    assert ">A blue widget, size M.</textarea>" in text
    assert '<option value="hidden" selected>' in text
    assert '<option value="Blue" selected>' in text
    assert '<option value="M" selected>' in text
    assert 'value="250"' in text
    assert '<option value="true" selected>' in text
    assert 'value="/static/widget-blue.png"' in text
    assert '<img src="/static/widget-blue.png"' in text
    assert f'<option value="{widgets_category.acme_id}" selected>' in text
    assert 'value="Fragile"' in text
    assert en("admin.product.choose_another") not in text  # category is fixed after creation


def test_update_product_and_values(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id)
    response = admin_client.post(
        f"{PRODUCTS}/{product_id}",
        data=_product_data(
            widgets_category.id,
            name="Widget Blue M (v2)",
            price="24.50",
            description="",
            prop_color="Green",  # changed
            prop_weight_g="",  # cleared -> row deleted
            prop_waterproof="false",  # changed
            prop_notes="New note",  # added
        ),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"{PRODUCTS}/{product_id}"

    body = admin_client.get(API_PRODUCT).json()
    assert body["name"] == "Widget Blue M (v2)"
    assert body["price_cents"] == 2450
    assert body["description"] is None
    props = {p["key"]: p["value"] for p in body["properties"]}
    assert props["color"] == {"value": "Green"}
    assert props["weight_g"] == {}
    assert props["waterproof"] == {"value": False}
    assert props["notes"] == {"value": "New note"}
    assert props["size"] == {"value": "M"}
    rows = db_session.scalars(select(PropertyValue).where(PropertyValue.product_id == product_id)).all()
    assert {r.definition_id for r in rows} == {
        d.id for d in db_session.scalars(select(PropertyDefinition).where(
            PropertyDefinition.category_id == widgets_category.id,
            PropertyDefinition.key.in_(["color", "size", "waterproof", "photo", "notes"]))).all()
    }
    assert en("flash.product_saved", name="Widget Blue M (v2)") in page_text(admin_client.get(f"{PRODUCTS}/{product_id}"))


def test_update_ignores_posted_category(admin_client, widgets_category):
    product_id = _create_id(admin_client, widgets_category.id)
    response = admin_client.post(f"{PRODUCTS}/{product_id}",
                                 data=_product_data(widgets_category.brands_id), follow_redirects=False)
    assert response.status_code == 303
    assert admin_client.get(API_PRODUCT).json()["category_id"] == widgets_category.id


def test_update_validation_keeps_product_unchanged(admin_client, widgets_category):
    product_id = _create_id(admin_client, widgets_category.id)
    response = admin_client.post(f"{PRODUCTS}/{product_id}",
                                 data=_product_data(widgets_category.id, name="", prop_color="Purple"))
    assert response.status_code == 400
    assert en("admin.product.error.name_required") in response.text
    assert en("admin.product.error.enum_choice") in response.text
    body = admin_client.get(API_PRODUCT).json()
    assert body["name"] == "Widget Blue M"
    assert {p["key"]: p["value"] for p in body["properties"]}["color"] == {"value": "Blue"}


def test_hiding_a_product(admin_client, widgets_category):
    product_id = _create_id(admin_client, widgets_category.id)
    response = admin_client.post(f"{PRODUCTS}/{product_id}", data=_product_data(widgets_category.id, status="hidden"),
                                 follow_redirects=False)
    assert response.status_code == 303
    assert admin_client.get(API_PRODUCT).status_code == 404
    assert admin_client.get("/api/stores/demo-widgets/products").json() == []
    assert en("status.hidden") in page_text(admin_client.get(PRODUCTS))
    # Still editable in admin.
    assert admin_client.get(f"{PRODUCTS}/{product_id}").status_code == 200


def test_removed_enum_option_is_flagged_on_edit(admin_client, widgets_category):
    product_id = _create_id(admin_client, widgets_category.id)  # color = Blue
    definitions = {d["key"]: d for d in next(
        c for c in admin_client.get("/api/stores/demo-widgets/categories").json() if c["slug"] == "widgets"
    )["property_definitions"]}
    admin_client.post(f"{STORE}/categories/{widgets_category.id}/properties/{definitions['color']['id']}",
                      data={"key": "color", "label": "Color", "prop_type": "enum", "options": "Red\nGreen"})

    page = admin_client.get(f"{PRODUCTS}/{product_id}").text
    assert f'<option value="Blue" selected>Blue {en("admin.product.no_longer_option")}</option>' in page
    response = admin_client.post(f"{PRODUCTS}/{product_id}", data=_product_data(widgets_category.id))
    assert response.status_code == 400
    assert en("admin.product.error.enum_choice") in response.text


# --------------------------------------------------------------------------- delete


def test_delete_product(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id)
    url = f"{PRODUCTS}/{product_id}/delete"

    confirm = admin_client.get(url)
    assert confirm.status_code == 200
    assert en("admin.product.delete_title", name="Widget Blue M") in page_text(confirm)
    # color, size, weight_g, waterproof, photo (brand / notes are empty -> not stored)
    assert en("admin.product.delete_consequence", count=5) in page_text(confirm)

    response = admin_client.post(url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == PRODUCTS
    assert en("flash.product_deleted", name="Widget Blue M") in page_text(admin_client.get(PRODUCTS))
    assert admin_client.get(API_PRODUCT).status_code == 404
    assert admin_client.get(f"{PRODUCTS}/{product_id}").status_code == 404
    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(PropertyValue)) == 0

    # With its last product gone, the category can be deleted.
    deleted = admin_client.post(f"{STORE}/categories/{widgets_category.id}/delete", follow_redirects=False)
    assert deleted.status_code == 303


def test_delete_product_without_values(admin_client, demo_store):
    response = admin_client.post(f"{STORE}/categories", data={"slug": "misc", "name": "Misc"}, follow_redirects=False)
    category_id = int(response.headers["location"].rsplit("/", 1)[1])
    product_id = _create_id(admin_client, category_id, slug="thing")
    confirm = page_text(admin_client.get(f"{PRODUCTS}/{product_id}/delete"))
    assert not pattern("en", "admin.product.delete_consequence").search(confirm)
    assert admin_client.post(f"{PRODUCTS}/{product_id}/delete", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ scoping and auth


def test_product_of_other_store_is_404(admin_client, widgets_category, db_session):
    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.flush()
    category = Category(store_id=other.id, slug="c", name="C")
    db_session.add(category)
    db_session.flush()
    foreign = Product(store_id=other.id, category_id=category.id, slug="foreign", name="Foreign")
    db_session.add(foreign)
    db_session.commit()
    url = f"{PRODUCTS}/{foreign.id}"

    assert admin_client.get(url).status_code == 404
    assert admin_client.post(url, data=_product_data(widgets_category.id)).status_code == 404
    assert admin_client.get(f"{url}/delete").status_code == 404
    assert admin_client.post(f"{url}/delete").status_code == 404
    db_session.expire_all()
    assert db_session.get(Product, foreign.id).name == "Foreign"


def test_unknown_store_is_404(admin_client):
    assert admin_client.get("/admin/stores/nope/products").status_code == 404
    assert admin_client.post("/admin/stores/nope/products", data={"category_id": "1"}).status_code == 404


def test_product_routes_require_admin(client, widgets_category, db_session):
    product_id = _create_id(client, widgets_category.id)
    client.post("/logout")
    for method, path in [
        ("get", PRODUCTS),
        ("get", f"{PRODUCTS}/new"),
        ("post", PRODUCTS),
        ("get", f"{PRODUCTS}/{product_id}"),
        ("post", f"{PRODUCTS}/{product_id}"),
        ("get", f"{PRODUCTS}/{product_id}/delete"),
        ("post", f"{PRODUCTS}/{product_id}/delete"),
    ]:
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")

    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert client.get(PRODUCTS).status_code == 403
    assert client.post(PRODUCTS, data=_product_data(widgets_category.id, slug="x")).status_code == 403
    assert client.post(f"{PRODUCTS}/{product_id}/delete").status_code == 403
    assert _product_count(db_session) == 1


# --------------------------------------------------------------------- unit helpers


def _definition(prop_type, **kwargs):
    return PropertyDefinition(id=1, key="k", label="K", prop_type=prop_type, required=False,
                              config=kwargs.pop("config", {}), **kwargs)


@pytest.mark.parametrize(
    "prop_type,stored,expected",
    [
        (PropertyType.text, {"value": "abc"}, "abc"),
        (PropertyType.number, {"value": 2.5}, "2.5"),
        (PropertyType.boolean, {"value": True}, "true"),
        (PropertyType.boolean, {"value": False}, "false"),
        (PropertyType.enum, {"value": "M"}, "M"),
        (PropertyType.image, {"media_path": "/x.png"}, "/x.png"),
        (PropertyType.image, {}, ""),
        (PropertyType.category_ref, {"category_id": 4}, "4"),
        (PropertyType.category_ref, {"category_id": None}, ""),
        (PropertyType.text, {"value": None}, ""),
        (PropertyType.text, None, ""),
    ],
)
def test_value_to_form(prop_type, stored, expected):
    assert svc.value_to_form(_definition(prop_type), stored) == expected


def test_enum_without_configured_options_rejects_values():
    payload, error = svc.parse_value(_definition(PropertyType.enum, config={}), "S", [])
    assert payload is None and error == ("enum_choice", {})


@pytest.mark.parametrize("raw,cents", [("19.99", 1999), ("19.990", 1999), ("1,299.50", 129950), ("0", 0),
                                       ("7", 700)])
def test_parse_price(raw, cents):
    errors: dict[str, str] = {}
    assert svc.parse_price(raw, errors) == cents
    assert errors == {}


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "1e99999"])
def test_parse_price_rejects_special_numbers(raw):
    errors: dict[str, str] = {}
    svc.parse_price(raw, errors)
    assert errors["price"] == ("price_format", {})


def test_unrestricted_category_ref_offers_every_category(admin_client, widgets_category):
    url = f"{STORE}/categories/{widgets_category.id}/properties"
    admin_client.post(url, data={"key": "related", "label": "Related", "prop_type": "category_ref"})
    form = admin_client.get(f"{PRODUCTS}/new?category_id={widgets_category.id}").text
    select_html = form[form.index('name="prop_related"'):form.index("</select>", form.index('name="prop_related"'))]
    for category_id in (widgets_category.brands_id, widgets_category.acme_id, widgets_category.id):
        assert f'value="{category_id}"' in select_html

    _create_id(admin_client, widgets_category.id, prop_related=str(widgets_category.id))
    assert _props(admin_client)["related"]["value"] == {"category_id": widgets_category.id}


# ------------------------------------------------------------------------------ SKU
# The store's own product code: required in the admin form, unique per store (case-insensitive:
# stored upper-case), the same code allowed in another store.

SKU_REQUIRED = en("admin.product.error.sku_required")
SKU_FORMAT = en("admin.product.error.sku_format", max_len=64)
WB_TAKEN = en("admin.product.error.sku_taken", sku="WB-100-M", name="Widget Blue M", slug="widget-blue-m")


def _sku_in_db(db_session, product_id: int):
    db_session.expire_all()
    return db_session.get(Product, product_id).sku


def _other_store_category(admin_client, slug: str = "other-store") -> int:
    """A second store + category, created through admin like demo-widgets."""
    assert admin_client.post("/admin/stores", data={"slug": slug, "name": "Other", "currency": "EUR"},
                             follow_redirects=False).status_code == 303
    response = admin_client.post(f"/admin/stores/{slug}/categories", data={"slug": "gadgets", "name": "Gadgets"},
                                 follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_create_product_with_sku(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id, sku="  wb-100-m  ")
    assert _sku_in_db(db_session, product_id) == "WB-100-M"  # trimmed, upper-case

    form = admin_client.get(f"{PRODUCTS}/{product_id}").text
    assert '<input id="sku" name="sku" value="WB-100-M"' in form
    listing = admin_client.get(PRODUCTS).text
    assert f"<th>{en('field.sku')}</th>" in listing
    assert "<td><code>WB-100-M</code></td>" in listing


def test_new_product_form_has_an_empty_sku_field(admin_client, widgets_category):
    form = admin_client.get(f"{PRODUCTS}/new?category_id={widgets_category.id}").text
    assert '<input id="sku" name="sku" value=""' in form
    assert en("hint.sku") in page_text(admin_client.get(f"{PRODUCTS}/new?category_id={widgets_category.id}"))


@pytest.mark.parametrize(
    "sku,message",
    [
        ("", SKU_REQUIRED),
        ("   ", SKU_REQUIRED),
        ("WB 100", SKU_FORMAT),  # no spaces inside
        ("-WB-100", SKU_FORMAT),  # starts with a letter or digit
        ("WB/100", SKU_FORMAT),
        ("ſku", SKU_FORMAT),  # non-ASCII, even if upper() would make it "SKU"
        ("W" * 65, SKU_FORMAT),
    ],
)
def test_create_rejects_empty_or_malformed_sku(admin_client, widgets_category, db_session, sku, message):
    response = _create(admin_client, widgets_category.id, sku=sku)
    assert response.status_code == 400
    assert message in response.text
    assert 'id="sku-error"' in response.text
    assert 'aria-describedby="sku-error"' in response.text
    assert _product_count(db_session) == 0


def test_create_without_sku_field_is_rejected(admin_client, widgets_category, db_session):
    data = _product_data(widgets_category.id)
    del data["sku"]  # e.g. an old curl call
    response = admin_client.post(PRODUCTS, data=data)
    assert response.status_code == 400
    assert SKU_REQUIRED in response.text
    assert _product_count(db_session) == 0


@pytest.mark.parametrize("sku", ["WB-100-M", "wb-100-m", " Wb-100-M "])
def test_create_rejects_duplicate_sku_in_the_store(admin_client, widgets_category, db_session, sku):
    _create_id(admin_client, widgets_category.id)  # WB-100-M
    response = _create(admin_client, widgets_category.id, slug="widget-red-s", name="Widget Red S", sku=sku)
    assert response.status_code == 400
    assert page_text(response).count(WB_TAKEN) == 1
    assert 'id="sku-error"' in response.text
    assert 'id="slug-error"' not in response.text  # the slug is free: only the SKU is reported
    assert 'value="widget-red-s"' in response.text  # the form keeps the input
    assert _product_count(db_session) == 1


def test_edit_rejects_empty_sku(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id)
    response = admin_client.post(f"{PRODUCTS}/{product_id}", data=_product_data(widgets_category.id, sku=""))
    assert response.status_code == 400
    assert SKU_REQUIRED in response.text
    assert _sku_in_db(db_session, product_id) == "WB-100-M"


def test_edit_rejects_sku_of_another_product_in_the_store(admin_client, widgets_category, db_session):
    _create_id(admin_client, widgets_category.id)  # WB-100-M
    second = _create_id(admin_client, widgets_category.id, slug="widget-red-s", name="Widget Red S", sku="WR-100-S")
    response = admin_client.post(f"{PRODUCTS}/{second}", data=_product_data(
        widgets_category.id, slug="widget-red-s", name="Widget Red S", sku="wb-100-m"))
    assert response.status_code == 400
    assert WB_TAKEN in page_text(response)
    assert _sku_in_db(db_session, second) == "WR-100-S"


def test_edit_can_keep_or_change_its_own_sku(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id)
    url = f"{PRODUCTS}/{product_id}"
    same = admin_client.post(url, data=_product_data(widgets_category.id, name="Renamed"), follow_redirects=False)
    assert same.status_code == 303  # its own SKU is not a duplicate
    changed = admin_client.post(url, data=_product_data(widgets_category.id, sku="wb-200"), follow_redirects=False)
    assert changed.status_code == 303
    assert _sku_in_db(db_session, product_id) == "WB-200"
    # The old code is free again.
    _create_id(admin_client, widgets_category.id, slug="widget-new", sku="WB-100-M")


def test_same_sku_is_allowed_in_another_store(admin_client, widgets_category, db_session):
    first = _create_id(admin_client, widgets_category.id)  # demo-widgets, WB-100-M
    other_category = _other_store_category(admin_client)
    response = admin_client.post("/admin/stores/other-store/products", data={
        "category_id": str(other_category), "slug": "gadget", "name": "Gadget", "price": "1", "sku": "wb-100-m", "stock": "1",
    }, follow_redirects=False)
    assert response.status_code == 303
    second = int(response.headers["location"].rsplit("/", 1)[1])

    db_session.expire_all()
    rows = db_session.execute(select(Product.store_id, Product.sku).where(Product.id.in_([first, second]))).all()
    assert [sku for _, sku in rows] == ["WB-100-M", "WB-100-M"]
    assert len({store_id for store_id, _ in rows}) == 2
    # Each store's list shows it; the duplicate check stays inside each store.
    assert "<td><code>WB-100-M</code></td>" in admin_client.get("/admin/stores/other-store/products").text
    again = admin_client.post("/admin/stores/other-store/products", data={
        "category_id": str(other_category), "slug": "gadget-2", "name": "Gadget 2", "price": "1", "sku": "WB-100-M", "stock": "12",
    })
    assert again.status_code == 400
    assert en("admin.product.error.sku_taken", sku="WB-100-M", name="Gadget", slug="gadget") in page_text(again)


def test_products_without_sku_must_get_one_on_edit(admin_client, widgets_category, db_session):
    """Products created without the admin form (store packs, older databases) have no SKU: several
    can coexist, the list says so, and saving one in admin requires a SKU."""
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    misc = Category(store_id=store.id, slug="misc", name="Misc")
    db_session.add(misc)
    db_session.flush()
    legacy = [Product(store_id=store.id, category_id=misc.id, slug=f"legacy-{i}", name=f"Legacy {i}") for i in (1, 2)]
    db_session.add_all(legacy)
    db_session.commit()  # two NULL SKUs in one store: no clash

    listing = page_text(admin_client.get(PRODUCTS))
    assert en("value.not_set") in listing
    url = f"{PRODUCTS}/{legacy[0].id}"
    assert '<input id="sku" name="sku" value=""' in admin_client.get(url).text
    data = {"slug": "legacy-1", "name": "Legacy 1", "price": "1", "stock": "0"}
    refused = admin_client.post(url, data=data)
    assert refused.status_code == 400 and SKU_REQUIRED in refused.text
    assert _sku_in_db(db_session, legacy[0].id) is None
    assert admin_client.post(url, data={**data, "sku": "LEG-1"}, follow_redirects=False).status_code == 303
    assert _sku_in_db(db_session, legacy[0].id) == "LEG-1"


def test_insert_race_on_duplicate_sku_is_reported(admin_client, widgets_category, monkeypatch):
    """Another request takes the SKU between validation and the insert: the database refuses it and
    the form names the SKU (not the slug)."""
    _create_id(admin_client, widgets_category.id)  # WB-100-M
    second = _create_id(admin_client, widgets_category.id, slug="widget-red-s", sku="WR-100-S", name="Widget Red S")
    real_validate = svc.validate_product

    def validate_without_errors(*args, **kwargs):
        result = real_validate(*args, **kwargs)
        args[3].errors.clear()  # pretend the duplicate was not seen yet
        return result

    monkeypatch.setattr(svc, "validate_product", validate_without_errors)
    created = _create(admin_client, widgets_category.id, slug="widget-green-l")
    assert created.status_code == 400
    assert WB_TAKEN in page_text(created)
    assert en("admin.product.error.slug_taken") not in created.text

    updated = admin_client.post(f"{PRODUCTS}/{second}", data=_product_data(
        widgets_category.id, slug="widget-red-s", name="Widget Red S"))
    assert updated.status_code == 400
    assert WB_TAKEN in page_text(updated)


def test_database_enforces_sku_unique_per_store(db_session):
    """The constraint itself, below the form: same store refused, other store fine, NULLs never clash."""
    from sqlalchemy.exc import IntegrityError

    stores = [Store(slug="s1", name="S1"), Store(slug="s2", name="S2")]
    db_session.add_all(stores)
    db_session.flush()
    categories = [Category(store_id=s.id, slug="c", name="C") for s in stores]
    db_session.add_all(categories)
    db_session.flush()

    def product(i: int, slug: str, sku):
        return Product(store_id=stores[i].id, category_id=categories[i].id, slug=slug, name=slug, sku=sku)

    db_session.add_all([product(0, "a", "X-1"), product(1, "a", "X-1"), product(0, "n1", None), product(0, "n2", None)])
    db_session.commit()
    db_session.add(product(0, "b", "X-1"))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_older_databases_get_the_sku_column_at_startup():
    """A products table created before ``sku`` existed: init_db (run at every start) adds the column
    (existing products get none) and its per-store unique index; running it again changes nothing."""
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.pool import StaticPool

    from app.db import init_db
    from app.schema_upgrades import add_product_sku

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:  # the table as the previous version created it
        connection.execute(text(
            "CREATE TABLE products (id INTEGER PRIMARY KEY, store_id INTEGER, category_id INTEGER, slug VARCHAR(64), "
            "name VARCHAR(200), description TEXT, price_cents INTEGER, is_active BOOLEAN, created_at DATETIME, "
            "CONSTRAINT uq_product_store_slug UNIQUE (store_id, slug))"))
        connection.execute(text("INSERT INTO products (id, store_id, category_id, slug, name, price_cents, is_active) "
                                "VALUES (1, 1, 1, 'a', 'A', 100, 1), (2, 1, 1, 'b', 'B', 200, 1)"))

    init_db(bind=engine)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT id, sku FROM products ORDER BY id")).all() == [(1, None), (2, None)]
    indexes = {i["name"]: i for i in inspect(engine).get_indexes("products")}
    assert indexes["uq_product_store_sku"]["unique"]
    assert indexes["uq_product_store_sku"]["column_names"] == ["store_id", "sku"]
    with engine.begin() as connection:
        connection.execute(text("UPDATE products SET sku = 'X-1' WHERE id = 1"))
        connection.execute(text("INSERT INTO products (id, store_id, category_id, slug, name, sku) "
                                "VALUES (3, 2, 1, 'a', 'A', 'X-1')"))  # other store: fine
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("UPDATE products SET sku = 'X-1' WHERE id = 2"))  # same store: refused

    assert add_product_sku(engine) is False  # already upgraded: no-op
    assert add_product_sku(create_engine("sqlite://")) is False  # no products table yet


# ---------------------------------------------------------------------------- stock
# Units on hand: required in the admin form (create and edit), a whole number from 0 up; the
# database refuses negatives too.

STOCK_NEGATIVE = en("admin.product.error.stock_negative")
STOCK_REQUIRED = en("admin.product.error.stock_required")
STOCK_FORMAT = en("admin.product.error.stock_format")
STOCK_TOO_LARGE = en("admin.product.error.stock_too_large")


def _stock_in_db(db_session, product_id: int):
    db_session.expire_all()
    return db_session.get(Product, product_id).stock_on_hand


def test_create_product_with_stock(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id, stock=" 25 ")
    assert _stock_in_db(db_session, product_id) == 25

    # Shown on the product's admin page (edit form) and in the admin list.
    form = admin_client.get(f"{PRODUCTS}/{product_id}").text
    assert '<input id="stock" name="stock" value="25"' in form
    listing = admin_client.get(PRODUCTS).text
    assert f"<th>{en('field.stock')}</th>" in listing
    assert '<td class="number">25</td>' in listing


def test_zero_stock_is_allowed(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id, stock="0")
    assert _stock_in_db(db_session, product_id) == 0
    assert f'<td class="number">0 <span class="meta">· {en("stock.out_of_stock")}</span></td>' in admin_client.get(PRODUCTS).text


def test_new_product_form_starts_at_zero_stock(admin_client, widgets_category):
    page = admin_client.get(f"{PRODUCTS}/new?category_id={widgets_category.id}")
    assert '<input id="stock" name="stock" value="0"' in page.text
    assert en("hint.stock") in page_text(page)


def test_edit_sets_stock(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id)  # 12
    url = f"{PRODUCTS}/{product_id}"
    for raw, expected in (("40", 40), ("0", 0), ("7", 7)):
        response = admin_client.post(url, data=_product_data(widgets_category.id, stock=raw), follow_redirects=False)
        assert response.status_code == 303, raw
        assert _stock_in_db(db_session, product_id) == expected
    assert '<input id="stock" name="stock" value="7"' in admin_client.get(url).text


@pytest.mark.parametrize("raw", ["-1", "-25", " -3 ", "-0000001"])
def test_create_rejects_negative_stock(admin_client, widgets_category, db_session, raw):
    response = _create(admin_client, widgets_category.id, stock=raw)
    assert response.status_code == 400
    assert STOCK_NEGATIVE in response.text
    assert 'id="stock-error"' in response.text
    assert 'aria-describedby="stock-error"' in response.text
    assert f'value="{raw.strip()}"' in response.text  # the form keeps what was typed
    assert _product_count(db_session) == 0


def test_edit_rejects_negative_stock(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id)  # 12
    response = admin_client.post(f"{PRODUCTS}/{product_id}",
                                 data=_product_data(widgets_category.id, stock="-5", name="Renamed"))
    assert response.status_code == 400
    assert STOCK_NEGATIVE in response.text
    assert '<input id="stock" name="stock" value="-5"' in response.text
    db_session.expire_all()
    product = db_session.get(Product, product_id)
    assert (product.stock_on_hand, product.name) == (12, "Widget Blue M")  # nothing saved


@pytest.mark.parametrize(
    "raw,message",
    [
        ("", STOCK_REQUIRED),
        ("   ", STOCK_REQUIRED),
        ("2.5", STOCK_FORMAT),
        ("1,000", STOCK_FORMAT),
        ("1.000", STOCK_FORMAT),
        ("1_000", STOCK_FORMAT),  # int() would accept it
        ("١٢", STOCK_FORMAT),  # non-ASCII digits: int() would accept them too
        ("1e3", STOCK_FORMAT),
        ("+5", STOCK_FORMAT),
        ("ten", STOCK_FORMAT),
        ("- 5", STOCK_FORMAT),
        ("2147483648", STOCK_TOO_LARGE),
        ("9" * 21, STOCK_TOO_LARGE),
    ],
)
def test_stock_validation(admin_client, widgets_category, db_session, raw, message):
    response = _create(admin_client, widgets_category.id, stock=raw)
    assert response.status_code == 400
    assert message in page_text(response)
    assert 'id="stock-error"' in response.text
    assert _product_count(db_session) == 0


def test_largest_stock_is_accepted(admin_client, widgets_category, db_session):
    product_id = _create_id(admin_client, widgets_category.id, stock="2147483647")
    assert _stock_in_db(db_session, product_id) == 2_147_483_647


def test_post_without_stock_field_never_zeroes_it(admin_client, widgets_category, db_session):
    """Edits replace every field: a post that omits stock is refused instead of wiping it."""
    product_id = _create_id(admin_client, widgets_category.id)  # 12
    data = _product_data(widgets_category.id)
    del data["stock"]
    response = admin_client.post(f"{PRODUCTS}/{product_id}", data=data)
    assert response.status_code == 400
    assert STOCK_REQUIRED in response.text
    assert _stock_in_db(db_session, product_id) == 12
    assert admin_client.post(PRODUCTS, data={**data, "slug": "other", "sku": "OTHER"}).status_code == 400


def test_database_refuses_negative_stock(db_session):
    """Below the form: the CHECK constraint. Products created without stock get 0."""
    from sqlalchemy.exc import IntegrityError

    store = Store(slug="s1", name="S1")
    db_session.add(store)
    db_session.flush()
    category = Category(store_id=store.id, slug="c", name="C")
    db_session.add(category)
    db_session.flush()
    plain = Product(store_id=store.id, category_id=category.id, slug="plain", name="Plain")
    db_session.add(plain)
    db_session.commit()
    assert plain.stock_on_hand == 0

    db_session.add(Product(store_id=store.id, category_id=category.id, slug="neg", name="Neg", stock_on_hand=-1))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_pack_products_start_at_zero_and_reimport_keeps_stock(db_session):
    """Packs don't carry stock: new products get 0, and a re-import leaves the stock set in admin."""
    import json
    from pathlib import Path

    from app.services.store_pack_import import import_store_pack

    pack = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "packs"
                       / "clothing-demo.store-pack.v1.json").read_text(encoding="utf-8"))
    import_store_pack(db_session, pack)
    slug = pack["products"][0]["slug"]
    product = db_session.scalar(select(Product).where(Product.slug == slug))
    assert product.stock_on_hand == 0
    product.stock_on_hand = 9
    db_session.commit()

    import_store_pack(db_session, pack)
    assert _stock_in_db(db_session, product.id) == 9


def test_older_databases_get_the_stock_column_at_startup():
    """A products table created before ``stock_on_hand`` existed: init_db (run at every start) adds
    it with 0 for existing products and refuses negatives; running it again changes nothing."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.pool import StaticPool

    from app.db import init_db
    from app.schema_upgrades import add_product_stock

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:  # the table as the version before stock created it
        connection.execute(text(
            "CREATE TABLE products (id INTEGER PRIMARY KEY, store_id INTEGER, category_id INTEGER, slug VARCHAR(64), "
            "sku VARCHAR(64), name VARCHAR(200), description TEXT, price_cents INTEGER, is_active BOOLEAN, "
            "created_at DATETIME, CONSTRAINT uq_product_store_slug UNIQUE (store_id, slug), "
            "CONSTRAINT uq_product_store_sku UNIQUE (store_id, sku))"))
        connection.execute(text("INSERT INTO products (id, store_id, category_id, slug, name, price_cents, is_active) "
                                "VALUES (1, 1, 1, 'a', 'A', 100, 1), (2, 1, 1, 'b', 'B', 200, 1)"))

    init_db(bind=engine)

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, stock_on_hand FROM products ORDER BY id")).all()
    assert rows == [(1, 0), (2, 0)]
    with engine.begin() as connection:
        connection.execute(text("UPDATE products SET stock_on_hand = 5 WHERE id = 1"))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text("UPDATE products SET stock_on_hand = -1 WHERE id = 2"))

    assert add_product_stock(engine) is False  # already upgraded: no-op
    assert add_product_stock(create_engine("sqlite://")) is False  # no products table yet


# ------------------------------------------------------------- error messages in the page language
# The admin is Spanish unless ?lang= / the cookie says otherwise: the product form's validation
# messages follow it (they used to be English on Spanish pages, e.g. a negative stock).


@pytest.fixture()
def default_lang_admin(default_lang_client):
    """Admin session with no language chosen: admin pages are in Spanish."""
    response = default_lang_client.post("/login", data={"email": "admin@example.com",
                                                        "password": "ChangeMeAdmin123!"}, follow_redirects=False)
    assert response.status_code == 303
    return default_lang_client


@pytest.fixture()
def spanish_store(default_lang_admin):
    """A Spanish store (locale es, like solares) with a category that has a required enum.
    Returns (products URL, category id)."""
    client = default_lang_admin
    assert client.post("/admin/stores", data={"slug": "tienda", "name": "Tienda", "currency": "MXN"},
                       follow_redirects=False).status_code == 303
    response = client.post("/admin/stores/tienda/categories", data={"slug": "gas", "name": "Gas"},
                           follow_redirects=False)
    category_id = int(response.headers["location"].rsplit("/", 1)[1])
    client.post(f"/admin/stores/tienda/categories/{category_id}/properties",
                data={"key": "tipo", "label": "Tipo de gas", "prop_type": "enum",
                      "options": "LP\nNatural", "required": "1"})
    return "/admin/stores/tienda/products", category_id


def _spanish_product(category_id, **overrides):
    return {"category_id": str(category_id), "slug": "boiler", "sku": "B-1", "name": "Boiler", "price": "100",
            "stock": "3", "prop_tipo": "LP", **overrides}


def test_negative_stock_error_is_in_spanish_by_default(default_lang_admin, spanish_store, db_session):
    products, category_id = spanish_store
    response = default_lang_admin.post(products, data=_spanish_product(category_id, stock="-1"))
    assert response.status_code == 400
    text = page_text(response)
    assert es("admin.product.error.stock_negative") in text
    assert en("admin.product.error.stock_negative") not in text
    assert f'id="stock-error">{es("admin.product.error.stock_negative")}</p>' in response.text
    assert _product_count(db_session) == 0

    # Same on edit.
    product_url = default_lang_admin.post(products, data=_spanish_product(category_id),
                                          follow_redirects=False).headers["location"]
    edit = default_lang_admin.post(product_url, data=_spanish_product(category_id, stock="-5"))
    assert edit.status_code == 400
    assert es("admin.product.error.stock_negative") in page_text(edit)
    assert en("admin.product.error.stock_negative") not in page_text(edit)


def test_every_product_error_follows_the_page_language(default_lang_admin, spanish_store):
    """One post with a problem in every kind of field: each message in the page's language, under
    its field; the same post with ?lang=en answers in English."""
    products, category_id = spanish_store
    bad = _spanish_product(category_id, slug="", sku="", name="", price="-1", stock="-1", status="x", prop_tipo="")
    expected = {
        "slug": ("slug_required", {}),
        "sku": ("sku_required", {}),
        "name": ("name_required", {}),
        "price": ("price_negative", {}),
        "stock": ("stock_negative", {}),
        "status": ("status_invalid", {}),
        "prop_tipo": ("value_required", {"label": "Tipo de gas"}),
    }
    for locale, url, lookup in (("es", products, es), ("en", f"{products}?lang=en", en)):
        response = default_lang_admin.post(url, data=bad)
        assert response.status_code == 400, locale
        text = page_text(response)
        for field, (code, params) in expected.items():
            assert lookup(f"admin.product.error.{code}", **params) in text, (locale, field)
            assert f'id="{field}-error"' in response.text, (locale, field)


def test_duplicate_sku_error_is_in_spanish_and_escaped(default_lang_admin, spanish_store):
    products, category_id = spanish_store
    default_lang_admin.post(products, data=_spanish_product(category_id, name="<b>Boiler</b>"))
    response = default_lang_admin.post(products, data=_spanish_product(category_id, slug="otro"))
    assert response.status_code == 400
    assert es("admin.product.error.sku_taken", sku="B-1", name="<b>Boiler</b>", slug="boiler") in page_text(response)
    assert "<b>Boiler</b>" not in response.text  # the product name is text, not HTML


def test_every_error_code_of_the_service_has_a_message():
    """A code the service can return but the router can't show would be a 500: every one is mapped,
    and its text differs between Spanish and English (so it really is translated)."""
    import inspect
    import re

    from app.i18n import translator
    from app.routers.admin_products import field_error_text

    source = inspect.getsource(svc)
    codes = set(re.findall(r'\(\s*"([a-z_]+)",\s*\{', source))
    assert {"stock_negative", "sku_taken", "value_required", "price_format"} <= codes  # the scan works
    params = {"max_len": 64, "label": "Tipo", "sku": "B-1", "name": "Boiler", "slug": "boiler"}
    for code in codes:
        spanish = field_error_text(translator("es"), (code, params))
        english = field_error_text(translator("en"), (code, params))
        assert spanish == es(f"admin.product.error.{code}", **params), code
        assert spanish != english, code
