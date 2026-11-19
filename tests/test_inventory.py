"""Inventory: the SKU lookup API (admin), the admin SKU search, and the inventory CSV export
(admin download and command line)."""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.auth import create_user
from app.db import init_db
from app.models import Category, Product, Store, UserRole
from app.schemas import SkuLookupOut, SkuProductOut
from app.services import inventory_export
from app.services import stock as stock_svc
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from tests.markdown_doc import section, table_rows
from tests.ui_text import en, en_html, es, page_text

PROJECT = Path(__file__).resolve().parent.parent
README = (PROJECT / "README.md").read_text(encoding="utf-8")
SOLARES = read_store_pack(PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json")
PACK_SKUS = {p["slug"]: p["sku"] for p in SOLARES["products"]}  # the fixture's known SKUs
API = "/api/stores/solares/skus"
LIST = "/admin/stores/solares/products"
EXPORT = f"{LIST}/export.csv"


@pytest.fixture()
def solares(db_session):
    """The solares fixture store: every product has a SKU and stock 1 (from its pack)."""
    import_store_pack(db_session, SOLARES)
    return db_session.scalar(select(Store).where(Store.slug == "solares"))


def _product(db_session, slug: str) -> Product:
    db_session.expire_all()
    return db_session.scalar(select(Product).where(Product.slug == slug))


def _rows(response_or_bytes) -> list[dict[str, str]]:
    """The CSV's rows keyed by column id (the header is in the export's language)."""
    raw = response_or_bytes if isinstance(response_or_bytes, bytes) else response_or_bytes.content
    text = raw.decode("utf-8-sig")
    header, *rows = list(csv.reader(io.StringIO(text)))
    known = inventory_export.known_headers()
    ids = [known[inventory_export.header_key(cell)] for cell in header]
    return [dict(zip(ids, row)) for row in rows]


def _header(raw: bytes) -> list[str]:
    return next(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))


def _add_product(db_session, store: Store, slug: str, **fields) -> Product:
    category = db_session.scalar(select(Category).where(Category.store_id == store.id))
    product = Product(store_id=store.id, category_id=category.id, slug=slug, name=fields.pop("name", slug), **fields)
    db_session.add(product)
    db_session.commit()
    return product


# ==================================================================== SKU lookup API


def test_lookup_hit_returns_the_product_and_its_stock(admin_client, solares, db_session):
    response = admin_client.get(f"{API}/SOL-8T-ALTA")
    assert response.status_code == 200
    body = response.json()
    product = _product(db_session, "solar-8-tubos-alta")
    assert body == {
        "store": "solares", "currency": "MXN", "sku": "SOL-8T-ALTA", "stock_on_hand": 1, "in_stock": True,
        "product": {"id": product.id, "slug": "solar-8-tubos-alta", "name": product.name,
                    "price_cents": product.price_cents, "is_active": True, "category_id": product.category_id},
    }


@pytest.mark.parametrize("asked", ["sol-8t-alta", "Sol-8T-Alta", " SOL-8T-ALTA ", "SOL-8T-ALTA%20"])
def test_lookup_ignores_case_and_surrounding_spaces(admin_client, solares, asked):
    response = admin_client.get(f"{API}/{asked}")
    assert response.status_code == 200
    assert response.json()["sku"] == "SOL-8T-ALTA"  # as stored


def test_lookup_every_fixture_sku(admin_client, solares):
    for slug, sku in PACK_SKUS.items():
        body = admin_client.get(f"{API}/{sku}").json()
        assert (body["product"]["slug"], body["stock_on_hand"]) == (slug, 1), sku


def test_lookup_follows_the_stock(admin_client, solares, db_session):
    stock_svc.adjust_stock(db_session, _product(db_session, "boiler-gas-40-litros"), -1, "Sold")
    body = admin_client.get(f"{API}/GAS-BOIL-40L").json()
    assert (body["stock_on_hand"], body["in_stock"]) == (0, False)


def test_lookup_finds_hidden_products_too(admin_client, solares, db_session):
    product = _product(db_session, "solar-12-tubos-baja")
    product.is_active = False
    db_session.commit()
    body = admin_client.get(f"{API}/SOL-12T-BAJA").json()
    assert body["product"]["is_active"] is False  # inventory: hidden products still have stock


