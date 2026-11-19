"""Inventory import: push stock numbers and prices back from a CSV (the inventory export, edited
offline, or a hand-made ``sku,qty`` / ``sku,price`` file). It only sets stock on hand and price: name,
description, properties and every other field of the products are never touched.

Rules (one store per import):

- Each row finds its product **by SKU** in that store (ignoring case and spaces) and sets its stock
  on hand and / or its price to the row's values (an empty cell leaves that value as it is; a row
  with neither is refused). Every stock change is logged as an ``inventory_import`` stock movement
  (reason: the file name; the admin, for uploads); price changes are listed in the report.
- A row is applied whole or not at all: a refused row writes neither its stock nor its price.
- **Partial apply**: rows that can be applied are; the others are refused and reported with their
  line and the reason, and change nothing. Refused: unknown SKU, a SKU that appears more than once
  in the file (every occurrence), a row without SKU, a negative / missing / non-whole stock, a
  row of another store (``store`` column), a bad price (negative, more than 2 decimals, a comma:
  "6499,00" is ambiguous, write "6499.00"), and, when the file has ``stock_at_export`` /
  ``price_at_export`` (the export does), a row whose value was edited while the product's stock /
  price changed since the export (an import never silently undoes a sale or a repricing; ``force``
  applies it anyway). A value that wasn't edited (equal to its "at export" column) changes nothing,
  whatever happened since.
- The whole file is refused (nothing applied) when it can't be read as CSV, is too large, or lacks
  the SKU column or both value columns (stock and price).

The header row may be in any UI language (``Existencias`` / ``Stock on hand``, see
``inventory_export.known_headers``) or use the column ids (``sku``, ``stock_on_hand``, or ``qty``).
Comma or semicolon separated (spreadsheets in Spanish save with ``;``); UTF-8 (with or without BOM),
else read as Windows-1252 (only the ASCII SKU and number columns are used). Extra columns are ignored.

    python -m app.services.inventory_import solares solares-inventory.csv [--force]
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.i18n import translator
from app.models import Product, StockMovementKind, Store, User
from app.services import catalog
from app.services import stock as stock_svc
from app.services.inventory_export import column_labels, header_key, known_headers
from app.services.inventory_export import price_text
from app.services.product_admin import PRICE_MAX_CENTS, normalize_sku, product_by_sku
from app.services.stock import STOCK_MAX, STOCK_MAX_LEN, STOCK_RE
from app.services.store_admin import FieldError

MAX_MB = 5
MAX_BYTES = MAX_MB * 1024 * 1024
MAX_ROWS = 50_000
VALUE_COLUMNS = ("stock_on_hand", "price")  # a file needs at least one of them
PRICE_RE = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,2})?$")
DELIMITERS = (",", ";", "\t")
APPLIED, UNCHANGED, REFUSED = "applied", "unchanged", "refused"


class InventoryImportError(Exception):
    """The whole file (or the store) is refused: nothing was applied. ``error`` = (code, params)."""

    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.error: FieldError = (code, params)


@dataclass
class RowResult:
    line: int  # line of the file (the header is line 1)
    sku: str  # as written in the file
    status: str  # APPLIED, UNCHANGED or REFUSED
    before: Optional[int] = None  # stock
    after: Optional[int] = None
    price_before: Optional[int] = None  # cents; set when the row changed the price
    price_after: Optional[int] = None
    error: Optional[FieldError] = None  # why it was refused

    @property
    def price_changed(self) -> bool:
        return self.price_after is not None and self.price_after != self.price_before

    @property
    def stock_changed(self) -> bool:
        return self.before is not None and self.after != self.before


@dataclass
class InventoryImportReport:
    store: Store
    filename: str
    rows: list[RowResult] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for row in self.rows if row.status == status)

    @property
    def applied(self) -> list[RowResult]:
        return [row for row in self.rows if row.status == APPLIED]

    @property
    def refused(self) -> list[RowResult]:
        return [row for row in self.rows if row.status == REFUSED]


# ------------------------------------------------------------------------------ reading


def decode(raw: bytes) -> str:
    if b"\x00" in raw:
        raise InventoryImportError("not_csv")  # a binary file (xlsx, zip, image...)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")  # a spreadsheet's "CSV": only ASCII columns matter


def read_table(raw: bytes) -> tuple[dict[str, int], list[tuple[int, list[str]]]]:
    """({column id: index}, [(line, cells)]) of the data rows. Raises InventoryImportError."""
    if not raw.strip():
        raise InventoryImportError("empty_file")
    if len(raw) > MAX_BYTES:
        raise InventoryImportError("too_large", max_mb=MAX_MB)
    text = decode(raw)
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = max(DELIMITERS, key=first_line.count)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    known = known_headers()
    columns: dict[str, int] = {}
    rows: list[tuple[int, list[str]]] = []
    try:
        header = next(reader)
        for index, cell in enumerate(header):
            column = known.get(header_key(cell))
            if column is None:
                continue  # an extra column: ignored
            if column in columns:
                raise InventoryImportError("duplicate_column", column=column)
            columns[column] = index
        if "sku" not in columns:
            raise InventoryImportError("missing_columns", columns=["sku"])
        if not any(column in columns for column in VALUE_COLUMNS):
            raise InventoryImportError("missing_columns", columns=list(VALUE_COLUMNS), either=True)
        for cells in reader:
            if not any(cell.strip() for cell in cells):
                continue  # blank line
            rows.append((reader.line_num, cells))
            if len(rows) > MAX_ROWS:
                raise InventoryImportError("too_many_rows", max=MAX_ROWS)
    except csv.Error as exc:  # e.g. a field over the csv module's size limit
        raise InventoryImportError("not_csv") from exc
    if not rows:
        raise InventoryImportError("no_rows")
    return columns, rows


def _cell(cells: list[str], columns: dict[str, int], column: str) -> Optional[str]:
    """The row's value for a column (trimmed); None if the file has no such column."""
    if column not in columns:
        return None
    index = columns[column]
    return cells[index].strip() if index < len(cells) else ""


