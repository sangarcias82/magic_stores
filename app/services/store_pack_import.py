"""Import a store pack (contract v1) into the database, using only the engine tables:
stores, categories, property_definitions, products, property_values, translations, shipping_methods.

Re-import behavior (upsert by natural keys; nothing is deleted):

- Rows are matched by their natural keys: store ``slug``; category ``slug`` within the store;
  property definition ``key`` within its category; product ``slug`` within the store; shipping
  method ``code`` within the store.
  A match is updated in place (ids, URLs and category references stay the same); anything
  else is created.
- The pack is authoritative for what it names: every field of a matched row is overwritten,
  and optional fields the pack omits get their defaults (e.g. no ``theme`` = default theme),
  so importing the same pack always gives the same result.
- A product's property values are replaced as a whole: a value that is null or missing in
  the pack is removed from the product.
- Rows the pack does not name (e.g. a product created in admin) are never changed or deleted.
- A property's ``prop_type`` can't change while products *outside the pack* have values for
  it (same rule as the admin form); that is reported as a conflict before anything is written.
- Product ``sku`` (optional): set when given, kept when the pack omits it (so re-importing a pack
  without SKUs keeps the ones entered in admin). A SKU already used by a product the pack doesn't
  give another SKU is a conflict.
- ``payment_terminals`` (optional): upserted by ``code`` (name, enabled, order); terminals the pack
  doesn't name are kept. A terminal's ``config`` (a bank transfer's deposit instructions) is only
  written when the terminal has none: a re-import never replaces bank details entered in admin.
  Its ``i18n`` names replace the terminal's name translations; its ``i18n`` notes (the note's
  translations) go with the config: written only when the config is.
- Product ``stock`` (optional): the pack wins, like its other fields. Every import sets the stock to
  the pack's number (a product without ``stock`` in the pack keeps its stock). Each change is logged
  as a ``pack_import`` stock movement, in the import's own transaction (all or nothing).
- Translations (level 2, ``translations`` table): the ``i18n`` blocks of every row the pack
  names (shipping methods included) replace that row's translations (no ``i18n`` = its translations are removed), and
  ``ui_translations`` replaces the store's UI overrides.

The pack is validated first (:func:`app.services.store_pack_validation.check_store_pack`) and the import
runs in one transaction: on any error nothing is written.

Storefront folder (``storefront.copy_from_pack_dir``): when the pack is imported from its folder
(``import_store_pack_file``, the command line, or ``pack_dir=``), that folder is copied to
``app/storefronts/<store-slug>/``, replacing an earlier copy (see ``storefront_install``). It is
copied to a staging folder before the commit and put in place only after it, so a failed import
installs nothing. A missing or unsafe folder stops the import (conflict) before anything is
written. ``null`` / no ``storefront``: the store folder is left alone (no folder = ``_default``).
Without a pack folder (an uploaded file, ``import_store_pack(db, data)``) nothing can be copied;
the report lists it as skipped.

Images (``media/`` next to the pack file, imported from its folder): published to the content
app's folder ``CONTENT_ROOT/<store-slug>/`` (see ``media_publish``), staged and published around
the commit like the storefront folder. Re-import merges (other files there are kept). The pack
refers to them as ``/content/<store-slug>/<file>``; templates resolve that through CONTENT_BASE_URL.

Usage::

    report = import_store_pack(db, data)        # or import_store_pack_file(db, path)
    python -m app.services.store_pack_import fixtures/packs/clothing-demo.store-pack.v1.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.i18n import DEFAULT_LOCALE, normalize_locale
from app.models import (
    Category,
    Product,
    PropertyDefinition,
    PropertyType,
    PropertyValue,
    PaymentTerminal,
    ShippingMethod,
    StockMovementKind,
    Store,
    TerminalKind,
    Translation,
    TranslationEntity,
)
from app.services import bank_transfer
from app.services import stock as stock_svc
from app.services.product_admin import normalize_sku
from app.services.store_pack_validation import PackIssue, StorePackError, check_store_pack, read_store_pack
from app.services.storefront import DEFAULT_THEME, THEME_COLOR_KEYS, normalize_hex_color
from app.services.storefront_install import (
    StagedStorefront,
    StorefrontCopyError,
    discard_staged,
    publish_storefront,
    stage_storefront,
)
from app.services.media_publish import (
    MEDIA_DIR,
    StagedMedia,
    content_root,
    discard_media,
    missing_content_files,
    pack_has_media,
    publish_media,
    stage_media,
)
from app.services.pack_files import PackFilesError
from app.services.translations import content_locale

logger = logging.getLogger(__name__)

STOREFRONT_FIELD = "storefront.copy_from_pack_dir"
STOREFRONT_NOT_COPIED = (
    f"{STOREFRONT_FIELD}: not copied, the pack was imported without its folder (e.g. an uploaded file); "
    "import the pack file from its folder on the command line to copy it"
)
MEDIA_FIELD = f"{MEDIA_DIR}/"  # issue path: the pack's media folder (not a JSON field)
MEDIA_NOT_PUBLISHED = f"{MEDIA_FIELD}: images not published, the setting CONTENT_ROOT is empty"


class StorePackImportError(ValueError):
    """A valid pack that can't be applied (conflict with the current database, or a storefront
    folder that can't be copied); nothing was written."""

    def __init__(self, issues: list[PackIssue]) -> None:
        self.issues = list(issues)
        lines = "\n".join(f"- {issue}" for issue in self.issues)
        super().__init__(f"Store pack not imported ({len(self.issues)} conflict(s)):\n{lines}")


