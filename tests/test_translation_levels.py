"""The two translation levels, case by case (complements tests/test_translations.py):

- Level 1 alone: JSON catalogs (app/locales/*.json).
- Level 2 UI overrides: store -> platform -> JSON, page language then Spanish.
- Level 2 content: translation in the page language, else the text as entered (field by field).
- Source language kept: admin and the JSON API never show translations.
- Packs: i18n blocks -> content translations (per language), ui_translations -> store overrides.

UI text is compared with the catalogs (``es_html(key)`` / ``en_html(key)``), pack data with the fixtures.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from markupsafe import escape
from sqlalchemy import func, select
from starlette.requests import Request

from app import i18n
from app.models import (
    Category,
    Product,
    PropertyDefinition,
    PropertyType,
    PropertyValue,
    Store,
    Translation,
    TranslationEntity,
    User,
)
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import PACK_FILE_SUFFIX, validate_store_pack
from app.services.translations import (
    ContentTranslations,
    TranslationError,
    content_locale,
    delete_content_translations,
    load_ui_overrides,
    set_content_translation,
    set_ui_translation,
)
from app.schemas import ProductOut, PropertyValueOut
from tests.ui_text import en_html, es_html, link

PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
WIDGETS = PACKS_DIR / f"demo-widgets{PACK_FILE_SUFFIX}"
PRODUCT_PAGE = "/stores/shop/p/widget"


def _prop(html: str, key: str) -> str:
    match = re.search(rf'data-key="{key}">(.*?)</dd>', html, re.S)
    assert match, key
    return " ".join(match.group(1).split())


def _rows(db) -> int:
    return db.scalar(select(func.count()).select_from(Translation))


def _add_store(db, slug: str) -> Store:
    store = Store(slug=slug, name=f"Store {slug}", currency="EUR", locale_default="es", theme={})
    db.add(store)
    db.commit()
    return store


@pytest.fixture()
def shop(db_session):
    """Spanish store "shop": category "widgets" (enum color, text notes, number weight,
    boolean waterproof) and product "widget" with a value for each."""
    store = _add_store(db_session, "shop")
    category = Category(store_id=store.id, slug="widgets", name="Widgets source", description="Category description source")
    db_session.add(category)
    db_session.flush()
    definitions = {
        "color": PropertyDefinition(category_id=category.id, key="color", label="Color source", sort_order=1,
                                    prop_type=PropertyType.enum, config={"options": ["Rojo", "Azul"]}),
        "notes": PropertyDefinition(category_id=category.id, key="notes", label="Notes source", sort_order=2,
                                    prop_type=PropertyType.text),
        "weight": PropertyDefinition(category_id=category.id, key="weight", label="Weight source", sort_order=3,
                                     prop_type=PropertyType.number),
        "waterproof": PropertyDefinition(category_id=category.id, key="waterproof", label="Waterproof source",
                                         sort_order=4, prop_type=PropertyType.boolean),
    }
    db_session.add_all(definitions.values())
    db_session.flush()
    product = Product(store_id=store.id, category_id=category.id, slug="widget", name="Widget source",
                      description="Product description source", price_cents=1000)
    product.property_values = [
        PropertyValue(definition_id=definitions["color"].id, value={"value": "Azul"}),
        PropertyValue(definition_id=definitions["notes"].id, value={"value": "Notes value source"}),
        PropertyValue(definition_id=definitions["weight"].id, value={"value": 250}),
        PropertyValue(definition_id=definitions["waterproof"].id, value={"value": True}),
    ]
    db_session.add(product)
    db_session.commit()
    return {"store": store, "category": category, "product": product, **definitions}


# ============================================================================ level 1


def test_level_1_alone_gives_every_page_its_json_text(default_lang_client, db_session, shop):
    assert _rows(db_session) == 0
    home = default_lang_client.get("/").text
    assert link(es_html("nav.stores")) in home and link(es_html("nav.sign_in")) in home
    store_es = default_lang_client.get("/stores/shop").text
    assert link(es_html("store.nav.catalog")) in store_es and es_html("store.categories") in store_es
    store_en = default_lang_client.get("/stores/shop?lang=en").text
    assert link(en_html("store.nav.catalog")) in store_en and en_html("store.categories") in store_en


def test_rows_for_other_stores_or_languages_do_not_change_level_1(default_lang_client, db_session, shop):
    other = _add_store(db_session, "other")
    set_ui_translation(db_session, "es", "store.nav.catalog", "Other store only", store=other)
    set_ui_translation(db_session, "en", "store.nav.catalog", "English only")
    html = default_lang_client.get("/stores/shop?lang=es").text
    assert link(es_html("store.nav.catalog")) in html
    assert "Other store only" not in html and "English only" not in html


def test_translator_for_a_language_code_uses_only_the_json_files(db_session):
    set_ui_translation(db_session, "en", "nav.stores", "Override")
    assert i18n.translator("en")("nav.stores") == i18n.catalogs()["en"]["nav.stores"]


def test_ui_overrides_are_empty_without_the_database_layer():
    request = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": []})
    assert i18n.ui_overrides(request, "en") == {}


def test_wrong_placeholder_parameters_leave_the_text_unformatted():
    assert i18n.translate("en", "action.back_to", wrong="x") == i18n.catalogs()["en"]["action.back_to"]


def test_missing_spanish_catalog_is_a_startup_error(tmp_path, monkeypatch):
    (tmp_path / "en.json").write_text('{"nav.stores": "Stores"}', encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", tmp_path)
    i18n.catalogs.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="Missing reference catalog"):
            i18n.catalogs()
    finally:
        monkeypatch.undo()
        i18n.catalogs.cache_clear()
    assert "es" in i18n.catalogs()


# ================================================================ level 2: UI overrides


def test_override_order_on_a_store_page(default_lang_client, db_session, shop):
    """store -> platform -> JSON, each step only when the previous one has nothing."""
    key = "store.nav.catalog"
    assert link(es_html(key)) in default_lang_client.get("/stores/shop").text  # JSON
    set_ui_translation(db_session, "es", key, "Platform text")
    assert link("Platform text") in default_lang_client.get("/stores/shop").text  # platform beats JSON
    set_ui_translation(db_session, "es", key, "Store text", store=shop["store"])
    assert link("Store text") in default_lang_client.get("/stores/shop").text  # store beats platform
    # Platform pages never use a store's overrides.
    set_ui_translation(db_session, "es", "nav.stores", "Store-only stores", store=shop["store"])
    assert link(es_html("nav.stores")) in default_lang_client.get("/").text


def test_override_in_another_language_is_not_used(default_lang_client, db_session, shop):
    set_ui_translation(db_session, "en", "store.nav.catalog", "English override")
    assert link(es_html("store.nav.catalog")) in default_lang_client.get("/stores/shop?lang=es").text
    assert link("English override") in default_lang_client.get("/stores/shop?lang=en").text


def test_platform_loader_ignores_store_rows(db_session, shop):
    set_ui_translation(db_session, "en", "nav.stores", "Platform")
    set_ui_translation(db_session, "en", "nav.sign_in", "Store", store=shop["store"])
    assert load_ui_overrides(db_session, "en", None) == {"nav.stores": "Platform"}
    assert load_ui_overrides(db_session, "en", shop["store"].id) == {"nav.stores": "Platform", "nav.sign_in": "Store"}


def test_override_text_is_escaped(default_lang_client, db_session):
    set_ui_translation(db_session, "es", "nav.stores", "<b>Stores</b>")
    html = default_lang_client.get("/").text
    assert link("&lt;b&gt;Stores&lt;/b&gt;") in html and "<b>Stores</b>" not in html


def test_override_with_broken_placeholder_syntax_is_rejected(db_session):
    with pytest.raises(TranslationError, match="invalid placeholder syntax"):
        set_ui_translation(db_session, "en", "nav.stores", "Stores {")


# =========================================================== level 2: content fallback


def test_content_falls_back_to_the_text_as_entered_field_by_field(default_lang_client, db_session, shop):
    set_content_translation(db_session, shop["product"], "en", "name", "Widget EN")
    set_content_translation(db_session, shop["color"], "en", "label", "Color EN")
    set_content_translation(db_session, shop["category"], "en", "name", "Widgets EN")

    html = default_lang_client.get(f"{PRODUCT_PAGE}?lang=en").text
    assert "<h1>Widget EN</h1>" in html  # translated
    assert "Product description source" in html  # no translation: as entered
    assert "<dt>Color EN</dt>" in html and "<dt>Notes source</dt>" in html
    category = default_lang_client.get("/stores/shop/c/widgets?lang=en").text
    assert "<h1>Widgets EN</h1>" in category and "Category description source" in category
    # The language without translations shows everything as entered.
    spanish = default_lang_client.get(f"{PRODUCT_PAGE}?lang=es").text
    assert "<h1>Widget source</h1>" in spanish and "<dt>Color source</dt>" in spanish


def test_enum_value_chain_product_then_option_then_as_entered(default_lang_client, db_session, shop):
    url = f"{PRODUCT_PAGE}?lang=en"
    assert _prop(default_lang_client.get(url).text, "color") == "Azul"  # as entered
    set_content_translation(db_session, shop["color"], "en", "option:Azul", "Blue (option)")
    assert _prop(default_lang_client.get(url).text, "color") == "Blue (option)"
    set_content_translation(db_session, shop["product"], "en", "value:color", "Blue (product)")
    assert _prop(default_lang_client.get(url).text, "color") == "Blue (product)"


def test_text_values_translate_but_numbers_and_booleans_never_do(default_lang_client, db_session, shop):
    set_content_translation(db_session, shop["product"], "en", "value:notes", "Notes EN")
    set_content_translation(db_session, shop["product"], "en", "value:weight", "two hundred fifty")
    set_content_translation(db_session, shop["product"], "en", "value:waterproof", "certainly")
    html = default_lang_client.get(f"{PRODUCT_PAGE}?lang=en").text
    assert _prop(html, "notes") == "Notes EN"
    assert _prop(html, "weight") == "250"
    assert _prop(html, "waterproof") == en_html("value.yes")


def test_translations_of_other_languages_are_not_shown(default_lang_client, db_session, shop):
    set_content_translation(db_session, shop["product"], "pt", "name", "Widget PT")
    assert "Widget PT" not in default_lang_client.get(f"{PRODUCT_PAGE}?lang=en").text
    assert "<h1>Widget source</h1>" in default_lang_client.get(f"{PRODUCT_PAGE}?lang=en").text


def test_regional_codes_are_stored_as_their_language(default_lang_client, db_session, shop):
    row = set_content_translation(db_session, shop["product"], "en-US", "name", "Widget US")
    assert row.locale == "en"
    assert "<h1>Widget US</h1>" in default_lang_client.get(f"{PRODUCT_PAGE}?lang=en").text
    set_content_translation(db_session, shop["product"], "en-GB", "name", "Widget GB")  # same language: updated
    assert _rows(db_session) == 1


def test_translated_content_is_escaped(default_lang_client, db_session, shop):
    set_content_translation(db_session, shop["product"], "en", "name", "<script>alert(1)</script>")
    html = default_lang_client.get(f"{PRODUCT_PAGE}?lang=en").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html


def test_product_translation_without_its_category_keeps_labels_as_entered(shop):
    product = ProductOut(id=1, slug="p", name="P", price_cents=0, is_active=True, in_stock=False, category_id=999, properties=[
        PropertyValueOut(key="color", label="Color source", prop_type=PropertyType.enum, value={"value": "Azul"})])
    out = ContentTranslations("en", {(TranslationEntity.product, 1, "name"): "P EN"}).product(product, {})
    assert out.name == "P EN" and out.properties[0].label == "Color source"


# ============================================================== source language kept


def test_api_keeps_the_source_language_even_with_a_language_chosen(default_lang_client, db_session, shop):
    translated = {"Shop EN": (shop["store"], "name"), "Widgets EN": (shop["category"], "name"),
                  "Color EN": (shop["color"], "label"), "Widget EN": (shop["product"], "name"),
                  "Blue EN": (shop["product"], "value:color")}
    for text, (entity, field_name) in translated.items():
        set_content_translation(db_session, entity, "en", field_name, text)
    default_lang_client.get("/?lang=en")  # remembered choice (cookie) ...
    for url in ("/api/stores/shop", "/api/stores/shop/categories", "/api/stores/shop/products",
                "/api/stores/shop/products/widget", "/api/stores"):
        body = default_lang_client.get(f"{url}?lang=en").text  # ... and explicit ?lang=en
        assert not [text for text in translated if text in body], url
    detail = default_lang_client.get("/api/stores/shop/products/widget").json()
    assert detail["name"] == "Widget source"
    assert {p["key"]: p["value"] for p in detail["properties"]}["color"] == {"value": "Azul"}


def test_admin_keeps_the_source_language(admin_client, db_session, shop):
    set_content_translation(db_session, shop["product"], "en", "name", "Widget EN")
    set_content_translation(db_session, shop["category"], "en", "name", "Widgets EN")
    for url in ("/admin/stores/shop", "/admin/stores/shop/products",
                f"/admin/stores/shop/products/{shop['product'].id}",
                f"/admin/stores/shop/categories/{shop['category'].id}"):
        html = admin_client.get(url).text  # admin_client uses ?lang=en
        assert "Widget EN" not in html and "Widgets EN" not in html, url
    assert 'value="Widget source"' in admin_client.get(f"/admin/stores/shop/products/{shop['product'].id}").text


# ================================================================ packs -> level 2


def _pack_translation_count(pack: dict) -> int:
    """Rows a pack's i18n blocks should produce: one per translated text (options one each)."""
    count = 0
    objects = [pack["store"], *pack["categories"], *pack["products"]]
    objects += [d for c in pack["categories"] for d in c.get("property_definitions", [])]
    for obj in objects:
        for block in (obj.get("i18n") or {}).values():
            count += sum(1 for f in ("name", "description", "label") if isinstance(block.get(f), str))
            count += len((block.get("config") or {}).get("options") or [])
            count += len(block.get("values") or {})
    return count


