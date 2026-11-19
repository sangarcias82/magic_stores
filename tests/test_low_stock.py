"""Low stock (app/services/low_stock.py): a threshold per store (Inventory -> Low stock) and an
optional one per product (product form); the admin list of a store's low products, Spanish first."""

from __future__ import annotations

import re
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.pool import StaticPool

from app.auth import create_user
from app.db import init_db
from app.models import DEFAULT_LOW_STOCK_THRESHOLD, Category, Product, Store, UserRole
from app.schema_upgrades import add_low_stock_thresholds
from app.services import low_stock
from tests.ui_text import en, es, page_text

SHOP = "/admin/stores/shop"
LOW = f"{SHOP}/inventory/low-stock"
PRODUCTS = f"{SHOP}/products"


@pytest.fixture()
def shop(db_session) -> Store:
    """A store with five products: 0, 1, 2, 3 and 10 units (one hidden, one with its own threshold)."""
    store = Store(slug="shop", name="Shop", currency="MXN")
    db_session.add(store)
    db_session.flush()
    category = Category(store_id=store.id, slug="misc", name="Misc")
    db_session.add(category)
    db_session.flush()
    for sku, stock, extra in (("OUT-0", 0, {}), ("ONE-1", 1, {"is_active": False}), ("TWO-2", 2, {}),
                              ("THREE-3", 3, {}), ("TEN-10", 10, {"low_stock_threshold": 12})):
        db_session.add(Product(store_id=store.id, category_id=category.id, slug=sku.lower(), sku=sku,
                               name=f"Product {sku}", price_cents=1000, stock_on_hand=stock, **extra))
    db_session.commit()
    return store


def _listed(client) -> list[tuple[str, str]]:
    """(SKU, state) of each row of the list, in order."""
    return re.findall(r'<tr data-sku="([^"]*)" data-state="(\w+)">', client.get(LOW).text)


def _product(db_session, sku: str) -> Product:
    db_session.expire_all()
    return db_session.scalar(select(Product).where(Product.sku == sku))


def _store(db_session) -> Store:
    db_session.expire_all()
    return db_session.scalar(select(Store).where(Store.slug == "shop"))


# ------------------------------------------------------------------------------ the rules


def test_a_new_store_has_the_default_threshold(db_session, shop):
    assert shop.low_stock_threshold == DEFAULT_LOW_STOCK_THRESHOLD == 2


def test_the_products_own_threshold_wins(db_session, shop):
    ten, two = _product(db_session, "TEN-10"), _product(db_session, "TWO-2")
    assert low_stock.effective_threshold(ten, shop) == 12 and low_stock.is_low(ten, shop)
    assert low_stock.effective_threshold(two, shop) == 2 and low_stock.is_low(two, shop)  # at the threshold: low
    assert not low_stock.is_low(_product(db_session, "THREE-3"), shop)
    ten.low_stock_threshold = 0  # its own 0: only when it runs out
    assert not low_stock.is_low(ten, shop)


@pytest.mark.parametrize("raw, required, expected", [
    ("5", True, (5, None)), (" 0 ", True, (0, None)), ("1000000", True, (1_000_000, None)),
    ("", False, (None, None)), ("  ", False, (None, None)),
    ("", True, (None, ("threshold_required", {}))),
    ("-1", True, (None, ("threshold_negative", {}))),
    ("1000001", True, (None, ("threshold_format", {"max": low_stock.THRESHOLD_MAX}))),
    ("99999999999999999999", False, (None, ("threshold_format", {"max": low_stock.THRESHOLD_MAX}))),
    ("2.5", True, (None, ("threshold_format", {"max": low_stock.THRESHOLD_MAX}))),
    ("1,000", True, (None, ("threshold_format", {"max": low_stock.THRESHOLD_MAX}))),
    ("+3", True, (None, ("threshold_format", {"max": low_stock.THRESHOLD_MAX}))),
    ("١٢", True, (None, ("threshold_format", {"max": low_stock.THRESHOLD_MAX}))),  # non-ASCII digits
    ("three", True, (None, ("threshold_format", {"max": low_stock.THRESHOLD_MAX}))),
])
def test_parse_threshold(raw, required, expected):
    assert low_stock.parse_threshold(raw, required=required) == expected


# ------------------------------------------------------------------------------ the list