@dataclass
class ImportCounts:
    created: int = 0
    updated: int = 0

    def __str__(self) -> str:
        return f"{self.created} created, {self.updated} updated"


@dataclass
class ImportReport:
    """What an import did. ``updated`` = an existing row matched by its natural key."""

    store_slug: str
    store_created: bool
    categories: ImportCounts = field(default_factory=ImportCounts)
    properties: ImportCounts = field(default_factory=ImportCounts)
    products: ImportCounts = field(default_factory=ImportCounts)
    shipping_methods: ImportCounts = field(default_factory=ImportCounts)
    payment_terminals: ImportCounts = field(default_factory=ImportCounts)
    values_written: int = 0
    values_removed: int = 0
    translations_written: int = 0
    # Products whose stock the pack changed / that already had the pack's stock.
    stock_set: int = 0
    stock_unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    # Set when the pack's storefront folder was copied: files copied and the store's folder.
    storefront_files: Optional[int] = None
    storefront_path: Optional[str] = None
    # Set when the pack's media/ images were published: files published and the content folder.
    media_files: Optional[int] = None
    media_path: Optional[str] = None
    # /content/... images (image values, logo_url) the pack uses that aren't in CONTENT_ROOT after
    # the import: they would show as broken images (e.g. a .json upload, or a typo in a file name).
    missing_media: list[str] = field(default_factory=list)
    content_root: Optional[str] = None
    # Files that could not be installed *after* the database commit (disk full, a folder changed
    # since it was checked...): the catalog is imported, these files are not.
    file_problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"store {self.store_slug}: {'created' if self.store_created else 'updated'}",
            f"categories: {self.categories}",
            f"property definitions: {self.properties}",
            f"products: {self.products}",
            f"property values: {self.values_written} written, {self.values_removed} removed",
            f"translations: {self.translations_written} written",
        ]
        if self.shipping_methods.created or self.shipping_methods.updated:  # packs with shipping_methods only
            lines.append(f"shipping methods: {self.shipping_methods}")
        if self.payment_terminals.created or self.payment_terminals.updated:  # packs with payment_terminals only
            lines.append(f"payment terminals: {self.payment_terminals}")
        if self.stock_set or self.stock_unchanged:  # packs with "stock" only
            lines.append(f"stock: {self.stock_set} set, {self.stock_unchanged} unchanged")
        if self.storefront_files is not None:
            lines.append(f"storefront: {self.storefront_files} file(s) copied to {self.storefront_path}")
        if self.media_files is not None:
            lines.append(f"media: {self.media_files} image(s) published to {self.media_path}")
        if self.missing_media:
            lines.append(f"missing media: {len(self.missing_media)} image(s) used by the pack are not in "
                         f"{self.content_root} (broken images): {', '.join(self.missing_media)}")
        lines += [f"problem: {problem}" for problem in self.file_problems]
        lines += [f"skipped: {note}" for note in self.skipped]
        return "\n".join(lines)