def _whole_number(raw: str) -> Optional[int]:
    if not STOCK_RE.match(raw) or len(raw.lstrip("-")) > STOCK_MAX_LEN:
        return None
    return int(raw)


def _parse_stock(raw: str) -> tuple[Optional[int], Optional[FieldError]]:
    units = _whole_number(raw)
    if units is None:
        return None, ("stock_format", {"value": raw})
    if units < 0:
        return None, ("stock_negative", {"value": units})
    if units > STOCK_MAX:
        return None, ("stock_too_large", {})
    return units, None


def _price_cents(raw: str) -> Optional[int]:
    """ "6499.00" / "6499.5" / "6499" -> cents; None if not that shape (no sign, no comma)."""
    if not PRICE_RE.match(raw):
        return None
    units, _, decimals = raw.partition(".")
    return int(units) * 100 + int((decimals + "00")[:2])


def _parse_price(raw: str, exported: bool = False) -> tuple[Optional[int], Optional[FieldError]]:
    """A price cell -> cents. Commas are refused, not guessed ("6499,00" or "6,499.00")."""
    cents = _price_cents(raw)
    if cents is not None:
        if cents > PRICE_MAX_CENTS:
            return None, ("price_too_large", {})
        return cents, None
    if exported:
        return None, ("exported_price_format", {"value": raw})
    if raw.startswith("-") and _price_cents(raw[1:]) is not None:
        return None, ("price_negative", {"value": raw})
    if "," in raw:
        return None, ("price_comma", {"value": raw})
    return None, ("price_format", {"value": raw})


# ------------------------------------------------------------------------------ import


