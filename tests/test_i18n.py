"""UI language: Spanish by default, ``?lang=`` switching, fallback to Spanish, Store.locale_default.

These tests use ``default_lang_client`` (no language chosen), not ``client`` (English).
Expected UI text is read from the catalogs (``app/locales/<code>.json``) with ``es_html(key)`` /
``en_html(key)`` from ``tests/ui_text.py``, so editing a translation does not break the tests.
"""

from __future__ import annotations

import re
import string
from pathlib import Path

import pytest

from app import i18n
from app.models import PropertyType, Store
from app.services.storefront import THEME_COLOR_KEYS
from tests.ui_text import distinct, en_html, es_html, link

APP_DIR = Path(i18n.__file__).resolve().parent
ADMIN_LOGIN = {"email": "admin@example.com", "password": "ChangeMeAdmin123!"}


@pytest.fixture()
def default_lang_admin(default_lang_client):
    """``default_lang_client`` signed in as the seeded admin, still with no language chosen."""
    response = default_lang_client.post("/login", data=ADMIN_LOGIN, follow_redirects=False)
    assert response.status_code == 303
    return default_lang_client


def _html_lang(response) -> str:
    match = re.search(r'<html lang="([^"]+)"', response.text)
    assert match, "page has no <html lang>"
    return match.group(1)


def _add_store(db_session, slug: str, locale_default: str) -> Store:
    store = Store(slug=slug, name=f"Store {slug}", currency="EUR", locale_default=locale_default, theme={})
    db_session.add(store)
    db_session.commit()
    return store


# ------------------------------------------------------------------------ default: Spanish


def test_public_chrome_is_spanish_by_default(default_lang_client):
    response = default_lang_client.get("/")
    assert _html_lang(response) == "es"
    text = response.text
    sign_in_es, sign_in_en = distinct("nav.sign_in")
    no_stores_es, no_stores_en = distinct("home.no_stores")
    assert link(es_html("nav.stores")) in text and link(sign_in_es) in text
    assert no_stores_es in text
    assert sign_in_en not in text and no_stores_en not in text
    # No choice made -> nothing remembered.
    assert i18n.COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_login_page_and_error_are_spanish_by_default(default_lang_client):
    page = default_lang_client.get("/login")
    assert _html_lang(page) == "es"
    assert es_html("login.email") in page.text and es_html("login.password") in page.text
    failed = default_lang_client.post("/login", data={**ADMIN_LOGIN, "password": "wrong"})
    assert failed.status_code == 401
    assert distinct("login.invalid")[0] in failed.text


def test_admin_chrome_and_flash_are_spanish_by_default(default_lang_admin):
    dashboard = default_lang_admin.get("/admin")
    assert _html_lang(dashboard) == "es"
    dashboard_es, dashboard_en = distinct("admin.nav.dashboard")
    sign_out_es, sign_out_en = distinct("nav.sign_out")
    assert link(dashboard_es) in dashboard.text and sign_out_es in dashboard.text
    assert dashboard_en not in dashboard.text and sign_out_en not in dashboard.text

    created = default_lang_admin.post("/admin/stores", data={"slug": "shop", "name": "Shop", "currency": "EUR"})
    assert created.status_code == 200  # followed the redirect to the list
    assert distinct("flash.store_created", name="Shop", slug="shop")[0] in created.text
    assert link(es_html("admin.stores.new")) in created.text

    tabs = default_lang_admin.get("/admin/stores/shop/theme").text
    assert link(es_html("admin.tabs.categories")) in tabs and link(es_html("admin.tabs.products")) in tabs
    assert es_html("admin.theme.save") in tabs


def test_public_storefront_chrome_is_spanish_for_a_spanish_store(default_lang_client, db_session):
    _add_store(db_session, "es-store", "es")
    page = default_lang_client.get("/stores/es-store")
    assert _html_lang(page) == "es"
    assert link(distinct("store.nav.catalog")[0]) in page.text
    assert link(es_html("store.nav.all_stores")) in page.text
    assert distinct("store.catalog_empty")[0] in page.text