def test_the_list_shows_low_products_out_of_stock_first(admin_client, db_session, shop):
    assert _listed(admin_client) == [("OUT-0", "out"), ("ONE-1", "low"), ("TWO-2", "low"), ("TEN-10", "low")]
    page = page_text(admin_client.get(LOW))
    assert f"<h1>{en('admin.low_stock.title')}</h1>" in page
    assert en("admin.low_stock.intro", threshold=2) in page
    assert en("admin.low_stock.count", count=4) in page
    assert f"12 <span class=\"meta\">({en('admin.low_stock.own')})</span>" in page  # TEN-10's own threshold
    assert f"2 <span class=\"meta\">({en('admin.low_stock.store')})</span>" in page
    assert f"· {en('status.hidden')}" in page  # ONE-1 is hidden: still the store's stock
    assert f'<a href="{PRODUCTS}/{_product(db_session, "TWO-2").id}">Product TWO-2</a>' in page
    assert "THREE-3" not in page  # 3 units > 2


def test_the_inventory_page_links_to_it_with_the_count(admin_client, shop):
    page = page_text(admin_client.get(f"{SHOP}/inventory"))
    assert f'<a href="{LOW}" class="low-stock-link">{en("admin.low_stock.link", count=4)}</a>' in page


def test_nothing_low(admin_client, db_session, shop):
    for product in db_session.scalars(select(Product)):
        product.stock_on_hand = 50
    db_session.commit()
    assert _listed(admin_client) == []
    assert en("admin.low_stock.empty") in page_text(admin_client.get(LOW))


def test_the_list_is_spanish_by_default(default_lang_client, shop):
    default_lang_client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
    page = page_text(default_lang_client.get(LOW))
    for text_ in (f"<h1>{es('admin.low_stock.title')}</h1>", es("admin.low_stock.threshold_heading"),
                  es("admin.low_stock.save"), es("stock.out_of_stock"), es("admin.low_stock.low"),
                  es("admin.low_stock.count", count=4)):
        assert text_ in page, text_
    refused = default_lang_client.post(f"{LOW}/threshold", data={"threshold": "-3"})
    assert refused.status_code == 400 and es("admin.low_stock.error.negative") in page_text(refused)


