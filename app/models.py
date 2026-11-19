"""Generic catalog schema. Domain fields (tubes, sizes, brands) live in PropertyDefinition rows, not columns."""

from __future__ import annotations

import enum
import secrets
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    event,
    insert,
    select,
)
from sqlalchemy.orm import Mapped, backref, mapped_column, relationship

from app.db import Base
from app.i18n import DEFAULT_LOCALE



def utc_now() -> datetime:
    """Current UTC time as a naive datetime (what the DateTime columns store). Replaces
    ``datetime.utcnow``, deprecated since Python 3.12 (one warning per inserted row)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

class PropertyType(str, enum.Enum):
    text = "text"
    number = "number"
    boolean = "boolean"
    enum = "enum"
    image = "image"
    category_ref = "category_ref"


DEFAULT_LOW_STOCK_THRESHOLD = 2  # units; a new store's low-stock threshold (admin: Inventory -> Low stock)


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    # UI language of the public storefront when the visitor has not chosen one (see app/i18n.py).
    locale_default: Mapped[str] = mapped_column(String(16), default=DEFAULT_LOCALE)
    # Theme as data (colors, logo URL placeholders) — no hard-coded brand CSS per vertical
    theme: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Low stock: a product is "low" when its stock on hand is at or below this number of units
    # (unless the product has its own threshold). See app/services/low_stock.py.
    low_stock_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=DEFAULT_LOW_STOCK_THRESHOLD,
                                                     server_default=str(DEFAULT_LOW_STOCK_THRESHOLD))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    categories: Mapped[list[Category]] = relationship(back_populates="store", cascade="all, delete-orphan")
    products: Mapped[list[Product]] = relationship(back_populates="store", cascade="all, delete-orphan")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("store_id", "slug", name="uq_category_store_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    store: Mapped[Store] = relationship(back_populates="categories")
    parent: Mapped[Optional[Category]] = relationship(remote_side="Category.id")
    property_definitions: Mapped[list[PropertyDefinition]] = relationship(
        back_populates="category",
        cascade="all, delete-orphan",
        foreign_keys="PropertyDefinition.category_id",
    )
    products: Mapped[list[Product]] = relationship(back_populates="category")


class PropertyDefinition(Base):
    __tablename__ = "property_definitions"
    __table_args__ = (UniqueConstraint("category_id", "key", name="uq_prop_def_category_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(200))
    prop_type: Mapped[PropertyType] = mapped_column(
        Enum(PropertyType, native_enum=False), default=PropertyType.text
    )
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # For enum: {"options": ["S", "M", "L"]}; for category_ref: optional hints
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ref_category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )

    category: Mapped[Category] = relationship(
        back_populates="property_definitions",
        foreign_keys=[category_id],
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("store_id", "slug", name="uq_product_store_slug"),
        # Store-scoped: two stores may use the same SKU. NULL (no SKU yet) never clashes.
        UniqueConstraint("store_id", "sku", name="uq_product_store_sku"),
        CheckConstraint("stock_on_hand >= 0", name="ck_product_stock_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64))
    # Stock-keeping unit: the store's own product code (generic retail field, not per vertical).
    # Stored upper-case (app/services/product_admin.py), so uniqueness ignores case. Required by
    # the admin form; NULL for products created without it (store packs, older databases).
    sku: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    # Units on hand (generic retail field): a whole number >= 0, set in the admin form. The database
    # refuses negatives too (CheckConstraint above). 0 for products created without it (store
    # packs, older databases). Nothing reserves or decrements it yet (no orders).
    stock_on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Its own low-stock threshold (units); NULL = the store's (Store.low_stock_threshold).
    low_stock_threshold: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    store: Mapped[Store] = relationship(back_populates="products")
    category: Mapped[Category] = relationship(back_populates="products")
    property_values: Mapped[list[PropertyValue]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    # Its stock history (newest last). Deleting the product deletes it.
    stock_movements: Mapped[list[StockMovement]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="StockMovement.id"
    )

    @property
    def in_stock(self) -> bool:
        """What the public side may know about stock: whether any unit is on hand (the count itself
        stays in admin). The JSON API's ``in_stock``."""
        return (self.stock_on_hand or 0) > 0