def test_client_fixture_starts_in_english(client):
    """The fixture used by the other suites: same app, English chosen up front."""
    assert _html_lang(client.get("/")) == "en"


# ------------------------------------------------------------------------------ ?lang=en


def test_lang_en_switches_visible_strings_and_is_remembered(default_lang_client):
    english = default_lang_client.get("/?lang=en")
    assert _html_lang(english) == "en"
    assert link(distinct("nav.stores")[1]) in english.text
    assert link(distinct("nav.sign_in")[1]) in english.text
    assert distinct("home.no_stores")[1] in english.text
    assert f"{i18n.COOKIE_NAME}=en" in english.headers["set-cookie"]

    # Remembered for the next pages, without the parameter.
    assert _html_lang(default_lang_client.get("/login")) == "en"
    # ...until another language is chosen.
    assert _html_lang(default_lang_client.get("/?lang=es")) == "es"
    assert _html_lang(default_lang_client.get("/")) == "es"


def test_lang_en_applies_to_admin_and_flash(default_lang_admin):
    assert link(distinct("admin.nav.dashboard")[1]) in default_lang_admin.get("/admin?lang=en").text
    created = default_lang_admin.post("/admin/stores", data={"slug": "shop", "name": "Shop", "currency": "USD"})
    assert distinct("flash.store_created", name="Shop", slug="shop")[1] in created.text


def test_language_choice_survives_sign_in(default_lang_client):
    default_lang_client.get("/login?lang=en")
    response = default_lang_client.post("/login", data=ADMIN_LOGIN)
    assert _html_lang(response) == "en"  # the admin page reached after the redirect


@pytest.mark.parametrize("code", ["en-US", "EN", "en_GB"])
def test_region_and_case_variants_map_to_the_base_language(default_lang_client, code):
    assert _html_lang(default_lang_client.get(f"/?lang={code}")) == "en"


def test_language_switcher_links_keep_other_query_parameters(default_lang_client):
    html = default_lang_client.get("/login?next=%2Fadmin").text
    assert 'href="/login?next=%2Fadmin&amp;lang=en"' in html
    assert en_html("lang.name") in html and es_html("lang.name") in html


# -------------------------------------------------------------------- fallback to Spanish


@pytest.mark.parametrize("code", ["fr", "pt-BR", "xx", "", "../en", "<script>"])
def test_unsupported_lang_falls_back_to_spanish(default_lang_client, code):
    response = default_lang_client.get("/", params={"lang": code})
    assert response.status_code == 200
    assert _html_lang(response) == "es"
    assert link(distinct("nav.sign_in")[0]) in response.text
    assert i18n.COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_unsupported_lang_on_admin_falls_back_to_spanish(default_lang_admin):
    page = default_lang_admin.get("/admin?lang=fr")
    assert _html_lang(page) == "es" and link(distinct("admin.nav.dashboard")[0]) in page.text


def test_language_cookie_is_used_only_when_supported(default_lang_client):
    default_lang_client.cookies.set(i18n.COOKIE_NAME, "en")
    assert _html_lang(default_lang_client.get("/")) == "en"
    default_lang_client.cookies.set(i18n.COOKIE_NAME, "fr")  # forged / no longer available
    assert _html_lang(default_lang_client.get("/")) == "es"


def test_missing_key_in_requested_language_falls_back_to_spanish(default_lang_client, monkeypatch):
    """A partial catalog is fine: keys it lacks are shown in Spanish."""
    stores_en = distinct("nav.stores")[1]
    sign_in_es = distinct("nav.sign_in")[0]
    real = i18n.catalogs()
    partial_en = {k: v for k, v in real["en"].items() if k != "nav.sign_in"}
    monkeypatch.setattr(i18n, "catalogs", lambda: {**real, "en": partial_en})

    html = default_lang_client.get("/?lang=en").text
    assert link(stores_en) in html  # translated key
    assert link(sign_in_es) in html  # missing key -> Spanish