def test_only_for_admins(client, db_session, shop):
    for method, path in (("GET", LOW), ("POST", f"{LOW}/threshold")):
        response = client.request(method, path, data={"threshold": "9"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == f"/login?next={quote(path, safe='')}"
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    for method, path in (("GET", LOW), ("POST", f"{LOW}/threshold")):
        assert client.request(method, path, data={"threshold": "9"}).status_code == 403
    assert _store(db_session).low_stock_threshold == 2


def test_unknown_store_is_404(admin_client):
    assert admin_client.get("/admin/stores/nope/inventory/low-stock").status_code == 404


# ------------------------------------------------------------------------- store threshold


def test_set_the_store_threshold(admin_client, db_session, shop):
    saved = admin_client.post(f"{LOW}/threshold", data={"threshold": " 3 "}, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == LOW
    assert _store(db_session).low_stock_threshold == 3
    page = page_text(admin_client.get(LOW))
    assert en("flash.low_stock_threshold_saved", threshold=3) in page
    assert 'name="threshold" value="3"' in page
    assert [sku for sku, _ in _listed(admin_client)] == ["OUT-0", "ONE-1", "TWO-2", "THREE-3", "TEN-10"]

    admin_client.post(f"{LOW}/threshold", data={"threshold": "0"})  # 0: only what ran out
    assert _listed(admin_client) == [("OUT-0", "out"), ("TEN-10", "low")]  # TEN-10 keeps its own 12


@pytest.mark.parametrize("raw, key, params", [
    ("", "required", {}), ("-1", "negative", {}), ("2.5", "format", {"max": low_stock.THRESHOLD_MAX}),
    ("1000001", "format", {"max": low_stock.THRESHOLD_MAX}),
])
def test_a_bad_store_threshold_is_refused(admin_client, db_session, shop, raw, key, params):
    refused = admin_client.post(f"{LOW}/threshold", data={"threshold": raw})
    assert refused.status_code == 400
    page = page_text(refused)
    assert f'id="threshold-error">{en(f"admin.low_stock.error.{key}", **params)}</p>' in page
    assert f'name="threshold" value="{raw}"' in page  # what was typed stays
    assert _store(db_session).low_stock_threshold == 2  # nothing saved


def test_a_post_without_the_field_is_refused(admin_client, db_session, shop):
    assert admin_client.post(f"{LOW}/threshold").status_code == 400
    assert _store(db_session).low_stock_threshold == 2


# --------------------------------------------------------------------- product threshold


def _edit(client, db_session, sku: str, **fields):
    product = _product(db_session, sku)
    data = {"slug": product.slug, "sku": product.sku, "name": product.name, "price": "10.00",
            "stock": str(product.stock_on_hand), "stock_seen": str(product.stock_on_hand), **fields}
    return client.post(f"{PRODUCTS}/{product.id}", data=data, follow_redirects=False)


def test_the_product_form_sets_and_clears_its_threshold(admin_client, db_session, shop):
    form = page_text(admin_client.get(f"{PRODUCTS}/{_product(db_session, 'TEN-10').id}"))
    assert 'name="low_stock" value="12"' in form and en("admin.low_stock.product_hint", threshold=2) in form

    assert _edit(admin_client, db_session, "THREE-3", low_stock=" 5 ").status_code == 303
    assert _product(db_session, "THREE-3").low_stock_threshold == 5
    assert ("THREE-3", "low") in _listed(admin_client)

    assert _edit(admin_client, db_session, "THREE-3", low_stock="").status_code == 303  # empty: the store's
    assert _product(db_session, "THREE-3").low_stock_threshold is None
    assert "THREE-3" not in dict(_listed(admin_client))


def test_an_older_form_without_the_field_keeps_it(admin_client, db_session, shop):
    assert _edit(admin_client, db_session, "TEN-10").status_code == 303  # no low_stock field at all
    assert _product(db_session, "TEN-10").low_stock_threshold == 12


@pytest.mark.parametrize("raw, key, params", [
    ("-2", "negative", {}), ("abc", "format", {"max": low_stock.THRESHOLD_MAX}),
    ("1000001", "format", {"max": low_stock.THRESHOLD_MAX}),
])
def test_a_bad_product_threshold_is_refused(admin_client, db_session, shop, raw, key, params):
    refused = _edit(admin_client, db_session, "TEN-10", low_stock=raw, name="Renamed")
    assert refused.status_code == 400
    assert f'id="low_stock-error">{en(f"admin.low_stock.error.{key}", **params)}</p>' in page_text(refused)
    product = _product(db_session, "TEN-10")
    assert (product.low_stock_threshold, product.name) == (12, "Product TEN-10")  # nothing saved


def test_create_a_product_with_its_threshold(admin_client, db_session, shop):
    category_id = db_session.scalar(select(Category.id).where(Category.slug == "misc"))
    created = admin_client.post(PRODUCTS, data={"category_id": str(category_id), "slug": "new-one", "sku": "NEW-1",
                                                "stock": "4", "name": "New", "price": "1", "low_stock": "4"},
                                follow_redirects=False)
    assert created.status_code == 303
    assert _product(db_session, "NEW-1").low_stock_threshold == 4
    assert ("NEW-1", "low") in _listed(admin_client)


def test_the_products_list_marks_low_stock(admin_client, shop):
    page = page_text(admin_client.get(PRODUCTS))
    assert page.count(f'<span class="meta low-stock-mark">· {en("admin.low_stock.low")}</span>') == 3  # 1, 2, 10 (own 12)
    assert f"0 <span class=\"meta\">· {en('stock.out_of_stock')}</span>" in page


# --------------------------------------------------------------------------- older databases


def test_older_tables_get_the_threshold_columns():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE stores (id INTEGER PRIMARY KEY, slug VARCHAR(64), name VARCHAR(200))"))
        connection.execute(text("INSERT INTO stores (id, slug, name) VALUES (1, 'old', 'Old')"))
        connection.execute(text("CREATE TABLE products (id INTEGER PRIMARY KEY, store_id INTEGER)"))
    init_db(bind=engine)
    assert "low_stock_threshold" in {c["name"] for c in inspect(engine).get_columns("products")}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT low_stock_threshold FROM stores")).scalar() == DEFAULT_LOW_STOCK_THRESHOLD
    assert add_low_stock_thresholds(engine) == 0
    assert add_low_stock_thresholds(create_engine("sqlite://")) == 0
