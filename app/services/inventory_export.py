"""Inventory export: a store's products with their SKU and stock on hand, as CSV, to edit the
numbers offline (a spreadsheet) and push them back with the inventory import
(app/services/inventory_import.py). Only the stock numbers: this is not a store exporter (store
packs are the catalog format).

Columns, one row per product (active and hidden), products with a SKU first (by SKU), then the
ones without (by slug). The header row is in the export's language (``admin.inventory.csv.<id>``
in app/locales/, e.g. ``Existencias`` / ``Stock on hand``); the import reads any language's
headers and the column ids below:

    store            the store's slug: the import refuses rows of another store
    sku              the product's SKU: what the import matches the row's product by (empty: a
                     product without SKU, which the import can't update; give it one in admin)
    slug             the product's slug, for people (not read back)
    name             for people only (not read back); a leading = + - @ is neutralised with ' so a
                     spreadsheet never runs it as a formula
    price_at_export  the price when the file was made (store currency, "6499.00"): don't edit
    stock_at_export  the stock when the file was made: don't edit. These two let the import tell an
                     offline edit from a change made since the export, instead of silently overwriting it
    price            a price to edit (store currency, dot for decimals)
    stock_on_hand    a number to edit

The file is UTF-8 with a BOM (so spreadsheets show accents right) and CRLF line ends (RFC 4180).

    python -m app.services.inventory_export solares -o solares-inventory.csv      # in the store's language
    python -m app.services.inventory_export solares --lang en                     # to standard output (no BOM)
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.i18n import available_locales, normalize_locale, translate
from app.models import Product, Store, utc_now
from app.services import catalog

COLUMNS = ("store", "sku", "slug", "name", "price_at_export", "stock_at_export", "price", "stock_on_hand")
CSV_ENCODING = "utf-8-sig"  # UTF-8 with a BOM: spreadsheets (Excel) read the accents right
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def column_labels(locale: str) -> dict[str, str]:
    """The header of each column in ``locale`` (literal keys: tests/test_i18n.py finds them)."""
    return {
        "store": translate(locale, "admin.inventory.csv.store"),
        "sku": translate(locale, "admin.inventory.csv.sku"),
        "slug": translate(locale, "admin.inventory.csv.slug"),
        "name": translate(locale, "admin.inventory.csv.name"),
        "price_at_export": translate(locale, "admin.inventory.csv.price_at_export"),
        "stock_at_export": translate(locale, "admin.inventory.csv.stock_at_export"),
        "price": translate(locale, "admin.inventory.csv.price"),
        "stock_on_hand": translate(locale, "admin.inventory.csv.stock_on_hand"),
    }


# Extra names the import accepts for the stock column (a hand-made "sku,qty" file).
STOCK_ALIASES = ("qty", "quantity", "cantidad")


def header_key(text: str) -> str:
    """How header cells are compared: trimmed, spaces collapsed, case-insensitive."""
    return " ".join(text.split()).casefold()


def known_headers() -> dict[str, str]:
    """{header as compared: column id} for every language's headers, the ids and the aliases."""
    known = {header_key(column): column for column in COLUMNS}
    for locale in available_locales():
        for column, label in column_labels(locale).items():
            known[header_key(label)] = column
    for alias in STOCK_ALIASES:
        known[header_key(alias)] = "stock_on_hand"
    return known


class InventoryExportError(Exception):
    """Nothing to export. ``code``: ``store_not_found`` or ``no_products``; ``params`` for messages."""

    def __init__(self, code: str, **params: Any) -> None:
        self.code, self.params = code, params
        messages = {
            "store_not_found": f"no store with slug {params.get('store')!r}",
            "no_products": f"store {params.get('store')!r} has no products to export",
        }
        super().__init__(messages[code])


@dataclass
class InventoryExport:
    store: Store
    rows: list[dict[str, Any]]
    exported_at: datetime
    locale: str  # language of the header row

    @property
    def filename(self) -> str:
        """e.g. ``inventory-solares-20251117-093000.csv`` (UTC)."""
        return f"inventory-{self.store.slug}-{self.exported_at:%Y%m%d-%H%M%S}.csv"

    def csv_text(self) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=COLUMNS, lineterminator="\r\n")
        writer.writerow(column_labels(self.locale))  # the header, in the export's language
        writer.writerows(self.rows)
        return buffer.getvalue()

    def csv_bytes(self) -> bytes:
        """The downloadable file (with the BOM)."""
        return self.csv_text().encode(CSV_ENCODING)


def price_text(cents: int) -> str:
    """A price as the file writes it: store currency units, dot for decimals, e.g. "6499.00"."""
    return f"{cents // 100}.{cents % 100:02d}"


def safe_cell(text: str) -> str:
    """Text a spreadsheet shows as text: a leading =, +, -, @ (a formula) gets a ' in front."""
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def inventory_rows(db: Session, store: Store) -> list[dict[str, Any]]:
    """One row per product of the store (read in one query: a consistent snapshot)."""
    products = db.scalars(
        select(Product)
        .where(Product.store_id == store.id)
        .order_by(Product.sku.is_(None), Product.sku, Product.slug)
    ).all()
    return [
        {
            "store": store.slug,
            "sku": product.sku or "",
            "slug": product.slug,
            "name": safe_cell(product.name),
            "price_at_export": price_text(product.price_cents),
            "stock_at_export": product.stock_on_hand,
            "price": price_text(product.price_cents),
            "stock_on_hand": product.stock_on_hand,
        }
        for product in products
    ]


def export_inventory(db: Session, store_slug: str, locale: Optional[str] = None) -> InventoryExport:
    """The store's inventory, headers in ``locale`` (default: the store's language). Raises
    :class:`InventoryExportError` for an unknown store or a store without products (nothing useful
    to edit)."""
    store = catalog.get_store_by_slug(db, store_slug)
    if store is None:
        raise InventoryExportError("store_not_found", store=store_slug)
    rows = inventory_rows(db, store)
    if not rows:
        raise InventoryExportError("no_products", store=store.slug)
    language = normalize_locale(locale) or normalize_locale(store.locale_default) or available_locales()[0]
    return InventoryExport(store=store, rows=rows, exported_at=utc_now(), locale=language)


# ------------------------------------------------------------------------------------ CLI


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.inventory_export",
        description="Export a store's products with their SKU and stock on hand as CSV (uses DATABASE_URL).",
    )
    parser.add_argument("store", help="store slug, e.g. solares")
    parser.add_argument("-o", "--output", help="CSV file to write (UTF-8 with BOM); default: standard output")
    parser.add_argument("--lang", choices=available_locales(),
                        help="language of the header row; default: the store's language")
    args = parser.parse_args(argv)

    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as db:
        try:
            export = export_inventory(db, args.store, args.lang)
        except InventoryExportError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.output:
            with open(args.output, "wb") as handle:
                handle.write(export.csv_bytes())
            print(f"{len(export.rows)} product(s) of {export.store.slug} written to {args.output}", file=sys.stderr)
        else:
            sys.stdout.write(export.csv_text())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