def test_new_catalog_file_is_picked_up_with_spanish_fallback(default_lang_client, monkeypatch):
    """A language exists as soon as it has a catalog; everything it lacks stays Spanish."""
    sign_in_es = es_html("nav.sign_in")
    real = i18n.catalogs()
    french = {"lang.name": "French", "nav.stores": "Stores (fr)"}  # a stand-in catalog for the test
    monkeypatch.setattr(i18n, "catalogs", lambda: {**real, "fr": french})

    html = default_lang_client.get("/?lang=fr").text
    assert '<html lang="fr">' in html
    assert link(french["nav.stores"]) in html and link(sign_in_es) in html


def test_translate_unknown_key_and_bad_placeholders_do_not_crash():
    assert i18n.translate("en", "no.such.key") == "no.such.key"
    english = i18n.catalogs()["en"]
    # A missing parameter leaves the text unformatted instead of raising.
    assert i18n.translate("en", "action.back_to") == english["action.back_to"]
    # "key" can be a placeholder name (the message id is positional-only).
    params = {"label": "Color", "key": "color", "category": "W"}
    assert i18n.translate("en", "admin.property.delete_message", **params) == (
        english["admin.property.delete_message"].format(**params)
    )
    assert "(color)" in i18n.translate("en", "admin.property.delete_message", **params)


# ------------------------------------------------------------------- Store.locale_default


def test_new_store_defaults_to_spanish(default_lang_admin):
    default_lang_admin.post("/admin/stores", data={"slug": "new-shop", "name": "New shop", "currency": "EUR"})
    assert default_lang_admin.get("/api/stores/new-shop").json()["locale_default"] == "es"
    assert _html_lang(default_lang_admin.get("/stores/new-shop")) == "es"


def test_store_locale_default_can_be_chosen_in_admin(default_lang_admin):
    form = default_lang_admin.get("/admin/stores/new").text
    assert f'<option value="es" selected>{es_html("lang.name")} (es)</option>' in form
    assert f'<option value="en" >{en_html("lang.name")} (en)</option>' in form

    default_lang_admin.post(
        "/admin/stores", data={"slug": "english", "name": "English", "currency": "USD", "locale_default": "en"}
    )
    assert default_lang_admin.get("/api/stores/english").json()["locale_default"] == "en"


def test_store_form_rejects_unsupported_locale(default_lang_admin):
    response = default_lang_admin.post(
        "/admin/stores", data={"slug": "x", "name": "X", "currency": "USD", "locale_default": "fr"}
    )
    assert response.status_code == 400
    # The admin page is Spanish by default, and so is its field message.
    assert f'id="locale_default-error">{es_html("admin.form.error.locale_invalid")}</p>' in response.text
    assert en_html("admin.form.error.locale_invalid") not in response.text
    assert default_lang_admin.get("/api/stores/x").status_code == 404


def test_store_locale_default_drives_its_public_pages(default_lang_client, db_session):
    _add_store(db_session, "en-store", "en")
    page = default_lang_client.get("/stores/en-store")
    assert _html_lang(page) == "en"
    assert link(distinct("store.nav.catalog")[1]) in page.text
    assert distinct("store.catalog_empty")[1] in page.text
    # Only that store's pages: the platform home and sign-in stay Spanish.
    assert _html_lang(default_lang_client.get("/")) == "es"
    assert _html_lang(default_lang_client.get("/login")) == "es"


def test_admin_stays_spanish_for_an_english_store(default_lang_admin, db_session):
    _add_store(db_session, "en-store", "en")
    page = default_lang_admin.get("/admin/stores/en-store")
    assert _html_lang(page) == "es" and link(distinct("admin.tabs.categories")[0]) in page.text


