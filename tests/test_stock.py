"""Stock adjustments and the movement log (app/services/stock.py, the admin product list and page),
and the solares pack's default SKUs and stock (pack fields ``sku`` / ``stock``)."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth import create_user
from app.i18n import translator
from app.models import Product, StockMovement, StockMovementKind, Store, User, UserRole
from app.routers.admin_products import movement_kind_labels, stock_error_text
from app.services import stock as svc
from app.services.store_pack_import import StorePackImportError, import_store_pack
from app.services.store_pack_validation import read_store_pack, validate_store_pack
from tests.ui_text import en, en_html, es, es_html, page_text

PROJECT = Path(__file__).resolve().parent.parent
SOLARES = PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json"
CLOTHING = PROJECT / "fixtures" / "packs" / "clothing-demo.store-pack.v1.json"
PRODUCTS = "/admin/stores/demo-widgets/products"
LOGIN = {"email": "admin@example.com", "password": "ChangeMeAdmin123!"}


# ------------------------------------------------------------------------------ helpers


def _create(client, category_id: int, slug: str = "widget", stock: str = "10", **extra) -> int:
    data = {"category_id": str(category_id), "slug": slug, "sku": slug.upper(), "name": slug.title(), "price": "5",
            "stock": stock, "prop_size": "M", **extra}
    response = client.post(PRODUCTS, data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


def _product(db_session, product_id: int) -> Product:
    db_session.expire_all()
    return db_session.get(Product, product_id)


def _movements(db_session, product_id: int) -> list[StockMovement]:
    db_session.expire_all()
    return list(db_session.scalars(select(StockMovement).where(StockMovement.product_id == product_id)
                                   .order_by(StockMovement.id)))


def _adjust(client, product_id: int, delta: str, reason: str = "Recount", **extra):
    return client.post(f"{PRODUCTS}/{product_id}/stock", data={"delta": delta, "reason": reason, **extra},
                       follow_redirects=False)


def _admin(db_session) -> User:
    return db_session.scalar(select(User).where(User.role == UserRole.admin))


# ------------------------------------------------------------------ the service


def test_adjust_adds_and_removes_units_with_a_logged_movement(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock="10")
    product, admin = _product(db_session, product_id), _admin(db_session)

    movement = svc.adjust_stock(db_session, product, 5, "Received from supplier", user=admin)
    assert (movement.kind, movement.delta, movement.quantity_after) == (StockMovementKind.adjustment, 5, 15)
    assert movement.reason == "Received from supplier" and movement.user_id == admin.id
    assert movement.store_id == product.store_id
    svc.adjust_stock(db_session, product, -15, "Sold out at the fair", user=admin)  # down to exactly 0: allowed
    assert _product(db_session, product_id).stock_on_hand == 0

    log = _movements(db_session, product_id)
    assert [(m.kind, m.delta, m.quantity_after) for m in log] == [
        (StockMovementKind.initial, 10, 10),  # created in admin with stock 10
        (StockMovementKind.adjustment, 5, 15),
        (StockMovementKind.adjustment, -15, 0),
    ]


@pytest.mark.parametrize("delta,code,result", [(-11, "below_zero", -1), (-1000, "below_zero", -990)])
def test_adjustment_below_zero_is_refused_and_writes_nothing(admin_client, widgets_category, db_session,
                                                            delta, code, result):
    product_id = _create(admin_client, widgets_category.id, stock="10")
    with pytest.raises(svc.StockError) as refused:
        svc.adjust_stock(db_session, _product(db_session, product_id), delta, "Oops")
    assert refused.value.error == (code, {"current": 10, "delta": f"{delta:+d}", "result": result})
    assert _product(db_session, product_id).stock_on_hand == 10
    assert len(_movements(db_session, product_id)) == 1  # only the initial one


def test_adjustment_over_the_maximum_is_refused(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock=str(svc.STOCK_MAX - 1))
    with pytest.raises(svc.StockError) as refused:
        svc.adjust_stock(db_session, _product(db_session, product_id), 2, "Too many")
    assert refused.value.error[0] == "too_large"
    with pytest.raises(svc.StockError):
        svc.adjust_stock(db_session, _product(db_session, product_id), 0, "Nothing")  # zero: refused too


def test_below_zero_check_uses_the_current_stock_not_a_stale_copy(admin_client, widgets_category, db_session):
    """Two admins, same product: the second one's page still says 5, but the first already removed
    4. The refusal is decided by the database row (one conditional UPDATE), not by the stale value."""
    product_id = _create(admin_client, widgets_category.id, stock="5")
    stale = _product(db_session, product_id)
    assert stale.stock_on_hand == 5
    other = _product(db_session, product_id)
    svc.adjust_stock(db_session, other, -4, "First admin")
    stale.stock_on_hand = 5  # what the second admin's copy still believes (not flushed)
    db_session.expunge(stale)
    with pytest.raises(svc.StockError) as refused:
        svc.adjust_stock(db_session, stale, -3, "Second admin")
    assert refused.value.error[1]["current"] == 1
    assert _product(db_session, product_id).stock_on_hand == 1


@pytest.mark.parametrize(
    "delta,reason,errors",
    [
        ("", "r", {"delta": ("delta_required", {})}),
        ("abc", "r", {"delta": ("delta_format", {})}),
        ("1.5", "r", {"delta": ("delta_format", {})}),
        ("1,000", "r", {"delta": ("delta_format", {})}),
        ("١٢", "r", {"delta": ("delta_format", {})}),
        ("0", "r", {"delta": ("delta_zero", {})}),
        ("-0", "r", {"delta": ("delta_zero", {})}),
        ("9" * 21, "r", {"delta": ("delta_too_large", {})}),
        ("2147483648", "r", {"delta": ("delta_too_large", {})}),
        ("5", "", {"reason": ("reason_required", {})}),
        ("5", "   ", {"reason": ("reason_required", {})}),
        ("5", "x" * 201, {"reason": ("reason_too_long", {"max_len": 200})}),
        ("", "", {"delta": ("delta_required", {}), "reason": ("reason_required", {})}),
    ],
)
def test_adjustment_form_validation(delta, reason, errors):
    form = svc.AdjustmentForm(delta=delta, reason=reason)
    svc.parse_adjustment(form)
    assert form.errors == errors


@pytest.mark.parametrize("raw,expected", [("5", 5), ("+5", 5), (" -2 ", -2), ("-2147483647", -2147483647)])
def test_adjustment_form_accepts_signed_whole_numbers(raw, expected):
    form = svc.AdjustmentForm(delta=raw, reason="  Received   from   supplier ")
    assert svc.parse_adjustment(form) == (expected, "Received from supplier")  # reason: spaces collapsed
    assert form.errors == {}


def test_database_refuses_a_zero_or_negative_movement(admin_client, widgets_category, db_session):
    product = _product(db_session, _create(admin_client, widgets_category.id))
    for delta, after in ((0, 3), (-1, -1)):
        svc.record_movement(db_session, product, StockMovementKind.adjustment, delta, after, reason="x")
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


def test_deleting_a_product_deletes_its_movements(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id)
    assert _adjust(admin_client, product_id, "3").status_code == 303
    assert admin_client.post(f"{PRODUCTS}/{product_id}/delete", follow_redirects=False).status_code == 303
    assert _movements(db_session, product_id) == []


# ------------------------------------------------------------ admin: the product list


def test_list_has_an_adjust_control_per_product(admin_client, widgets_category):
    product_id = _create(admin_client, widgets_category.id)
    page = admin_client.get(PRODUCTS)
    html = page.text
    assert f'<tr id="product-{product_id}">' in html
    assert f'action="{PRODUCTS}/{product_id}/stock"' in html
    assert f'<input id="delta-{product_id}" name="delta" type="number" step="1"' in html
    assert "data-stepper" in html and f'name="reason"' in html and 'name="return" value="list"' in html
    assert f">{en_html('admin.stock.save')}</button>" in html
    assert f"<summary>{en_html('admin.stock.adjust')}</summary>" in html
    assert '<script src="/static/admin-stock.js" defer></script>' in html
    assert "<details open" not in html  # closed until used
    assert admin_client.get("/static/admin-stock.js").status_code == 200


def test_adjust_from_the_list_saves_only_the_stock_and_returns_to_the_list(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock="10")
    before = _product(db_session, product_id)
    response = _adjust(admin_client, product_id, "+5", "Received from supplier", **{
        "return": "list", "category_id": str(widgets_category.id)})
    assert response.status_code == 303
    assert response.headers["location"] == f"{PRODUCTS}?category_id={widgets_category.id}#product-{product_id}"

    after = _product(db_session, product_id)
    assert after.stock_on_hand == 15
    assert (after.name, after.price_cents, after.sku) == (before.name, before.price_cents, before.sku)
    movement = _movements(db_session, product_id)[-1]
    assert (movement.delta, movement.reason, movement.user.email) == (5, "Received from supplier", LOGIN["email"])

    listing = page_text(admin_client.get(response.headers["location"]))
    assert en("flash.stock_adjusted", name="Widget", before="10", after="15", delta="+5") in listing


def test_adjust_below_zero_from_the_list_is_refused_in_that_row(admin_client, widgets_category, db_session):
    first = _create(admin_client, widgets_category.id, slug="first", stock="2")
    second = _create(admin_client, widgets_category.id, slug="second", stock="2")
    response = _adjust(admin_client, second, "-3", "Broken in transport", **{"return": "list"})
    assert response.status_code == 400
    html = response.text
    message = en_html("admin.stock.error.below_zero", current=2, delta="-3", result=-1)
    row = html[html.index(f'<tr id="product-{second}">'):]
    row = row[:row.index("</tr>")]
    assert "<details open>" in row and f'id="delta-{second}-error">{message}</p>' in row
    assert 'value="-3"' in row and 'value="Broken in transport"' in row  # kept to correct it
    assert f'id="delta-{first}-error"' not in html  # only that row
    assert _product(db_session, second).stock_on_hand == 2
    assert len(_movements(db_session, second)) == 1  # nothing logged


def test_adjust_without_reason_is_refused_in_that_row(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id)
    response = _adjust(admin_client, product_id, "4", "", **{"return": "list"})
    assert response.status_code == 400
    assert f'id="reason-{product_id}-error">{en_html("admin.stock.error.reason_required")}</p>' in response.text
    assert _product(db_session, product_id).stock_on_hand == 10


def test_adjust_errors_are_spanish_by_default(default_lang_client, db_session):
    """No language chosen (the admin default): the refusal is in Spanish."""
    client = default_lang_client
    assert client.post("/login", data=LOGIN, follow_redirects=False).status_code == 303
    assert client.post("/admin/stores", data={"slug": "demo-widgets", "name": "Demo", "currency": "USD"},
                       follow_redirects=False).status_code == 303
    category = client.post("/admin/stores/demo-widgets/categories", data={"slug": "c", "name": "C"},
                           follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    created = client.post(PRODUCTS, data={"category_id": category, "slug": "p", "sku": "P", "name": "P", "price": "1",
                                          "stock": "1"}, follow_redirects=False)
    product_id = int(created.headers["location"].rsplit("/", 1)[1])
    response = client.post(f"{PRODUCTS}/{product_id}/stock", data={"delta": "-2", "reason": "Rotura", "return": "list"})
    assert response.status_code == 400
    assert es_html("admin.stock.error.below_zero", current=1, delta="-2", result=-1) in response.text
    assert en_html("admin.stock.error.below_zero", current=1, delta="-2", result=-1) not in response.text
    assert f"<summary>{es_html('admin.stock.adjust')}</summary>" in response.text


# ---------------------------------------------------------- admin: the product page


def test_product_page_adjusts_and_shows_the_movement_log(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock="10")
    url = f"{PRODUCTS}/{product_id}"
    page = admin_client.get(url).text
    assert 'id="stock"' in page and 'name="return" value="product"' in page

    response = _adjust(admin_client, product_id, "-4", "Damaged <b>box</b>", **{"return": "product"})
    assert response.status_code == 303 and response.headers["location"] == f"{url}#stock"
    page = admin_client.get(url)
    html, text = page.text, page_text(page)
    assert en("admin.stock.history") in text
    labels = movement_kind_labels(translator("en"))
    history = html[html.index('class="admin-table stock-history"'):]
    assert f"<td>{labels[StockMovementKind.adjustment]}</td>" in history
    assert '<td class="number">-4</td>' in history and '<td class="number">6</td>' in history
    assert f"<td>{labels[StockMovementKind.initial]}</td>" in history and '<td class="number">+10</td>' in history
    assert "Damaged &lt;b&gt;box&lt;/b&gt;" in history and "<b>box</b>" not in history  # reason escaped
    assert f"<td>{LOGIN['email']}</td>" in history
    # Newest first.
    assert history.index(labels[StockMovementKind.adjustment]) < history.index(labels[StockMovementKind.initial])
    assert '<strong>6</strong>' in html  # the summary line


def test_product_page_refusal_keeps_the_form_and_changes_nothing(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock="1")
    response = _adjust(admin_client, product_id, "-5", "Theft", **{"return": "product"})
    assert response.status_code == 400
    assert en_html("admin.stock.error.below_zero", current=1, delta="-5", result=-4) in response.text
    assert 'value="-5"' in response.text and 'value="Theft"' in response.text
    assert '<input id="stock" name="stock" value="1"' in response.text  # the product form is intact
    assert _product(db_session, product_id).stock_on_hand == 1


def test_new_product_without_stock_has_an_empty_log(admin_client, widgets_category):
    product_id = _create(admin_client, widgets_category.id, stock="0")
    assert en("admin.stock.history_empty") in page_text(admin_client.get(f"{PRODUCTS}/{product_id}"))


def test_long_history_shows_the_latest_movements(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock="0")
    product = _product(db_session, product_id)
    for _ in range(svc.HISTORY_LIMIT + 1):
        svc.adjust_stock(db_session, product, 1, "Count")
    text = page_text(admin_client.get(f"{PRODUCTS}/{product_id}"))
    assert en("admin.stock.history_more", shown=svc.HISTORY_LIMIT, total=svc.HISTORY_LIMIT + 1) in text
    movements, total = svc.product_history(db_session, product)
    assert len(movements) == svc.HISTORY_LIMIT and total == svc.HISTORY_LIMIT + 1
    assert movements[0].quantity_after == svc.HISTORY_LIMIT + 1  # newest first


def test_the_public_flag_follows_adjustments(admin_client, widgets_category):
    product_id = _create(admin_client, widgets_category.id, slug="widget", stock="1")
    assert _adjust(admin_client, product_id, "-1").status_code == 303
    assert admin_client.get("/api/stores/demo-widgets/products/widget").json()["in_stock"] is False
    assert _adjust(admin_client, product_id, "2").status_code == 303
    assert admin_client.get("/api/stores/demo-widgets/products/widget").json()["in_stock"] is True


# ------------------------------------------------------ the product form and the log


def _form_data(category_id: int, **overrides) -> dict[str, str]:
    return {"category_id": str(category_id), "slug": "widget", "sku": "WIDGET", "name": "Widget", "price": "5",
            "prop_size": "M", **overrides}


def test_form_edit_of_the_stock_is_logged(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock="10")
    response = admin_client.post(f"{PRODUCTS}/{product_id}", data=_form_data(widgets_category.id, stock="4",
                                                                             stock_seen="10"), follow_redirects=False)
    assert response.status_code == 303
    last = _movements(db_session, product_id)[-1]
    assert (last.kind, last.delta, last.quantity_after, last.reason) == (StockMovementKind.edit, -6, 4, None)
    # Saving other fields with the stock unchanged logs nothing.
    admin_client.post(f"{PRODUCTS}/{product_id}", data=_form_data(widgets_category.id, name="Renamed", stock="4",
                                                                  stock_seen="4"))
    assert len(_movements(db_session, product_id)) == 2


def test_form_opened_before_an_adjustment_never_overwrites_it(admin_client, widgets_category, db_session):
    product_id = _create(admin_client, widgets_category.id, stock="10")
    url = f"{PRODUCTS}/{product_id}"
    assert 'name="stock_seen" value="10"' in admin_client.get(url).text  # admin A opens the form
    assert _adjust(admin_client, product_id, "-2", "Sold at the counter").status_code == 303  # admin B: 8

    # A saves a new name, Stock field untouched (still 10): the name is saved, the stock stays 8.
    renamed = admin_client.post(url, data=_form_data(widgets_category.id, name="Renamed", stock="10", stock_seen="10"),
                                follow_redirects=False)
    assert renamed.status_code == 303
    product = _product(db_session, product_id)
    assert (product.name, product.stock_on_hand) == ("Renamed", 8)

    # A types 20 in a form that saw 10: refused (409), nothing saved, the current stock explained.
    conflict = admin_client.post(url, data=_form_data(widgets_category.id, name="Again", stock="20", stock_seen="10"))
    assert conflict.status_code == 409
    assert f'id="stock-error">{en_html("admin.stock.error.stock_changed", current=8)}</p>' in conflict.text
    assert 'name="stock_seen" value="8"' in conflict.text and '<input id="stock" name="stock" value="20"' in conflict.text
    assert _product(db_session, product_id).name == "Renamed"
    # Saving that page again applies 20 on purpose.
    again = admin_client.post(url, data=_form_data(widgets_category.id, name="Again", stock="20", stock_seen="8"),
                              follow_redirects=False)
    assert again.status_code == 303
    assert _product(db_session, product_id).stock_on_hand == 20
    assert _movements(db_session, product_id)[-1].delta == 12


def test_post_without_stock_seen_sets_the_stock_as_given(admin_client, widgets_category, db_session):
    """Older clients (curl) that don't send stock_seen: the number is set as before, and logged."""
    product_id = _create(admin_client, widgets_category.id, stock="10")
    response = admin_client.post(f"{PRODUCTS}/{product_id}", data=_form_data(widgets_category.id, stock="3"),
                                 follow_redirects=False)
    assert response.status_code == 303
    assert _movements(db_session, product_id)[-1].delta == -7
    garbage = admin_client.post(f"{PRODUCTS}/{product_id}",
                                data=_form_data(widgets_category.id, stock="5", stock_seen="x"), follow_redirects=False)
    assert garbage.status_code == 303 and _product(db_session, product_id).stock_on_hand == 5