class PropertyValue(Base):
    __tablename__ = "property_values"
    __table_args__ = (UniqueConstraint("product_id", "definition_id", name="uq_prop_val_product_def"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    definition_id: Mapped[int] = mapped_column(
        ForeignKey("property_definitions.id", ondelete="CASCADE"), index=True
    )
    # Typed payload: {"value": ...} or {"media_path": "..."} or {"category_id": N}
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    product: Mapped[Product] = relationship(back_populates="property_values")
    definition: Mapped[PropertyDefinition] = relationship()


class StockMovementKind(str, enum.Enum):
    """Why a product's stock changed (see app/services/stock.py)."""

    initial = "initial"  # the product was created in admin with stock > 0
    edit = "edit"  # the Stock number was changed in the product form
    adjustment = "adjustment"  # an admin adjusted it by a delta, with a reason
    pack_import = "pack_import"  # a store pack set it (every import of a pack with "stock")
    inventory_import = "inventory_import"  # a row of an inventory CSV import set it
    order = "order"  # an order's payment succeeded: its units were taken (reason: the order's id)


class StockMovement(Base):
    """One change of a product's ``stock_on_hand``: the movement log. Every change made by the app
    (admin form, adjustment, pack import) adds a row in the same transaction as the change itself,
    so the log and the stock can't disagree. Generic retail data: no per-vertical columns."""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint("delta <> 0", name="ck_stock_movement_delta_not_zero"),
        CheckConstraint("quantity_after >= 0", name="ck_stock_movement_quantity_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    kind: Mapped[StockMovementKind] = mapped_column(Enum(StockMovementKind, native_enum=False, length=16))
    delta: Mapped[int] = mapped_column(Integer)  # units added (> 0) or removed (< 0)
    quantity_after: Mapped[int] = mapped_column(Integer)  # stock_on_hand right after this movement
    # Required for adjustments (the admin's short explanation); empty for the other kinds.
    reason: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    product: Mapped[Product] = relationship(back_populates="stock_movements")
    user: Mapped[Optional[User]] = relationship()


class Cart(Base):
    """A visitor's cart in **one** store (public: no login). The browser holds a random token in a
    cookie scoped to that store's cart API path; only its SHA-256 is stored, so the table can't be
    used to take over carts. Lines are products with a quantity (stock is checked, not reserved).
    See app/services/cart.py."""

    __tablename__ = "carts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    store: Mapped[Store] = relationship(backref=backref("carts", cascade="all, delete-orphan"))
    lines: Mapped[list[CartLine]] = relationship(
        back_populates="cart", cascade="all, delete-orphan", order_by="CartLine.id"
    )


class CartLineIssue(str, enum.Enum):
    """Why a cart line can't be bought as it is (found when it was last validated)."""

    out_of_stock = "out_of_stock"  # stock on hand is 0
    insufficient_stock = "insufficient_stock"  # less stock than the line's quantity
    unavailable = "unavailable"  # the product was hidden


class CartLine(Base):
    """One product of a cart with its quantity (one line per product: adding it again adds to it).

    Its name, SKU and price are a **snapshot** taken when it was added, so editing the catalog
    doesn't rewrite open carts; ``validated_at`` says when the snapshot was last checked against
    the product (see app/services/cart.py: after CART_REVALIDATE_MINUTES it is refreshed, and a
    changed price is kept in ``previous_unit_price_cents`` for the visitor to see)."""

    __tablename__ = "cart_lines"
    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", name="uq_cart_line_product"),
        CheckConstraint("quantity > 0", name="ck_cart_line_quantity_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200), server_default="")  # snapshot
    # Snapshot too: the name's content translations, {language: name}, so a page in another language
    # shows the product's name in that language.
    name_translations: Mapped[Optional[dict[str, str]]] = mapped_column(JSON, nullable=True)
    sku: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # snapshot
    unit_price_cents: Mapped[int] = mapped_column(Integer, server_default="0")  # snapshot: what the cart charges
    # Set when the last validation found a new price: the price the line had before it.
    previous_unit_price_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    issue: Mapped[Optional[CartLineIssue]] = mapped_column(
        Enum(CartLineIssue, native_enum=False, length=20), nullable=True
    )  # from the last validation; None = buyable as it is
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # None = never
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    cart: Mapped[Cart] = relationship(back_populates="lines")
    # Deleting a product removes it from every cart.
    product: Mapped[Product] = relationship(backref=backref("cart_lines", cascade="all, delete-orphan"))


class CartRequest(Base):
    """An ``Idempotency-Key`` of an "add to cart" request (rage clicks, retries): the same key
    gives back the first answer instead of adding again. Recorded before the work (so two copies
    arriving together can't both add) and kept 24 hours. See app/services/cart.py."""

    __tablename__ = "cart_requests"
    __table_args__ = (UniqueConstraint("store_id", "key", name="uq_cart_request_store_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))  # SHA-256 of the request body: a reused key is refused
    cart_id: Mapped[Optional[int]] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), nullable=True)
    response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)  # None while in progress
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    alt_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)


class UserRole(str, enum.Enum):
    admin = "admin"
    customer = "customer"


class User(Base):
    """Login account. Passwords are stored only as a salted hash (see ``app.auth``)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Always stored normalized (stripped + lower-case) so uniqueness is case-insensitive.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, native_enum=False), default=UserRole.customer
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class TranslationEntity(str, enum.Enum):
    """What a ``Translation`` row translates."""

    ui = "ui"  # a UI message key from app/locales/*.json (override)
    store = "store"
    category = "category"
    property_definition = "property_definition"
    product = "product"
    shipping_method = "shipping_method"
    payment_terminal = "payment_terminal"  # its name and, for a bank transfer, the note for the shopper


class Translation(Base):
    """Level 2 of the translations (level 1 = the JSON catalogs in app/locales/).

    - UI overrides: ``entity_type="ui"``, ``field`` = message key, ``entity_id`` = NULL;
      ``store_id`` NULL = whole platform, set = that store's pages only.
    - Content: ``entity_type`` + ``entity_id`` name a row (category 12, product 5, ...) and
      ``field`` one of its texts: ``name``, ``description``, ``label``, ``option:<enum option>``,
      ``value:<property key>``. ``store_id`` is the row's store (loading + cleanup).

    One generic table for every vertical: no per-domain columns. See app/services/translations.py.
    """

    __tablename__ = "translations"
    __table_args__ = (
        UniqueConstraint("store_id", "locale", "entity_type", "entity_id", "field", name="uq_translation"),
        Index("ix_translation_lookup", "locale", "store_id", "entity_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=True)
    locale: Mapped[str] = mapped_column(String(16))
    entity_type: Mapped[TranslationEntity] = mapped_column(Enum(TranslationEntity, native_enum=False, length=32))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    field: Mapped[str] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)


def _delete_translations_of(entity_type: TranslationEntity):
    """ORM hook: deleting a row deletes its translations (the app's SQLite engine does not
    enforce foreign keys, and entity_id can't have one since it points at several tables)."""

    def listener(_mapper, connection, target) -> None:
        table = Translation.__table__
        connection.execute(
            delete(table).where(table.c.entity_type == entity_type, table.c.entity_id == target.id)
        )
        if entity_type == TranslationEntity.store:
            connection.execute(delete(table).where(table.c.store_id == target.id))
        if entity_type == TranslationEntity.property_definition:
            # Products' translated values of this property ("value:<key>") are dead too.
            products = Product.__table__
            connection.execute(
                delete(table).where(
                    table.c.entity_type == TranslationEntity.product,
                    table.c.field == f"value:{target.key}",
                    table.c.entity_id.in_(select(products.c.id).where(products.c.category_id == target.category_id)),
                )
            )

    return listener


for _model, _entity in (
    (Store, TranslationEntity.store),
    (Category, TranslationEntity.category),
    (PropertyDefinition, TranslationEntity.property_definition),
    (Product, TranslationEntity.product),
):
    event.listen(_model, "after_delete", _delete_translations_of(_entity))


class PackImportStatus(str, enum.Enum):
    """Outcome of one store pack import attempt (see app/services/pack_import_log.py)."""

    running = "running"  # started, not finished: the process stopped mid-import
    succeeded = "succeeded"
    invalid = "invalid"  # not a valid pack (issues)
    too_large = "too_large"
    refused = "refused"  # the store exists and updating was not allowed
    conflict = "conflict"  # valid pack that conflicts with the data (issues)
    failed = "failed"  # unexpected error (exception)


class PackImport(Base):
    """One store pack import attempt and its result. Written in its own transactions, so it
    survives the rollback of a failed import."""

    __tablename__ = "pack_imports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[PackImportStatus] = mapped_column(
        Enum(PackImportStatus, native_enum=False, length=16), default=PackImportStatus.running, index=True
    )
    source: Mapped[str] = mapped_column(String(16))  # "upload", "cli", ...
    filename: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    file_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    update_existing: Mapped[bool] = mapped_column(Boolean, default=False)
    store_slug: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    store_id: Mapped[Optional[int]] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), nullable=True)
    store_created: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Counts of a successful import: {"categories": {"created": 1, "updated": 0}, ...}
    report: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # Expected failures (invalid / conflict): [{"path": "store.slug", "message": "required"}, ...]
    issues: Mapped[Optional[list[dict[str, str]]]] = mapped_column(JSON, nullable=True)
    # Unexpected disruption (status "failed"): "ExceptionType: message" + traceback.
    exception: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    user: Mapped[Optional[User]] = relationship()

    @property
    def duration_ms(self) -> Optional[int]:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


