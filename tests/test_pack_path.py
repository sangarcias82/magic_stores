"""The pack path end to end: upload a fixture in admin -> public storefront and API show every
imported product property; uploading the same fixture again always ends in a known state.

Expected values are derived from the fixture files and UI text from the catalogs, so editing
either does not break these tests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import pytest
from markupsafe import escape
from sqlalchemy import func, insert, select

from app import i18n
from app.models import Category, PackImport, PackImportStatus, Product, PropertyDefinition, PropertyValue, Store, Translation
from app.services import catalog, store_pack_import
from app.services.store_pack_validation import MAX_JSON_DEPTH, PACK_FILE_SUFFIX
from tests.ui_text import ui_html

UPLOAD = "/admin/packs"
PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
CLOTHING = PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}"
WIDGETS = PACKS_DIR / f"demo-widgets{PACK_FILE_SUFFIX}"
TABLES = (Store, Category, PropertyDefinition, Product, PropertyValue, Translation)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _upload(client, content: bytes, *, update: bool = False, follow: bool = False):
    data = {"update_existing": "1"} if update else {}
    return client.post(UPLOAD, files={"pack": ("pack.json", content, "application/json")}, data=data,
                       follow_redirects=follow)


@pytest.fixture()
def default_lang_admin(default_lang_client):
    """An admin with no language chosen (storefront pages use each store's language)."""
    response = default_lang_client.post(
        "/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"}, follow_redirects=False
    )
    assert response.status_code == 303
    return default_lang_client


# --------------------------------------------------------- expected output from a fixture


def _translated(obj: dict, lang: str, field: str) -> Any:
    """A pack text in ``lang``: its i18n block if it has one, else the original."""
    return (obj.get("i18n") or {}).get(lang, {}).get(field, obj.get(field))


def _page_lang(pack: dict, query_lang: Optional[str]) -> str:
    return query_lang or i18n.normalize_locale(pack["store"].get("locale_default")) or i18n.DEFAULT_LOCALE


def _expected_properties(pack: dict, product: dict, lang: str) -> list[tuple[str, str, str]]:
    """(key, label, rendered value) in display order, as product.html renders them."""
    categories = {c["slug"]: c for c in pack["categories"]}
    definitions = sorted(categories[product["category_slug"]]["property_definitions"],
                         key=lambda d: d.get("sort_order", 0))
    value_translations = (product.get("i18n") or {}).get(lang, {}).get("values", {})
    rows = []
    for d in definitions:
        value = (product.get("values") or {}).get(d["key"])
        kind = d["prop_type"]
        if value is None:
            shown = f'<span class="meta">{ui_html(lang, "store.no_image")}</span>' if kind == "image" \
                else '<span class="meta">—</span>'
        elif kind == "boolean":
            shown = ui_html(lang, "value.yes" if value else "value.no")
        elif kind == "image":
            shown = f'<img src="{escape(value)}" alt="{escape(_translated(d, lang, "label"))}" class="prop-image">'
        elif kind == "category_ref":
            ref = categories[value]
            shown = f'<a href="/stores/{pack["store"]["slug"]}/c/{value}">{escape(_translated(ref, lang, "name"))}</a>'
        elif kind in ("text", "enum") and d["key"] in value_translations:
            shown = str(escape(value_translations[d["key"]]))
        elif kind == "enum":
            options = d["config"]["options"]
            translated = ((d.get("i18n") or {}).get(lang, {}).get("config") or {}).get("options") or options
            shown = str(escape(translated[options.index(value)]))
        else:
            shown = str(escape(value))
        rows.append((d["key"], str(escape(_translated(d, lang, "label"))), shown))
    return rows


def _rendered_properties(html: str) -> list[tuple[str, str, str]]:
    pattern = r'<dt>(.*?)</dt>\s*<dd class="prop prop-\w+" data-key="(\w+)">(.*?)</dd>'
    return [(key, label, " ".join(value.split())) for label, key, value in re.findall(pattern, html, re.S)]


def _snapshot(db) -> dict:
    """Every row id per table + what the API shows: proof that nothing changed."""
    db.expire_all()
    return {model.__tablename__: sorted(db.scalars(select(model.id))) for model in TABLES}