# ---------------------------------------------------------------------- access


def test_adjusting_needs_an_admin(client, widgets_category, db_session):
    product_id = _create(client, widgets_category.id)
    client.post("/logout")
    anonymous = _adjust(client, product_id, "5")
    assert anonymous.status_code == 303 and anonymous.headers["location"].startswith("/login")
    create_user(db_session, "shopper@example.com", "shopper-pass", role=UserRole.customer)
    client.post("/login", data={"email": "shopper@example.com", "password": "shopper-pass"})
    assert _adjust(client, product_id, "5").status_code == 403
    assert _product(db_session, product_id).stock_on_hand == 10


def test_other_stores_products_are_404(admin_client, widgets_category, db_session):
    other = Store(slug="other-store", name="Other")
    db_session.add(other)
    db_session.commit()
    product_id = _create(admin_client, widgets_category.id)
    assert admin_client.post(f"/admin/stores/other-store/products/{product_id}/stock",
                             data={"delta": "1", "reason": "x"}).status_code == 404


def test_every_stock_error_code_has_a_translated_message():
    params = {"max_len": 200, "current": 3, "delta": "-5", "result": -2}
    codes = ["delta_required", "delta_format", "delta_zero", "delta_too_large", "reason_required", "reason_too_long",
             "below_zero", "too_large", "stock_changed"]
    for code in codes:
        spanish = stock_error_text(translator("es"), (code, params))
        english = stock_error_text(translator("en"), (code, params))
        assert spanish != english and "{" not in spanish + english, code
    assert len(set(movement_kind_labels(translator("es")).values())) == len(StockMovementKind)