class ShippingMethod(Base):
    """A store's flat-rate shipping method (see app/services/shipping.py).

    Quote = ``rate_cents``, or 0 when ``free_shipping_threshold_cents`` is set and the subtotal
    reaches it. ``countries`` limits where it ships (ISO 3166-1 alpha-2, upper case); empty = anywhere.
    Amounts are in the store's currency, in cents, like ``Product.price_cents``.
    """

    __tablename__ = "shipping_methods"
    __table_args__ = (UniqueConstraint("store_id", "code", name="uq_shipping_method_store_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))  # slug, e.g. "standard"; unique per store
    name: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    rate_cents: Mapped[int] = mapped_column(Integer, default=0)
    free_shipping_threshold_cents: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    countries: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    # Declared from this side so the Store class stays as it is; deleting a store deletes its methods.
    store: Mapped[Store] = relationship(backref=backref("shipping_methods", cascade="all, delete-orphan"))


# Its name can be translated (content translations, like a product name): deleting it deletes them.
event.listen(ShippingMethod, "after_delete", _delete_translations_of(TranslationEntity.shipping_method))


# Every store has these mock payment terminals (code, display name, order), added when the store is
# inserted (listener below: admin form, pack import, startup seed alike) and, for stores created
# before terminals existed, at app startup (app/services/payments.py). Turn one off with is_enabled.
# Terminals whose code starts with this are mocks: only they can be answered by the admin simulator.
MOCK_TERMINAL_PREFIX = "mock_"
DEFAULT_PAYMENT_TERMINALS: tuple[tuple[str, str, int], ...] = (
    ("mock_card", "Mock card", 0),
    ("mock_transfer", "Mock bank transfer", 1),
)


class TerminalKind(str, enum.Enum):
    """How a terminal's payments get their outcome."""

    mock = "mock"  # code "mock_…": the signed callback, or the admin simulator (demos)
    bank_transfer = "bank_transfer"  # offline bank transfer: an admin confirms or rejects the deposit


class PaymentTerminal(Base):
    """A store's payment terminal. ``kind`` says how its payments are answered: ``mock`` (code
    ``mock_…``, demos) or ``bank_transfer`` (offline deposit, confirmed by an admin; ``config`` holds
    the deposit instructions, see app/services/bank_transfer.py). Listed with
    GET /api/stores/{slug}/payment/terminals, disabled ones included and marked; a disabled terminal
    takes no new payments. Managed in admin (app/routers/admin_terminals.py)."""

    __tablename__ = "payment_terminals"
    __table_args__ = (UniqueConstraint("store_id", "code", name="uq_payment_terminal_store_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))  # e.g. "mock_card"; unique per store
    name: Mapped[str] = mapped_column(String(200))  # display name
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[TerminalKind] = mapped_column(
        Enum(TerminalKind, native_enum=False, length=24), default=TerminalKind.mock, server_default="mock"
    )
    # bank_transfer: {"bank", "beneficiary", "clabe", "account", "note"} (the shopper's instructions).
    config: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    @property
    def is_mock(self) -> bool:
        return self.code.startswith(MOCK_TERMINAL_PREFIX)

    @property
    def is_bank_transfer(self) -> bool:
        return self.kind == TerminalKind.bank_transfer


def default_payment_terminal_rows(store_id: int) -> list[dict[str, Any]]:
    return [
        {"store_id": store_id, "code": code, "name": name, "is_enabled": True, "sort_order": order,
         "kind": TerminalKind.mock.value, "created_at": utc_now()}
        for code, name, order in DEFAULT_PAYMENT_TERMINALS
    ]


@event.listens_for(Store, "after_insert")
def _add_default_payment_terminals(_mapper, connection, target: Store) -> None:
    connection.execute(insert(PaymentTerminal.__table__), default_payment_terminal_rows(target.id))


class PaymentStatus(str, enum.Enum):
    """Where a payment is. Every payment starts ``pending``; it is then marked **once**, as
    ``succeeded`` or ``failed``, and never changes again (see ``PAYMENT_TRANSITIONS``)."""

    pending = "pending"  # created for an amount, waiting for the terminal's answer
    succeeded = "succeeded"  # the terminal accepted it (final)
    failed = "failed"  # the terminal refused it (final)


# Allowed status changes. Anything else is refused (app/services/payments.py).
PAYMENT_TRANSITIONS: dict[PaymentStatus, frozenset[PaymentStatus]] = {
    PaymentStatus.pending: frozenset({PaymentStatus.succeeded, PaymentStatus.failed}),
    PaymentStatus.succeeded: frozenset(),
    PaymentStatus.failed: frozenset(),
}


# Largest amount a payment can have: ``amount_cents`` is a plain INTEGER (32 bits on most databases).
MAX_AMOUNT_CENTS = 2_147_483_647
PAYMENT_ID_PREFIX = "pay_"


def new_payment_public_id() -> str:
    """An unguessable payment id for URLs and the API, e.g. "pay_3q0V…" (144 random bits). The
    integer primary key is never shown: sequential numbers would let anyone walk through payments."""
    return PAYMENT_ID_PREFIX + secrets.token_urlsafe(18)


class Payment(Base):
    """A payment of ``amount_cents`` (in ``currency``, the store's at creation) through one of the
    store's terminals. No card network: the status is set by the terminal's signed callback
    (app/services/payment_callbacks.py) or, for mock terminals, by the admin simulator. Outside the
    database a payment is known only by ``public_id``."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # internal only
    # The payment's id everywhere outside the database (API, admin URLs). See app/schema_upgrades.py
    # for databases created before it existed.
    public_id: Mapped[str] = mapped_column(String(40), unique=True, index=True, default=new_payment_public_id)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    terminal_id: Mapped[int] = mapped_column(ForeignKey("payment_terminals.id", ondelete="CASCADE"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=16), default=PaymentStatus.pending, index=True
    )
    # What the (mock) terminal answered with, e.g. an authorization code; optional.
    mock_reference: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Bank transfer: the short reference the shopper writes on the deposit (unique in the store), and
    # the instructions they were given (a copy: later edits of the terminal's details don't change it).
    transfer_reference: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    instructions: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    # The admin who confirmed / rejected it (offline payments), with their note (e.g. the bank's tracking key).
    decided_by_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decision_note: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    terminal: Mapped[PaymentTerminal] = relationship()
    decided_by: Mapped[Optional[User]] = relationship()

    __table_args__ = (UniqueConstraint("store_id", "transfer_reference", name="uq_payment_store_transfer_reference"),)


@event.listens_for(Store, "after_delete")
def _delete_payment_terminals(_mapper, connection, target: Store) -> None:
    # The app's SQLite engine doesn't enforce foreign keys, so ON DELETE CASCADE alone isn't enough.
    for table in (Payment.__table__, PaymentTerminal.__table__):
        connection.execute(delete(table).where(table.c.store_id == target.id))


@event.listens_for(Store, "after_delete")
def _delete_cart_requests(_mapper, connection, target: Store) -> None:
    # Carts go with the store through the ORM cascade; these rows have no relationship.
    connection.execute(delete(CartRequest.__table__).where(CartRequest.__table__.c.store_id == target.id))


# ------------------------------------------------------------------------------------- orders

ORDER_ID_PREFIX = "ord_"


def new_order_public_id() -> str:
    """An unguessable order id for URLs and the API, e.g. "ord_3q0V…" (144 random bits)."""
    return ORDER_ID_PREFIX + secrets.token_urlsafe(18)


class OrderStatus(str, enum.Enum):
    """Where an order is, read from its payment (the order itself stores no status)."""

    awaiting_payment = "awaiting_payment"  # payment pending
    paid = "paid"  # payment succeeded
    payment_failed = "payment_failed"  # payment failed


ORDER_STATUS_BY_PAYMENT = {
    PaymentStatus.pending: OrderStatus.awaiting_payment,
    PaymentStatus.succeeded: OrderStatus.paid,
    PaymentStatus.failed: OrderStatus.payment_failed,
}


class Order(Base):
    """What a checkout made of a cart: copies of its lines (name, SKU, price, quantity), the chosen
    shipping method with its quote, the totals, and the payment of the total. Created by
    app/services/checkout.py; the payment's outcome (its terminal) says whether it is paid."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # internal only
    public_id: Mapped[str] = mapped_column(String(40), unique=True, index=True, default=new_order_public_id)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    cart_id: Mapped[Optional[int]] = mapped_column(ForeignKey("carts.id", ondelete="SET NULL"), nullable=True)
    currency: Mapped[str] = mapped_column(String(8))
    subtotal_cents: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer)
    total_cents: Mapped[int] = mapped_column(Integer)  # subtotal + shipping = the payment's amount
    # The shipping method as quoted (copied: a later edit of the method doesn't change the order).
    shipping_method_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("shipping_methods.id", ondelete="SET NULL"), nullable=True
    )
    shipping_code: Mapped[str] = mapped_column(String(64))
    shipping_name: Mapped[str] = mapped_column(String(200))
    shipping_country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    free_shipping: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id", ondelete="CASCADE"), index=True)
    # When its payment succeeded and its lines' stock was taken (None: not taken: pending or failed).
    stock_taken_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    store: Mapped[Store] = relationship(backref=backref("orders", cascade="all, delete-orphan"))
    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="OrderLine.id"
    )
    payment: Mapped[Payment] = relationship()

    @property
    def status(self) -> OrderStatus:
        return ORDER_STATUS_BY_PAYMENT[self.payment.status]


class OrderLine(Base):
    """A product of an order as it was bought: copies, so catalog changes never alter it."""

    __tablename__ = "order_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    # The product (None once it is deleted: the line keeps its copies).
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    sku: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    name_translations: Mapped[Optional[dict[str, str]]] = mapped_column(JSON, nullable=True)  # {language: name}
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    line_total_cents: Mapped[int] = mapped_column(Integer)
    # Units that could not be taken when the payment succeeded (sold meanwhile, or product deleted):
    # the order was paid for them, so an admin restocks or refunds. 0 = every unit was taken.
    short_quantity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    order: Mapped[Order] = relationship(back_populates="lines")
    # Deleting a product leaves its order lines (product_id becomes None).
    product: Mapped[Optional[Product]] = relationship(backref=backref("order_lines"))
