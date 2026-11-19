"""Inventory import: stock numbers back from a CSV (app/services/inventory_import.py), from admin
and the command line. Matches by SKU in one store, only sets stock, logs every change, applies
what it can (partial) and reports the rest."""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import init_db
from app.models import Category, Product, PropertyValue, StockMovement, StockMovementKind, Store, User, UserRole
from app.services import inventory_export, inventory_import
from app.services import stock as stock_svc
from app.services.inventory_import import InventoryImportError, import_inventory
from app.services.store_pack_import import import_store_pack
from app.services.store_pack_validation import read_store_pack
from tests.ui_text import en, en_html, es, page_text

PROJECT = Path(__file__).resolve().parent.parent
README = (PROJECT / "README.md").read_text(encoding="utf-8")
SOLARES = read_store_pack(PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json")
SKUS = [p["sku"] for p in SOLARES["products"]]
PAGE = "/admin/stores/solares/inventory"
LOGIN = {"email": "admin@example.com", "password": "ChangeMeAdmin123!"}


@pytest.fixture()
def solares(db_session):
    """The solares fixture store: every product has a SKU and stock 1."""
    import_store_pack(db_session, SOLARES)
    return db_session.scalar(select(Store).where(Store.slug == "solares"))


def _stock(db_session) -> dict[str, int]:
    db_session.expire_all()
    return {p.sku: p.stock_on_hand for p in db_session.scalars(select(Product).where(Product.sku.is_not(None)))}


def _csv(rows: list[list], delimiter: str = ",") -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _column(header: list[str], column: str) -> int:
    known = inventory_export.known_headers()
    return next(i for i, cell in enumerate(header) if known.get(inventory_export.header_key(cell)) == column)


def _edited_export(db_session, new_stock: dict[str, int], locale: str = "es", new_price: dict | None = None) -> bytes:
    """The real export file, with some stock / price cells edited (what a person does offline)."""
    export = inventory_export.export_inventory(db_session, "solares", locale)
    header, *rows = list(csv.reader(io.StringIO(export.csv_bytes().decode("utf-8-sig"))))
    stock, price, sku = _column(header, "stock_on_hand"), _column(header, "price"), _column(header, "sku")
    for row in rows:
        if row[sku] in new_stock:
            row[stock] = str(new_stock[row[sku]])
        if new_price and row[sku] in new_price:
            row[price] = new_price[row[sku]]
    return _csv([header, *rows])


def _codes(report) -> dict[str, tuple]:
    return {row.sku: row.error for row in report.refused}


def _snapshot(db_session) -> list[tuple]:
    """Everything but the stock, to prove the import touches nothing else."""
    db_session.expire_all()
    products = [(p.id, p.slug, p.sku, p.name, p.description, p.price_cents, p.is_active, p.category_id)
                for p in db_session.scalars(select(Product).order_by(Product.id))]
    values = [(v.product_id, v.definition_id, v.value) for v in db_session.scalars(select(PropertyValue)
                                                                                 .order_by(PropertyValue.id))]
    return [products, values]


# ============================================================ the service: round trip


def test_round_trip_sets_only_the_edited_stock_and_logs_it(solares, db_session):
    before = _snapshot(db_session)
    admin = None  # the command line has no user (the admin test is below)
    raw = _edited_export(db_session, {"SOL-8T-ALTA": 5, "GAS-BOIL-40L": 0})
    report = import_inventory(db_session, "solares", raw, filename="/tmp/conteo-noviembre.csv", user=admin)
    assert (report.count("applied"), report.count("unchanged"), report.count("refused")) == (2, 3, 0)
    assert {(row.sku, row.before, row.after) for row in report.applied} == {("SOL-8T-ALTA", 1, 5),
                                                                           ("GAS-BOIL-40L", 1, 0)}
    assert _stock(db_session) == {**{sku: 1 for sku in SKUS}, "SOL-8T-ALTA": 5, "GAS-BOIL-40L": 0}
    assert _snapshot(db_session) == before  # name, price, description, properties... untouched

    movements = db_session.scalars(select(StockMovement).where(StockMovement.kind == StockMovementKind.inventory_import)
                                   .order_by(StockMovement.id)).all()
    assert [(m.delta, m.quantity_after, m.reason) for m in movements] == [(-1, 0, "conteo-noviembre.csv"),
                                                                          (4, 5, "conteo-noviembre.csv")]
    # Importing the same file again changes nothing.
    again = import_inventory(db_session, "solares", raw, filename="conteo-noviembre.csv")
    assert again.count("applied") == 0 and again.count("unchanged") == 5


def test_the_admin_who_imported_is_in_the_log(solares, db_session):
    admin = User(email="inv@example.com", password_hash="x", role=UserRole.admin)
    db_session.add(admin)
    db_session.commit()
    import_inventory(db_session, "solares", _csv([["sku", "qty"], ["SOL-8T-ALTA", "3"]]), user=admin)
    movement = db_session.scalar(select(StockMovement).where(StockMovement.kind == StockMovementKind.inventory_import))
    assert movement.user_id == admin.id


@pytest.mark.parametrize("header", [
    [en("admin.inventory.csv.sku"), en("admin.inventory.csv.stock_on_hand")],  # English headers
    ["sku", "stock_on_hand"],  # the column ids
    ["  sku ", f' {es("admin.inventory.csv.stock_on_hand").upper()} '],  # Spanish, other case and spaces
    ["Sku", "QTY"], ["SKU", "Cantidad"], ["SKU", "Quantity"],  # the stock aliases (inventory_export.STOCK_ALIASES)
])
def test_headers_in_any_language_or_the_column_ids(solares, db_session, header):
    report = import_inventory(db_session, "solares", _csv([header, ["sol-12t-baja", "7"]]))
    assert report.count("applied") == 1 and _stock(db_session)["SOL-12T-BAJA"] == 7  # SKU case ignored


def test_a_spreadsheet_csv_with_semicolons_and_windows_1252(solares, db_session):
    """Spanish Excel "CSV": semicolon separated, not UTF-8 (names with accents)."""
    header = ";".join(es(f"admin.inventory.csv.{column}")
                      for column in ("store", "sku", "slug", "name", "stock_at_export", "stock_on_hand"))
    text = header + "\r\nsolares;SOL-16T-ALTA;solar-16-tubos-alta;Calentador solar 16 tubos, alta presión;1;9\r\n"
    report = import_inventory(db_session, "solares", text.encode("cp1252"))
    assert report.count("applied") == 1 and _stock(db_session)["SOL-16T-ALTA"] == 9


def test_extra_columns_and_blank_lines_are_ignored(solares, db_session):
    raw = _csv([["notes", "sku", "stock_on_hand", "whatever"], ["x", "SOL-8T-ALTA", "2", "y"], [], ["", "", "", ""],
                ["", "SOL-20T-ALTA", "4", ""]])
    report = import_inventory(db_session, "solares", raw)
    assert report.count("applied") == 2 and [row.line for row in report.rows] == [2, 5]


# ============================================================ refused rows (partial apply)


def test_refused_rows_are_reported_and_the_others_applied(solares, db_session):
    raw = _csv([
        ["store", "sku", "stock_on_hand"],
        ["solares", "SOL-8T-ALTA", "6"],       # 2: applied
        ["solares", "NOPE-1", "3"],            # 3: unknown SKU
        ["solares", "SOL-12T-BAJA", "2"],      # 4: duplicate (with 6)
        ["solares", "SOL-16T-ALTA", "-4"],     # 5: negative
        ["solares", "sol-12t-baja", "3"],      # 6: duplicate (case ignored)
        ["solares", "", "3"],                  # 7: no SKU
        ["otra", "SOL-20T-ALTA", "3"],         # 8: another store
        ["solares", "GAS-BOIL-40L", "2.5"],    # 9: not whole
        ["solares", "SOL-20T-ALTA", ""],       # 10: empty stock
    ])
    before = _stock(db_session)
    report = import_inventory(db_session, "solares", raw)
    assert report.count("applied") == 1 and report.count("refused") == 8
    assert {row.line: row.error for row in report.refused} == {
        3: ("unknown_sku", {"sku": "NOPE-1", "store": "solares"}),
        4: ("duplicate_sku", {"sku": "SOL-12T-BAJA", "lines": "4, 6"}),
        5: ("stock_negative", {"value": -4}),
        6: ("duplicate_sku", {"sku": "SOL-12T-BAJA", "lines": "4, 6"}),
        7: ("no_sku", {}),
        8: ("other_store", {"store": "otra"}),
        9: ("stock_format", {"value": "2.5"}),
        10: ("no_values", {}),
    }
    assert _stock(db_session) == {**before, "SOL-8T-ALTA": 6}  # only the good row
    assert db_session.scalar(select(func.count()).select_from(StockMovement)
                             .where(StockMovement.kind == StockMovementKind.inventory_import)) == 1


@pytest.mark.parametrize("value,code", [("-1", "stock_negative"), ("-0000003", "stock_negative"), ("abc", "stock_format"),
                                        ("1,000", "stock_format"), ("+5", "stock_format"), ("1e3", "stock_format"),
                                        ("٣", "stock_format"), ("9" * 21, "stock_format"),
                                        ("2147483648", "stock_too_large")])
def test_bad_stock_values_are_refused(solares, db_session, value, code):
    report = import_inventory(db_session, "solares", _csv([["sku", "qty"], ["SOL-8T-ALTA", value]]))
    assert report.refused[0].error[0] == code
    assert _stock(db_session)["SOL-8T-ALTA"] == 1


def test_an_edited_row_whose_stock_changed_since_the_export_is_refused(solares, db_session):
    raw = _edited_export(db_session, {"SOL-8T-ALTA": 10})  # exported with 1 everywhere
    boiler = db_session.scalar(select(Product).where(Product.sku == "GAS-BOIL-40L"))
    eight = db_session.scalar(select(Product).where(Product.sku == "SOL-8T-ALTA"))
    stock_svc.adjust_stock(db_session, eight, 2, "Received after the export")  # now 3
    stock_svc.adjust_stock(db_session, boiler, -1, "Sold after the export")  # now 0, row not edited

    report = import_inventory(db_session, "solares", raw)
    assert _codes(report) == {"SOL-8T-ALTA": ("stock_changed", {"current": 3, "exported": 1})}
    boiler_row = next(row for row in report.rows if row.sku == "GAS-BOIL-40L")
    assert boiler_row.status == "unchanged" and boiler_row.after == 0  # the sale is kept
    assert _stock(db_session)["SOL-8T-ALTA"] == 3 and _stock(db_session)["GAS-BOIL-40L"] == 0

    forced = import_inventory(db_session, "solares", raw, force=True)  # the count is the truth
    assert forced.count("refused") == 0
    assert _stock(db_session)["SOL-8T-ALTA"] == 10
    assert _stock(db_session)["GAS-BOIL-40L"] == 1  # force: every row's number is applied


def test_an_edited_stock_at_export_cell_is_refused(solares, db_session):
    raw = _csv([["sku", "stock_at_export", "stock_on_hand"], ["SOL-8T-ALTA", "uno", "3"], ["SOL-20T-ALTA", "", "3"]])
    report = import_inventory(db_session, "solares", raw)
    assert _codes(report) == {"SOL-8T-ALTA": ("exported_format", {"value": "uno"})}
    assert _stock(db_session)["SOL-20T-ALTA"] == 3  # empty = no check


def test_skus_of_another_store_are_unknown(solares, db_session):
    other = Store(slug="otra", name="Otra")
    db_session.add(other)
    db_session.flush()
    category = Category(store_id=other.id, slug="c", name="C")
    db_session.add(category)
    db_session.flush()
    db_session.add(Product(store_id=other.id, category_id=category.id, slug="p", name="P", sku="ONLY-OTRA"))
    db_session.commit()
    report = import_inventory(db_session, "solares", _csv([["sku", "qty"], ["ONLY-OTRA", "4"]]))
    assert report.refused[0].error[0] == "unknown_sku"


# ============================================================ whole file refused


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"", "empty_file"),
        (b"  \r\n ", "empty_file"),
        (b"PK\x03\x04\x00\x00binary", "not_csv"),
        (b"sku,qty\r\n", "no_rows"),
        (b"name,price\r\nx,1\r\n", "missing_columns"),
        (b"sku,name\r\nX,1\r\n", "missing_columns"),
        (b"sku,qty,stock_on_hand\r\nX,1,2\r\n", "duplicate_column"),  # an alias and the id: one column twice
        (b'sku,qty\r\n"SOL-8T-ALTA' + b"x" * 200_000 + b'",1\r\n', "not_csv"),
    ],
)
def test_unusable_files_are_refused_whole(solares, db_session, raw, code):
    with pytest.raises(InventoryImportError) as refused:
        import_inventory(db_session, "solares", raw)
    assert refused.value.error[0] == code
    assert _stock(db_session) == {sku: 1 for sku in SKUS}