def import_pack_terminals(db: Session, store: Store, terminals: list[dict[str, Any]], counts: ImportCounts) -> int:
    """Creates / updates the store's terminals named in a pack's ``payment_terminals`` (validated),
    with their translations. Returns how many translations were written."""
    existing = {t.code: t for t in db.scalars(select(PaymentTerminal).where(PaymentTerminal.store_id == store.id))}
    written = 0
    for data in terminals:
        terminal = existing.get(data["code"])
        if terminal is None:
            terminal = PaymentTerminal(store_id=store.id, code=data["code"], kind=TerminalKind(data["kind"]))
            db.add(terminal)
            counts.created += 1
        else:
            counts.updated += 1
        terminal.name = _clean(data["name"])
        terminal.is_enabled = data.get("enabled", True)
        terminal.sort_order = data.get("sort_order", 0)
        config_written = bool(data.get("config")) and not terminal.config
        if config_written:
            terminal.config, _ = bank_transfer.validate_config(data["config"])
        db.flush()  # its id, for the translations
        fields = ("name", "note") if config_written else ("name",)
        written += write_terminal_translations(db, store, terminal, data, fields, replace=True)
    return written


def _terminal_texts(data: dict[str, Any]) -> dict[tuple[str, str], str]:
    """A pack terminal's translations: {(language, "name" / "note"): text}."""
    return {(content_locale(locale), name): text.strip()
            for locale, block in (data.get("i18n") or {}).items()
            for name, text in block.items() if isinstance(text, str) and text.strip()}


def write_terminal_translations(db: Session, store: Store, terminal: PaymentTerminal, data: dict[str, Any],
                                fields: tuple[str, ...], *, replace: bool) -> int:
    """The pack terminal's translations of ``fields``. ``replace``: the terminal's translations of
    those fields become exactly the pack's (an import). Otherwise only gaps are filled (the startup
    catch-up): a translation is added for a language that has none, and only while the text it
    translates is still the pack's (a renamed terminal or an edited note keeps its own)."""
    kind = TranslationEntity.payment_terminal
    current = {(row.locale, row.field): row for row in db.scalars(
        select(Translation).where(Translation.entity_type == kind, Translation.entity_id == terminal.id))}
    source = {"name": (terminal.name, _clean(data["name"])),
              "note": ((terminal.config or {}).get("note", ""), (data.get("config") or {}).get("note", "").strip())}
    written = 0
    if replace:
        for (locale, name), row in current.items():
            if name in fields:
                db.delete(row)
        db.flush()
        current = {}
    for (locale, name), text in _terminal_texts(data).items():
        if name not in fields or (locale, name) in current:
            continue
        if not replace and source[name][0] != source[name][1]:
            continue
        db.add(Translation(store_id=store.id, locale=locale, entity_type=kind, entity_id=terminal.id, field=name,
                           text=text))
        written += 1
    return written


# ------------------------------------------------------------------------------ public API