def test_pack_i18n_blocks_become_one_row_per_translated_text(db_session):
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    report = import_store_pack(db_session, pack)
    assert report.translations_written == _pack_translation_count(pack) == _rows(db_session)
    kinds = dict(db_session.execute(
        select(Translation.entity_type, func.count()).group_by(Translation.entity_type)).all())
    assert set(kinds) == {TranslationEntity.store, TranslationEntity.category,
                          TranslationEntity.property_definition, TranslationEntity.product}


def test_pack_regional_i18n_block_is_imported_as_its_language(default_lang_client, db_session):
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    product = pack["products"][0]
    product["i18n"] = {"en-US": product["i18n"]["en"]}
    import_store_pack(db_session, pack)
    html = default_lang_client.get(f"/stores/demo-widgets/p/{product['slug']}?lang=en").text
    assert f"<h1>{escape(product['i18n']['en-US']['name'])}</h1>" in html


@pytest.mark.parametrize(
    "mutate,path,message",
    [
        (lambda p: p["store"].__setitem__("i18n", {"en": {"name": "A"}, "en-GB": {"name": "B"}}),
         "store.i18n.en-GB", 'same language as "en": translations are stored per language ("en"), so use one block'),
        (lambda p: p["categories"][2]["property_definitions"][0]["i18n"]["en"]["config"].__setitem__(
            "options", ["R" * 101, "Blue", "Green"]),
         "categories[2].property_definitions[0].i18n.en.config.options", "each option must be at most 100 characters"),
        (lambda p: p["categories"][2]["property_definitions"][0]["i18n"]["en"]["config"].__setitem__("options", "Red"),
         "categories[2].property_definitions[0].i18n.en.config.options", "must be an array of non-empty strings"),
        # property_definitions[2] is weight_g, a number property
        (lambda p: p["categories"][2]["property_definitions"][2]["i18n"]["en"].__setitem__("config", {"options": ["x"]}),
         "categories[2].property_definitions[2].i18n.en.config.options", "only allowed for enum properties"),
        (lambda p: p["products"][0]["i18n"]["en"]["values"].__setitem__("notes", "n" * 2001),
         "products[0].i18n.en.values.notes", "must be at most 2000 characters"),
    ],
    ids=["same-language-twice", "option-too-long", "options-not-a-list", "options-on-number", "value-too-long"],
)
def test_pack_translation_rules(mutate, path, message):
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    mutate(pack)
    assert (path, message) in [(i.path, i.message) for i in validate_store_pack(pack)]