# ------------------------------------------------------------ storefront after an upload


@pytest.mark.parametrize("fixture", [CLOTHING, WIDGETS], ids=["clothing", "widgets"])
@pytest.mark.parametrize("query_lang", [None, "es", "en"], ids=["store-default", "es", "en"])
def test_storefront_shows_every_imported_property(default_lang_admin, fixture, query_lang):
    pack = _load(fixture)
    assert _upload(default_lang_admin, fixture.read_bytes()).status_code == 303
    lang = _page_lang(pack, query_lang)
    suffix = f"?lang={query_lang}" if query_lang else ""

    for product in pack["products"]:
        page = default_lang_admin.get(f"/stores/{pack['store']['slug']}/p/{product['slug']}{suffix}")
        assert page.status_code == 200
        html = page.text
        assert f'<html lang="{lang}">' in html
        assert f"<h1>{escape(_translated(product, lang, 'name'))}</h1>" in html
        assert catalog.format_price(product["price_cents"], pack["store"]["currency"]) in html
        assert _rendered_properties(html) == _expected_properties(pack, product, lang)


def test_storefront_falls_back_to_the_pack_enum_option_translations(default_lang_admin):
    """Without a product-level translation of an enum value, the property's translated
    options (by position) are shown."""
    pack = _load(WIDGETS)
    for product in pack["products"]:
        product["i18n"]["en"]["values"].pop("color")
    _upload(default_lang_admin, json.dumps(pack).encode())
    color = next(d for d in pack["categories"][2]["property_definitions"] if d["key"] == "color")
    options, translated = color["config"]["options"], color["i18n"]["en"]["config"]["options"]
    for product in pack["products"]:
        html = default_lang_admin.get(f"/stores/{pack['store']['slug']}/p/{product['slug']}?lang=en").text
        rendered = dict((key, value) for key, _, value in _rendered_properties(html))
        assert rendered["color"] == str(escape(translated[options.index(product["values"]["color"])]))
        assert _rendered_properties(html) == _expected_properties(pack, product, "en")


@pytest.mark.parametrize("fixture", [CLOTHING, WIDGETS], ids=["clothing", "widgets"])
def test_api_detail_matches_the_fixture_after_upload(admin_client, fixture):
    pack = _load(fixture)
    slug = pack["store"]["slug"]
    _upload(admin_client, fixture.read_bytes())
    category_ids = {c["slug"]: c["id"] for c in admin_client.get(f"/api/stores/{slug}/categories").json()}
    kinds = {d["key"]: d["prop_type"] for c in pack["categories"] for d in c.get("property_definitions", [])}

    for product in pack["products"]:
        detail = admin_client.get(f"/api/stores/{slug}/products/{product['slug']}").json()
        assert (detail["name"], detail["price_cents"], detail["category_id"]) == (
            product["name"], product["price_cents"], category_ids[product["category_slug"]]
        )
        expected = {}
        for key, value in product["values"].items():
            if value is None:
                expected[key] = {}
            elif kinds[key] == "image":
                expected[key] = {"media_path": value}
            elif kinds[key] == "category_ref":
                expected[key] = {"category_id": category_ids[value]}
            else:
                expected[key] = {"value": value}
        got = {p["key"]: p["value"] for p in detail["properties"]}
        assert {k: got[k] for k in expected} == expected


def test_store_and_category_pages_list_the_imported_catalog(default_lang_admin):
    pack = _load(CLOTHING)
    _upload(default_lang_admin, CLOTHING.read_bytes())
    slug, (category,), (product,) = pack["store"]["slug"], pack["categories"], pack["products"]
    store_page = default_lang_admin.get(f"/stores/{slug}").text
    assert f'href="/stores/{slug}/p/{product["slug"]}">{escape(product["name"])}</a>' in store_page
    assert f'href="/stores/{slug}/c/{category["slug"]}">{escape(category["name"])}</a>' in store_page

    category_page = default_lang_admin.get(f"/stores/{slug}/c/{category['slug']}").text
    for d in category["property_definitions"]:
        assert f"<strong>{escape(d['label'])}</strong> <code>{d['key']}</code>" in category_page


