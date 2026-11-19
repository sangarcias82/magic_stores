"""Admin "Translations" tab (/admin/stores/<slug>/translations): edit a store's content
translations and its UI overrides, through app.services.translations.

UI text is compared with the catalogs (``es_html(key)`` / ``en_html(key)``), catalog data with the pack.
``admin_client`` calls every endpoint with ?lang=en; ``default_lang_admin`` uses the default (es).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from markupsafe import escape
from sqlalchemy import func, select

from app.models import Product, Store, Translation, TranslationEntity
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import PACK_FILE_SUFFIX
from app.services.translations import (
    ContentTranslations,
    TranslationError,
    delete_content_translation,
    save_content_translations,
    set_ui_translation,
    translatable_fields,
)
from tests.ui_text import en_html, es_html

PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
WIDGETS = PACKS_DIR / f"demo-widgets{PACK_FILE_SUFFIX}"
CLOTHING = PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}"
PAGE = "/admin/stores/demo-widgets/translations"


def _flash(html: str) -> str:
    return re.search(r'<p class="flash" role="status">(.*?)</p>', html, re.S).group(1)


def _rows(db, **where) -> int:
    query = select(func.count()).select_from(Translation)
    for column, value in where.items():
        query = query.where(getattr(Translation, column) == value)
    return db.scalar(query)


def _field(db, entity_type: TranslationEntity, field: str, slug: str = "demo-widgets"):
    store = db.scalar(select(Store).where(Store.slug == slug))
    return next(f for f in translatable_fields(db, store) if f.entity_type == entity_type and f.field == field)


@pytest.fixture()
def widgets(db_session) -> dict:
    """demo-widgets imported (Spanish content, with its English pack translations)."""
    pack = json.loads(WIDGETS.read_text(encoding="utf-8"))
    import_store_pack(db_session, pack)
    return pack


@pytest.fixture()
def default_lang_admin(default_lang_client):
    response = default_lang_client.post(
        "/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"}, follow_redirects=False
    )
    assert response.status_code == 303
    return default_lang_client


# ------------------------------------------------------------------------------- the page


def test_page_is_a_store_tab_in_spanish_by_default(default_lang_admin, widgets):
    html = default_lang_admin.get(PAGE).text
    assert '<html lang="es">' in html
    assert f'href="{PAGE}" aria-current="page">{es_html("admin.tabs.translations")}</a>' in html
    for key in ("admin.translations.content", "admin.translations.save", "admin.translations.ui",
                "admin.translations.ui_save"):
        assert es_html(key) in html
    # The tab is on every store page.
    assert f'href="{PAGE}" >{es_html("admin.tabs.translations")}</a>' in default_lang_admin.get(
        "/admin/stores/demo-widgets").text


def test_page_lists_every_translatable_text_with_its_source(admin_client, db_session, widgets):
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    fields = translatable_fields(db_session, store)
    html = admin_client.get(PAGE).text
    assert html.count('name="tr|') == len(fields)
    for f in fields:
        assert f'name="{escape(f.form_name)}"' in html
        assert f'<td class="source-text">{escape(f.source)}</td>' in html


def test_existing_translations_are_prefilled(admin_client, widgets):
    product = widgets["products"][0]
    html = admin_client.get(f"{PAGE}?locale=en").text
    assert f'value="{escape(product["i18n"]["en"]["name"])}"' in html
    assert f'value="{escape(widgets["store"]["i18n"]["en"]["name"])}"' in html


def test_edited_language_defaults_to_one_that_is_not_the_store_language(admin_client, db_session, widgets):
    assert '<option value="en" selected>' in admin_client.get(PAGE).text  # es store -> en
    assert '<option value="es" selected>' in admin_client.get(f"{PAGE}?locale=es").text
    assert '<option value="en" selected>' in admin_client.get(f"{PAGE}?locale=fr").text  # unknown -> default
    import_store_pack(db_session, json.loads(CLOTHING.read_text(encoding="utf-8")))  # an "en" store
    assert '<option value="es" selected>' in admin_client.get("/admin/stores/clothing-demo/translations").text


# ------------------------------------------------------------------- content translations


def test_saving_translations_reaches_the_storefront(admin_client, db_session, widgets):
    name = _field(db_session, TranslationEntity.product, "name")
    option = _field(db_session, TranslationEntity.property_definition, "option:Rojo")
    response = admin_client.post(PAGE, data={"locale": "en", name.form_name: "Widget (edited)",
                                              option.form_name: "Red (edited)"}, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"{PAGE}?locale=en"
    page = admin_client.get(response.headers["location"]).text
    assert _flash(page) == en_html("flash.translations_saved", language=en_html("lang.name"), count=2)

    product_slug = name.entity.slug
    storefront = admin_client.get(f"/stores/demo-widgets/p/{product_slug}").text  # admin_client: ?lang=en
    assert "<h1>Widget (edited)</h1>" in storefront
    # Admin and the API keep the source text.
    assert admin_client.get(f"/api/stores/demo-widgets/products/{product_slug}").json()["name"] == name.source


def test_empty_field_removes_the_translation(admin_client, db_session, widgets):
    name = _field(db_session, TranslationEntity.product, "name")
    assert _rows(db_session, entity_type=TranslationEntity.product, entity_id=name.entity_id, field="name") == 1
    admin_client.post(PAGE, data={"locale": "en", name.form_name: "   "})
    assert _rows(db_session, entity_type=TranslationEntity.product, entity_id=name.entity_id, field="name") == 0
    html = admin_client.get(f"/stores/demo-widgets/p/{name.entity.slug}").text
    assert f"<h1>{escape(name.source)}</h1>" in html  # as entered


def test_saving_the_page_unchanged_changes_nothing(admin_client, db_session, widgets):
    """What the form sends back when the admin changes nothing: every field, as stored."""
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    stored = ContentTranslations.load(db_session, "en", [store.id]).texts
    data = {"locale": "en", **{f.form_name: stored.get((f.entity_type, f.entity_id, f.field), "")
                               for f in translatable_fields(db_session, store)}}
    before = _rows(db_session)
    page = admin_client.post(PAGE, data=data, follow_redirects=True).text
    assert _flash(page) == en_html("flash.translations_saved", language=en_html("lang.name"), count=0)
    assert _rows(db_session) == before


def test_one_invalid_field_saves_nothing(default_lang_admin, db_session, widgets):
    name = _field(db_session, TranslationEntity.product, "name")
    option = _field(db_session, TranslationEntity.property_definition, "option:Azul")
    before = _rows(db_session)
    response = default_lang_admin.post(PAGE, data={"locale": "en", name.form_name: "Fine",
                                                   option.form_name: "x" * 101})
    assert response.status_code == 400
    assert es_html("admin.translations.not_saved") in response.text
    assert f'<p class="field-error">{es_html("admin.translations.error.too_long", max_len=100)}</p>' in response.text
    assert 'value="Fine"' in response.text  # what the admin typed is kept
    assert _rows(db_session) == before
    assert "Fine" not in default_lang_admin.get(f"/stores/demo-widgets/p/{name.entity.slug}?lang=en").text


def test_fields_of_other_stores_and_unknown_fields_are_ignored(admin_client, db_session, widgets):
    import_store_pack(db_session, json.loads(CLOTHING.read_text(encoding="utf-8")))
    other = db_session.scalar(select(Product).where(Product.slug == "linen-shirt-m"))
    before = _rows(db_session)
    admin_client.post(PAGE, data={"locale": "en", f"tr|product|{other.id}|name": "Hijacked",
                                  "tr|product|999999|name": "Nope", "tr|store|1|nonsense": "x", "random": "y"})
    assert _rows(db_session) == before
    assert "Hijacked" not in admin_client.get("/stores/clothing-demo/p/linen-shirt-m?lang=en").text


def test_a_form_with_more_than_a_thousand_fields_is_accepted(admin_client, db_session, widgets):
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    category_id = db_session.scalar(select(Product.category_id).where(Product.store_id == store.id))
    db_session.add_all([Product(store_id=store.id, category_id=category_id, slug=f"bulk-{i}", name=f"Bulk {i}",
                                description=f"Bulk description {i}", price_cents=1) for i in range(600)])
    db_session.commit()
    fields = translatable_fields(db_session, store)
    assert len(fields) > 1000
    data = {"locale": "en", **{f.form_name: f"{f.source} EN" for f in fields if f.source.startswith("Bulk")}}
    response = admin_client.post(PAGE, data=data, follow_redirects=False)
    assert response.status_code == 303
    assert "<h1>Bulk 599 EN</h1>" in admin_client.get("/stores/demo-widgets/p/bulk-599").text


# -------------------------------------------------------------------------- UI overrides


def test_set_a_store_ui_override(admin_client, db_session, widgets):
    import_store_pack(db_session, json.loads(CLOTHING.read_text(encoding="utf-8")))
    response = admin_client.post(f"{PAGE}/ui", data={"locale": "en", "key": " store.nav.catalog ", "text": "Shop"},
                                 follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"{PAGE}?locale=en"
    page = admin_client.get(response.headers["location"]).text
    assert _flash(page) == en_html("flash.ui_override_saved", key="store.nav.catalog")
    assert "<td><code>store.nav.catalog</code></td>" in page and "<td>Shop</td>" in page
    assert f'<td class="meta">{en_html("store.nav.catalog")}</td>' in page  # the default text, for comparison

    assert ">Shop</a>" in admin_client.get("/stores/demo-widgets").text
    assert f">{en_html('store.nav.catalog')}</a>" in admin_client.get("/stores/clothing-demo").text  # other store
    # Setting the same key again updates it.
    admin_client.post(f"{PAGE}/ui", data={"locale": "en", "key": "store.nav.catalog", "text": "Shop now"})
    assert _rows(db_session, entity_type=TranslationEntity.ui) == 1


def test_delete_a_store_ui_override(admin_client, db_session, widgets):
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    set_ui_translation(db_session, "en", "store.nav.catalog", "Shop", store=store)
    response = admin_client.post(f"{PAGE}/ui/delete", data={"locale": "en", "key": "store.nav.catalog"},
                                 follow_redirects=True)
    assert _flash(response.text) == en_html("flash.ui_override_deleted", key="store.nav.catalog")
    assert en_html("admin.translations.no_overrides") in response.text
    assert f">{en_html('store.nav.catalog')}</a>" in admin_client.get("/stores/demo-widgets").text
    # Deleting something that isn't there just goes back, without a message.
    again = admin_client.post(f"{PAGE}/ui/delete", data={"locale": "en", "key": "store.nav.catalog"},
                              follow_redirects=True)
    assert again.status_code == 200 and 'class="flash"' not in again.text


@pytest.mark.parametrize(
    "key,text,message_key,params",
    [
        ("no.such.key", "x", "admin.translations.error.unknown_key", {"key": "no.such.key"}),
        ("action.back_to", "Volver", "admin.translations.error.placeholders", {"expected": "{name}"}),
        ("action.back_to", "Volver a {name", "admin.translations.error.placeholder_syntax", {}),
        ("store.nav.catalog", "   ", "admin.translations.error.empty", {}),
        ("store.nav.catalog", "x" * 5001, "admin.translations.error.too_long", {"max_len": 5000}),
    ],
    ids=["unknown-key", "placeholders", "placeholder-syntax", "empty", "too-long"],
)
def test_invalid_ui_override_is_rejected_in_spanish(default_lang_admin, db_session, widgets, key, text, message_key,
                                                    params):
    response = default_lang_admin.post(f"{PAGE}/ui", data={"locale": "es", "key": key, "text": text})
    assert response.status_code == 400
    assert f'role="alert">{es_html(message_key, **params)}</p>' in response.text
    assert f'value="{escape(key.strip())}"' in response.text  # the form keeps the key
    assert _rows(db_session, entity_type=TranslationEntity.ui) == 0


def test_pages_require_an_admin(client, widgets):
    for method, url in (("get", PAGE), ("post", PAGE), ("post", f"{PAGE}/ui"), ("post", f"{PAGE}/ui/delete")):
        response = getattr(client, method)(url, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_unknown_store_is_404(admin_client):
    assert admin_client.get("/admin/stores/nope/translations").status_code == 404


# ------------------------------------------------------------------------ service helpers


def test_translatable_fields_cover_what_the_storefront_translates(db_session, widgets):
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    fields = {(f.entity_type, f.field) for f in translatable_fields(db_session, store)}
    assert (TranslationEntity.store, "name") in fields
    assert (TranslationEntity.category, "description") in fields
    assert (TranslationEntity.property_definition, "label") in fields
    color = next(d for d in widgets["categories"][2]["property_definitions"] if d["key"] == "color")
    for option in color["config"]["options"]:
        assert (TranslationEntity.property_definition, f"option:{option}") in fields
    assert (TranslationEntity.product, "value:notes") in fields  # text value
    assert (TranslationEntity.product, "value:color") in fields  # enum value
    assert (TranslationEntity.product, "value:weight_g") not in fields  # numbers are never translated


def test_save_content_translations_returns_errors_by_field(db_session, widgets):
    store = db_session.scalar(select(Store).where(Store.slug == "demo-widgets"))
    name = _field(db_session, TranslationEntity.product, "name")
    changes, errors = save_content_translations(db_session, store, "en", {name.form_name: "x" * 201})
    assert changes == 0 and isinstance(errors[name.form_name], TranslationError)
    assert (errors[name.form_name].code, errors[name.form_name].params) == ("too_long", {"max_len": 200})


def test_delete_one_content_translation(db_session, widgets):
    name = _field(db_session, TranslationEntity.product, "name")
    assert delete_content_translation(db_session, name.entity, "en-US", "name") is True  # stored as "en"
    assert delete_content_translation(db_session, name.entity, "en", "name") is False  # already gone
    db_session.rollback()  # committed: a rollback doesn't bring it back
    assert _rows(db_session, entity_type=TranslationEntity.product, entity_id=name.entity_id, field="name") == 0