# ========================================================== services: hardening rules


@pytest.mark.parametrize("locale", ["", "EN", "e", "en_US", "english!", 5, None])
def test_content_locale_rejects_non_language_codes(locale):
    with pytest.raises(TranslationError, match="locale must be a language code"):
        content_locale(locale)


@pytest.mark.parametrize(
    "entity_key,field_name,limit",
    [("product", "name", 200), ("color", "label", 200), ("category", "description", 2000),
     ("product", "description", 5000), ("color", "option:Azul", 100), ("product", "value:notes", 2000)],
)
def test_content_translations_have_the_limits_of_their_field(db_session, shop, entity_key, field_name, limit):
    entity = shop[entity_key]
    set_content_translation(db_session, entity, "en", field_name, "x" * limit)
    with pytest.raises(TranslationError, match=f"at most {limit} characters"):
        set_content_translation(db_session, entity, "en", field_name, "x" * (limit + 1))


def test_only_catalog_rows_can_be_translated(db_session):
    with pytest.raises(TranslationError, match="User rows can't be translated"):
        set_content_translation(db_session, User(email="x@example.com", password_hash="x"), "en", "name", "X")


def test_delete_content_translations_of_one_row(db_session, shop):
    set_content_translation(db_session, shop["product"], "en", "name", "A")
    set_content_translation(db_session, shop["product"], "pt", "name", "B", commit=False)
    set_content_translation(db_session, shop["category"], "en", "name", "C")
    assert delete_content_translations(db_session, shop["product"]) == 2
    assert _rows(db_session) == 1


