"""The solares business pack (fixtures/packs/solares/): solar water heaters with tube count and
pressure as property definitions (rows), an optional gas category, its own theme, storefront
templates + stylesheet, and images published to the content app.

Imports use temporary CONTENT_ROOT / STOREFRONTS_ROOT folders (conftest.py fixtures)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Category, Product, PropertyDefinition, PropertyType
from app.services import store_pack_import
from app.services.store_pack_import import import_store_pack_file
from app.services.store_pack_validation import PACK_FILE_SUFFIX, read_store_pack
from app.services.storefront import DEFAULT_THEME

PROJECT = Path(__file__).resolve().parent.parent
PACKS_DIR = PROJECT / "fixtures" / "packs"
SOLARES_DIR = PACKS_DIR / "solares"
SOLARES = SOLARES_DIR / f"solares{PACK_FILE_SUFFIX}"
MEDIA = SOLARES_DIR / "media"
BASE = "https://media.example.test"  # CONTENT_BASE_URL under the content_root fixture (conftest.py)
IMPORT_COMMAND = "python -m app.services.store_pack_import fixtures/packs/solares/solares.store-pack.v1.json"


def _pack() -> dict:
    return json.loads(SOLARES.read_text(encoding="utf-8"))


def _definitions(pack: dict, category: str) -> dict[str, dict]:
    [cat] = [c for c in pack["categories"] if c["slug"] == category]
    return {d["key"]: d for d in cat["property_definitions"]}


@pytest.fixture()
def imported(db_session, content_root, storefronts):
    return import_store_pack_file(db_session, SOLARES)


# ------------------------------------------------------------------------ the contract


def test_validation_command_passes():
    result = subprocess.run(
        [sys.executable, "-m", "app.services.store_pack_validation", str(SOLARES.relative_to(PROJECT))],
        cwd=PROJECT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "fixtures/packs/solares/solares.store-pack.v1.json: valid",
        "Checked 1 file(s): 1 valid, 0 invalid or unreadable. Nothing was imported.",
    ]


def test_pack_file_name_and_slug():
    pack = read_store_pack(SOLARES)  # strict parse + validation
    assert SOLARES.name == "solares.store-pack.v1.json" and pack["store"]["slug"] == "solares"
    assert pack["storefront"]["copy_from_pack_dir"] == "storefront/"


def test_tubes_cover_8_to_20_and_pressure_is_high_or_low():
    pack = _pack()
    solar = _definitions(pack, "calentadores-solares")
    tubes, pressure = solar["tubos"], solar["presion"]
    assert tubes["prop_type"] == "enum" and tubes["required"] and pressure["prop_type"] == "enum" and pressure["required"]
    options = [int(o) for o in tubes["config"]["options"]]
    assert min(options) == 8 and max(options) == 20 and options == sorted(options)
    assert pressure["config"]["options"] == ["alta", "baja"]
    assert pressure["i18n"]["en"]["config"]["options"] == ["high", "low"]

    products = [p for p in pack["products"] if p["category_slug"] == "calentadores-solares"]
    used = {int(p["values"]["tubos"]) for p in products}
    assert min(used) == 8 and max(used) == 20  # the products span the whole range
    assert {p["values"]["presion"] for p in products} == {"alta", "baja"}


def test_optional_gas_category_with_a_gas_heater():
    pack = _pack()
    gas = _definitions(pack, "calentadores-gas")
    assert gas["tipo_gas"]["prop_type"] == "enum" and gas["foto"]["prop_type"] == "image"
    heaters = [p for p in pack["products"] if p["category_slug"] == "calentadores-gas"]
    assert [p["slug"] for p in heaters] == ["boiler-gas-40-litros"]


def test_theme_differs_from_every_other_fixture():
    theme = _pack()["store"]["theme"]
    others = [DEFAULT_THEME] + [
        json.loads(p.read_text(encoding="utf-8"))["store"].get("theme") or {}
        for p in sorted(PACKS_DIR.rglob("*.json")) if p != SOLARES
    ]
    assert len(others) >= 4
    for other in others:
        for key in ("primary", "secondary", "accent"):
            if other.get(key):
                assert theme[key].lower() != other[key].lower(), (key, other)


def test_every_image_field_points_at_a_shipped_media_file():
    pack = _pack()
    images = [pack["store"]["theme"]["logo_url"]] + [
        p["values"]["foto"] for p in pack["products"] if p["values"].get("foto")
    ]
    assert len(images) == 6 and all(i.startswith("/content/solares/") for i in images)
    shipped = {f.name for f in MEDIA.iterdir()}
    assert {i.removeprefix("/content/solares/") for i in images} == shipped  # each used, none missing


# ------------------------------------------------------------------------- the engine


def test_import_uses_engine_rows_not_new_columns(db_session, imported):
    columns_before = {t.name: {c.name for c in t.columns} for t in Base.metadata.sorted_tables}
    inspector = inspect(db_session.get_bind())
    columns_now = {t: {c["name"] for c in inspector.get_columns(t)} for t in inspector.get_table_names()}
    assert columns_now == columns_before  # the schema is the engine's, unchanged
    assert not any({"tubos", "presion", "tube_count", "pressure"} & cols for cols in columns_now.values())

    definitions = {(d.category.slug, d.key): d for d in db_session.scalars(select(PropertyDefinition))}
    tubes = definitions[("calentadores-solares", "tubos")]
    assert tubes.prop_type == PropertyType.enum and tubes.config["options"][0] == "8"
    assert definitions[("calentadores-solares", "presion")].config == {"options": ["alta", "baja"]}
    assert {c.slug for c in db_session.scalars(select(Category))} == {"calentadores-solares", "calentadores-gas"}
    assert len(db_session.scalars(select(Product)).all()) == 5


def test_import_report(imported, content_root, storefronts):
    assert imported.store_created and imported.products.created == 5
    assert imported.media_files == 6 and imported.media_path == str(content_root / "solares")
    assert imported.storefront_files == 4  # store.html, product.html, style.css, logo-solar.jpg
    assert imported.missing_media == [] and imported.skipped == []
    assert sorted(p.name for p in (content_root / "solares").iterdir()) == sorted(p.name for p in MEDIA.iterdir())


# ------------------------------------------------------------------------- the storefront


def _srcs(html: str) -> list[str]:
    return re.findall(r'<img src="([^"]*)"', html)


def test_store_page_uses_the_pack_storefront_and_theme(client, imported):
    html = client.get("/stores/solares?lang=es").text
    assert 'data-storefront="solares"' in html and "¿Cuántos tubos necesito?" in html
    assert "--color-primary: #0077b6;" in html and "--color-accent: #ffb703;" in html
    assert f'<img src="{BASE}/content/solares/logo-solar.jpg" alt="" class="brand-logo">' in html
    assert '<img class="solar-hero-logo" src="/stores/solares/assets/logo-solar.jpg" alt="">' in html
    assert "Calentadores solares" in html and "Calentadores de gas" in html
    assert "Solares · Agua caliente con la energía del sol" in html  # ui_translations

    english = client.get("/stores/solares?lang=en").text
    assert "How many tubes do I need?" in english and "Solar water heaters" in english
    assert "Solares · Hot water from the sun" in english


def test_product_page_photo_and_badges(client, imported):
    html = client.get("/stores/solares/p/solar-12-tubos-baja?lang=es").text
    assert f'<img src="{BASE}/content/solares/tubo-12-baja.jpg" alt="Calentador solar 12 tubos, baja presión" ' \
           'class="product-photo">' in html
    assert '<span class="badge badge-tubes">Número de tubos: 12</span>' in html
    assert '<span class="badge badge-pressure">Presión: baja</span>' in html
    assert "MXN 7,299.00" in html
    assert html.count("tubo-12-baja.jpg") == 1  # the photo is not repeated in the property list
    assert '<dd class="prop prop-number" data-key="capacidad_litros">' in html  # the other properties are

    english = client.get("/stores/solares/p/solar-12-tubos-baja?lang=en").text
    assert '<span class="badge badge-tubes">Number of tubes: 12</span>' in english
    assert '<span class="badge badge-pressure">Pressure: low</span>' in english
    assert "Solar heater, 12 tubes, low pressure" in english


def test_every_page_renders_and_every_image_loads(client, imported, content_root, content_app, storefronts):
    content = content_app(content_root)
    pack = _pack()
    pages = ["/stores/solares"] + [f"/stores/solares/c/{c['slug']}" for c in pack["categories"]] + \
            [f"/stores/solares/p/{p['slug']}" for p in pack["products"]]
    loaded = set()
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200, page
        for src in _srcs(response.text):
            if src.startswith(BASE):  # product photos and the logo: the content app
                image = content.get(src.removeprefix(BASE))
            else:  # storefront static files: Magic Stores
                image = client.get(src)
            assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg", (page, src)
            loaded.add(src)
    assert {s.rsplit("/", 1)[1] for s in loaded} == {p.name for p in MEDIA.iterdir()}  # every image is shown
    css = client.get("/stores/solares/assets/style.css")
    assert css.content == (SOLARES_DIR / "storefront" / "static" / "style.css").read_bytes()


# ----------------------------------------------------------------------------- the CLI


def test_documented_cli_command_imports_the_pack(engine, content_root, storefronts, capsys):
    argv = IMPORT_COMMAND.split()[3:]  # after "python -m app.services.store_pack_import"
    assert store_pack_import.main([str(PROJECT / a) for a in argv], sessionmaker(bind=engine)) == 0
    out = capsys.readouterr().out
    assert "store solares: created" in out and "products: 5 created, 0 updated" in out
    assert f"media: 6 image(s) published to {content_root / 'solares'}" in out
    assert "missing media" not in out and "skipped" not in out
    assert IMPORT_COMMAND in (PACKS_DIR / "README.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------ the API


EXPECTED = {  # product slug -> (tubos, presion)
    "solar-8-tubos-alta": ("8", "alta"),
    "solar-12-tubos-baja": ("12", "baja"),
    "solar-16-tubos-alta": ("16", "alta"),
    "solar-20-tubos-alta": ("20", "alta"),
}


def test_api_shows_store_categories_products_and_tube_pressure_values(client, imported):
    store = client.get("/api/stores/solares").json()
    assert (store["slug"], store["name"], store["currency"], store["locale_default"]) == ("solares", "Solares", "MXN", "es")
    assert store["theme"]["primary"] == "#0077b6" and store["theme"]["logo_url"] == "/content/solares/logo-solar.jpg"
    assert "solares" in {s["slug"] for s in client.get("/api/stores").json()}

    categories = {c["slug"]: c for c in client.get("/api/stores/solares/categories").json()}
    assert set(categories) == {"calentadores-solares", "calentadores-gas"}
    definitions = {d["key"]: d for d in categories["calentadores-solares"]["property_definitions"]}
    assert definitions["tubos"]["prop_type"] == "enum" and definitions["tubos"]["required"] is True
    assert definitions["tubos"]["config"]["options"] == ["8", "10", "12", "14", "16", "18", "20"]
    assert definitions["presion"]["config"]["options"] == ["alta", "baja"]

    listed = {p["slug"] for p in client.get("/api/stores/solares/products").json()}
    assert listed == set(EXPECTED) | {"boiler-gas-40-litros"}
    for slug, (tubes, pressure) in EXPECTED.items():
        values = {p["key"]: p["value"] for p in client.get(f"/api/stores/solares/products/{slug}").json()["properties"]}
        assert values["tubos"] == {"value": tubes} and values["presion"] == {"value": pressure}, slug
        assert values["foto"] == {"media_path": f"/content/solares/tubo-{tubes}-{pressure}.jpg"}  # stored as-is
    gas = {p["key"]: p["value"] for p in client.get("/api/stores/solares/products/boiler-gas-40-litros").json()["properties"]}
    assert gas["tipo_gas"] == {"value": "Gas LP"} and "tubos" not in gas


# -------------------------------------------------------------- storefront resolution


def _template_file(slug: str, name: str) -> Path:
    import app.services.storefront as sf

    return Path(sf.templates_for_store(slug).env.get_template(name).filename)


def test_solares_resolves_to_its_copied_storefront_and_clothing_to_default(db_session, imported, storefronts):
    import app.services.storefront as sf

    import_store_pack_file(db_session, PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}")  # no storefront folder
    solares, default = storefronts / "solares", storefronts / "_default"
    assert _template_file("solares", "store.html") == solares / "templates" / "store.html"
    assert _template_file("solares", "product.html") == solares / "templates" / "product.html"
    assert _template_file("solares", "category.html") == default / "templates" / "category.html"  # not in the pack
    assert sf.resolve_static_file("solares", "style.css") == solares / "static" / "style.css"
    assert sf.storefront_has_override("solares") and not sf.storefront_has_override("clothing-demo")
    for name in ("store.html", "product.html", "category.html"):
        assert _template_file("clothing-demo", name) == default / "templates" / name
    assert sf.resolve_static_file("clothing-demo", "style.css") == default / "static" / "style.css"


def test_clothing_demo_pages_stay_on_default_next_to_solares(client, db_session, imported, storefronts):
    import_store_pack_file(db_session, PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}")
    store = client.get("/stores/clothing-demo").text
    product = client.get("/stores/clothing-demo/p/linen-shirt-m").text
    for html in (store, product):
        assert 'data-storefront="solares"' not in html and "solar-" not in html
    assert '<section class="grid-two">' in store  # _default/templates/store.html
    assert '<article class="product-detail">' in product  # _default/templates/product.html
    default_css = (storefronts / "_default" / "static" / "style.css").read_bytes()
    assert client.get("/stores/clothing-demo/assets/style.css").content == default_css
    assert client.get("/stores/solares/assets/style.css").content != default_css
