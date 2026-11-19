"""The store's stock log view (/admin/stores/<slug>/inventory/log): every stock movement of the
store, newest first, filters (SKU, type, product), pages; the Inventory tab and links."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select

from app.i18n import translator
from app.models import Category, Product, StockMovementKind, Store
from app.routers.admin_products import movement_kind_labels
from app.services import stock as stock_svc
from app.services.inventory_import import import_inventory
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from tests.ui_text import en, en_html, es, page_text

PROJECT = Path(__file__).resolve().parent.parent
SOLARES = read_store_pack(PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json")
LOG = "/admin/stores/solares/inventory/log"
LABELS = movement_kind_labels(translator("en"))


@pytest.fixture()
def solares(db_session):
    import_store_pack(db_session, SOLARES)  # 5 pack_import movements
    return db_session.scalar(select(Store).where(Store.slug == "solares"))


def _product(db_session, sku: str) -> Product:
    db_session.expire_all()
    return db_session.scalar(select(Product).where(Product.sku == sku))


def _rows(html: str) -> list[list[str]]:
    body = html[html.index('class="admin-table stock-log"'):]
    body = body[:body.index("</table>")]
    return [[re.sub(r"<[^>]+>", "", cell).strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            for row in re.findall(r"<tr>(.*?)</tr>", body, re.S) if "<td" in row]


def test_log_lists_every_products_movements_newest_first(admin_client, solares, db_session):
    stock_svc.adjust_stock(db_session, _product(db_session, "SOL-8T-ALTA"), 4, "Received from supplier")
    import_inventory(db_session, "solares", b"sku,qty\r\nGAS-BOIL-40L,0\r\n", filename="conteo.csv")
    page = admin_client.get(LOG)
    assert page.status_code == 200
    rows = _rows(page.text)
    assert len(rows) == 7  # 5 from the pack, 1 adjustment, 1 inventory import
    newest, second = rows[0], rows[1]
    assert newest[1:8] == ["Boiler de gas de depósito 40 L", "GAS-BOIL-40L", LABELS[StockMovementKind.inventory_import],
                           "-1", "0", "conteo.csv", "—"]
    assert second[2:7] == ["SOL-8T-ALTA", LABELS[StockMovementKind.adjustment], "+4", "5", "Received from supplier"]
    assert {row[3] for row in rows[2:]} == {LABELS[StockMovementKind.pack_import]}
    assert en("admin.stock_log.count", total=7) in page_text(page)
    product = _product(db_session, "GAS-BOIL-40L")
    assert f'href="/admin/stores/solares/products/{product.id}#stock"' in page.text


@pytest.mark.parametrize("query,expected", [
    ("sku=alta", {"SOL-8T-ALTA", "SOL-16T-ALTA", "SOL-20T-ALTA"}),
    ("sku=gas-boil", {"GAS-BOIL-40L"}),
    ("sku=_", set()),  # a LIKE wildcard is a plain character
    ("kind=adjustment", {"SOL-8T-ALTA"}),
    ("kind=pack_import&sku=12t", {"SOL-12T-BAJA"}),
])
def test_log_filters(admin_client, solares, db_session, query, expected):
    stock_svc.adjust_stock(db_session, _product(db_session, "SOL-8T-ALTA"), 1, "Count")
    html = admin_client.get(f"{LOG}?{query}").text
    found = {row[2] for row in _rows(html)} if 'class="admin-table stock-log"' in html else set()
    assert found == expected
    if not expected:
        assert en("admin.stock_log.empty") in page_text(admin_client.get(f"{LOG}?{query}"))


def test_log_of_one_product_linked_from_its_page(admin_client, solares, db_session):
    product = _product(db_session, "SOL-16T-ALTA")
    product_page = admin_client.get(f"/admin/stores/solares/products/{product.id}").text
    link = f"/admin/stores/solares/inventory/log?product={product.id}"
    assert f'href="{link}">{en_html("admin.stock_log.product_link")}</a>' in product_page
    page = admin_client.get(link)
    assert {row[2] for row in _rows(page.text)} == {"SOL-16T-ALTA"}
    assert en("admin.stock_log.only_product", name=product.name) in page_text(page)
    assert f'name="product" value="{product.id}"' in page.text  # kept by the filter form


@pytest.mark.parametrize("query", ["kind=bogus", "product=abc", f"product={10 ** 30}", "page=x", "page=-3",
                                   "sku=" + "x" * 500])
def test_odd_parameters_never_fail(admin_client, solares, query):
    assert admin_client.get(f"{LOG}?{query}").status_code == 200


def test_log_pages(admin_client, solares, db_session):
    product = _product(db_session, "SOL-8T-ALTA")
    for _ in range(stock_svc.LOG_PAGE_SIZE):
        stock_svc.adjust_stock(db_session, product, 1, "Count")
    total = stock_svc.LOG_PAGE_SIZE + 5
    first = admin_client.get(f"{LOG}?sku=sol")
    assert len(_rows(first.text)) == stock_svc.LOG_PAGE_SIZE
    assert en("admin.stock_log.page", page=1, pages=2) in page_text(first)
    assert f'href="{LOG}?sku=SOL&amp;page=2"' in first.text
    second = admin_client.get(f"{LOG}?sku=sol&page=2")
    assert len(_rows(second.text)) == total - stock_svc.LOG_PAGE_SIZE - 1  # the boiler isn't SOL
    assert f'href="{LOG}?sku=SOL&amp;page=1"' in second.text
    assert len(_rows(admin_client.get(f"{LOG}?page=2").text)) == total - stock_svc.LOG_PAGE_SIZE


def test_typed_filters_are_escaped(admin_client, solares):
    html = admin_client.get(f"{LOG}?sku=a%26b%22%3E").text  # a&b"> typed in the box
    assert 'value="A&amp;B&#34;&gt;"' in html and 'B">' not in html


def test_other_stores_movements_are_not_listed(admin_client, solares, db_session):
    other = Store(slug="otra", name="Otra")
    db_session.add(other)
    db_session.flush()
    category = Category(store_id=other.id, slug="c", name="C")
    db_session.add(category)
    db_session.flush()
    foreign = Product(store_id=other.id, category_id=category.id, slug="p", name="Ajeno", sku="OTRA-1")
    db_session.add(foreign)
    db_session.commit()
    stock_svc.adjust_stock(db_session, foreign, 3, "Other store")
    assert "Ajeno" not in admin_client.get(LOG).text
    assert len(_rows(admin_client.get("/admin/stores/otra/inventory/log").text)) == 1


def test_empty_log_and_spanish_default(default_lang_client, db_session):
    client = default_lang_client
    client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
    client.post("/admin/stores", data={"slug": "vacia", "name": "Vacía", "currency": "MXN"})
    page = client.get("/admin/stores/vacia/inventory/log")
    assert f"<h1>{es('admin.stock_log.title')}</h1>" in page.text
    assert es("admin.stock_log.empty") in page_text(page)


def test_inventory_tab_and_links(admin_client, solares):
    inventory = admin_client.get("/admin/stores/solares/inventory").text
    assert f'href="/admin/stores/solares/inventory" aria-current="page">{en_html("admin.tabs.inventory")}</a>' in inventory
    assert f'href="{LOG}">{en_html("admin.stock_log.link")}</a>' in inventory
    log = admin_client.get(LOG).text
    assert 'href="/admin/stores/solares/inventory" aria-current="page"' in log
    tab = re.compile(r'href="/admin/stores/solares/inventory"\s*>' + re.escape(en_html("admin.tabs.inventory")) + "</a>")
    assert tab.search(admin_client.get("/admin/stores/solares/products").text)  # not the current tab there


def test_log_needs_an_admin(client, solares):
    response = client.get(LOG, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
    assert client.get("/admin/stores/nope/inventory/log").status_code == 404


# ------------------------------------------------------------------ reason filter


@pytest.mark.parametrize("query,expected", [
    ("reason=supplier", {"Received from supplier"}),
    ("reason=SUPPLIER", {"Received from supplier"}),  # ignoring case
    ("reason=rotura", {"Rotura en transporte"}),
    ("reason=conteo.csv", {"conteo.csv"}),  # inventory imports: the file name
    ("reason=%25", {"100% vendido"}),  # a typed % is a plain character
    ("reason=_", set()),
    ("reason=  from   supplier ", {"Received from supplier"}),  # spaces collapsed
    ("reason=transporte&kind=adjustment&sku=gas", {"Rotura en transporte"}),
    ("reason=transporte&sku=sol", set()),
])
def test_log_filter_by_reason(admin_client, solares, db_session, query, expected):
    stock_svc.adjust_stock(db_session, _product(db_session, "SOL-8T-ALTA"), 4, "Received from supplier")
    stock_svc.adjust_stock(db_session, _product(db_session, "GAS-BOIL-40L"), -1, "Rotura en transporte")
    stock_svc.adjust_stock(db_session, _product(db_session, "SOL-20T-ALTA"), -1, "100% vendido")
    import_inventory(db_session, "solares", b"sku,qty\r\nSOL-16T-ALTA,3\r\n", filename="conteo.csv")
    html = admin_client.get(f"{LOG}?{query}").text
    found = {row[6] for row in _rows(html)} if 'class="admin-table stock-log"' in html else set()
    assert found == expected


def test_reason_filter_is_kept_in_the_form_and_pages(admin_client, solares, db_session):
    product = _product(db_session, "SOL-8T-ALTA")
    for _ in range(stock_svc.LOG_PAGE_SIZE + 1):
        stock_svc.adjust_stock(db_session, product, 1, "Weekly count")
    html = admin_client.get(f"{LOG}?reason=weekly").text
    assert 'name="reason" type="search" value="weekly"' in html
    assert f'href="{LOG}?reason=weekly&amp;page=2"' in html
    assert len(_rows(admin_client.get(f"{LOG}?reason=weekly&page=2").text)) == 1