@pytest.mark.parametrize("asked", ["NOPE-1", "SOL-8T", "SOL-8T-ALTA-X", "%", "SOL_8T_ALTA", "has%20space", "x" * 65])
def test_lookup_miss_is_404(admin_client, solares, asked):
    response = admin_client.get(f"{API}/{asked}")
    assert response.status_code == 404
    assert response.json() == {"detail": "No product with this SKU in this store"}


def test_lookup_is_per_store(admin_client, solares, db_session):
    other = Store(slug="otra", name="Otra")
    db_session.add(other)
    db_session.flush()
    db_session.add(Category(store_id=other.id, slug="c", name="C"))
    db_session.commit()
    _add_product(db_session, other, "mismo-sku", sku="SOL-8T-ALTA", stock_on_hand=9)
    assert admin_client.get("/api/stores/otra/skus/SOL-8T-ALTA").json()["stock_on_hand"] == 9
    assert admin_client.get(f"{API}/SOL-8T-ALTA").json()["stock_on_hand"] == 1
    assert admin_client.get("/api/stores/otra/skus/GAS-BOIL-40L").status_code == 404  # solares' SKU


def test_unknown_store_is_404(admin_client):
    response = admin_client.get("/api/stores/nope/skus/SOL-8T-ALTA")
    assert response.status_code == 404 and response.json() == {"detail": "Store not found"}


def test_lookup_needs_an_admin_session_before_any_lookup(client, solares, db_session):
    for path in (f"{API}/SOL-8T-ALTA", f"{API}/NOPE", "/api/stores/nope/skus/X"):
        response = client.get(path)
        assert response.status_code == 401, path  # same answer: reveals nothing
        assert response.json() == {"detail": "Sign in as an admin to use the inventory API"}
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert client.get(f"{API}/SOL-8T-ALTA").status_code == 403


def test_documented_lookup_fields_are_the_response_models():
    """README "Lookup by SKU" documents exactly the response's fields."""
    documented = {row[0].strip("`") for row in table_rows(section(README, "Lookup by SKU"))
                  if row[0].startswith("`")}
    expected = set(SkuLookupOut.model_fields) - {"product"} | {f"product.{f}" for f in SkuProductOut.model_fields}
    assert documented == expected


# ==================================================================== admin SKU search


def test_admin_search_by_sku_contains_ignoring_case(admin_client, solares):
    page = admin_client.get(f"{LIST}?sku=alta")
    html = page.text
    found = set(re.findall(r'<tr id="product-\d+">\s*<td><a href="[^"]+">([^<]+)</a>', html))
    assert len(found) == 3  # SOL-8T-ALTA, SOL-16T-ALTA, SOL-20T-ALTA
    assert all("alta" in name.lower() for name in found)
    assert 'name="sku" type="search" value="ALTA"' in html  # normalized, kept in the box
    assert en("admin.products.clear_filters") in page_text(page)


def test_admin_search_exact_sku_finds_one(admin_client, solares):
    html = admin_client.get(f"{LIST}?sku=GAS-BOIL-40L").text
    assert html.count('<tr id="product-') == 1 and "<td><code>GAS-BOIL-40L</code></td>" in html


def test_admin_search_miss_says_so(admin_client, solares):
    page = admin_client.get(f"{LIST}?sku=zzz")
    assert '<tr id="product-' not in page.text
    assert en("admin.products.no_sku_match", sku="ZZZ") in page_text(page)


@pytest.mark.parametrize("wildcard", ["_", "%", "SOL_8T", "S%L"])
def test_admin_search_treats_like_wildcards_as_text(admin_client, solares, wildcard):
    """"_" and "%" are LIKE wildcards: typed in the box they match only themselves (no SKU here has them)."""
    assert '<tr id="product-' not in admin_client.get(f"{LIST}?sku={wildcard.replace('%', '%25')}").text


def test_admin_search_matches_a_literal_underscore(admin_client, solares, db_session):
    _add_product(db_session, solares, "under", sku="A_B-1")
    html = admin_client.get(f"{LIST}?sku=a_b").text
    assert html.count('<tr id="product-') == 1 and "<td><code>A_B-1</code></td>" in html


def test_admin_search_combines_with_the_category(admin_client, solares, db_session):
    gas = db_session.scalar(select(Category).where(Category.slug == "calentadores-gas"))
    assert '<tr id="product-' not in admin_client.get(f"{LIST}?sku=SOL&category_id={gas.id}").text
    assert admin_client.get(f"{LIST}?sku=GAS&category_id={gas.id}").text.count('<tr id="product-') == 1