def test_deleting_a_property_removes_products_translated_values(admin_client, db_session, shop):
    set_content_translation(db_session, shop["product"], "en", "value:notes", "Notes EN")
    set_content_translation(db_session, shop["product"], "en", "value:color", "Blue")
    set_content_translation(db_session, shop["notes"], "en", "label", "Notes label EN")
    url = f"/admin/stores/shop/categories/{shop['category'].id}/properties/{shop['notes'].id}/delete"
    assert admin_client.post(url, follow_redirects=False).status_code == 303
    left = db_session.execute(select(Translation.field)).scalars().all()
    assert left == ["value:color"]  # the deleted property's label and values are gone


def test_category_reference_shows_the_referenced_category_translated_name(default_lang_client, db_session, shop):
    brand = Category(store_id=shop["store"].id, slug="acme", name="Acme source")
    db_session.add(brand)
    db_session.flush()
    ref = PropertyDefinition(category_id=shop["category"].id, key="brand", label="Brand source", sort_order=5,
                             prop_type=PropertyType.category_ref)
    db_session.add(ref)
    db_session.flush()
    db_session.add(PropertyValue(product_id=shop["product"].id, definition_id=ref.id, value={"category_id": brand.id}))
    db_session.commit()
    set_content_translation(db_session, brand, "en", "name", "Acme EN")

    assert _prop(default_lang_client.get(f"{PRODUCT_PAGE}?lang=en").text, "brand") == \
        '<a href="/stores/shop/c/acme">Acme EN</a>'
    assert _prop(default_lang_client.get(f"{PRODUCT_PAGE}?lang=es").text, "brand") == \
        '<a href="/stores/shop/c/acme">Acme source</a>'