def test_too_large_and_too_many_rows(solares, db_session, monkeypatch):
    with pytest.raises(InventoryImportError) as big:
        import_inventory(db_session, "solares", b"sku,qty\r\n" + b"A,1\r\n" * (inventory_import.MAX_BYTES // 5 + 1))
    assert big.value.error == ("too_large", {"max_mb": inventory_import.MAX_MB})
    monkeypatch.setattr(inventory_import, "MAX_ROWS", 2)
    with pytest.raises(InventoryImportError) as many:
        import_inventory(db_session, "solares", _csv([["sku", "qty"], ["A", "1"], ["B", "1"], ["C", "1"]]))
    assert many.value.error == ("too_many_rows", {"max": 2})


def test_unknown_store_is_refused(db_session):
    with pytest.raises(InventoryImportError) as refused:
        import_inventory(db_session, "nope", _csv([["sku", "qty"], ["A", "1"]]))
    assert refused.value.error == ("store_not_found", {"store": "nope"})


def test_missing_columns_are_named_in_the_messages_language():
    error = ("missing_columns", {"columns": ["sku", "stock_on_hand"]})
    columns = [f'"{es("admin.inventory.csv.sku")}"', f'"{es("admin.inventory.csv.stock_on_hand")}"']
    assert inventory_import.error_text("es", error) == es("admin.inventory.error.missing_columns",
                                                           columns=", ".join(columns))
    assert f'"{en("admin.inventory.csv.stock_on_hand")}"' in inventory_import.error_text("en", error)


def test_every_error_code_has_a_translated_message():
    params = {"store": "x", "max_mb": 5, "column": "sku", "columns": ["sku"], "max": 9, "sku": "A", "lines": "2, 3",
              "value": "-1", "current": 2, "exported": 1}
    codes = ["store_not_found", "empty_file", "too_large", "not_csv", "duplicate_column", "missing_columns",
             "too_many_rows", "no_rows", "other_store", "no_sku", "duplicate_sku", "unknown_sku", "no_values",
             "stock_format", "stock_negative", "stock_too_large", "exported_format", "stock_changed", "price_format",
             "price_comma", "price_negative", "price_too_large", "exported_price_format", "price_changed"]
    for code in codes:
        spanish, english = (inventory_import.error_text(locale, (code, params)) for locale in ("es", "en"))
        assert spanish != english and "{" not in spanish + english, code


# ============================================================ admin


def _upload(client, raw: bytes, name: str = "conteo.csv", url: str = f"{PAGE}/import", **form):
    return client.post(url, files={"file": (name, raw, "text/csv")}, data=form)


def test_inventory_page_links_export_and_import(admin_client, solares):
    products = admin_client.get("/admin/stores/solares/products").text
    assert f'href="{PAGE}">{en_html("admin.inventory.import")}</a>' in products
    page = admin_client.get(PAGE)
    assert page.status_code == 200
    assert 'href="/admin/stores/solares/products/export.csv" download' in page.text
    assert f'action="{PAGE}/import" enctype="multipart/form-data"' in page.text
    assert en("admin.inventory.import_rule_match") in page_text(page)


def test_admin_import_reports_changed_and_refused_rows(admin_client, solares, db_session):
    raw = _csv([["sku", "stock_on_hand"], ["SOL-8T-ALTA", "4"], ["NOPE-1", "2"], ["SOL-20T-ALTA", "-1"]])
    response = _upload(admin_client, raw)
    assert response.status_code == 200
    text = page_text(response)
    assert en("admin.inventory.result.summary", applied=1, unchanged=0, refused=2) in text
    assert en("admin.inventory.result.heading", file="conteo.csv") in text
    assert "1 → 4" in text
    assert en("admin.inventory.row.unknown_sku", sku="NOPE-1", store="solares") in text
    assert en("admin.inventory.row.stock_negative", value=-1) in text
    assert _stock(db_session)["SOL-8T-ALTA"] == 4

    # The product page's log shows it, with the file and the admin.
    product = db_session.scalar(select(Product).where(Product.sku == "SOL-8T-ALTA"))
    history = admin_client.get(f"/admin/stores/solares/products/{product.id}").text
    history = history[history.index('class="admin-table stock-history"'):]
    assert f"<td>{en_html('admin.stock.kind.inventory_import')}</td>" in history
    assert "<td>conteo.csv</td>" in history and f"<td>{LOGIN['email']}</td>" in history


def test_admin_round_trip_with_the_spanish_export(default_lang_client, solares, db_session):
    """Exported from the Spanish admin (Spanish headers), edited, uploaded back: Spanish report."""
    client = default_lang_client
    client.post("/login", data=LOGIN)
    exported = client.get("/admin/stores/solares/products/export.csv").content
    header, *rows = list(csv.reader(io.StringIO(exported.decode("utf-8-sig"))))
    assert header[-1] == es("admin.inventory.csv.stock_on_hand")
    stock = _column(header, "stock_on_hand")
    for row in rows:
        if row[1] == "SOL-20T-ALTA":
            row[stock] = "8"
    response = _upload(client, _csv([header, *rows]), name="inventario.csv")
    assert es("admin.inventory.result.summary", applied=1, unchanged=4, refused=0) in page_text(response)
    assert _stock(db_session)["SOL-20T-ALTA"] == 8


def test_admin_force_box(admin_client, solares, db_session):
    raw = _edited_export(db_session, {"SOL-8T-ALTA": 10})
    eight = db_session.scalar(select(Product).where(Product.sku == "SOL-8T-ALTA"))
    stock_svc.adjust_stock(db_session, eight, 1, "Received")
    refused = _upload(admin_client, raw)
    assert en("admin.inventory.row.stock_changed", current=2, exported=1) in page_text(refused)
    forced = _upload(admin_client, raw, force="1")
    assert 'name="force" value="1" checked' in forced.text
    assert _stock(db_session)["SOL-8T-ALTA"] == 10


@pytest.mark.parametrize("raw,status,key,params", [
    (b"PK\x03\x04\x00", 400, "admin.inventory.error.not_csv", {}),
    (b"sku,qty\r\n", 400, "admin.inventory.error.no_rows", {}),
])
def test_admin_unusable_file(admin_client, solares, db_session, raw, status, key, params):
    response = _upload(admin_client, raw)
    assert response.status_code == status
    assert f'role="alert">{en_html(key, **params)}</p>' in response.text
    assert _stock(db_session) == {sku: 1 for sku in SKUS}


def test_admin_without_file_or_too_large(admin_client, solares, monkeypatch):
    none = admin_client.post(f"{PAGE}/import", data={})
    assert none.status_code == 400 and en("admin.inventory.error.no_file") in page_text(none)
    monkeypatch.setattr(inventory_import, "MAX_BYTES", 10)
    big = _upload(admin_client, b"sku,qty\r\nSOL-8T-ALTA,5\r\n")
    assert big.status_code == 413
    assert en("admin.inventory.error.too_large", max_mb=inventory_import.MAX_MB) in page_text(big)


def test_admin_import_needs_an_admin_and_a_store(client, solares):
    response = _upload(client, b"sku,qty\r\nSOL-8T-ALTA,5\r\n")
    assert response.status_code == 200 and "/login" in str(response.url)  # sent to sign in
    client.post("/login", data=LOGIN)
    assert client.get("/admin/stores/nope/inventory").status_code == 404
    assert _upload(client, b"sku,qty\r\nA,5\r\n", url="/admin/stores/nope/inventory/import").status_code == 404


def test_page_of_a_store_without_products(admin_client, demo_store):
    page = page_text(admin_client.get("/admin/stores/demo-widgets/inventory"))
    assert en("admin.inventory.error.no_products") in page


# ============================================================ command line


@pytest.fixture()
def cli_db(engine, monkeypatch):
    monkeypatch.setattr("app.db.SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr("app.db.init_db", lambda: None)


def test_cli_applies_and_exits_0(solares, cli_db, tmp_path, capsys, db_session):
    file = tmp_path / "conteo.csv"
    file.write_bytes(_csv([["sku", "qty"], ["SOL-8T-ALTA", "5"], ["GAS-BOIL-40L", "1"]]))
    assert inventory_import.main(["solares", str(file)]) == 0
    out = capsys.readouterr().out
    assert "solares: 1 changed, 1 unchanged, 0 refused" in out and "line 2: SOL-8T-ALTA: stock 1 -> 5" in out
    assert _stock(db_session)["SOL-8T-ALTA"] == 5
    movement = db_session.scalar(select(StockMovement).where(StockMovement.kind == StockMovementKind.inventory_import))
    assert movement.reason == "conteo.csv" and movement.user_id is None


def test_cli_partial_exits_3_with_the_refused_rows(solares, cli_db, tmp_path, capsys, db_session):
    file = tmp_path / "conteo.csv"
    file.write_bytes(_csv([["sku", "qty"], ["SOL-8T-ALTA", "5"], ["NOPE-1", "1"], ["SOL-8T-ALTA", "6"]]))
    assert inventory_import.main(["solares", str(file)]) == 3
    captured = capsys.readouterr()
    assert "solares: 0 changed, 0 unchanged, 3 refused" in captured.out
    assert "line 3: refused: " + en("admin.inventory.row.unknown_sku", sku="NOPE-1", store="solares") in captured.err
    assert en("admin.inventory.row.duplicate_sku", sku="SOL-8T-ALTA", lines="2, 4") in captured.err


def test_cli_force(solares, cli_db, tmp_path, db_session):
    file = tmp_path / "conteo.csv"
    file.write_bytes(_csv([["sku", "stock_at_export", "qty"], ["SOL-8T-ALTA", "7", "5"]]))  # exported "7": changed
    assert inventory_import.main(["solares", str(file)]) == 3
    assert inventory_import.main(["solares", str(file), "--force"]) == 0
    assert _stock(db_session)["SOL-8T-ALTA"] == 5


@pytest.mark.parametrize("store,content,message", [
    ("nope", b"sku,qty\r\nA,1\r\n", "error: " + 'No store with slug "nope".'),
    ("solares", b"", "error: The file is empty."),
])
def test_cli_errors_exit_1(solares, cli_db, tmp_path, capsys, store, content, message):
    file = tmp_path / "x.csv"
    file.write_bytes(content)
    assert inventory_import.main([store, str(file)]) == 1
    assert capsys.readouterr().err.strip() == message


def test_cli_unreadable_file(cli_db, tmp_path, capsys):
    assert inventory_import.main(["solares", str(tmp_path / "missing.csv")]) == 1
    assert "error: can't read" in capsys.readouterr().err


def test_documented_commands_round_trip_as_processes(tmp_path):
    """README: export, edit, import, as real processes against a database file."""
    db_file = tmp_path / "inventory.db"
    engine = create_engine(f"sqlite:///{db_file}")
    init_db(bind=engine)
    with sessionmaker(bind=engine)() as db:
        import_store_pack(db, SOLARES)
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file}", "SEED_PACKS": ""}

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", *args], cwd=tmp_path, env={**env, "PYTHONPATH": str(PROJECT)},
                              capture_output=True, text=True, timeout=60)

    assert run("app.services.inventory_export", "solares", "-o", "solares-inventory.csv").returncode == 0
    exported = (tmp_path / "solares-inventory.csv").read_bytes().decode("utf-8-sig")
    edited = exported.replace('"Calentador solar 8 tubos, alta presión",6499.00,1,6499.00,1',
                              '"Calentador solar 8 tubos, alta presión",6499.00,1,6499.00,4')
    assert edited != exported
    (tmp_path / "solares-inventory.csv").write_bytes(edited.encode("utf-8-sig"))
    done = run("app.services.inventory_import", "solares", "solares-inventory.csv")
    assert done.returncode == 0, done.stderr
    assert "solares: 1 changed, 4 unchanged, 0 refused" in done.stdout
    with sessionmaker(bind=engine)() as db:
        assert db.scalar(select(Product.stock_on_hand).where(Product.sku == "SOL-8T-ALTA")) == 4
    assert "python -m app.services.inventory_import solares solares-inventory.csv" in README


# ============================================================ prices


def _price(db_session, sku: str) -> int:
    db_session.expire_all()
    return db_session.scalar(select(Product.price_cents).where(Product.sku == sku))


def test_round_trip_sets_an_edited_price_and_nothing_else(solares, db_session):
    before = _snapshot(db_session)
    raw = _edited_export(db_session, {}, new_price={"SOL-8T-ALTA": "6999.50"})
    report = import_inventory(db_session, "solares", raw)
    assert (report.count("applied"), report.count("unchanged")) == (1, 4)
    (row,) = report.applied
    assert (row.price_before, row.price_after, row.stock_changed) == (649900, 699950, False)
    assert _price(db_session, "SOL-8T-ALTA") == 699950
    products, values = _snapshot(db_session)
    assert values == before[1] and [p[:5] for p in products] == [p[:5] for p in before[0]]  # name, description...
    assert db_session.scalar(select(func.count()).select_from(StockMovement)
                             .where(StockMovement.kind == StockMovementKind.inventory_import)) == 0  # no stock change


def test_a_row_sets_stock_and_price_together(solares, db_session):
    raw = _csv([["sku", "price", "qty"], ["GAS-BOIL-40L", "4999", "6"], ["SOL-20T-ALTA", "17000.5", ""]])
    report = import_inventory(db_session, "solares", raw)
    assert report.count("applied") == 2
    assert (_price(db_session, "GAS-BOIL-40L"), _stock(db_session)["GAS-BOIL-40L"]) == (499900, 6)
    assert (_price(db_session, "SOL-20T-ALTA"), _stock(db_session)["SOL-20T-ALTA"]) == (1700050, 1)  # empty: kept


@pytest.mark.parametrize("value,code", [
    ("6499,00", "price_comma"), ("6,499.00", "price_comma"), ("-5", "price_negative"), ("-5.50", "price_negative"),
    ("6499.999", "price_format"), ("abc", "price_format"), ("1e3", "price_format"), ("$6499", "price_format"),
    (" 64 99", "price_format"), ("9" * 13, "price_format"), ("999999999999.99", "price_too_large"),
])
def test_bad_prices_refuse_the_whole_row(solares, db_session, value, code):
    report = import_inventory(db_session, "solares", _csv([["sku", "price", "qty"], ["SOL-8T-ALTA", value, "3"]]))
    assert report.refused[0].error[0] == code
    assert (_price(db_session, "SOL-8T-ALTA"), _stock(db_session)["SOL-8T-ALTA"]) == (649900, 1)  # stock not set either


def test_a_price_edited_after_a_repricing_is_refused(solares, db_session):
    raw = _edited_export(db_session, {"SOL-12T-BAJA": 4}, new_price={"SOL-8T-ALTA": "7000.00", "SOL-12T-BAJA": "7500"})
    for sku in ("SOL-8T-ALTA", "SOL-16T-ALTA", "SOL-12T-BAJA"):  # repriced in admin after the export
        product = db_session.scalar(select(Product).where(Product.sku == sku))
        product.price_cents += 100
    db_session.commit()
    report = import_inventory(db_session, "solares", raw)
    assert _codes(report) == {
        "SOL-8T-ALTA": ("price_changed", {"current": "6500.00", "exported": "6499.00"}),
        "SOL-12T-BAJA": ("price_changed", {"current": "7300.00", "exported": "7299.00"}),
    }
    assert _price(db_session, "SOL-16T-ALTA") == 1350000  # not edited in the file: the new price is kept
    assert _stock(db_session)["SOL-12T-BAJA"] == 1  # the refused row's stock isn't applied either
    forced = import_inventory(db_session, "solares", raw, force=True)
    assert forced.count("refused") == 0
    assert (_price(db_session, "SOL-8T-ALTA"), _price(db_session, "SOL-12T-BAJA")) == (700000, 750000)


def test_a_refused_stock_doesnt_write_the_rows_price(solares, db_session):
    raw = _edited_export(db_session, {"GAS-BOIL-40L": 9}, new_price={"GAS-BOIL-40L": "1.00"})
    stock_svc.adjust_stock(db_session, db_session.scalar(select(Product).where(Product.sku == "GAS-BOIL-40L")), 2,
                           "Received")
    report = import_inventory(db_session, "solares", raw)
    assert report.refused[0].error[0] == "stock_changed"
    assert _price(db_session, "GAS-BOIL-40L") == 519900


def test_an_edited_price_at_export_cell_is_refused(solares, db_session):
    raw = _csv([["sku", "price_at_export", "price"], ["SOL-8T-ALTA", "seis mil", "7000"]])
    assert import_inventory(db_session, "solares", raw).refused[0].error == ("exported_price_format",
                                                                              {"value": "seis mil"})


def test_a_file_needs_a_stock_or_a_price_column(solares, db_session):
    with pytest.raises(InventoryImportError) as refused:
        import_inventory(db_session, "solares", _csv([["sku", "name"], ["SOL-8T-ALTA", "x"]]))
    assert refused.value.error == ("missing_columns", {"columns": ["stock_on_hand", "price"], "either": True})
    either = f'"{es("admin.inventory.csv.stock_on_hand")}" / "{es("admin.inventory.csv.price")}"'
    assert inventory_import.error_text("es", refused.value.error) == es("admin.inventory.error.missing_columns",
                                                                        columns=either)


def test_admin_report_and_cli_show_price_changes(admin_client, solares, db_session, cli_db, tmp_path, capsys):
    response = _upload(admin_client, _csv([["sku", "price"], ["SOL-8T-ALTA", "7000"]]))
    assert "MXN 6,499.00 → MXN 7,000.00" in page_text(response)
    file = tmp_path / "p.csv"
    file.write_bytes(_csv([["sku", "price", "qty"], ["SOL-8T-ALTA", "7100.00", "2"]]))
    assert inventory_import.main(["solares", str(file)]) == 0
    assert "line 2: SOL-8T-ALTA: stock 1 -> 2, price 7000.00 -> 7100.00" in capsys.readouterr().out


def test_setting_the_price_it_already_has_is_unchanged(solares, db_session):
    report = import_inventory(db_session, "solares", _csv([["sku", "price"], ["SOL-8T-ALTA", "6499"]]))
    assert report.count("unchanged") == 1 and report.rows[0].price_changed is False