# ------------------------------------------------- solares: default SKUs and stock


def test_solares_starts_with_skus_and_one_unit_of_each_product(client, db_session):
    pack = read_store_pack(SOLARES)
    report = import_store_pack(db_session, pack)
    assert report.stock_set == len(pack["products"]) and report.stock_unchanged == 0
    assert f"stock: {len(pack['products'])} set, 0 unchanged" in report.summary()
    for data in pack["products"]:
        product = db_session.scalar(select(Product).where(Product.slug == data["slug"]))
        assert (product.sku, product.stock_on_hand) == (data["sku"], 1), data["slug"]
        (movement,) = _movements(db_session, product.id)
        assert (movement.kind, movement.delta, movement.quantity_after) == (StockMovementKind.pack_import, 1, 1)
    assert all(item["in_stock"] for item in client.get("/api/stores/solares/products").json())
    assert len({p["sku"] for p in pack["products"]}) == len(pack["products"])


def test_reimport_overwrites_the_stock_and_logs_it(db_session):
    """The pack wins for the stock too: every import sets it to the pack's number, logged as a
    pack_import movement; products already at that number log nothing."""
    pack = read_store_pack(SOLARES)
    import_store_pack(db_session, pack)
    boiler = db_session.scalar(select(Product).where(Product.slug == "boiler-gas-40-litros"))
    svc.adjust_stock(db_session, boiler, 6, "Received")  # 7 in admin
    report = import_store_pack(db_session, pack)
    assert (report.stock_set, report.stock_unchanged) == (1, len(pack["products"]) - 1)
    assert _product(db_session, boiler.id).stock_on_hand == 1
    last = _movements(db_session, boiler.id)[-1]
    assert (last.kind, last.delta, last.quantity_after, last.user_id) == (StockMovementKind.pack_import, -6, 1, None)

    changed = copy.deepcopy(pack)
    changed["products"][0]["stock"] = 12
    report = import_store_pack(db_session, changed)
    first = db_session.scalar(select(Product).where(Product.slug == changed["products"][0]["slug"]))
    assert report.stock_set == 1 and _product(db_session, first.id).stock_on_hand == 12
    assert _movements(db_session, first.id)[-1].delta == 11