def test_adjusting_from_a_search_comes_back_to_it(admin_client, solares, db_session):
    product = _product(db_session, "solar-8-tubos-alta")
    html = admin_client.get(f"{LIST}?sku=8t").text
    assert 'name="filter_sku" value="8T"' in html
    response = admin_client.post(f"{LIST}/{product.id}/stock", data={
        "delta": "2", "reason": "Count", "return": "list", "filter_sku": "8T"}, follow_redirects=False)
    assert response.headers["location"] == f"{LIST}?sku=8T#product-{product.id}"


# ==================================================================== inventory export


def test_admin_export_has_every_fixture_sku_and_stock(admin_client, solares):
    response = admin_client.get(EXPORT)
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert re.fullmatch(r'attachment; filename="inventory-solares-\d{8}-\d{6}\.csv"',
                        response.headers["content-disposition"])
    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM: spreadsheets read the accents right
    labels = inventory_export.column_labels("en")  # the page's language (the client asks ?lang=en)
    assert _header(response.content) == [labels[column] for column in inventory_export.COLUMNS]
    assert b"\r\n" in response.content
    rows = _rows(response)
    assert {row["sku"]: row["stock_on_hand"] for row in rows} == {sku: "1" for sku in PACK_SKUS.values()}
    assert {row["sku"]: row["slug"] for row in rows} == {sku: slug for slug, sku in PACK_SKUS.items()}
    assert all(row["store"] == "solares" and row["stock_at_export"] == row["stock_on_hand"] for row in rows)
    assert [row["sku"] for row in rows] == sorted(PACK_SKUS.values())
    assert "Calentador solar 8 tubos, alta presión" in {row["name"] for row in rows}  # accents kept


def test_export_has_the_current_numbers_and_products_without_sku(admin_client, solares, db_session):
    stock_svc.adjust_stock(db_session, _product(db_session, "solar-20-tubos-alta"), 6, "Received")
    _add_product(db_session, solares, "sin-sku", name="=HYPERLINK(\"http://x\")", stock_on_hand=3)
    rows = _rows(admin_client.get(EXPORT))
    by_slug = {row["slug"]: row for row in rows}
    assert by_slug["solar-20-tubos-alta"]["stock_on_hand"] == "7"
    assert rows[-1]["slug"] == "sin-sku" and rows[-1]["sku"] == ""  # no SKU: last, matched by slug
    assert rows[-1]["stock_on_hand"] == "3"
    assert rows[-1]["name"] == "'=HYPERLINK(\"http://x\")"  # not a formula in a spreadsheet


def test_export_button_only_when_there_are_products(admin_client, solares, demo_store):
    assert f'href="{EXPORT}" download>{en_html("admin.inventory.export")}</a>' in admin_client.get(LIST).text
    assert "export.csv" not in admin_client.get("/admin/stores/demo-widgets/products").text


def test_admin_export_of_an_empty_store_fails_cleanly(admin_client, demo_store):
    response = admin_client.get("/admin/stores/demo-widgets/products/export.csv")
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/html")  # the list page, not a CSV
    assert en("admin.inventory.error.no_products") in page_text(response)


def test_admin_export_of_a_missing_store_is_404(admin_client):
    assert admin_client.get("/admin/stores/nope/products/export.csv").status_code == 404


