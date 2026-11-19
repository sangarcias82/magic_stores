"""Every way a store can end up without its own storefront folder keeps it on _default, even with
another store's override (solares) installed next to it."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

import app.services.storefront as sf
from app.services.store_pack_import import import_store_pack_file

PROJECT = Path(__file__).resolve().parent.parent
PACKS = PROJECT / "fixtures" / "packs"
SOLARES = PACKS / "solares" / "solares.store-pack.v1.json"


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload(client, filename: str, content: bytes) -> None:
    response = client.post("/admin/packs", files={"pack": (filename, content, "application/octet-stream")},
                           follow_redirects=False)
    assert response.status_code == 303, response.text


def _admin_store(client, db, slug):
    response = client.post("/admin/stores", data={"slug": slug, "name": slug.title(), "currency": "USD"},
                           follow_redirects=False)
    assert response.status_code == 303
    return slug


def _cli(client, db, slug):
    return import_store_pack_file(db, PACKS / f"{slug}.store-pack.v1.json").store_slug


CASES = {
    "created in admin": lambda c, db: _admin_store(c, db, "made-in-admin"),
    "CLI, pack without storefront": lambda c, db: _cli(c, db, "clothing-demo"),
    "CLI, copy_from_pack_dir null": lambda c, db: _cli(c, db, "demo-widgets"),
    "slug starting like solares": lambda c, db: _admin_store(c, db, "solares-outlet"),
}


def _json_upload_with_declared_folder(client, db):
    pack = PACKS / "storefront-demo" / "storefront-demo.store-pack.v1.json"  # declares storefront/
    _upload(client, pack.name, pack.read_bytes())
    return "storefront-demo"


def _zip_upload_without_storefront(client, db):
    pack = json.loads((PACKS / "clothing-demo.store-pack.v1.json").read_text(encoding="utf-8"))
    _upload(client, "clothing.zip", _zip({"clothing-demo.store-pack.v1.json": json.dumps(pack).encode()}))
    return "clothing-demo"


CASES[".json upload (folder declared, not uploaded)"] = _json_upload_with_declared_folder
CASES[".zip upload without storefront/"] = _zip_upload_without_storefront


@pytest.mark.parametrize("case", list(CASES))
def test_store_without_a_folder_stays_on_default(case, admin_client, db_session, content_root, storefronts):
    import_store_pack_file(db_session, SOLARES)  # an override exists next door
    assert (storefronts / "solares" / "templates" / "store.html").is_file()

    slug = CASES[case](admin_client, db_session)
    assert not (storefronts / slug).exists() and not sf.storefront_has_override(slug)
    templates = sf.templates_for_store(slug).env
    for name in ("base.html", "store.html", "category.html", "product.html"):
        assert Path(templates.get_template(name).filename) == storefronts / "_default" / "templates" / name
    assert sf.resolve_static_file(slug, "style.css") == storefronts / "_default" / "static" / "style.css"

    html = admin_client.get(f"/stores/{slug}").text
    assert '<section class="grid-two">' in html and "data-storefront" not in html
    assert admin_client.get(f"/stores/{slug}/assets/style.css").content == \
        (storefronts / "_default" / "static" / "style.css").read_bytes()
    assert 'data-storefront="solares"' in admin_client.get("/stores/solares").text  # the neighbour keeps its own