def import_store_pack(db: Session, data: Any, pack_dir: Union[str, Path, None] = None) -> ImportReport:
    """Validate ``data`` and import it in one transaction (committed on success).
    ``pack_dir``: the folder of the pack file. Only when it is given, the storefront folder
    (``storefront.copy_from_pack_dir``, relative to it) is copied and its ``media/`` images are
    published to the content app (``CONTENT_ROOT/<store-slug>/``, see ``media_publish``).
    Raises :class:`StorePackError` (invalid pack) or :class:`StorePackImportError` (conflict
    with the database, or a storefront / media folder that can't be copied); in both cases
    nothing is written."""
    check_store_pack(data)
    slug = data["store"]["slug"]
    folder = (data.get("storefront") or {}).get("copy_from_pack_dir")
    staged: Optional[StagedStorefront] = None
    media: Optional[StagedMedia] = None
    media_skipped = False
    try:
        # 1. Files are checked and staged before the database is touched: a bad folder stops here.
        if folder and pack_dir is not None:
            try:
                staged = stage_storefront(Path(pack_dir), folder, slug)
            except StorefrontCopyError as exc:
                raise StorePackImportError([PackIssue(STOREFRONT_FIELD, str(exc))]) from exc
        if pack_dir is not None and pack_has_media(Path(pack_dir)):
            root = content_root()
            if root is None:
                media_skipped = True
            else:
                try:
                    media = stage_media(Path(pack_dir), slug, root)
                except PackFilesError as exc:
                    raise StorePackImportError([PackIssue(MEDIA_FIELD, str(exc))]) from exc
        # 2. The database, in one transaction.
        try:
            report = _Importer(db, data).run()
            db.commit()
        except IntegrityError as exc:  # e.g. a concurrent import created the same slug
            db.rollback()
            raise StorePackImportError([PackIssue("", f"database rejected the import: {exc.orig}")]) from exc
        except BaseException:
            db.rollback()
            raise
        # 3. After the commit: install the files. Everything was checked and staged before, so this
        # only fails for reasons outside the pack (disk full, a folder changed meanwhile). The
        # catalog is imported by now, so such a failure is reported, not raised as "not imported".
        if staged is not None:
            try:
                report.storefront_path = str(publish_storefront(staged))
                report.storefront_files = staged.files
                staged = None
            except (PackFilesError, OSError) as exc:
                logger.exception("storefront folder of store %s not installed", slug)
                report.file_problems.append(f"{STOREFRONT_FIELD}: storefront folder not installed: {exc}")
        if media is not None:
            try:
                report.media_path = str(publish_media(media))
                report.media_files = len(media.files)
                media = None
            except (PackFilesError, OSError) as exc:
                logger.exception("media of store %s not published", slug)
                report.file_problems.append(f"{MEDIA_FIELD}: images not published: {exc}")
    finally:  # anything not published (an error on the way) is removed
        if staged is not None:
            discard_staged(staged)
        if media is not None:
            discard_media(media)
    if folder and report.storefront_files is None and pack_dir is None:
        report.skipped.append(STOREFRONT_NOT_COPIED)
    if media_skipped:
        report.skipped.append(MEDIA_NOT_PUBLISHED)
    root = content_root()
    if root is not None:  # after publishing: what the storefront would show as a broken image
        report.content_root = str(root)
        report.missing_media = missing_content_files(data, root)
    return report


def import_store_pack_file(db: Session, path: Union[str, Path]) -> ImportReport:
    """Read a pack file strictly (see :func:`read_store_pack`) and import it; its storefront
    folder and ``media/`` images, if any, are taken from the file's folder."""
    return import_store_pack(db, read_store_pack(path), pack_dir=Path(path).parent)


# -------------------------------------------------------------------------- value mapping


def value_payload(prop_type: PropertyType, value: Any, category_ids: dict[str, int]) -> Optional[dict[str, Any]]:
    """Pack value -> stored ``PropertyValue.value`` JSON (the format the admin writes):
    text / enum / number / boolean -> {"value": v}; image -> {"media_path": url};
    category_ref -> {"category_id": id of the referenced category}. None = no value."""
    if value is None:
        return None
    if prop_type == PropertyType.image:
        return {"media_path": value}
    if prop_type == PropertyType.category_ref:
        return {"category_id": category_ids[value]}
    return {"value": value}


def _clean(text: str) -> str:
    """Collapse whitespace, like the admin forms do for names and labels."""
    return " ".join(text.split())


def _description(value: Optional[str]) -> Optional[str]:
    return (value or "").strip() or None