def test_pack_without_stock_keeps_the_stock(db_session):
    pack = read_store_pack(SOLARES)
    import_store_pack(db_session, pack)
    boiler = db_session.scalar(select(Product).where(Product.slug == "boiler-gas-40-litros"))
    svc.adjust_stock(db_session, boiler, 4, "Received")
    without = copy.deepcopy(pack)
    for product in without["products"]:
        del product["stock"]
    report = import_store_pack(db_session, without)
    assert (report.stock_set, report.stock_unchanged) == (0, 0)
    assert "stock:" not in report.summary()
    assert _product(db_session, boiler.id).stock_on_hand == 5


def test_failed_pack_import_changes_no_stock(db_session):
    """All or nothing: a conflicting pack leaves every stock (and the log) as it was."""
    pack = read_store_pack(SOLARES)
    import_store_pack(db_session, pack)
    store = db_session.scalar(select(Store).where(Store.slug == "solares"))
    category = db_session.scalar(select(Product)).category_id
    db_session.add(Product(store_id=store.id, category_id=category, slug="admin-made", name="A", sku="TAKEN-9"))
    db_session.commit()
    changed = copy.deepcopy(pack)
    for product in changed["products"]:
        product["stock"] = 50
    changed["products"][4]["sku"] = "TAKEN-9"  # conflict
    movements_before = db_session.scalar(select(func.count()).select_from(StockMovement))
    with pytest.raises(StorePackImportError):
        import_store_pack(db_session, changed)
    db_session.expire_all()
    assert {p.stock_on_hand for p in db_session.scalars(select(Product).where(Product.slug != "admin-made"))} == {1}
    assert db_session.scalar(select(func.count()).select_from(StockMovement)) == movements_before