def test_hidden_product_from_a_pack_is_not_public(admin_client):
    pack = _load(CLOTHING)
    hidden = {**pack["products"][0], "slug": "linen-shirt-draft", "name": "Draft shirt", "is_active": False}
    pack["products"].append(hidden)
    _upload(admin_client, json.dumps(pack).encode())
    slug = pack["store"]["slug"]
    assert admin_client.get(f"/stores/{slug}/p/linen-shirt-draft").status_code == 404
    assert admin_client.get(f"/api/stores/{slug}/products/linen-shirt-draft").status_code == 404
    assert "Draft shirt" not in admin_client.get(f"/stores/{slug}").text
    assert "Draft shirt" in admin_client.get(f"/admin/stores/{slug}/products").text


# ------------------------------------------------------------- re-uploading the same file


def test_same_file_without_update_is_refused_and_changes_nothing(admin_client, db_session):
    raw = CLOTHING.read_bytes()
    slug = _load(CLOTHING)["store"]["slug"]
    assert _upload(admin_client, raw).status_code == 303
    before = _snapshot(db_session)

    again = _upload(admin_client, raw)
    assert again.status_code == 409
    assert ui_html("en", "admin.packs.error.same_file", slug=slug, record=1) in again.text
    assert _snapshot(db_session) == before
    statuses = [r.status for r in db_session.scalars(select(PackImport).order_by(PackImport.id))]
    assert statuses == [PackImportStatus.succeeded, PackImportStatus.refused]


def test_same_file_hint_points_at_the_latest_successful_import(admin_client):
    raw = CLOTHING.read_bytes()
    slug = _load(CLOTHING)["store"]["slug"]
    _upload(admin_client, raw)
    _upload(admin_client, raw, update=True)  # record #2
    assert ui_html("en", "admin.packs.error.same_file", slug=slug, record=2) in _upload(admin_client, raw).text


def test_different_file_for_an_existing_store_gets_the_plain_refusal(admin_client):
    pack = _load(CLOTHING)
    _upload(admin_client, json.dumps(pack).encode())
    pack["products"][0]["price_cents"] += 100  # another file for the same store
    response = _upload(admin_client, json.dumps(pack).encode())
    assert response.status_code == 409
    assert ui_html("en", "admin.packs.error.exists", slug=pack["store"]["slug"]) in response.text
    assert ui_html("en", "admin.packs.error.same_file", slug=pack["store"]["slug"], record=1) not in response.text


@pytest.mark.parametrize("fixture", [CLOTHING, WIDGETS], ids=["clothing", "widgets"])
def test_reuploading_with_update_is_idempotent(admin_client, db_session, fixture):
    raw = fixture.read_bytes()
    pack = json.loads(raw)
    slug = pack["store"]["slug"]
    _upload(admin_client, raw)
    first_rows = _snapshot(db_session)
    first_pages = {p["slug"]: admin_client.get(f"/stores/{slug}/p/{p['slug']}").text for p in pack["products"]}
    first_api = {p["slug"]: admin_client.get(f"/api/stores/{slug}/products/{p['slug']}").json() for p in pack["products"]}

    for _ in range(2):
        assert _upload(admin_client, raw, update=True).status_code == 303

    rows = _snapshot(db_session)
    # Same rows, same ids; translation rows are rewritten (same content, new ids).
    assert {t: ids for t, ids in rows.items() if t != "translations"} == {
        t: ids for t, ids in first_rows.items() if t != "translations"
    }
    assert len(rows["translations"]) == len(first_rows["translations"])
    for product in pack["products"]:
        assert admin_client.get(f"/stores/{slug}/p/{product['slug']}").text == first_pages[product["slug"]]
        assert admin_client.get(f"/api/stores/{slug}/products/{product['slug']}").json() == first_api[product["slug"]]

    records = list(db_session.scalars(select(PackImport).order_by(PackImport.id)))
    assert [r.status for r in records] == [PackImportStatus.succeeded] * 3
    for record in records[1:]:
        assert record.store_created is False
        assert {k: record.report[k]["created"] for k in ("categories", "properties", "products")} == {
            "categories": 0, "properties": 0, "products": 0
        }