def _theme(pack_theme: Optional[dict[str, Any]]) -> dict[str, Any]:
    theme = {**DEFAULT_THEME, **(pack_theme or {})}
    for key in THEME_COLOR_KEYS:
        theme[key] = normalize_hex_color(theme[key]) or DEFAULT_THEME[key]
    return theme


# ------------------------------------------------------------------------------- importer


class _Importer:
    def __init__(self, db: Session, pack: dict[str, Any]) -> None:
        self.db = db
        self.pack = pack
        self.pack_categories: list[dict[str, Any]] = pack.get("categories", [])
        self.pack_products: list[dict[str, Any]] = pack.get("products", [])
        self.categories: dict[str, Category] = {}  # slug -> row (pack categories only)
        self.definitions: dict[tuple[str, str], PropertyDefinition] = {}  # (category slug, key) -> row

    def run(self) -> ImportReport:
        existing_store = self.db.scalar(select(Store).where(Store.slug == self.pack["store"]["slug"]))
        if existing_store is not None:
            conflicts = self.conflicts(existing_store)
            if conflicts:
                raise StorePackImportError(conflicts)

        store = self.store(existing_store)
        report = ImportReport(store_slug=store.slug, store_created=existing_store is None)
        self.import_categories(store, report)
        self.import_definitions(report)
        self.import_products(store, report)
        self.import_shipping_methods(store, report)
        self.import_payment_terminals(store, report)
        self.db.flush()  # product ids for their translations
        self.import_translations(store, report)
        self.db.flush()
        return report

    # ------------------------------------------------------------------- conflicts

    def conflicts(self, store: Store) -> list[PackIssue]:
        """prop_type changes that would leave typed values of products outside the pack, and SKUs
        the pack gives that another product of the store keeps."""
        issues = self.sku_conflicts(store)
        pack_slugs = {p["slug"] for p in self.pack_products}
        pack_product_ids = set(
            self.db.scalars(select(Product.id).where(Product.store_id == store.id, Product.slug.in_(pack_slugs)))
        )
        for c_index, pack_category in enumerate(self.pack_categories):
            category = self.db.scalar(
                select(Category).where(Category.store_id == store.id, Category.slug == pack_category["slug"])
            )
            if category is None:
                continue
            by_key = {d.key: d for d in category.property_definitions}
            for d_index, pack_def in enumerate(pack_category.get("property_definitions", [])):
                current = by_key.get(pack_def["key"])
                if current is None or current.prop_type.value == pack_def["prop_type"]:
                    continue
                others = self.db.scalar(
                    select(func.count())
                    .select_from(PropertyValue)
                    .where(PropertyValue.definition_id == current.id, PropertyValue.product_id.not_in(pack_product_ids))
                )
                if others:
                    issues.append(PackIssue(
                        f"categories[{c_index}].property_definitions[{d_index}].prop_type",
                        f'can\'t change "{current.key}" from {current.prop_type.value} to {pack_def["prop_type"]}: '
                        f"{others} value(s) of products not in this pack use it (change or delete them in admin first)",
                    ))
        return issues

    def pack_skus(self) -> dict[str, str]:
        """{product slug: normalized SKU} for the pack's products that give one."""
        return {p["slug"]: normalize_sku(p["sku"]) for p in self.pack_products if "sku" in p}

    def sku_conflicts(self, store: Store) -> list[PackIssue]:
        desired = self.pack_skus()
        if not desired:
            return []
        holders = {
            sku: slug for sku, slug in self.db.execute(
                select(Product.sku, Product.slug).where(Product.store_id == store.id, Product.sku.in_(desired.values()))
            )
        }
        issues = []
        for index, data in enumerate(self.pack_products):
            sku = desired.get(data["slug"])
            holder = holders.get(sku) if sku else None
            # Free if nobody has it, the product itself has it, or its holder gets another SKU from the pack.
            if holder is None or holder == data["slug"] or holder in desired:
                continue
            issues.append(PackIssue(
                f"products[{index}].sku",
                f'SKU "{sku}" is already used by product "{holder}" of this store, which the pack doesn\'t give '
                f"another SKU (change its SKU in admin, or give it one in the pack)",
            ))
        return issues

    # ----------------------------------------------------------------------- store

    def store(self, store: Optional[Store]) -> Store:
        data = self.pack["store"]
        if store is None:
            store = Store(slug=data["slug"])
            self.db.add(store)
        store.name = _clean(data["name"])
        store.currency = data["currency"]
        store.locale_default = normalize_locale(data.get("locale_default")) or DEFAULT_LOCALE
        store.theme = _theme(data.get("theme"))  # new dict: JSON column change is detected
        self.db.flush()
        return store

    # ------------------------------------------------------------------ categories

    def import_categories(self, store: Store, report: ImportReport) -> None:
        existing = {c.slug: c for c in self.db.scalars(select(Category).where(Category.store_id == store.id))}
        for data in self.pack_categories:
            category = existing.get(data["slug"])
            if category is None:
                category = Category(store_id=store.id, slug=data["slug"])
                self.db.add(category)
                report.categories.created += 1
            else:
                report.categories.updated += 1
            category.name = _clean(data["name"])
            category.description = _description(data.get("description"))
            category.sort_order = data.get("sort_order", 0)
            self.categories[data["slug"]] = category
        self.db.flush()  # ids for parents and category_ref values
        for data in self.pack_categories:
            parent = data.get("parent_slug")
            self.categories[data["slug"]].parent_id = self.categories[parent].id if parent else None

    # -------------------------------------------------------- property definitions

    def import_definitions(self, report: ImportReport) -> None:
        for data in self.pack_categories:
            category = self.categories[data["slug"]]
            by_key = {d.key: d for d in category.property_definitions}
            for pack_def in data.get("property_definitions", []):
                definition = by_key.get(pack_def["key"])
                if definition is None:
                    definition = PropertyDefinition(key=pack_def["key"])
                    category.property_definitions.append(definition)
                    report.properties.created += 1
                else:
                    report.properties.updated += 1
                prop_type = PropertyType(pack_def["prop_type"])
                definition.label = _clean(pack_def["label"])
                definition.prop_type = prop_type
                definition.required = pack_def.get("required", False)
                definition.sort_order = pack_def.get("sort_order", 0)
                definition.config = dict(pack_def.get("config") or {})
                ref = pack_def.get("ref_category_slug") if prop_type == PropertyType.category_ref else None
                definition.ref_category_id = self.categories[ref].id if ref else None
                self.definitions[(data["slug"], pack_def["key"])] = definition
        self.db.flush()  # ids for property values

    # -------------------------------------------------------------------- products

    def import_products(self, store: Store, report: ImportReport) -> None:
        existing = {p.slug: p for p in self.db.scalars(select(Product).where(Product.store_id == store.id))}
        category_ids = {slug: category.id for slug, category in self.categories.items()}
        desired_skus = self.pack_skus()
        # SKUs that move between the pack's products (A<->B) are released first, or the unique
        # (store, sku) constraint would refuse the intermediate state.
        moving = [p for slug, p in existing.items() if slug in desired_skus and p.sku != desired_skus[slug]]
        for product in moving:
            product.sku = None
        if moving:
            self.db.flush()
        stock_changes: list[tuple[Product, int, int]] = []  # (product, before, after): logged once ids exist
        for data in self.pack_products:
            product = existing.get(data["slug"])
            if product is None:
                product = Product(store_id=store.id, slug=data["slug"], stock_on_hand=0)
                self.db.add(product)
                report.products.created += 1
            else:
                report.products.updated += 1
            if "stock" in data:  # the pack wins: overwritten on every import
                if data["stock"] != product.stock_on_hand:
                    stock_changes.append((product, product.stock_on_hand, data["stock"]))
                else:
                    report.stock_unchanged += 1
            if data["slug"] in desired_skus:
                product.sku = desired_skus[data["slug"]]
            category_slug = data["category_slug"]
            product.category_id = self.categories[category_slug].id
            product.name = _clean(data["name"])
            product.description = _description(data.get("description"))
            product.price_cents = data["price_cents"]
            product.is_active = data.get("is_active", True)

            desired: dict[int, dict[str, Any]] = {}
            for key, value in (data.get("values") or {}).items():
                definition = self.definitions[(category_slug, key)]
                payload = value_payload(definition.prop_type, value, category_ids)
                if payload is not None:
                    desired[definition.id] = payload
            self.replace_values(product, desired, report)
        if stock_changes:
            self.db.flush()  # ids of the new products
            for product, before, after in stock_changes:
                product.stock_on_hand = after
                stock_svc.record_movement(self.db, product, StockMovementKind.pack_import, after - before, after)
                report.stock_set += 1

    def replace_values(self, product: Product, desired: dict[int, dict[str, Any]], report: ImportReport) -> None:
        current = {pv.definition_id: pv for pv in product.property_values}
        for definition_id, pv in current.items():
            if definition_id not in desired:
                product.property_values.remove(pv)  # delete-orphan cascade removes the row
                report.values_removed += 1
        for definition_id, payload in desired.items():
            if definition_id in current:
                current[definition_id].value = payload  # new dict -> change detected
            else:
                product.property_values.append(PropertyValue(definition_id=definition_id, value=payload))
            report.values_written += 1

    # ------------------------------------------------------------ shipping methods

    def import_payment_terminals(self, store: Store, report: ImportReport) -> None:
        """Upsert by ``code`` (see the module docstring: ``config`` only when the terminal has none)."""
        self.db.flush()  # a new store's default terminals (inserted with it) are visible
        report.translations_written += import_pack_terminals(self.db, store, self.pack.get("payment_terminals") or [],
                                                             report.payment_terminals)

    def import_shipping_methods(self, store: Store, report: ImportReport) -> None:
        """Upsert by ``code``: the pack's methods are created or overwritten; other methods of the
        store (e.g. created in admin) are kept."""
        existing = {m.code: m for m in self.db.scalars(select(ShippingMethod).where(ShippingMethod.store_id == store.id))}
        for data in self.pack.get("shipping_methods") or []:
            method = existing.get(data["code"])
            if method is None:
                method = ShippingMethod(store_id=store.id, code=data["code"])
                self.db.add(method)
                report.shipping_methods.created += 1
            else:
                report.shipping_methods.updated += 1
            method.name = _clean(data["name"])
            method.rate_cents = data["rate_cents"]
            method.free_shipping_threshold_cents = data.get("free_shipping_threshold_cents")
            method.countries = list(data.get("countries") or [])
            method.sort_order = data.get("sort_order", 0)
            method.is_active = data.get("is_active", True)

    # ---------------------------------------------------------------- translations

    def import_translations(self, store: Store, report: ImportReport) -> None:
        # UI overrides of this store: replaced by the pack's ui_translations.
        self.db.execute(
            delete(Translation).where(Translation.entity_type == TranslationEntity.ui, Translation.store_id == store.id)
        )
        for locale, texts in (self.pack.get("ui_translations") or {}).items():
            for key, text in texts.items():
                self.add_translation(store.id, normalize_locale(locale), TranslationEntity.ui, None, key, text, report)

        # Content: each row named by the pack gets exactly its i18n block.
        self.replace_content(store, TranslationEntity.store, store.id, self.pack["store"], report)
        for data in self.pack_categories:
            category = self.categories[data["slug"]]
            self.replace_content(store, TranslationEntity.category, category.id, data, report)
            for pack_def in data.get("property_definitions", []):
                definition = self.definitions[(data["slug"], pack_def["key"])]
                options = (pack_def.get("config") or {}).get("options") or []
                self.replace_content(store, TranslationEntity.property_definition, definition.id, pack_def, report,
                                     options=options)
        products = {p.slug: p for p in self.db.scalars(select(Product).where(Product.store_id == store.id))}
        for data in self.pack_products:
            self.replace_content(store, TranslationEntity.product, products[data["slug"]].id, data, report)
        methods = {m.code: m for m in self.db.scalars(select(ShippingMethod).where(ShippingMethod.store_id == store.id))}
        for data in self.pack.get("shipping_methods") or []:
            self.replace_content(store, TranslationEntity.shipping_method, methods[data["code"]].id, data, report)

    def replace_content(self, store: Store, entity_type: TranslationEntity, entity_id: int, data: dict[str, Any],
                        report: ImportReport, options: Optional[list[str]] = None) -> None:
        self.db.execute(
            delete(Translation).where(Translation.entity_type == entity_type, Translation.entity_id == entity_id)
        )
        for locale, block in (data.get("i18n") or {}).items():
            pairs = [(name, block.get(name)) for name in ("name", "description", "label")]
            # Enum options are translated in order: option:<original> -> translated text.
            translated_options = (block.get("config") or {}).get("options") or []
            pairs += [(f"option:{orig}", text) for orig, text in zip(options or [], translated_options)]
            pairs += [(f"value:{key}", text) for key, text in (block.get("values") or {}).items()]
            for field_name, text in pairs:
                if isinstance(text, str) and text.strip():
                    self.add_translation(store.id, content_locale(locale), entity_type, entity_id, field_name, text,
                                         report)

    def add_translation(self, store_id: int, locale: str, entity_type: TranslationEntity, entity_id: Optional[int],
                        field_name: str, text: str, report: ImportReport) -> None:
        self.db.add(Translation(store_id=store_id, locale=locale, entity_type=entity_type, entity_id=entity_id,
                                field=field_name, text=text.strip()))
        report.translations_written += 1