def test_visitor_choice_beats_store_locale_default(default_lang_client, db_session):
    _add_store(db_session, "en-store", "en")
    assert _html_lang(default_lang_client.get("/stores/en-store?lang=es")) == "es"
    assert _html_lang(default_lang_client.get("/stores/en-store")) == "es"  # remembered


@pytest.mark.parametrize("stored,expected", [("en-US", "en"), ("fr", "es"), ("", "es")])
def test_store_locale_default_is_normalized_with_spanish_fallback(default_lang_client, db_session, stored, expected):
    _add_store(db_session, "shop", stored)
    assert _html_lang(default_lang_client.get("/stores/shop")) == expected


def test_unsupported_lang_on_storefront_uses_the_store_default(default_lang_client, db_session):
    """An unusable ?lang= is ignored, so the store's own language applies (then Spanish)."""
    _add_store(db_session, "en-store", "en")
    assert _html_lang(default_lang_client.get("/stores/en-store?lang=fr")) == "en"


# ---------------------------------------------------------------------- catalog integrity


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_spanish_is_the_complete_reference_catalog():
    catalogs = i18n.catalogs()
    assert i18n.DEFAULT_LOCALE == "es"
    assert i18n.available_locales()[0] == "es"
    assert "en" in catalogs
    for code, table in catalogs.items():
        extra = set(table) - set(catalogs["es"])
        assert not extra, f"{code}.json has keys missing from es.json: {sorted(extra)}"
        for key, text in table.items():
            assert _placeholders(text) == _placeholders(catalogs["es"][key]), f"{code}:{key} placeholders differ"


def test_english_catalog_is_complete():
    """Every UI string has its English version (the suites run in English)."""
    catalogs = i18n.catalogs()
    missing = set(catalogs["es"]) - set(catalogs["en"])
    assert not missing, f"keys missing from en.json: {sorted(missing)}"


def test_every_key_used_in_code_exists_in_spanish():
    pattern = re.compile(r"""\b(?:t|flash|_redirect|translate)\((?:[^()"']*,\s*)?["']([a-z_]+(?:\.[a-z0-9_]+)+)["']""")
    used = set()
    for path in [*APP_DIR.rglob("*.html"), *APP_DIR.rglob("*.py")]:
        used |= set(pattern.findall(path.read_text(encoding="utf-8")))
    # Keys built at runtime.
    used |= {f"proptype.{t.value}" for t in PropertyType}
    used |= {f"admin.theme.label.{key}" for key in THEME_COLOR_KEYS}
    assert len(used) > 150  # the scan really found the templates
    missing = used - set(i18n.catalogs()["es"])
    assert not missing, f"keys used but missing from es.json: {sorted(missing)}"


# ------------------------------------------------------------------ remembering the choice


def test_post_with_lang_is_answered_in_that_language_but_sets_no_cookie(default_lang_client):
    failed = default_lang_client.post("/login?lang=en", data={**ADMIN_LOGIN, "password": "wrong"})
    assert failed.status_code == 401
    assert distinct("login.invalid")[1] in failed.text
    assert "set-cookie" not in failed.headers  # a failed sign-in sets no cookie at all
    assert _html_lang(default_lang_client.get("/")) == "es"  # nothing remembered


def test_cookie_is_only_resent_when_the_language_changes(default_lang_client):
    assert f"{i18n.COOKIE_NAME}=en" in default_lang_client.get("/?lang=en").headers["set-cookie"]
    assert "set-cookie" not in default_lang_client.get("/?lang=en").headers  # unchanged
    assert f"{i18n.COOKIE_NAME}=es" in default_lang_client.get("/?lang=es").headers["set-cookie"]


def test_client_fixture_sends_lang_en_on_every_call_and_keeps_the_query(client):
    response = client.get("/login?next=%2Fadmin")
    assert response.request.url.params["lang"] == "en"
    assert response.request.url.params["next"] == "/admin"
    followed = client.get("/admin")  # redirected to /login: the redirect is called with it too
    assert all(r.request.url.params["lang"] == "en" for r in [*followed.history, followed])
