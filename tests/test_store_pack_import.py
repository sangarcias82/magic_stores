"""Store pack import: a validated pack becomes rows in the engine tables (no new tables or
columns), visible through the API, the storefront and admin. Re-import upserts by natural keys."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models import Category, Product, PropertyDefinition, PropertyType, PropertyValue, Store
from app.services.store_pack_validation import PACK_FILE_SUFFIX, StorePackError
from app.services.store_pack_import import (
    STOREFRONT_NOT_COPIED,
    StorePackImportError,
    import_store_pack,
    import_store_pack_file,
    main,
)
from app.services.storefront import DEFAULT_THEME

PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
CLOTHING = PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}"
WIDGETS = PACKS_DIR / f"demo-widgets{PACK_FILE_SUFFIX}"
SCHEMA_EXAMPLE = PACKS_DIR / "store-pack.v1.schema.example.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def clothing() -> dict:
    return _load(CLOTHING)


def _count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _store(db, slug: str) -> Store:
    db.expire_all()  # read what the import committed, not cached objects
    return db.scalar(select(Store).where(Store.slug == slug))


def _api_values(client, store_slug: str, product_slug: str) -> dict:
    detail = client.get(f"/api/stores/{store_slug}/products/{product_slug}").json()
    return {p["key"]: p["value"] for p in detail["properties"]}


# ------------------------------------------------------------------- the generic fixture


def test_import_generic_fixture_is_visible_in_the_api(client, db_session, clothing):
    report = import_store_pack_file(db_session, CLOTHING)
    pack_store = clothing["store"]
    (pack_category,) = clothing["categories"]
    (pack_product,) = clothing["products"]
    definitions = pack_category["property_definitions"]

    assert report.store_created and report.store_slug == pack_store["slug"]
    assert (report.categories.created, report.properties.created, report.products.created) == (1, len(definitions), 1)

    # Store + theme
    store = client.get(f"/api/stores/{pack_store['slug']}").json()
    assert (store["name"], store["currency"], store["locale_default"]) == (
        pack_store["name"], pack_store["currency"], pack_store["locale_default"]
    )
    for key in ("primary", "secondary", "accent"):
        assert store["theme"][key] == pack_store["theme"][key]

    # Category + property definitions (including the enum options)
    (category,) = client.get(f"/api/stores/{pack_store['slug']}/categories").json()
    assert category["slug"] == pack_category["slug"] and category["name"] == pack_category["name"]
    api_defs = {d["key"]: d for d in category["property_definitions"]}
    assert list(api_defs) == [d["key"] for d in definitions]  # sort order kept
    for pack_def in definitions:
        api_def = api_defs[pack_def["key"]]
        assert (api_def["label"], api_def["prop_type"], api_def["required"]) == (
            pack_def["label"], pack_def["prop_type"], pack_def["required"]
        )
        if pack_def["prop_type"] == "enum":
            assert api_def["config"]["options"] == pack_def["config"]["options"]

    # Product + property values
    (listed,) = client.get(f"/api/stores/{pack_store['slug']}/products").json()
    assert (listed["slug"], listed["price_cents"], listed["category_id"]) == (
        pack_product["slug"], pack_product["price_cents"], category["id"]
    )
    values = _api_values(client, pack_store["slug"], pack_product["slug"])
    assert values == {key: {"value": value} for key, value in pack_product["values"].items()}


def test_imported_values_are_stored_typed_like_admin_writes_them(db_session, clothing):
    import_store_pack(db_session, clothing)
    rows = db_session.execute(
        select(PropertyDefinition.key, PropertyValue.value)
        .join(PropertyValue, PropertyValue.definition_id == PropertyDefinition.id)
    ).all()
    stored = dict(rows)
    assert stored["size"] == {"value": "M"}
    assert stored["sleeve_length_cm"] == {"value": 62.5}
    assert stored["machine_washable"] == {"value": True}
    size = db_session.scalar(select(PropertyDefinition).where(PropertyDefinition.key == "size"))
    assert size.prop_type == PropertyType.enum and size.config == {"options": ["S", "M", "L", "XL"]}


def test_imported_product_shows_on_storefront_and_in_admin(admin_client, db_session, clothing):
    import_store_pack(db_session, clothing)
    product = clothing["products"][0]
    public = admin_client.get(f"/stores/clothing-demo/p/{product['slug']}")
    assert public.status_code == 200 and product["name"] in public.text

    product_id = db_session.scalar(select(Product.id).where(Product.slug == product["slug"]))
    form = admin_client.get(f"/admin/stores/clothing-demo/products/{product_id}")
    assert form.status_code == 200
    assert f'<option value="{product["values"]["size"]}" selected>' in form.text


def test_import_uses_only_the_engine_tables(engine, db_session):
    def schema():
        inspector = inspect(engine)
        return {t: sorted(c["name"] for c in inspector.get_columns(t)) for t in inspector.get_table_names()}

    before = schema()
    import_store_pack_file(db_session, CLOTHING)
    import_store_pack_file(db_session, WIDGETS)
    assert schema() == before


# ----------------------------------------------------------------- the other fixtures


def test_widgets_pack_resolves_parents_and_category_refs(client, db_session):
    report = import_store_pack_file(db_session, WIDGETS)
    categories = {c["slug"]: c for c in client.get("/api/stores/demo-widgets/categories").json()}
    assert categories["acme"]["parent_id"] == categories["brands"]["id"]
    brand = next(d for d in categories["widgets"]["property_definitions"] if d["key"] == "brand")
    assert brand["ref_category_id"] == categories["brands"]["id"]

    values = _api_values(client, "demo-widgets", "widget-blue-m")
    assert values["brand"] == {"category_id": categories["acme"]["id"]}
    assert values["photo"] == {}  # null in the pack = no value
    assert report.skipped == []  # i18n blocks are imported (tests/test_translations.py)
    assert report.translations_written > 0


def test_schema_example_imports_an_empty_store(db_session):
    # Without its folder (like an upload) the storefront folder can't be copied: listed as skipped.
    report = import_store_pack(db_session, json.loads(SCHEMA_EXAMPLE.read_text(encoding="utf-8")))
    assert report.store_created and _count(db_session, Category) == 0
    assert report.skipped == [STOREFRONT_NOT_COPIED] and report.storefront_files is None


def test_schema_example_file_needs_its_storefront_folder(db_session):
    # From its file, "storefront/" is looked up next to it; fixtures/packs/storefront/ doesn't exist.
    with pytest.raises(StorePackImportError) as info:
        import_store_pack_file(db_session, SCHEMA_EXAMPLE)
    [issue] = info.value.issues
    assert issue.path == "storefront.copy_from_pack_dir"
    assert issue.message == f'folder not found: "storefront/" in {PACKS_DIR.resolve()}'
    assert _count(db_session, Store) == 0  # nothing written


# ---------------------------------------------------------------------------- re-import


def test_reimporting_the_same_pack_changes_nothing(db_session, clothing):
    import_store_pack(db_session, clothing)
    ids = {m.__name__: sorted(db_session.scalars(select(m.id))) for m in (Store, Category, PropertyDefinition, Product, PropertyValue)}

    report = import_store_pack(db_session, copy.deepcopy(clothing))
    assert not report.store_created
    assert (report.categories.created, report.properties.created, report.products.created) == (0, 0, 0)
    assert (report.categories.updated, report.products.updated, report.values_removed) == (1, 1, 0)
    again = {m.__name__: sorted(db_session.scalars(select(m.id))) for m in (Store, Category, PropertyDefinition, Product, PropertyValue)}
    assert again == ids  # same rows, same ids


def test_reimport_upserts_what_the_pack_names_and_keeps_the_rest(client, db_session, clothing):
    import_store_pack(db_session, clothing)
    shirt_id = db_session.scalar(select(Product.id).where(Product.slug == "linen-shirt-m"))
    # Created outside the pack (e.g. in admin): must survive re-imports untouched.
    store = _store(db_session, "clothing-demo")
    shirts = db_session.scalar(select(Category).where(Category.slug == "shirts"))
    db_session.add(Category(store_id=store.id, slug="hats", name="Hats"))
    db_session.add(Product(store_id=store.id, category_id=shirts.id, slug="admin-tee", name="Admin tee", price_cents=100))
    db_session.commit()

    updated = copy.deepcopy(clothing)
    updated["store"]["name"] = "Demo Clothing 2"
    del updated["store"]["theme"]  # omitted -> default theme
    shirt = updated["products"][0]
    shirt.update(name="Linen shirt v2", price_cents=45000)
    del shirt["description"]  # omitted -> no description
    shirt["values"]["material"] = None  # null -> value removed
    del shirt["values"]["machine_washable"]  # missing -> value removed
    shirt["values"]["size"] = "L"
    updated["categories"][0]["property_definitions"][0]["config"]["options"] = ["M", "L"]
    updated["products"].append({**copy.deepcopy(shirt), "slug": "linen-shirt-l", "name": "Linen shirt L"})

    report = import_store_pack(db_session, updated)
    assert (report.products.created, report.products.updated, report.values_removed) == (1, 1, 2)

    store = _store(db_session, "clothing-demo")
    assert store.name == "Demo Clothing 2" and store.theme == DEFAULT_THEME
    detail = client.get("/api/stores/clothing-demo/products/linen-shirt-m").json()
    assert (detail["id"], detail["name"], detail["price_cents"], detail["description"]) == (
        shirt_id, "Linen shirt v2", 45000, None
    )
    values = {p["key"]: p["value"] for p in detail["properties"]}
    assert values["size"] == {"value": "L"} and values["material"] == {} and values["machine_washable"] == {}
    size = db_session.scalar(select(PropertyDefinition).where(PropertyDefinition.key == "size"))
    assert size.config == {"options": ["M", "L"]}
    slugs = {p["slug"] for p in client.get("/api/stores/clothing-demo/products").json()}
    assert slugs == {"linen-shirt-m", "linen-shirt-l", "admin-tee"}
    assert {c["slug"] for c in client.get("/api/stores/clothing-demo/categories").json()} == {"shirts", "hats"}


def test_reimport_can_move_a_product_to_another_category(db_session, clothing):
    import_store_pack(db_session, clothing)
    moved = copy.deepcopy(clothing)
    moved["categories"].append({
        "slug": "sale", "name": "Sale",
        "property_definitions": [{"key": "discount_pct", "label": "Discount (%)", "prop_type": "number"}],
    })
    moved["products"][0].update(category_slug="sale", values={"discount_pct": 30})
    report = import_store_pack(db_session, moved)
    assert report.values_removed == 5 and report.values_written == 1

    db_session.expire_all()
    product = db_session.scalar(select(Product).where(Product.slug == "linen-shirt-m"))
    assert product.category.slug == "sale"
    assert [(pv.definition.key, pv.value) for pv in product.property_values] == [("discount_pct", {"value": 30})]


def test_type_change_is_allowed_when_only_pack_products_use_the_property(db_session, clothing):
    import_store_pack(db_session, clothing)
    changed = copy.deepcopy(clothing)
    changed["categories"][0]["property_definitions"][3]["prop_type"] = "text"  # sleeve_length_cm
    changed["products"][0]["values"]["sleeve_length_cm"] = "62.5 cm"
    import_store_pack(db_session, changed)
    db_session.expire_all()
    sleeve = db_session.scalar(select(PropertyDefinition).where(PropertyDefinition.key == "sleeve_length_cm"))
    assert sleeve.prop_type == PropertyType.text
    assert db_session.scalar(select(PropertyValue.value).where(PropertyValue.definition_id == sleeve.id)) == {
        "value": "62.5 cm"
    }


def test_type_change_conflict_rolls_back_everything(db_session, clothing):
    import_store_pack(db_session, clothing)
    store = _store(db_session, "clothing-demo")
    shirts = db_session.scalar(select(Category).where(Category.slug == "shirts"))
    material = db_session.scalar(select(PropertyDefinition).where(PropertyDefinition.key == "material"))
    tee = Product(store_id=store.id, category_id=shirts.id, slug="admin-tee", name="Admin tee", price_cents=100)
    tee.property_values.append(PropertyValue(definition_id=material.id, value={"value": "cotton"}))
    db_session.add(tee)
    db_session.commit()

    changed = copy.deepcopy(clothing)
    changed["store"]["name"] = "Renamed"
    changed["categories"][0]["property_definitions"][2]["prop_type"] = "number"  # material
    changed["products"][0]["values"]["material"] = 100
    with pytest.raises(StorePackImportError) as caught:
        import_store_pack(db_session, changed)
    (issue,) = caught.value.issues
    assert issue.path == "categories[0].property_definitions[2].prop_type"
    assert 'can\'t change "material" from text to number: 1 value(s) of products not in this pack' in issue.message

    assert _store(db_session, "clothing-demo").name == clothing["store"]["name"]  # nothing written
    assert db_session.get(PropertyDefinition, material.id).prop_type == PropertyType.text


def test_invalid_pack_writes_nothing(db_session, clothing):
    del clothing["store"]["slug"]
    with pytest.raises(StorePackError, match="store.slug: required"):
        import_store_pack(db_session, clothing)
    assert _count(db_session, Store) == 0


def test_database_error_rolls_back_and_is_reported(db_session, clothing, monkeypatch):
    def failing_commit():
        raise IntegrityError("INSERT INTO stores", {}, Exception("UNIQUE constraint failed: stores.slug"))

    monkeypatch.setattr(db_session, "commit", failing_commit)
    with pytest.raises(StorePackImportError, match="database rejected the import: UNIQUE constraint failed"):
        import_store_pack(db_session, clothing)
    monkeypatch.undo()
    assert _count(db_session, Store) == 0


# ----------------------------------------------------------------------------------- CLI


def test_command_line_imports_each_file(engine, tmp_path, capsys, clothing):
    session_factory = sessionmaker(bind=engine, autoflush=False)
    del clothing["store"]["slug"]
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(clothing), encoding="utf-8")

    assert main([str(CLOTHING), str(bad), str(tmp_path / "missing.json")], session_factory) == 1
    out = capsys.readouterr().out
    assert f"{CLOTHING}: imported (record #1)\n  store clothing-demo: created" in out
    assert f"{bad}: NOT IMPORTED (invalid, record #2)\n  - store.slug: required" in out
    assert "missing.json: cannot read" in out
    assert main([str(CLOTHING)], session_factory) == 0
    assert "store clothing-demo: updated" in capsys.readouterr().out
    assert main([], session_factory) == 2


def test_every_pack_the_validator_accepts_imports_cleanly(engine):
    """The validator's promise: a valid pack imports without surprises. Checked on every
    single-field variant of both fixtures that is still valid (fresh database each time)."""
    from app.db import Base
    from app.services.store_pack_validation import validate_store_pack
    from tests.pack_variants import variants

    session_factory = sessionmaker(bind=engine, autoflush=False)
    imported = 0
    for fixture in (CLOTHING, WIDGETS):
        for description, pack in variants(_load(fixture)):
            if validate_store_pack(pack):
                continue
            with engine.begin() as connection:
                for table in reversed(Base.metadata.sorted_tables):
                    connection.execute(table.delete())
            db = session_factory()
            try:
                import_store_pack(db, pack)
            except Exception as exc:  # pragma: no cover - this is the failure being tested for
                pytest.fail(f"valid pack failed to import ({description}): {type(exc).__name__}: {exc}")
            finally:
                db.close()
            imported += 1
    assert imported > 100