def test_reupload_with_update_restores_the_pack_state(admin_client, db_session):
    pack = _load(CLOTHING)
    slug, product = pack["store"]["slug"], pack["products"][0]
    _upload(admin_client, CLOTHING.read_bytes())
    product_id = admin_client.get(f"/api/stores/{slug}/products/{product['slug']}").json()["id"]
    category_id = admin_client.get(f"/api/stores/{slug}/categories").json()[0]["id"]
    # Admin edits the imported product and adds one of its own.
    edit = {"slug": product["slug"], "sku": "PACK-1", "stock": "4", "name": "Edited in admin", "price": "1.00", "prop_size": "S",
            "prop_color": product["values"]["color"]}
    assert admin_client.post(f"/admin/stores/{slug}/products/{product_id}", data=edit,
                             follow_redirects=False).status_code == 303
    admin_client.post(f"/admin/stores/{slug}/products", data={
        "category_id": str(category_id), "slug": "admin-tee", "sku": "ADMIN-TEE", "stock": "3", "name": "Admin tee",
        "price": "5", "prop_size": "M", "prop_color": product["values"]["color"]})

    assert _upload(admin_client, CLOTHING.read_bytes(), update=True).status_code == 303
    html = admin_client.get(f"/stores/{slug}/p/{product['slug']}").text
    assert _rendered_properties(html) == _expected_properties(pack, product, "en")
    assert f"<h1>{escape(product['name'])}</h1>" in html
    assert admin_client.get(f"/api/stores/{slug}/products/admin-tee").status_code == 200  # kept


def test_concurrent_duplicate_upload_ends_as_a_conflict(admin_client, db_session, monkeypatch):
    """Two uploads of a new store at once: both pass the "store exists" check, the second one
    then hits the unique slug. Simulated by inserting the store just before the import writes it."""
    pack = _load(CLOTHING)
    original = store_pack_import._Importer.store

    def racing_store(self, existing):
        self.db.execute(insert(Store).values(slug=pack["store"]["slug"], name="Other upload", currency="USD",
                                             locale_default="es", theme={}))
        return original(self, existing)

    monkeypatch.setattr(store_pack_import._Importer, "store", racing_store)
    response = _upload(admin_client, CLOTHING.read_bytes())
    assert response.status_code == 409 and ui_html("en", "admin.packs.error.conflict") in response.text
    assert "database rejected the import" in response.text and "UNIQUE constraint failed: stores.slug" in response.text
    record = db_session.scalar(select(PackImport))
    assert record.status == PackImportStatus.conflict and record.exception is None
    assert db_session.scalar(select(func.count()).select_from(Category)) == 0  # nothing partial


# ------------------------------------------------------------------ hostile / broken files


@pytest.mark.parametrize("depth", [MAX_JSON_DEPTH + 1, 100_000], ids=["just-over-the-limit", "parser-limit"])
def test_deeply_nested_json_is_an_invalid_file_not_a_crash(admin_client, db_session, depth):
    if depth == MAX_JSON_DEPTH + 1:  # parses, but is deeper than allowed (inside an open theme)
        nested = "[" * (depth - 3) + "]" * (depth - 3)
        raw = ('{"version": 1, "store": {"slug": "s", "name": "S", "currency": "EUR", "theme": {"x": '
               + nested + "}}}").encode()
    else:  # deeper than Python's JSON parser can go
        raw = b"[" * depth + b"]" * depth
    response = _upload(admin_client, raw)
    assert response.status_code == 400 and ui_html("en", "admin.packs.error.invalid") in response.text
    assert f"the JSON is nested too deeply (more than {MAX_JSON_DEPTH} levels)" in response.text
    record = db_session.scalar(select(PackImport))
    assert record.status == PackImportStatus.invalid and record.exception is None


def test_pack_at_the_depth_limit_is_accepted(admin_client):
    nested = "[" * (MAX_JSON_DEPTH - 3) + "]" * (MAX_JSON_DEPTH - 3)
    raw = ('{"version": 1, "store": {"slug": "s", "name": "S", "currency": "EUR", "theme": {"x": '
           + nested + "}}}").encode()
    assert _upload(admin_client, raw).status_code == 303
