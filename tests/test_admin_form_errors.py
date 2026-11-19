"""Validation messages of the store, theme, category and property-definition forms follow the page
language (Spanish by default, ?lang=en English), like the product form's: their services return
error codes and app/routers/admin_form_errors.py shows them (admin.form.error.<code>)."""

from __future__ import annotations

import inspect
import re

import pytest
from sqlalchemy import select

from app.i18n import translator
from app.models import Product, Store
from app.routers.admin_form_errors import form_error_text
from app.services import category_admin, store_admin
from tests.ui_text import en, en_html, es, es_html, page_text

LOGIN = {"email": "admin@example.com", "password": "ChangeMeAdmin123!"}


@pytest.fixture()
def default_lang_admin(default_lang_client):
    """Admin session with no language chosen: admin pages are in Spanish."""
    assert default_lang_client.post("/login", data=LOGIN, follow_redirects=False).status_code == 303
    return default_lang_client


@pytest.fixture()
def tienda(default_lang_admin):
    """A store with one category ("gas"); returns the category id."""
    assert default_lang_admin.post("/admin/stores", data={"slug": "tienda", "name": "Tienda", "currency": "MXN"},
                                   follow_redirects=False).status_code == 303
    response = default_lang_admin.post("/admin/stores/tienda/categories", data={"slug": "gas", "name": "Gas"},
                                       follow_redirects=False)
    return int(response.headers["location"].rsplit("/", 1)[1])


def _field_error(response, field: str, html_text: str) -> bool:
    return f'id="{field}-error">{html_text}</p>' in response.text


# Each form, with one post that breaks several fields: (url, data, {field: (code, params)}).
def _cases(category_id: int):
    base = "/admin/stores/tienda"
    return {
        "store": ("/admin/stores", {"slug": "new", "name": "", "currency": "US", "locale_default": "fr"},
                  {"slug": ("slug_reserved", {"slug": "new"}), "name": ("name_required", {}),
                   "currency": ("currency_format", {}), "locale_default": ("locale_invalid", {})}),
        "category": (f"{base}/categories", {"slug": "gas", "name": "", "description": "d" * 2001,
                                            "sort_order": "x", "parent_id": "999999"},
                     {"slug": ("category_slug_taken", {}), "name": ("name_required", {}),
                      "description": ("description_too_long", {"max_len": 2000}), "sort_order": ("sort_format", {}),
                      "parent_id": ("parent_invalid", {})}),
        "property": (f"{base}/categories/{category_id}/properties",
                     {"key": "1x", "label": "", "prop_type": "enum", "options": "A\nA", "sort_order": "9999999999"},
                     {"key": ("key_format", {"max_len": 64}), "label": ("label_required", {}),
                      "options": ("option_duplicate", {"option": "A"}),
                      "sort_order": ("sort_range", {"min": "-1,000,000", "max": "1,000,000"})}),
    }


@pytest.mark.parametrize("form", ["store", "category", "property"])
def test_form_errors_are_spanish_by_default_and_english_on_request(default_lang_admin, tienda, form):
    url, data, expected = _cases(tienda)[form]
    for lookup, lookup_html, other_html, query in ((es, es_html, en_html, ""), (en, en_html, es_html, "?lang=en")):
        response = default_lang_admin.post(f"{url}{query}", data=data)
        assert response.status_code == 400, (form, query)
        for field, (code, params) in expected.items():
            key = f"admin.form.error.{code}"
            assert _field_error(response, field, lookup_html(key, **params)), (form, query, field)
            assert other_html(key, **params) not in response.text, (form, query, field)


def test_theme_errors_name_the_color_in_the_page_language(default_lang_admin, tienda):
    url = "/admin/stores/tienda/theme"
    data = {"primary": "red", "secondary": "#ffffff", "accent": "#000000"}
    spanish = default_lang_admin.post(url, data=data)
    assert spanish.status_code == 400
    assert _field_error(spanish, "primary", es_html("admin.form.error.color_format",
                                                    label=es("admin.theme.label.primary")))
    english = default_lang_admin.post(f"{url}?lang=en", data=data)
    assert _field_error(english, "primary", en_html("admin.form.error.color_format",
                                                    label=en("admin.theme.label.primary")))


def test_delete_blockers_are_in_the_page_language(default_lang_admin, tienda, db_session):
    store = db_session.scalar(select(Store).where(Store.slug == "tienda"))
    db_session.add(Product(store_id=store.id, category_id=tienda, slug="p", name="P"))
    db_session.commit()
    child = default_lang_admin.post("/admin/stores/tienda/categories",
                                    data={"slug": "hijo", "name": "Hijo", "parent_id": str(tienda)})
    assert child.status_code == 200  # followed redirect
    url = f"/admin/stores/tienda/categories/{tienda}/delete"
    for query, lookup in (("", es), ("?lang=en", en)):
        response = default_lang_admin.post(f"{url}{query}")
        assert response.status_code == 409
        text = page_text(response)
        assert lookup("admin.form.error.has_products", count=1) in text
        assert lookup("admin.form.error.has_subcategories", count=1) in text


def test_every_error_code_of_these_services_has_a_translated_message():
    """A code the services can return but the router can't show would be a 500."""
    source = inspect.getsource(store_admin) + inspect.getsource(category_admin)
    codes = set(re.findall(r'\(\s*"([a-z_]+)",\s*\{', source))
    assert {"slug_required", "locale_invalid", "color_format", "parent_cycle", "type_locked",
            "has_products"} <= codes  # the scan works
    params = {"max_len": 64, "slug": "new", "key": "primary", "count": 2, "min": "-1", "max": "1", "option": "A"}
    for code in codes:
        spanish = form_error_text(translator("es"), (code, params))
        english = form_error_text(translator("en"), (code, params))
        assert spanish and english and spanish != english, code
        assert "{" not in spanish + english, code  # every placeholder filled