def test_older_solares_database_gets_skus_and_stock_with_one_reimport(db_session):
    """Loaded before the pack had sku / stock: the re-import sets the SKUs and the pack's stock."""
    pack = read_store_pack(SOLARES)
    old = copy.deepcopy(pack)
    for product in old["products"]:
        del product["sku"], product["stock"]
    import_store_pack(db_session, old)
    products = {p.slug: p for p in db_session.scalars(select(Product))}
    assert all(p.sku is None and p.stock_on_hand == 0 for p in products.values())

    report = import_store_pack(db_session, pack)
    assert (report.stock_set, report.stock_unchanged) == (len(pack["products"]), 0)
    for data in pack["products"]:
        product = db_session.scalar(select(Product).where(Product.slug == data["slug"]))
        assert (product.sku, product.stock_on_hand) == (data["sku"], 1)


def test_pack_without_skus_keeps_admin_skus(db_session):
    pack = read_store_pack(CLOTHING)
    import_store_pack(db_session, pack)
    product = db_session.scalar(select(Product))
    product.sku = "ADMIN-1"
    db_session.commit()
    import_store_pack(db_session, pack)
    assert _product(db_session, product.id).sku == "ADMIN-1"


def test_sku_held_by_another_product_is_a_conflict_and_nothing_is_written(db_session):
    pack = read_store_pack(SOLARES)
    import_store_pack(db_session, pack)
    store = db_session.scalar(select(Store).where(Store.slug == "solares"))
    category = db_session.scalar(select(Product)).category_id
    db_session.add(Product(store_id=store.id, category_id=category, slug="admin-made", name="A", sku="TAKEN-1"))
    db_session.commit()
    changed = copy.deepcopy(pack)
    changed["products"][2]["sku"] = "taken-1"
    changed["products"][0]["name"] = "Should not be saved"
    with pytest.raises(StorePackImportError) as conflict:
        import_store_pack(db_session, changed)
    (issue,) = conflict.value.issues
    assert issue.path == "products[2].sku" and '"TAKEN-1"' in issue.message and '"admin-made"' in issue.message
    db_session.expire_all()
    first = db_session.scalar(select(Product).where(Product.slug == pack["products"][0]["slug"]))
    assert first.name == pack["products"][0]["name"]