def test_export_needs_an_admin(client, solares):
    response = client.get(EXPORT, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


def test_safe_cell():
    for text in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx"):
        assert inventory_export.safe_cell(text) == "'" + text
    assert inventory_export.safe_cell("Boiler 40 L") == "Boiler 40 L"


# ------------------------------------------------------------------ command line


@pytest.fixture()
def cli_db(engine, monkeypatch):
    """The command line's database = the test's."""
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr("app.db.init_db", lambda: None)


def test_cli_writes_the_csv_file(solares, cli_db, tmp_path, capsys):
    target = tmp_path / "solares.csv"
    assert inventory_export.main(["solares", "-o", str(target)]) == 0
    assert f"5 product(s) of solares written to {target}" in capsys.readouterr().err
    rows = _rows(target.read_bytes())
    assert {row["sku"]: row["stock_on_hand"] for row in rows} == {sku: "1" for sku in PACK_SKUS.values()}


def test_cli_prints_to_standard_output(solares, cli_db, capsys):
    assert inventory_export.main(["solares", "--lang", "en"]) == 0
    out = capsys.readouterr().out
    assert not out.startswith("\ufeff")  # no BOM when piped
    assert {row["sku"] for row in _rows(out.encode())} == set(PACK_SKUS.values())
    assert _header(out.encode())[-1] == en("admin.inventory.csv.stock_on_hand")


# ------------------------------------------------------------------ header language


def test_admin_export_headers_follow_the_page_language(default_lang_client, solares):
    """Exported from a Spanish admin page: Spanish headers (the data is the same)."""
    client = default_lang_client
    client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
    spanish = client.get(EXPORT)
    # Every header is the Spanish catalog's text for its column (app/locales/es.json), in column order.
    assert _header(spanish.content) == [es(f"admin.inventory.csv.{column}") for column in inventory_export.COLUMNS]
    english = client.get(f"{EXPORT}?lang=en")
    assert _header(english.content) == [en(f"admin.inventory.csv.{column}") for column in inventory_export.COLUMNS]
    assert _rows(spanish)[0]["sku"] == _rows(english)[0]["sku"]


def test_cli_export_uses_the_stores_language_unless_asked(solares, cli_db, tmp_path):
    target = tmp_path / "x.csv"
    inventory_export.main(["solares", "-o", str(target)])  # solares is a Spanish store
    assert _header(target.read_bytes())[-1] == inventory_export.column_labels("es")["stock_on_hand"]
    inventory_export.main(["solares", "-o", str(target), "--lang", "en"])
    assert _header(target.read_bytes())[-1] == inventory_export.column_labels("en")["stock_on_hand"]


def test_headers_of_every_language_are_distinct_columns():
    """A header must never mean one column in a language and another column in another."""
    seen: dict[str, str] = {}
    from app.i18n import available_locales

    for locale in available_locales():
        for column, label in inventory_export.column_labels(locale).items():
            key = inventory_export.header_key(label)
            assert seen.setdefault(key, column) == column, (locale, label)
            assert label.isascii(), label  # survives a spreadsheet saving in a legacy encoding


@pytest.mark.parametrize("slug,message", [("nope", "error: no store with slug 'nope'"),
                                          ("empty", "error: store 'empty' has no products to export")])
def test_cli_fails_cleanly(db_session, cli_db, tmp_path, capsys, slug, message):
    db_session.add(Store(slug="empty", name="Empty"))
    db_session.commit()
    target = tmp_path / "out.csv"
    assert inventory_export.main([slug, "-o", str(target)]) == 1
    captured = capsys.readouterr()
    assert captured.err.strip() == message and captured.out == ""
    assert not target.exists()  # no half-written file


def test_documented_command_runs(tmp_path):
    """The README command, as a real process against a database file."""
    db_file = tmp_path / "inventory.db"
    engine = create_engine(f"sqlite:///{db_file}")
    init_db(bind=engine)
    with sessionmaker(bind=engine)() as db:
        import_store_pack(db, SOLARES)
    engine.dispose()
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}", "SEED_PACKS": ""}
    target = tmp_path / "solares-inventory.csv"
    done = subprocess.run([sys.executable, "-m", "app.services.inventory_export", "solares", "-o", str(target)],
                          cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert {row["sku"] for row in _rows(target.read_bytes())} == set(PACK_SKUS.values())
    missing = subprocess.run([sys.executable, "-m", "app.services.inventory_export", "nope"],
                             cwd=PROJECT, env=env, capture_output=True, text=True, timeout=60)
    assert missing.returncode == 1 and "no store with slug 'nope'" in missing.stderr
    assert "python -m app.services.inventory_export solares -o solares-inventory.csv" in README


def test_export_has_the_prices(admin_client, solares):
    rows = _rows(admin_client.get(EXPORT))
    by_sku = {row["sku"]: row for row in rows}
    assert by_sku["SOL-8T-ALTA"]["price"] == by_sku["SOL-8T-ALTA"]["price_at_export"] == "6499.00"
    assert by_sku["GAS-BOIL-40L"]["price"] == "5199.00"
    assert inventory_export.price_text(5) == "0.05" and inventory_export.price_text(0) == "0.00"