# ----------------------------------------------------------------------------------- CLI


def main(argv: list[str], session_factory: Optional[Callable[[], Session]] = None) -> int:
    """``python -m app.services.store_pack_import FILE...``: import pack files into the
    configured database (``DATABASE_URL``), one transaction per file. Each attempt is recorded
    in ``pack_imports`` (source "cli"), like admin uploads."""
    from app.models import PackImportStatus
    from app.services.pack_import_log import SOURCE_CLI, import_pack_bytes  # imports this module

    parser = argparse.ArgumentParser(
        prog="python -m app.services.store_pack_import",
        description="Validate store pack files and import them into the database configured by DATABASE_URL "
                    "(tables are created if missing). Each file is one all-or-nothing transaction; an existing "
                    "store is updated (upsert, see fixtures/packs/README.md). Every attempt is recorded in "
                    "pack_imports.",
        epilog="Exit status: 0 = every file was imported, 1 = a file was not imported, 2 = usage error. "
               "To only check files: python -m app.services.store_pack_validation FILE...",
    )
    parser.add_argument("files", nargs="+", metavar="FILE", help="store pack file (.json)")
    try:
        args = parser.parse_args(argv)  # before touching the database: --help never creates tables
    except SystemExit as exc:  # usage error or --help: argparse already printed the message
        return int(exc.code or 0)
    if session_factory is None:  # pragma: no cover - the real database
        from app.db import SessionLocal, init_db

        init_db()
        session_factory = SessionLocal
    status = 0
    for name in args.files:
        try:
            raw = Path(name).read_bytes()
        except OSError as exc:
            status = 1
            print(f"{name}: cannot read ({exc.strerror})")
            continue
        db = session_factory()
        try:
            outcome = import_pack_bytes(db, raw, source=SOURCE_CLI, filename=name, update_existing=True,
                                        pack_dir=Path(name).parent)
        finally:
            db.close()
        if outcome.ok:
            print(f"{name}: imported (record #{outcome.record_id})")
            for line in outcome.report.summary().splitlines():
                print(f"  {line}")
            continue
        status = 1
        if outcome.status == PackImportStatus.failed:
            print(f"{name}: FAILED, unexpected error (details in record #{outcome.record_id})")
            print(f"  {outcome.exception.splitlines()[0]}")
        else:
            print(f"{name}: NOT IMPORTED ({outcome.status.value}, record #{outcome.record_id})")
            for issue in outcome.issues or []:
                print(f"  - {issue}")
    return status


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