def import_inventory(
    db: Session,
    store_slug: str,
    raw: bytes,
    *,
    filename: str = "inventory.csv",
    user: Optional[User] = None,
    force: bool = False,
) -> InventoryImportReport:
    """Applies the file's stock numbers to the store's products (committed). Raises
    :class:`InventoryImportError` (nothing applied) for an unknown store or an unreadable file."""
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise InventoryImportError("store_not_found", store=store_slug)
    columns, rows = read_table(raw)
    name = os.path.basename(filename or "")[:200] or "inventory.csv"
    report = InventoryImportReport(store=store, filename=name)

    skus = [normalize_sku(_cell(cells, columns, "sku") or "") for _, cells in rows]
    stores = [_cell(cells, columns, "store") for _, cells in rows]
    ours = [not row_store or row_store == store.slug for row_store in stores]
    # Duplicates among this store's rows (a row of another store is refused for that reason alone).
    lines_of: dict[str, list[int]] = {}
    for (line, _), sku, mine in zip(rows, skus, ours):
        if mine:
            lines_of.setdefault(sku, []).append(line)
    repeated = {sku for sku, n in Counter(s for s, mine in zip(skus, ours) if mine).items() if sku and n > 1}

    for (line, cells), sku, row_store, mine in zip(rows, skus, stores, ours):
        written = _cell(cells, columns, "sku") or ""
        result = RowResult(line=line, sku=written, status=REFUSED)
        report.rows.append(result)
        if not mine:
            result.error = ("other_store", {"store": row_store})
            continue
        if not sku:
            result.error = ("no_sku", {})
            continue
        if sku in repeated:
            result.error = ("duplicate_sku", {"sku": sku, "lines": ", ".join(map(str, lines_of[sku]))})
            continue
        product = product_by_sku(db, store, sku)
        error = _apply_row(db, product, cells, columns, result, force=force, reason=name, user=user)
        if error:
            result.error = error
            result.before = result.after = result.price_before = result.price_after = None
            if error[0] == "unknown_sku":
                result.error = ("unknown_sku", {"sku": sku, "store": store.slug})
    db.commit()
    return report


def _apply_row(
    db: Session,
    product: Optional[Product],
    cells: list[str],
    columns: dict[str, int],
    result: RowResult,
    *,
    force: bool,
    reason: str,
    user: Optional[User],
) -> Optional[FieldError]:
    """Checks and applies one row (its stock and / or price); fills ``result``. Returns why it was
    refused (then nothing of the row was written)."""
    stock_raw = _cell(cells, columns, "stock_on_hand") or ""
    price_raw = _cell(cells, columns, "price") or ""
    if not stock_raw and not price_raw:
        return ("no_values", {})
    units = price = stock_seen = price_seen = None
    if stock_raw:
        units, error = _parse_stock(stock_raw)
        if error:
            return error
        exported_raw = _cell(cells, columns, "stock_at_export")
        if exported_raw:
            stock_seen = _whole_number(exported_raw)
            if stock_seen is None or stock_seen < 0:
                return ("exported_format", {"value": exported_raw})
    if price_raw:
        price, error = _parse_price(price_raw)
        if error:
            return error
        exported_raw = _cell(cells, columns, "price_at_export")
        if exported_raw:
            price_seen, error = _parse_price(exported_raw, exported=True)
            if error:
                return error
    if product is None:
        return ("unknown_sku", {})
    if force:
        stock_seen = price_seen = None

    # The price is checked first (no write yet): a refused row writes nothing.
    write_price = False
    if price is not None:
        base = price_seen if price_seen is not None else product.price_cents
        if price != base:
            if product.price_cents != base:  # edited here, and repriced since the export
                return ("price_changed", {"current": price_text(product.price_cents), "exported": price_text(base)})
            write_price = True
        result.price_before = result.price_after = product.price_cents

    result.before = result.after = product.stock_on_hand
    if units is not None:
        try:
            movement = stock_svc.set_stock(db, product, units, stock_seen, StockMovementKind.inventory_import,
                                           reason=reason, user=user)
        except stock_svc.StockError as exc:  # changed since the export, and this row's number was edited
            return ("stock_changed", {"current": exc.error[1]["current"], "exported": stock_seen})
        if movement is not None:
            result.before, result.after = movement.quantity_after - movement.delta, movement.quantity_after
    else:
        result.before = result.after = None  # the row doesn't set the stock
    if write_price:
        # The stock UPDATE (if any) locked this product's row; otherwise the condition guards it.
        written = db.execute(
            update(Product)
            .where(Product.id == product.id, Product.price_cents == product.price_cents)
            .values(price_cents=price)
            .execution_options(synchronize_session=False)
        )
        if written.rowcount == 1:
            result.price_after = price
    result.status = APPLIED if (result.stock_changed or result.price_changed) else UNCHANGED
    return None


# ------------------------------------------------------------------------------ messages


