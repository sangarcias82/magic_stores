"""Translations in two levels: JSON catalogs (level 1) refined by the ``translations`` table
(level 2): UI overrides per platform / store, and content translations per row and field.

Expected UI text is read from the catalogs (``es_html(key)`` / ``en_html(key)``) and expected content from
the pack fixtures, so editing a translation does not break these tests.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from markupsafe import escape
from sqlalchemy import func, select

from app import i18n
from app.models import Category, Product, PropertyDefinition, PropertyType, PropertyValue, Store, Translation
from app.services.store_pack_validation import PACK_FILE_SUFFIX, validate_store_pack
from app.services.store_pack_import import import_store_pack
from app.services.translations import (
    TranslationError,
    delete_ui_translation,
    set_content_translation,
    set_ui_translation,
)
from tests.ui_text import en_html, es_html, link

PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
WIDGETS = PACKS_DIR / f"demo-widgets{PACK_FILE_SUFFIX}"


def _count(db, model=Translation) -> int:
    return db.scalar(select(func.count()).select_from(model))


def _add_store(db, slug: str, locale_default: str = "es") -> Store:
    store = Store(slug=slug, name=f"Store {slug}", currency="EUR", locale_default=locale_default, theme={})
    db.add(store)
    db.commit()
    return store


@pytest.fixture()
def shop(db_session):
    """A Spanish-default store with one category (enum + text properties) and one product."""
    store = _add_store(db_session, "shop")
    category = Category(store_id=store.id, slug="widgets", name="Widgets ES", description="Description ES")
    db_session.add(category)
    db_session.flush()
    color = PropertyDefinition(category_id=category.id, key="color", label="Color ES", prop_type=PropertyType.enum,
                               sort_order=1, config={"options": ["Rojo", "Azul"]})
    notes = PropertyDefinition(category_id=category.id, key="notes", label="Notes ES", prop_type=PropertyType.text,
                               sort_order=2)
    db_session.add_all([color, notes])
    db_session.flush()
    product = Product(store_id=store.id, category_id=category.id, slug="widget", name="Widget ES",
                      description="Product description ES", price_cents=1000)
    product.property_values = [
        PropertyValue(definition_id=color.id, value={"value": "Azul"}),
        PropertyValue(definition_id=notes.id, value={"value": "Notes value ES"}),
    ]
    db_session.add(product)
    db_session.commit()
    return store


# --------------------------------------------------------------------- level 1 alone


def test_without_rows_the_json_catalogs_apply(default_lang_client, db_session):
    assert _count(db_session) == 0
    html = default_lang_client.get("/").text
    assert link(es_html("nav.stores")) in html and link(es_html("nav.sign_in")) in html


# ------------------------------------------------------------------- UI overrides


def test_platform_override_replaces_the_json_text_for_its_language_only(default_lang_client, db_session):
    set_ui_translation(db_session, "es", "nav.stores", "Stores (platform override)")
    assert link("Stores (platform override)") in default_lang_client.get("/?lang=es").text
    english = default_lang_client.get("/?lang=en").text
    assert link(en_html("nav.stores")) in english and "platform override" not in english


def test_store_override_applies_only_to_that_store_and_beats_the_platform(default_lang_client, db_session):
    store_a, _ = _add_store(db_session, "store-a"), _add_store(db_session, "store-b")
    set_ui_translation(db_session, "es", "store.nav.catalog", "Catalog (platform)")
    set_ui_translation(db_session, "es", "store.nav.catalog", "Catalog (store A)", store=store_a)
    set_ui_translation(db_session, "es", "nav.stores", "Stores (store A)", store=store_a)

    assert link("Catalog (store A)") in default_lang_client.get("/stores/store-a").text
    assert link("Catalog (platform)") in default_lang_client.get("/stores/store-b").text
    # A store's overrides never leak to platform pages.
    assert link(es_html("nav.stores")) in default_lang_client.get("/").text


def test_resolution_order_across_both_levels(default_lang_client, db_session, monkeypatch):
    """store -> platform -> JSON for the page language, then the same for Spanish."""
    real = i18n.catalogs()
    partial_en = {k: v for k, v in real["en"].items() if k not in ("nav.sign_in", "nav.stores")}
    monkeypatch.setattr(i18n, "catalogs", lambda: {**real, "en": partial_en})
    set_ui_translation(db_session, "es", "nav.sign_in", "Sign in (es override)")
    set_ui_translation(db_session, "en", "login.submit", "Submit (en override)")

    html = default_lang_client.get("/login?lang=en").text
    assert ">Submit (en override)</button>" in html  # 1. en DB override beats en JSON
    assert en_html("login.email") in html  # 2. en JSON when there is no en override
    assert link("Sign in (es override)") in html  # 3. missing in en (DB and JSON) -> es DB override
    assert link(es_html("nav.stores")) in html  # 4. missing in en, no es override -> es JSON


def test_overrides_apply_to_flash_messages(admin_client, db_session):
    set_ui_translation(db_session, "en", "flash.store_created", 'New store "{name}" is ready ({slug}).')
    page = admin_client.post("/admin/stores", data={"slug": "demo", "name": "Demo", "currency": "USD"})
    assert str(escape('New store "Demo" is ready (demo).')) in page.text


def test_setting_an_override_twice_updates_one_row(db_session):
    store = _add_store(db_session, "shop")
    for scope in (None, store):
        set_ui_translation(db_session, "en", "nav.stores", "First", store=scope)
        set_ui_translation(db_session, "en", "nav.stores", "Second", store=scope)
    rows = db_session.scalars(select(Translation.text)).all()
    assert rows == ["Second", "Second"]


def test_deleting_an_override_restores_the_json_text(default_lang_client, db_session):
    set_ui_translation(db_session, "es", "nav.stores", "Temporary")
    assert delete_ui_translation(db_session, "es", "nav.stores") is True
    assert delete_ui_translation(db_session, "es", "nav.stores") is False
    assert link(es_html("nav.stores")) in default_lang_client.get("/").text


@pytest.mark.parametrize(
    "locale,key,text,message",
    [
        ("fr", "nav.stores", "Boutiques", '"fr" is not a UI language; add app/locales/<code>.json first'),
        ("en", "no.such.key", "X", 'unknown UI key "no.such.key" (not in app/locales/es.json)'),
        ("en", "action.back_to", "Back", "placeholders must be the same as the catalog's ({name})"),
        ("en", "nav.stores", "Stores {name}", "placeholders must be the same as the catalog's (none)"),
        ("en", "nav.stores", "  ", "text must be a non-empty string"),
    ],
)
def test_invalid_overrides_are_rejected(db_session, locale, key, text, message):
    with pytest.raises(TranslationError) as caught:
        set_ui_translation(db_session, locale, key, text)
    assert str(caught.value) == message
    assert _count(db_session) == 0


# ------------------------------------------------------------- content translations


def _translate_shop(db):
    store = db.scalar(select(Store).where(Store.slug == "shop"))
    category = db.scalar(select(Category).where(Category.slug == "widgets"))
    color, notes = (db.scalar(select(PropertyDefinition).where(PropertyDefinition.key == k)) for k in ("color", "notes"))
    product = db.scalar(select(Product).where(Product.slug == "widget"))
    for entity, field_name, text in [
        (store, "name", "Shop EN"),
        (category, "name", "Widgets EN"),
        (category, "description", "Description EN"),
        (color, "label", "Color EN"),
        (color, "option:Azul", "Blue (option)"),
        (notes, "label", "Notes EN"),
        (product, "name", "Widget EN"),
        (product, "description", "Product description EN"),
        (product, "value:notes", "Notes value EN"),
    ]:
        set_content_translation(db, entity, "en", field_name, text)
    return product


def test_storefront_shows_content_in_the_page_language(default_lang_client, db_session, shop):
    _translate_shop(db_session)
    spanish = default_lang_client.get("/stores/shop/p/widget").text
    for original in ("Widget ES", "Product description ES", "Color ES", "Azul", "Notes value ES"):
        assert original in spanish
    assert "Widget EN" not in spanish

    english = default_lang_client.get("/stores/shop/p/widget?lang=en").text
    for translated in ("Shop EN", "Widget EN", "Product description EN", "Color EN", "Blue (option)",
                       "Notes EN", "Notes value EN", "Widgets EN"):
        assert translated in english
    assert "Widget ES" not in english

    category_page = default_lang_client.get("/stores/shop/c/widgets?lang=en").text
    assert "Widgets EN" in category_page and "Description EN" in category_page and "Widget EN" in category_page
    assert "Shop EN" in default_lang_client.get("/?lang=en").text  # platform home store list


def test_product_value_translation_beats_the_option_translation(default_lang_client, db_session, shop):
    product = _translate_shop(db_session)
    set_content_translation(db_session, product, "en", "value:color", "Blue (this product)")
    html = default_lang_client.get("/stores/shop/p/widget?lang=en").text
    assert "Blue (this product)" in html and "Blue (option)" not in html


def test_api_and_admin_keep_the_content_as_entered(admin_client, db_session, shop):
    product = _translate_shop(db_session)
    detail = admin_client.get("/api/stores/shop/products/widget").json()  # admin_client sends ?lang=en
    assert detail["name"] == "Widget ES"
    assert {p["key"]: p["value"]["value"] for p in detail["properties"]}["color"] == "Azul"
    form = admin_client.get(f"/admin/stores/shop/products/{product.id}").text
    assert 'value="Widget ES"' in form


@pytest.mark.parametrize(
    "field_name,message",
    [
        ("label", 'product has no translatable field "label" (allowed: name, description, value:<...>)'),
        ("value:", 'product has no translatable field "value:" (allowed: name, description, value:<...>)'),
    ],
)
def test_invalid_content_fields_are_rejected(db_session, shop, field_name, message):
    product = db_session.scalar(select(Product))
    with pytest.raises(TranslationError) as caught:
        set_content_translation(db_session, product, "en", field_name, "X")
    assert str(caught.value) == message


def test_deleting_rows_deletes_their_translations(admin_client, db_session, shop):
    product = _translate_shop(db_session)
    before = _count(db_session)
    response = admin_client.post(f"/admin/stores/shop/products/{product.id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert _count(db_session) == before - 3  # name, description, value:notes

    category = db_session.scalar(select(Category))
    db_session.delete(category)  # also deletes its property definitions (ORM cascade)
    db_session.commit()
    remaining = db_session.execute(select(Translation.entity_type, Translation.field)).all()
    assert [(t.value, f) for t, f in remaining] == [("store", "name")]


# ----------------------------------------------------------------------- store packs


def test_pack_i18n_blocks_are_imported_and_shown(default_lang_client, db_session):
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    report = import_store_pack(db_session, pack)
    assert report.skipped == [] and report.translations_written == _count(db_session)

    product = pack["products"][0]
    definitions = {d["key"]: d for d in pack["categories"][2]["property_definitions"]}
    html_es = default_lang_client.get(f"/stores/demo-widgets/p/{product['slug']}?lang=es").text
    assert str(escape(product["name"])) in html_es and str(escape(product["values"]["color"])) in html_es

    html_en = default_lang_client.get(f"/stores/demo-widgets/p/{product['slug']}?lang=en").text
    translated = product["i18n"]["en"]
    assert str(escape(translated["name"])) in html_en and str(escape(translated["description"])) in html_en
    assert str(escape(translated["values"]["color"])) in html_en
    assert str(escape(translated["values"]["notes"])) in html_en
    assert str(escape(definitions["size"]["i18n"]["en"]["label"])) in html_en
    assert str(escape(pack["store"]["i18n"]["en"]["name"])) in html_en
    assert str(escape(product["name"])) not in html_en


def test_pack_enum_options_are_translated_in_order(db_session):
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    import_store_pack(db_session, pack)
    color = next(d for d in pack["categories"][2]["property_definitions"] if d["key"] == "color")
    rows = dict(db_session.execute(
        select(Translation.field, Translation.text).where(Translation.field.like("option:%"))
    ).all())
    expected = dict(zip(color["config"]["options"], color["i18n"]["en"]["config"]["options"]))
    assert rows == {f"option:{orig}": text for orig, text in expected.items()}


def test_pack_ui_translations_become_store_overrides(default_lang_client, db_session):
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    pack["ui_translations"] = {"es": {"store.nav.catalog": "Catalog (pack override)"}}
    import_store_pack(db_session, pack)
    _add_store(db_session, "other")
    assert link("Catalog (pack override)") in default_lang_client.get("/stores/demo-widgets?lang=es").text
    assert link(es_html("store.nav.catalog")) in default_lang_client.get("/stores/other?lang=es").text

    del pack["ui_translations"]  # re-import without it: the store's overrides are removed
    import_store_pack(db_session, pack)
    assert link(es_html("store.nav.catalog")) in default_lang_client.get("/stores/demo-widgets?lang=es").text


def test_reimport_replaces_the_translations_of_named_rows(db_session):
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    import_store_pack(db_session, pack)
    total = _count(db_session)
    import_store_pack(db_session, copy.deepcopy(pack))
    assert _count(db_session) == total  # replaced, not duplicated

    product = db_session.scalar(select(Product).where(Product.slug == pack["products"][0]["slug"]))
    set_content_translation(db_session, product, "pt", "name", "Extra (not in the pack)")
    del pack["products"][0]["i18n"]
    import_store_pack(db_session, pack)
    assert db_session.scalar(
        select(func.count()).select_from(Translation).where(Translation.entity_id == product.id,
                                                            Translation.entity_type == "product")
    ) == 0


@pytest.mark.parametrize(
    "ui_translations,path,message",
    [
        ({"fr": {"nav.stores": "X"}}, "ui_translations.fr", 'must be a UI language (es, en), got "fr"'),
        ({"en": {"nope": "X"}}, "ui_translations.en.nope", 'unknown UI key "nope" (not in app/locales/es.json)'),
        ({"en": {"action.back_to": "Back"}}, "ui_translations.en.action.back_to",
         "placeholders must be the same as the catalog's ({name})"),
        ({"en": []}, "ui_translations.en", "must be an object, got array"),
    ],
)
def test_pack_ui_translations_are_validated(ui_translations, path, message):
    pack = {"version": 1, "store": {"slug": "s", "name": "S", "currency": "EUR"}, "ui_translations": ui_translations}
    assert [(i.path, i.message) for i in validate_store_pack(pack)] == [(path, message)]
