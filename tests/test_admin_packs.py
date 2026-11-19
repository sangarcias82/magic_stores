"""Admin pack upload (/admin/packs): multipart .json -> validate -> import. Invalid uploads are
rejected on the page and leave the database exactly as it was.

UI text is compared with the catalogs (``es_html(key)`` / ``en_html(key)``), pack data with the fixtures.
``admin_client`` calls every endpoint with ?lang=en; ``default_lang_admin`` uses the default (es).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from markupsafe import escape

from app.routers import admin_packs
from app.services.store_pack_validation import PACK_FILE_SUFFIX
from tests.ui_text import en_html, es_html

UPLOAD = "/admin/packs"
PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
CLOTHING = PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}"
WIDGETS = PACKS_DIR / f"demo-widgets{PACK_FILE_SUFFIX}"


def _pack(path: Path = CLOTHING) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _upload(client, content, *, filename: str = "pack.json", update: bool = False, follow: bool = False):
    if isinstance(content, dict):
        content = json.dumps(content).encode("utf-8")
    data = {"update_existing": "1"} if update else {}
    return client.post(UPLOAD, files={"pack": (filename, content, "application/json")}, data=data,
                       follow_redirects=follow)


def _snapshot(client, slug: str) -> dict:
    """Everything the public API shows about a store (to prove nothing changed)."""
    products = client.get(f"/api/stores/{slug}/products").json()
    return {
        "store": client.get(f"/api/stores/{slug}").json(),
        "categories": client.get(f"/api/stores/{slug}/categories").json(),
        "products": [client.get(f"/api/stores/{slug}/products/{p['slug']}").json() for p in products],
    }


@pytest.fixture()
def default_lang_admin(default_lang_client):
    response = default_lang_client.post(
        "/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"}, follow_redirects=False
    )
    assert response.status_code == 303
    return default_lang_client


# ----------------------------------------------------------------------------- the page


def test_upload_page_is_spanish_by_default(default_lang_admin):
    page = default_lang_admin.get(UPLOAD)
    assert page.status_code == 200 and '<html lang="es">' in page.text
    for key in ("admin.packs.title", "admin.packs.file", "admin.packs.update_existing", "admin.packs.submit"):
        assert es_html(key) in page.text
    assert 'enctype="multipart/form-data"' in page.text and 'type="file"' in page.text
    assert f'href="/admin/packs" aria-current="page">{es_html("admin.nav.packs")}</a>' in page.text
    assert es_html("admin.nav.packs") in default_lang_admin.get("/admin").text  # dashboard link


def test_upload_page_in_english(admin_client):
    page = admin_client.get(UPLOAD)
    assert en_html("admin.packs.title") in page.text and en_html("admin.packs.submit") in page.text


def test_upload_requires_an_admin(client):
    response = _upload(client, _pack())
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert client.get("/api/stores").json() == []


# --------------------------------------------------------------------------- valid packs


def test_valid_fixture_upload_creates_the_store(admin_client):
    pack = _pack()
    response = _upload(admin_client, CLOTHING.read_bytes(), filename=CLOTHING.name)
    slug = pack["store"]["slug"]
    assert response.status_code == 303 and response.headers["location"] == f"/admin/stores/{slug}"

    page = admin_client.get(response.headers["location"])
    (category,) = pack["categories"]
    assert en_html("flash.pack_created", name=pack["store"]["name"], slug=slug, categories=1,
              properties=len(category["property_definitions"]), products=1) in page.text

    assert [s["slug"] for s in admin_client.get("/api/stores").json()] == [slug]
    (api_category,) = admin_client.get(f"/api/stores/{slug}/categories").json()
    assert api_category["slug"] == category["slug"]
    product = pack["products"][0]
    detail = admin_client.get(f"/api/stores/{slug}/products/{product['slug']}").json()
    assert {p["key"]: p["value"] for p in detail["properties"]} == {
        key: {"value": value} for key, value in product["values"].items()
    }


def test_valid_upload_message_is_spanish_by_default(default_lang_admin):
    pack = _pack()
    page = _upload(default_lang_admin, pack, follow=True)
    assert page.status_code == 200
    assert es_html("flash.pack_created", name=pack["store"]["name"], slug=pack["store"]["slug"], categories=1,
              properties=len(pack["categories"][0]["property_definitions"]), products=1) in page.text


def test_uploaded_pack_translations_reach_the_storefront(admin_client):
    pack = _pack(WIDGETS)
    assert _upload(admin_client, pack).status_code == 303
    product = pack["products"][0]
    html = admin_client.get(f"/stores/{pack['store']['slug']}/p/{product['slug']}").text  # ?lang=en
    assert str(escape(product["i18n"]["en"]["name"])) in html


def test_schema_example_upload_mentions_the_storefront_folder(admin_client):
    example = PACKS_DIR / "store-pack.v1.schema.example.json"
    page = _upload(admin_client, example.read_bytes(), follow=True)
    assert en_html("flash.pack_storefront_skipped") in page.text


# ------------------------------------------------------------------------- invalid packs


def _without_slug() -> dict:
    pack = _pack()
    del pack["store"]["slug"]
    return pack


@pytest.mark.parametrize(
    "content,path,message",
    [
        ({**_pack(), "version": 2}, "version", 'unsupported version 2; this engine reads store packs with "version": 1'),
        (_without_slug(), "store.slug", "required"),
        ({**_pack(), "products": [{**_pack()["products"][0], "category_slug": "pants"}]},
         "products[0].category_slug", 'no category with slug "pants" in this pack'),
        (b"not json at all", "", "not valid JSON: Expecting value (line 1, column 1)"),
        (b'{"version": 1, "version": 1}', "", 'duplicate key "version" in a JSON object'),
        (b"   ", "", "the file is empty"),
        (b"[1, 2]", "", "a store pack must be a JSON object, got array"),
    ],
    ids=["bad-version", "missing-store-slug", "unknown-category", "not-json", "duplicate-key", "empty", "array"],
)
def test_invalid_upload_is_rejected_on_the_page_and_writes_nothing(admin_client, content, path, message):
    response = _upload(admin_client, content)
    assert response.status_code == 400
    assert en_html("admin.packs.error.invalid") in response.text
    issue = f"<code>{escape(path)}</code>: {escape(message)}" if path else str(escape(message))
    assert issue in response.text
    assert admin_client.get("/api/stores").json() == []  # no bad (or partial) store


def test_invalid_upload_error_is_spanish_by_default(default_lang_admin):
    response = _upload(default_lang_admin, _without_slug())
    assert response.status_code == 400 and es_html("admin.packs.error.invalid") in response.text


def test_missing_file_is_rejected(admin_client):
    response = admin_client.post(UPLOAD, data={"update_existing": "1"})
    assert response.status_code == 400 and en_html("admin.packs.error.no_file") in response.text
    assert "checked" in response.text  # the form keeps the admin's choice


def test_form_submitted_without_choosing_a_file_is_rejected(admin_client):
    """What a browser sends when no file is chosen: an empty part with an empty filename."""
    response = admin_client.post(UPLOAD, files={"pack": ("", b"", "application/octet-stream")})
    assert response.status_code == 400 and en_html("admin.packs.error.no_file") in response.text


def test_too_large_file_is_rejected(admin_client, monkeypatch):
    monkeypatch.setattr(admin_packs, "MAX_PACK_BYTES", 100)
    response = _upload(admin_client, CLOTHING.read_bytes())
    assert response.status_code == 413
    assert en_html("admin.packs.error.too_large", max_mb=admin_packs.MAX_PACK_MB) in response.text
    assert admin_client.get("/api/stores").json() == []


def test_too_large_response_uses_no_deprecated_status_constant(admin_client, monkeypatch, recwarn):
    """Starlette deprecated HTTP_413_REQUEST_ENTITY_TOO_LARGE; the 413 answer must not warn."""
    monkeypatch.setattr(admin_packs, "MAX_PACK_BYTES", 100)
    assert _upload(admin_client, CLOTHING.read_bytes()).status_code == 413
    deprecated = [str(w.message) for w in recwarn if "HTTP_4" in str(w.message) or "HTTP_5" in str(w.message)]
    assert deprecated == []


# ------------------------------------------------------------------------- existing store


def test_existing_store_is_only_updated_when_asked(admin_client):
    pack = _pack()
    slug = pack["store"]["slug"]
    _upload(admin_client, pack)
    before = _snapshot(admin_client, slug)

    pack["store"]["name"] = "Renamed by the pack"
    refused = _upload(admin_client, pack)
    assert refused.status_code == 409 and en_html("admin.packs.error.exists", slug=slug) in refused.text
    assert _snapshot(admin_client, slug) == before

    accepted = _upload(admin_client, pack, update=True)
    assert accepted.status_code == 303
    page = admin_client.get(accepted.headers["location"])
    assert en_html("flash.pack_updated", name="Renamed by the pack", slug=slug, categories=1,
              properties=len(pack["categories"][0]["property_definitions"]), products=1) in page.text
    assert admin_client.get(f"/api/stores/{slug}").json()["name"] == "Renamed by the pack"


def test_invalid_pack_never_touches_an_existing_store(admin_client):
    pack = _pack()
    slug = pack["store"]["slug"]
    _upload(admin_client, pack)
    before = _snapshot(admin_client, slug)

    pack["store"]["name"] = "Should not be saved"
    pack["products"][0]["values"]["size"] = "XXL"  # not an option: the whole pack is rejected
    response = _upload(admin_client, pack, update=True)
    assert response.status_code == 400 and "products[0].values.size" in response.text
    assert _snapshot(admin_client, slug) == before


def test_conflicting_pack_is_rolled_back(admin_client):
    pack = _pack()
    slug = pack["store"]["slug"]
    _upload(admin_client, pack)
    # A product created in admin uses the "material" property (text).
    shirts_id = admin_client.get(f"/api/stores/{slug}/categories").json()[0]["id"]
    created = admin_client.post(
        f"/admin/stores/{slug}/products",
        data={"category_id": str(shirts_id), "slug": "admin-tee", "sku": "ADMIN-TEE", "stock": "3", "name": "Admin tee",
              "price": "5", "prop_size": "M", "prop_color": "Navy", "prop_material": "cotton"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    before = _snapshot(admin_client, slug)

    pack["store"]["name"] = "Should not be saved"
    pack["categories"][0]["property_definitions"][2]["prop_type"] = "number"  # material
    pack["products"][0]["values"]["material"] = 100
    response = _upload(admin_client, pack, update=True)
    assert response.status_code == 409 and en_html("admin.packs.error.conflict") in response.text
    assert "categories[0].property_definitions[2].prop_type" in response.text
    assert _snapshot(admin_client, slug) == before