def error_text(locale: str, error: FieldError) -> str:
    """A file or row error in ``locale`` (literal keys: tests/test_i18n.py finds them)."""
    t = translator(locale)
    code, params = error
    if code == "missing_columns":  # named with the headers of that language
        labels = column_labels(locale)
        joiner = " / " if params.get("either") else ", "
        params = {"columns": joiner.join(f'"{labels[c]}"' for c in params["columns"])}
    messages: dict[str, Callable[[], str]] = {
        "store_not_found": lambda: t("admin.inventory.error.store_not_found", **params),
        "empty_file": lambda: t("admin.inventory.error.empty_file"),
        "too_large": lambda: t("admin.inventory.error.too_large", **params),
        "not_csv": lambda: t("admin.inventory.error.not_csv"),
        "duplicate_column": lambda: t("admin.inventory.error.duplicate_column", **params),
        "missing_columns": lambda: t("admin.inventory.error.missing_columns", **params),
        "too_many_rows": lambda: t("admin.inventory.error.too_many_rows", **params),
        "no_rows": lambda: t("admin.inventory.error.no_rows"),
        "other_store": lambda: t("admin.inventory.row.other_store", **params),
        "no_sku": lambda: t("admin.inventory.row.no_sku"),
        "duplicate_sku": lambda: t("admin.inventory.row.duplicate_sku", **params),
        "unknown_sku": lambda: t("admin.inventory.row.unknown_sku", **params),
        "no_values": lambda: t("admin.inventory.row.no_values"),
        "price_format": lambda: t("admin.inventory.row.price_format", **params),
        "price_comma": lambda: t("admin.inventory.row.price_comma", **params),
        "price_negative": lambda: t("admin.inventory.row.price_negative", **params),
        "price_too_large": lambda: t("admin.inventory.row.price_too_large"),
        "exported_price_format": lambda: t("admin.inventory.row.exported_price_format", **params),
        "price_changed": lambda: t("admin.inventory.row.price_changed", **params),
        "stock_format": lambda: t("admin.inventory.row.stock_format", **params),
        "stock_negative": lambda: t("admin.inventory.row.stock_negative", **params),
        "stock_too_large": lambda: t("admin.inventory.row.stock_too_large"),
        "exported_format": lambda: t("admin.inventory.row.exported_format", **params),
        "stock_changed": lambda: t("admin.inventory.row.stock_changed", **params),
    }
    return messages[code]()


# ------------------------------------------------------------------------------------ CLI

EXIT_OK, EXIT_ERROR, EXIT_PARTIAL = 0, 1, 3


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.inventory_import",
        description="Set stock on hand and / or prices from a CSV (SKU + stock / price; e.g. an edited inventory "
                    "export). Nothing else of the products changes. Exit status: 0 every row applied or unchanged, 3 some rows refused (the others "
                    "applied), 1 nothing applied (unknown store, unreadable file). Uses DATABASE_URL.",
    )
    parser.add_argument("store", help="store slug, e.g. solares")
    parser.add_argument("file", help="CSV file")
    parser.add_argument("--force", action="store_true",
                        help="apply rows even if the product's stock or price changed since the export")
    args = parser.parse_args(argv)
    locale = "en"  # command-line output is English, like the pack commands

    try:
        with open(args.file, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError as exc:
        print(f"error: can't read {args.file}: {exc.strerror}", file=sys.stderr)
        return EXIT_ERROR

    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as db:
        try:
            report = import_inventory(db, args.store, raw, filename=args.file, force=args.force)
        except InventoryImportError as exc:
            print(f"error: {error_text(locale, exc.error)}", file=sys.stderr)
            return EXIT_ERROR
        print(f"{report.store.slug}: {report.count(APPLIED)} changed, {report.count(UNCHANGED)} unchanged, "
              f"{report.count(REFUSED)} refused")
        for row in report.applied:
            changes = []
            if row.stock_changed:
                changes.append(f"stock {row.before} -> {row.after}")
            if row.price_changed:
                changes.append(f"price {price_text(row.price_before)} -> {price_text(row.price_after)}")
            print(f"  line {row.line}: {row.sku}: {', '.join(changes)}")
        for row in report.refused:
            print(f"  line {row.line}: refused: {error_text(locale, row.error)}", file=sys.stderr)
    return EXIT_PARTIAL if report.refused else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