def test_skus_can_be_swapped_between_the_packs_products(db_session):
    pack = read_store_pack(SOLARES)
    import_store_pack(db_session, pack)
    swapped = copy.deepcopy(pack)
    a, b = swapped["products"][0], swapped["products"][1]
    a["sku"], b["sku"] = b["sku"], a["sku"]
    import_store_pack(db_session, swapped)
    db_session.expire_all()
    skus = dict(db_session.execute(select(Product.slug, Product.sku)).all())
    assert (skus[a["slug"]], skus[b["slug"]]) == (pack["products"][1]["sku"], pack["products"][0]["sku"])


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("sku", "has space", "use letters, digits"),
        ("sku", "", "use letters, digits"),
        ("sku", "x" * 65, "use letters, digits"),
        ("sku", 12, "must be a string"),
        ("stock", -1, "must be between 0 and 2147483647"),
        ("stock", 2_147_483_648, "must be between 0 and 2147483647"),
        ("stock", 1.5, "must be a whole number of units"),
        ("stock", True, "must be a whole number of units"),
        ("stock", "3", "must be a whole number of units"),
    ],
)
def test_pack_sku_and_stock_are_validated(field, value, message):
    pack = read_store_pack(SOLARES)
    pack["products"][1][field] = value
    issues = validate_store_pack(pack)
    assert [(i.path, message in i.message) for i in issues] == [(f"products[1].{field}", True)]


