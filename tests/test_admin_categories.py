"""Admin: categories (create / edit / delete) on the demo-widgets store."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.auth import create_user
from app.models import Category, Product, PropertyDefinition, PropertyType, PropertyValue, Store, UserRole
from app.services import category_admin as svc
from tests.ui_text import en, en_html, page_text

STORE = "/admin/stores/demo-widgets"
API_CATEGORIES = "/api/stores/demo-widgets/categories"


def _create(client, slug="gadgets", name="Gadgets", **extra):
    return client.post(f"{STORE}/categories", data={"slug": slug, "name": name, **extra}, follow_redirects=False)


def _create_id(client, slug="gadgets", name="Gadgets", **extra) -> int:
    response = _create(client, slug, name, **extra)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _api(client) -> dict[str, dict]:
    return {c["slug"]: c for c in client.get(API_CATEGORIES).json()}


def _store_id(db_session, slug="demo-widgets") -> int:
    return db_session.scalar(select(Store.id).where(Store.slug == slug))


# ----------------------------------------------------------------------- store page


def test_store_page_lists_categories(admin_client, demo_store):
    listing = admin_client.get("/admin/stores")
    assert f'href="{STORE}"' in listing.text
    assert en("admin.stores.manage_categories") in page_text(listing)

    empty = admin_client.get(STORE)
    assert empty.status_code == 200
    assert en("admin.store_detail.empty") in page_text(empty)
    assert empty.headers["cache-control"] == "no-store"

    _create_id(admin_client)
    page = admin_client.get(STORE)
    assert "Gadgets" in page.text and "<code>gadgets</code>" in page.text


def test_unknown_store_is_404(admin_client):
    assert admin_client.get("/admin/stores/nope").status_code == 404
    assert admin_client.get("/admin/stores/nope/categories/new").status_code == 404
    assert admin_client.post("/admin/stores/nope/categories", data={"slug": "a", "name": "A"}).status_code == 404


# --------------------------------------------------------------------------- create


def test_create_category(admin_client, demo_store):
    form = admin_client.get(f"{STORE}/categories/new")
    assert form.status_code == 200
    for field in ("name", "slug", "parent_id", "description", "sort_order"):
        assert f'name="{field}"' in form.text

    response = _create(admin_client, slug=" Gadgets ", name="  Small   gadgets ", description=" Handy. ",
                       sort_order="5")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"{STORE}/categories/")

    created = _api(admin_client)["gadgets"]
    assert created["name"] == "Small gadgets"
    assert created["description"] == "Handy."
    assert created["sort_order"] == 5
    assert created["parent_id"] is None
    assert created["property_definitions"] == []

    page = admin_client.get(location)
    assert en("flash.category_created", name="Small gadgets") in page_text(page)
    assert en("admin.category.definitions") in page_text(page)
    assert en("admin.category.no_properties") in page_text(page)

    assert admin_client.get("/stores/demo-widgets/c/gadgets").status_code == 200


def test_empty_description_and_sort_default(admin_client, demo_store):
    # An empty field is treated as missing by FastAPI (form default "0"); whitespace reaches the parser.
    _create_id(admin_client, description="   ", sort_order="   ")
    created = _api(admin_client)["gadgets"]
    assert created["description"] is None
    assert created["sort_order"] == 0


@pytest.mark.parametrize(
    "fields,field,code,params",
    [
        ({"slug": ""}, "slug", "slug_required", {}),
        ({"slug": "no spaces"}, "slug", "slug_format", {"max_len": 64}),
        ({"slug": "x" * 65}, "slug", "slug_format", {"max_len": 64}),
        ({"name": " "}, "name", "name_required", {}),
        ({"name": "N" * 201}, "name", "name_too_long", {"max_len": 200}),
        ({"description": "d" * 2001}, "description", "description_too_long", {"max_len": 2000}),
        ({"sort_order": "1.5"}, "sort_order", "sort_format", {}),
        ({"sort_order": "-1000001"}, "sort_order", "sort_range", {"min": "-1,000,000", "max": "1,000,000"}),
        ({"parent_id": "999999"}, "parent_id", "parent_invalid", {}),
        ({"parent_id": "abc"}, "parent_id", "parent_invalid", {}),
    ],
)
def test_create_category_validation(admin_client, demo_store, fields, field, code, params):
    data = {"slug": "gadgets", "name": "Gadgets", **fields}
    response = admin_client.post(f"{STORE}/categories", data=data)
    assert response.status_code == 400
    # The catalog message, under its own field.
    assert f'id="{field}-error">{en_html(f"admin.form.error.{code}", **params)}</p>' in response.text
    assert en("form.fix_errors") in page_text(response)
    assert _api(admin_client) == {}


def test_validation_error_does_not_invent_duplicate_slug(admin_client, demo_store):
    response = _create(admin_client, slug="fresh", name="")
    assert response.status_code == 400
    assert en("admin.form.error.name_required") in response.text
    assert en("admin.form.error.category_slug_taken") not in response.text
    assert 'value="fresh"' in response.text  # input kept


def test_slug_unique_per_store_only(admin_client, demo_store, db_session):
    _create_id(admin_client)
    duplicate = _create(admin_client, name="Other")
    assert duplicate.status_code == 400
    assert en("admin.form.error.category_slug_taken") in duplicate.text

    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.commit()
    response = admin_client.post("/admin/stores/other-store/categories", data={"slug": "gadgets", "name": "G"},
                                 follow_redirects=False)
    assert response.status_code == 303


def test_insert_race_on_duplicate_slug_is_reported(admin_client, demo_store, monkeypatch):
    first = _create_id(admin_client)
    second = _create_id(admin_client, slug="tools", name="Tools")
    real_validate = svc.validate_category

    def validate_without_errors(*args, **kwargs):
        values = real_validate(*args, **kwargs)
        args[2].errors.clear()  # pretend the duplicate was not seen yet
        return values

    monkeypatch.setattr(svc, "validate_category", validate_without_errors)
    created = _create(admin_client)
    assert created.status_code == 400
    assert en("admin.form.error.category_slug_taken") in created.text

    updated = admin_client.post(f"{STORE}/categories/{second}", data={"slug": "gadgets", "name": "Tools"})
    assert updated.status_code == 400
    assert en("admin.form.error.category_slug_taken") in updated.text
    assert set(_api(admin_client)) == {"gadgets", "tools"}
    assert _api(admin_client)["gadgets"]["id"] == first


# ----------------------------------------------------------------------------- tree


def test_subcategories_and_tree(admin_client, demo_store):
    root = _create_id(admin_client, slug="electronics", name="Electronics")
    child = _create_id(admin_client, slug="chargers", name="Chargers", parent_id=str(root))
    _create_id(admin_client, slug="usb-c", name="USB-C", parent_id=str(child))

    api = _api(admin_client)
    assert api["chargers"]["parent_id"] == root
    assert api["usb-c"]["parent_id"] == child

    page = admin_client.get(STORE).text
    assert page.index("Electronics") < page.index("— Chargers") < page.index("— — USB-C")

    # A category can't be its own parent or sit under its own descendant.
    form = admin_client.get(f"{STORE}/categories/{root}").text
    assert f'<option value="{child}"' not in form
    assert f'<option value="{root}"' not in form
    for bad_parent in (root, child):
        response = admin_client.post(
            f"{STORE}/categories/{root}",
            data={"slug": "electronics", "name": "Electronics", "parent_id": str(bad_parent)},
        )
        assert response.status_code == 400
        assert en("admin.form.error.parent_cycle") in response.text
    assert _api(admin_client)["electronics"]["parent_id"] is None


def test_parent_must_be_in_same_store(admin_client, demo_store, db_session):
    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.flush()
    foreign = Category(store_id=other.id, slug="foreign", name="Foreign")
    db_session.add(foreign)
    db_session.commit()
    response = _create(admin_client, parent_id=str(foreign.id))
    assert response.status_code == 400
    assert en("admin.form.error.parent_invalid") in response.text


def test_category_tree_handles_orphans_and_cycles():
    def cat(id_, parent_id, name):
        return SimpleNamespace(id=id_, parent_id=parent_id, name=name)

    a, b = cat(1, None, "A"), cat(2, 1, "B")
    orphan = cat(3, 99, "Orphan")  # parent not in list -> root
    x, y = cat(4, 5, "X"), cat(5, 4, "Y")  # corrupt cycle -> still listed once
    tree = svc.category_tree([a, b, orphan, x, y])
    assert [(c.name, d) for c, d in tree] == [("A", 0), ("B", 1), ("Orphan", 0), ("X", 0), ("Y", 0)]
    assert svc.descendant_ids([a, b, x, y], 1) == {2}
    assert svc.descendant_ids([a, b, x, y], 4) == {4, 5}


# ----------------------------------------------------------------------------- edit


def test_edit_category(admin_client, demo_store):
    parent = _create_id(admin_client, slug="electronics", name="Electronics")
    category_id = _create_id(admin_client, description="Old")
    url = f"{STORE}/categories/{category_id}"

    form = admin_client.get(url)
    assert form.status_code == 200
    assert 'value="gadgets"' in form.text and ">Old</textarea>" in form.text

    response = admin_client.post(
        url,
        data={"slug": "gizmos", "name": "Gizmos", "description": "New", "parent_id": str(parent), "sort_order": "7"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == url
    api = _api(admin_client)
    assert "gadgets" not in api
    assert api["gizmos"]["id"] == category_id
    assert api["gizmos"]["description"] == "New"
    assert api["gizmos"]["parent_id"] == parent
    assert api["gizmos"]["sort_order"] == 7
    page = page_text(admin_client.get(url))
    assert en("flash.category_saved", name="Gizmos") in page
    assert f'<option value="{parent}" selected>' in page

    # Move back to top level; keeping its own slug is not a duplicate.
    response = admin_client.post(url, data={"slug": "gizmos", "name": "Gizmos", "parent_id": ""},
                                 follow_redirects=False)
    assert response.status_code == 303
    assert _api(admin_client)["gizmos"]["parent_id"] is None


def test_edit_validation_keeps_category_unchanged(admin_client, demo_store):
    _create_id(admin_client, slug="tools", name="Tools")
    category_id = _create_id(admin_client)
    response = admin_client.post(f"{STORE}/categories/{category_id}", data={"slug": "tools", "name": ""})
    assert response.status_code == 400
    assert en("admin.form.error.name_required") in response.text
    assert en("admin.form.error.category_slug_taken") in response.text
    assert _api(admin_client)["gadgets"]["name"] == "Gadgets"


# --------------------------------------------------------------------------- delete


def test_delete_category_with_properties(admin_client, demo_store):
    category_id = _create_id(admin_client)
    for key in ("color", "size"):
        admin_client.post(f"{STORE}/categories/{category_id}/properties",
                          data={"key": key, "label": key.title(), "prop_type": "text"})
    url = f"{STORE}/categories/{category_id}/delete"

    confirm = admin_client.get(url)
    assert confirm.status_code == 200
    assert en("admin.category.delete_consequence", count=2) in page_text(confirm)
    assert f'action="{url}"' in confirm.text

    response = admin_client.post(url, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == STORE
    assert _api(admin_client) == {}
    assert en("flash.category_deleted", name="Gadgets") in page_text(admin_client.get(STORE))
    assert admin_client.get(f"{STORE}/categories/{category_id}").status_code == 404
    assert admin_client.get("/stores/demo-widgets/c/gadgets").status_code == 404


def test_delete_blocked_while_category_has_products(admin_client, demo_store, db_session):
    category_id = _create_id(admin_client)
    db_session.add(Product(store_id=_store_id(db_session), category_id=category_id, slug="w", name="W"))
    db_session.commit()
    url = f"{STORE}/categories/{category_id}/delete"

    confirm = admin_client.get(url)
    assert en("admin.form.error.has_products", count=1) in page_text(confirm)
    assert 'method="post"' not in confirm.text.split("<main")[1]  # no delete button

    response = admin_client.post(url)
    assert response.status_code == 409
    assert en("admin.form.error.has_products", count=1) in page_text(response)
    assert "gadgets" in _api(admin_client)


def test_delete_blocked_while_category_has_subcategories(admin_client, demo_store):
    parent = _create_id(admin_client, slug="electronics", name="Electronics")
    child = _create_id(admin_client, parent_id=str(parent))
    response = admin_client.post(f"{STORE}/categories/{parent}/delete")
    assert response.status_code == 409
    assert en("admin.form.error.has_subcategories", count=1) in page_text(response)
    assert "electronics" in _api(admin_client)

    # Bottom-up works: delete the (property-less) child, then the parent.
    assert admin_client.post(f"{STORE}/categories/{child}/delete", follow_redirects=False).status_code == 303
    assert admin_client.post(f"{STORE}/categories/{parent}/delete", follow_redirects=False).status_code == 303
    assert _api(admin_client) == {}


def test_delete_clears_references_and_values(admin_client, demo_store, db_session):
    brands = _create_id(admin_client, slug="brands", name="Brands")
    gadgets = _create_id(admin_client)
    admin_client.post(f"{STORE}/categories/{gadgets}/properties",
                      data={"key": "brand", "label": "Brand", "prop_type": "category_ref",
                            "ref_category_id": str(brands)})
    # A stray value attached to a definition of the category being deleted.
    store_id = _store_id(db_session)
    definition = PropertyDefinition(category_id=brands, key="logo", label="Logo", prop_type=PropertyType.image)
    product = Product(store_id=store_id, category_id=gadgets, slug="w", name="W")
    db_session.add_all([definition, product])
    db_session.flush()
    db_session.add(PropertyValue(product_id=product.id, definition_id=definition.id, value={"media_path": "/x.png"}))
    db_session.commit()
    definition_id = definition.id

    assert admin_client.post(f"{STORE}/categories/{brands}/delete", follow_redirects=False).status_code == 303

    brand_def = _api(admin_client)["gadgets"]["property_definitions"][0]
    assert brand_def["key"] == "brand"
    assert brand_def["ref_category_id"] is None
    db_session.expire_all()
    assert db_session.get(PropertyDefinition, definition_id) is None
    assert db_session.scalar(select(PropertyValue).where(PropertyValue.definition_id == definition_id)) is None


# ------------------------------------------------------------------ scoping and auth


def test_category_of_other_store_is_404(admin_client, demo_store, db_session):
    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.flush()
    foreign = Category(store_id=other.id, slug="foreign", name="Foreign")
    db_session.add(foreign)
    db_session.commit()
    url = f"{STORE}/categories/{foreign.id}"

    assert admin_client.get(url).status_code == 404
    assert admin_client.post(url, data={"slug": "hijack", "name": "Hijack"}).status_code == 404
    assert admin_client.post(f"{url}/delete").status_code == 404
    assert admin_client.post(f"{url}/properties", data={"key": "k", "label": "K"}).status_code == 404
    db_session.expire_all()
    assert db_session.get(Category, foreign.id).slug == "foreign"


def test_category_routes_require_admin(client, demo_store, db_session):
    category_id = _create_id(client)
    client.post("/logout")

    for method, path in [
        ("get", STORE),
        ("get", f"{STORE}/categories/new"),
        ("post", f"{STORE}/categories"),
        ("get", f"{STORE}/categories/{category_id}"),
        ("post", f"{STORE}/categories/{category_id}"),
        ("get", f"{STORE}/categories/{category_id}/delete"),
        ("post", f"{STORE}/categories/{category_id}/delete"),
    ]:
        response = getattr(client, method)(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login?next=")

    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert client.get(STORE).status_code == 403
    assert client.post(f"{STORE}/categories", data={"slug": "tools", "name": "Tools"}).status_code == 403
    assert client.post(f"{STORE}/categories/{category_id}/delete").status_code == 403

    assert set(_api(client)) == {"gadgets"}


# ------------------------------------------- cleanup without SQLite FK enforcement
# The app engine does not enable PRAGMA foreign_keys, so ON DELETE rules never fire there.
# (The shared test engine enables them, which would hide missing cleanup code.)


@pytest.fixture()
def session_without_fk():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base, init_db

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    init_db(bind=engine)
    session = sessionmaker(bind=engine)()
    assert session.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _seed_refs(db):
    store = Store(slug="demo-widgets", name="Demo Widgets")
    db.add(store)
    db.flush()
    brands = Category(store_id=store.id, slug="brands", name="Brands")
    gadgets = Category(store_id=store.id, slug="gadgets", name="Gadgets")
    db.add_all([brands, gadgets])
    db.flush()
    logo = PropertyDefinition(category_id=brands.id, key="logo", label="Logo", prop_type=PropertyType.image)
    brand = PropertyDefinition(category_id=gadgets.id, key="brand", label="Brand",
                               prop_type=PropertyType.category_ref, ref_category_id=brands.id)
    product = Product(store_id=store.id, category_id=gadgets.id, slug="w", name="W")
    db.add_all([logo, brand, product])
    db.flush()
    db.add_all([
        PropertyValue(product_id=product.id, definition_id=logo.id, value={"media_path": "/x.png"}),
        PropertyValue(product_id=product.id, definition_id=brand.id, value={"category_id": brands.id}),
    ])
    db.commit()
    return brands, logo, brand


def test_delete_category_cleans_up_without_fk_enforcement(session_without_fk):
    db = session_without_fk
    brands, logo, brand = _seed_refs(db)
    logo_id = logo.id

    svc.delete_category(db, brands)
    db.expire_all()
    assert db.get(PropertyDefinition, brand.id).ref_category_id is None
    assert db.get(PropertyDefinition, logo_id) is None
    assert db.scalar(select(PropertyValue).where(PropertyValue.definition_id == logo_id)) is None


def test_delete_property_cleans_up_without_fk_enforcement(session_without_fk):
    db = session_without_fk
    _, _, brand = _seed_refs(db)
    brand_id = brand.id

    svc.delete_property(db, brand)
    db.expire_all()
    assert db.get(PropertyDefinition, brand_id) is None
    assert db.scalar(select(PropertyValue).where(PropertyValue.definition_id == brand_id)) is None
