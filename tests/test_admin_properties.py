"""Admin: property definitions (create / edit / delete) on demo-widgets categories."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.auth import create_user
from app.models import Category, Product, PropertyDefinition, PropertyValue, Store, UserRole
from app.services import category_admin as svc
from tests.ui_text import en, en_html, page_text, pattern

STORE = "/admin/stores/demo-widgets"
API_CATEGORIES = "/api/stores/demo-widgets/categories"


# ----------------------------------------------------------------------------- helpers


def _create_category(client, slug="gadgets", name="Gadgets", **extra) -> int:
    response = client.post(f"{STORE}/categories", data={"slug": slug, "name": name, **extra}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _add_property(client, category_id, **fields):
    data = {"key": "color", "label": "Color", "prop_type": "text", **fields}
    return client.post(f"{STORE}/categories/{category_id}/properties", data=data, follow_redirects=False)


def _api_category(client, slug="gadgets") -> dict:
    return next(c for c in client.get(API_CATEGORIES).json() if c["slug"] == slug)


def _api_defs(client, slug="gadgets") -> dict[str, dict]:
    return {d["key"]: d for d in _api_category(client, slug)["property_definitions"]}


def _definition_id(client, key, slug="gadgets") -> int:
    return _api_defs(client, slug)[key]["id"]


def _add_product_value(db_session, category_id, definition_id, value):
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    product = Product(store_id=store.id, category_id=category_id, slug="widget-1", name="Widget 1", price_cents=100)
    db_session.add(product)
    db_session.flush()
    db_session.add(PropertyValue(product_id=product.id, definition_id=definition_id, value=value))
    db_session.commit()


# ------------------------------------------------------------------ requested scenario


def test_category_with_text_and_enum_defs_shows_in_api(admin_client, demo_store):
    category_id = _create_category(admin_client, slug="gadgets", name="Gadgets")

    text = _add_property(admin_client, category_id, key="model_name", label="Model name", prop_type="text",
                         required="1", sort_order="1")
    assert text.status_code == 303
    assert text.headers["location"] == f"{STORE}/categories/{category_id}"

    enum = _add_property(admin_client, category_id, key="color", label="Color", prop_type="enum",
                         options="Red\nGreen\nBlue", sort_order="2")
    assert enum.status_code == 303

    categories = admin_client.get(f"/api/stores/{demo_store}/categories").json()
    gadgets = next(c for c in categories if c["slug"] == "gadgets")
    defs = gadgets["property_definitions"]
    assert [d["key"] for d in defs] == ["model_name", "color"]  # sort_order respected

    model_name, color = defs
    assert model_name["prop_type"] == "text"
    assert model_name["label"] == "Model name"
    assert model_name["required"] is True
    assert model_name["config"] == {}

    assert color["prop_type"] == "enum"
    assert color["label"] == "Color"
    assert color["required"] is False
    assert color["config"] == {"options": ["Red", "Green", "Blue"]}

    # Admin category page lists both, with the enum options.
    page = admin_client.get(f"{STORE}/categories/{category_id}")
    assert "Model name" in page.text and "Red, Green, Blue" in page.text
    assert en("flash.property_added", label="Color") in page_text(page)
    # Public storefront category page shows the schema too.
    public = admin_client.get("/stores/demo-widgets/c/gadgets")
    assert public.status_code == 200
    assert "Model name" in public.text and "Color" in public.text


# ------------------------------------------------------------------------- all types


@pytest.mark.parametrize("prop_type", ["text", "number", "boolean", "image"])
def test_simple_types_have_empty_config(admin_client, demo_store, prop_type):
    category_id = _create_category(admin_client)
    # Options / reference are ignored for types that don't use them.
    response = _add_property(admin_client, category_id, key="field", label="Field", prop_type=prop_type,
                             options="A\nB", ref_category_id=str(category_id))
    assert response.status_code == 303
    field = _api_defs(admin_client)["field"]
    assert field["prop_type"] == prop_type
    assert field["config"] == {}
    assert field["ref_category_id"] is None


def test_category_ref_with_and_without_target(admin_client, demo_store):
    target_id = _create_category(admin_client, slug="brands", name="Brands")
    category_id = _create_category(admin_client)
    assert _add_property(admin_client, category_id, key="brand", label="Brand", prop_type="category_ref",
                         ref_category_id=str(target_id), options="ignored").status_code == 303
    assert _add_property(admin_client, category_id, key="related", label="Related",
                         prop_type="category_ref").status_code == 303

    defs = _api_defs(admin_client)
    assert defs["brand"]["prop_type"] == "category_ref"
    assert defs["brand"]["ref_category_id"] == target_id
    assert defs["brand"]["config"] == {}
    assert defs["related"]["ref_category_id"] is None
    assert "→ Brands" in admin_client.get(f"{STORE}/categories/{category_id}").text


def test_category_ref_must_point_into_same_store(admin_client, demo_store, db_session):
    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.flush()
    foreign = Category(store_id=other.id, slug="foreign", name="Foreign")
    db_session.add(foreign)
    db_session.commit()

    category_id = _create_category(admin_client)
    for bad in (str(foreign.id), "999999", "abc"):
        response = _add_property(admin_client, category_id, prop_type="category_ref", ref_category_id=bad)
        assert response.status_code == 400
        assert f'id="ref_category_id-error">{en_html("admin.form.error.ref_category_invalid")}</p>' in response.text
    assert _api_defs(admin_client) == {}


def test_type_select_offers_all_engine_types(admin_client, demo_store):
    category_id = _create_category(admin_client)
    form = admin_client.get(f"{STORE}/categories/{category_id}/properties/new")
    assert form.status_code == 200
    for value in ("text", "number", "boolean", "enum", "image", "category_ref"):
        assert f'<option value="{value}"' in form.text


# ---------------------------------------------------------------------------- enums


def test_enum_options_are_trimmed(admin_client, demo_store):
    category_id = _create_category(admin_client)
    response = _add_property(admin_client, category_id, prop_type="enum", options="  S \n\n M\r\nL  \n")
    assert response.status_code == 303
    assert _api_defs(admin_client)["color"]["config"] == {"options": ["S", "M", "L"]}


@pytest.mark.parametrize(
    "options,code,params",
    [
        ("", "options_required", {}),
        ("  \n \n", "options_required", {}),
        ("Red\nBlue\nRed", "option_duplicate", {"option": "Red"}),
        ("x" * 101, "option_too_long", {"max_len": 100}),
        ("\n".join(f"o{i}" for i in range(201)), "options_too_many", {"max": 200}),
    ],
)
def test_enum_options_validation(admin_client, demo_store, options, code, params):
    category_id = _create_category(admin_client)
    response = _add_property(admin_client, category_id, prop_type="enum", options=options)
    assert response.status_code == 400
    assert f'id="options-error">{en_html(f"admin.form.error.{code}", **params)}</p>' in response.text
    assert _api_defs(admin_client) == {}


# ----------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "fields,field,code,params",
    [
        ({"key": ""}, "key", "key_required", {}),
        ({"key": "1color"}, "key", "key_format", {"max_len": 64}),
        ({"key": "has space"}, "key", "key_format", {"max_len": 64}),
        ({"key": "has-hyphen"}, "key", "key_format", {"max_len": 64}),
        ({"key": "k" * 65}, "key", "key_format", {"max_len": 64}),
        ({"label": "  "}, "label", "label_required", {}),
        ({"label": "L" * 201}, "label", "label_too_long", {"max_len": 200}),
        ({"prop_type": "date"}, "prop_type", "type_invalid", {}),
        ({"sort_order": "first"}, "sort_order", "sort_format", {}),
        ({"sort_order": "99999999999"}, "sort_order", "sort_range", {"min": "-1,000,000", "max": "1,000,000"}),
    ],
)
def test_property_validation(admin_client, demo_store, fields, field, code, params):
    category_id = _create_category(admin_client)
    response = _add_property(admin_client, category_id, **fields)
    assert response.status_code == 400
    assert f'id="{field}-error">{en_html(f"admin.form.error.{code}", **params)}</p>' in response.text
    assert en("form.fix_errors") in page_text(response)
    assert _api_defs(admin_client) == {}


def test_invalid_form_keeps_input(admin_client, demo_store):
    category_id = _create_category(admin_client)
    response = _add_property(admin_client, category_id, key="", label="Sizes", prop_type="enum", options="S\nM")
    assert response.status_code == 400
    assert 'value="Sizes"' in response.text
    assert ">S\nM</textarea>" in response.text
    assert '<option value="enum" selected>' in response.text


def test_key_is_normalized_and_unique_per_category(admin_client, demo_store):
    first = _create_category(admin_client, slug="first", name="First")
    second = _create_category(admin_client, slug="second", name="Second")
    assert _add_property(admin_client, first, key="  Color ").status_code == 303
    assert "color" in _api_defs(admin_client, "first")

    duplicate = _add_property(admin_client, first, key="color", label="Other")
    assert duplicate.status_code == 400
    assert en("admin.form.error.key_taken") in duplicate.text

    # Same key in another category is fine.
    assert _add_property(admin_client, second, key="color").status_code == 303


def test_insert_race_on_duplicate_key_is_reported(admin_client, demo_store, monkeypatch):
    category_id = _create_category(admin_client)
    assert _add_property(admin_client, category_id, key="color").status_code == 303

    real_validate = svc.validate_property

    def validate_without_errors(*args, **kwargs):
        values = real_validate(*args, **kwargs)
        args[3].errors.clear()  # pretend the duplicate was not seen yet
        return values

    monkeypatch.setattr(svc, "validate_property", validate_without_errors)
    response = _add_property(admin_client, category_id, key="color")
    assert response.status_code == 400
    assert en("admin.form.error.key_taken") in response.text

    # Same race on update (validation still patched): renaming "size" to the taken key "color".
    definition_id = _definition_id(admin_client, "color")
    assert _add_property(admin_client, category_id, key="size").status_code == 303
    renamed = admin_client.post(
        f"{STORE}/categories/{category_id}/properties/{_definition_id(admin_client, 'size')}",
        data={"key": "color", "label": "Size", "prop_type": "text"},
    )
    assert renamed.status_code == 400
    assert en("admin.form.error.key_taken") in renamed.text
    assert _api_defs(admin_client)["color"]["id"] == definition_id


# ---------------------------------------------------------------------------- edit


def test_edit_form_is_prefilled(admin_client, demo_store):
    category_id = _create_category(admin_client)
    _add_property(admin_client, category_id, key="size", label="Size", prop_type="enum", options="S\nM", required="1",
                  sort_order="3")
    definition_id = _definition_id(admin_client, "size")
    page = admin_client.get(f"{STORE}/categories/{category_id}/properties/{definition_id}")
    assert page.status_code == 200
    assert 'value="size"' in page.text and 'value="Size"' in page.text
    assert ">S\nM</textarea>" in page.text
    assert '<option value="enum" selected>' in page.text
    assert "checked" in page.text
    assert 'value="3"' in page.text


def test_edit_property_changes_type_and_config(admin_client, demo_store):
    category_id = _create_category(admin_client)
    _add_property(admin_client, category_id, key="size", label="Size", prop_type="enum", options="S\nM", required="1")
    definition_id = _definition_id(admin_client, "size")
    url = f"{STORE}/categories/{category_id}/properties/{definition_id}"

    # enum -> text drops the options; unchecked box -> not required.
    response = admin_client.post(url, data={"key": "size_label", "label": "Size label", "prop_type": "text"},
                                 follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{STORE}/categories/{category_id}"
    updated = _api_defs(admin_client)["size_label"]
    assert updated["id"] == definition_id
    assert updated["prop_type"] == "text"
    assert updated["config"] == {}
    assert updated["required"] is False

    # text -> enum with new options.
    response = admin_client.post(url, data={"key": "size_label", "label": "Size label", "prop_type": "enum",
                                            "options": "XS\nXL"})
    assert response.status_code == 200
    assert _api_defs(admin_client)["size_label"]["config"] == {"options": ["XS", "XL"]}
    assert en("flash.property_saved", label="Size label") in page_text(response)


def test_edit_keeping_own_key_is_not_a_duplicate(admin_client, demo_store):
    category_id = _create_category(admin_client)
    _add_property(admin_client, category_id, key="color")
    definition_id = _definition_id(admin_client, "color")
    response = admin_client.post(
        f"{STORE}/categories/{category_id}/properties/{definition_id}",
        data={"key": "color", "label": "Colour", "prop_type": "text"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert _api_defs(admin_client)["color"]["label"] == "Colour"


def test_edit_validation_error_keeps_definition_unchanged(admin_client, demo_store):
    category_id = _create_category(admin_client)
    _add_property(admin_client, category_id, key="color", label="Color")
    definition_id = _definition_id(admin_client, "color")
    response = admin_client.post(
        f"{STORE}/categories/{category_id}/properties/{definition_id}",
        data={"key": "color", "label": "", "prop_type": "enum", "options": ""},
    )
    assert response.status_code == 400
    assert en("admin.form.error.label_required") in response.text
    assert en("admin.form.error.options_required") in response.text
    assert _api_defs(admin_client)["color"]["label"] == "Color"


def test_type_is_locked_once_products_have_values(admin_client, demo_store, db_session):
    category_id = _create_category(admin_client)
    _add_property(admin_client, category_id, key="size", label="Size", prop_type="enum", options="S\nM")
    definition_id = _definition_id(admin_client, "size")
    _add_product_value(db_session, category_id, definition_id, {"value": "S"})
    url = f"{STORE}/categories/{category_id}/properties/{definition_id}"

    assert en("admin.property.type_locked", count=1) in page_text(admin_client.get(url))

    response = admin_client.post(url, data={"key": "size", "label": "Size", "prop_type": "number"})
    assert response.status_code == 400
    assert en("admin.form.error.type_locked") in response.text
    assert _api_defs(admin_client)["size"]["prop_type"] == "enum"

    # Other edits are still allowed.
    response = admin_client.post(url, data={"key": "size", "label": "Shirt size", "prop_type": "enum",
                                            "options": "S\nM\nL"}, follow_redirects=False)
    assert response.status_code == 303
    assert _api_defs(admin_client)["size"]["config"] == {"options": ["S", "M", "L"]}


# --------------------------------------------------------------------------- delete


def test_delete_property(admin_client, demo_store):
    category_id = _create_category(admin_client)
    _add_property(admin_client, category_id, key="color")
    _add_property(admin_client, category_id, key="size", label="Size")
    definition_id = _definition_id(admin_client, "color")
    url = f"{STORE}/categories/{category_id}/properties/{definition_id}/delete"

    confirm = admin_client.get(url)
    assert confirm.status_code == 200
    assert en("admin.property.delete_title", label="Color") in page_text(confirm)
    assert not pattern("en", "admin.property.delete_consequence").search(page_text(confirm))

    response = admin_client.post(url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"{STORE}/categories/{category_id}"
    assert set(_api_defs(admin_client)) == {"size"}
    assert en("flash.property_deleted", label="Color") in page_text(admin_client.get(response.headers["location"]))
    assert admin_client.get(url).status_code == 404


def test_delete_property_removes_product_values(admin_client, demo_store, db_session):
    category_id = _create_category(admin_client)
    _add_property(admin_client, category_id, key="color")
    definition_id = _definition_id(admin_client, "color")
    _add_product_value(db_session, category_id, definition_id, {"value": "Red"})
    url = f"{STORE}/categories/{category_id}/properties/{definition_id}/delete"

    assert en("admin.property.delete_consequence", count=1) in page_text(admin_client.get(url))
    assert admin_client.post(url, follow_redirects=False).status_code == 303

    db_session.expire_all()
    assert db_session.scalar(select(PropertyValue).where(PropertyValue.definition_id == definition_id)) is None
    assert db_session.get(PropertyDefinition, definition_id) is None
    assert db_session.scalar(select(Product).where(Product.slug == "widget-1")) is not None  # product kept


# ------------------------------------------------------------------ scoping and auth


def test_definition_must_belong_to_category_in_url(admin_client, demo_store):
    first = _create_category(admin_client, slug="first", name="First")
    second = _create_category(admin_client, slug="second", name="Second")
    _add_property(admin_client, first, key="color")
    definition_id = _definition_id(admin_client, "color", slug="first")

    wrong = f"{STORE}/categories/{second}/properties/{definition_id}"
    assert admin_client.get(wrong).status_code == 404
    assert admin_client.post(wrong, data={"key": "x", "label": "X", "prop_type": "text"}).status_code == 404
    assert admin_client.post(f"{wrong}/delete").status_code == 404
    assert admin_client.get(f"{STORE}/categories/{first}/properties/999999").status_code == 404
    assert admin_client.get(f"{STORE}/categories/999999/properties/new").status_code == 404
    assert "color" in _api_defs(admin_client, "first")


def test_property_routes_require_admin(client, demo_store, db_session):
    category_id = _create_category(client)
    _add_property(client, category_id, key="color")
    definition_id = _definition_id(client, "color")
    base = f"{STORE}/categories/{category_id}/properties"
    client.post("/logout")

    for method, path in [
        ("get", f"{base}/new"),
        ("post", base),
        ("get", f"{base}/{definition_id}"),
        ("post", f"{base}/{definition_id}"),
        ("post", f"{base}/{definition_id}/delete"),
    ]:
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")

    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert client.get(f"{base}/new").status_code == 403
    assert client.post(base, data={"key": "size", "label": "Size", "prop_type": "text"}).status_code == 403
    assert client.post(f"{base}/{definition_id}/delete").status_code == 403

    assert set(_api_defs(client)) == {"color"}