def test_pack_skus_are_unique_ignoring_case():
    pack = read_store_pack(SOLARES)
    pack["products"][3]["sku"] = pack["products"][0]["sku"].lower()
    (issue,) = validate_store_pack(pack)
    assert issue.path == "products[3].sku" and "duplicate SKU" in issue.message and "products[0].sku" in issue.message


def test_import_record_counts_the_stock(admin_client, db_session):
    from app.models import PackImport

    response = admin_client.post("/admin/packs", files={"pack": ("solares.store-pack.v1.json", SOLARES.read_bytes(),
                                                                 "application/json")}, follow_redirects=False)
    assert response.status_code == 303
    record = db_session.scalar(select(PackImport).order_by(PackImport.id.desc()))
    assert record.report["stock_set"] == 5 and record.report["stock_unchanged"] == 0
    assert db_session.scalar(select(func.count()).select_from(StockMovement)) == 5


def test_setting_a_number_that_is_already_there_is_not_a_conflict(admin_client, widgets_category, db_session):
    """The form (or an import) saw 10 and wants 4; meanwhile someone already set 4: nothing to do,
    no "stock changed" error and no movement."""
    product_id = _create(admin_client, widgets_category.id, stock="10")
    product = _product(db_session, product_id)
    svc.adjust_stock(db_session, product, -6, "Counted")  # now 4
    count = len(_movements(db_session, product_id))
    assert svc.set_stock(db_session, _product(db_session, product_id), 4, 10, StockMovementKind.edit) is None
    assert len(_movements(db_session, product_id)) == count
    with pytest.raises(svc.StockError):  # a different number is still a conflict
        svc.set_stock(db_session, _product(db_session, product_id), 5, 10, StockMovementKind.edit)
