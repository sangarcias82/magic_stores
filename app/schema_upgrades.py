"""In-place upgrades for databases created by an earlier version of the app.

``Base.metadata.create_all`` creates missing tables but never changes existing ones, and the
project has no migration tool. Each function here brings one older table up to date, is a no-op on
a current database, and runs from :func:`app.db.init_db` (so at every app start).
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.models import new_payment_public_id


def add_payment_public_ids(bind: Engine) -> int:
    """``payments.public_id`` (unguessable payment id, added after the table): add the column, give
    every existing payment a random id, then make it unique. Returns how many payments got one."""
    inspector = inspect(bind)
    if "payments" not in inspector.get_table_names():
        return 0
    if "public_id" in {column["name"] for column in inspector.get_columns("payments")}:
        return 0
    with bind.begin() as connection:
        connection.execute(text("ALTER TABLE payments ADD COLUMN public_id VARCHAR(40)"))
        ids = list(connection.execute(text("SELECT id FROM payments")).scalars())
        for payment_id in ids:
            connection.execute(text("UPDATE payments SET public_id = :public_id WHERE id = :id"),
                               {"public_id": new_payment_public_id(), "id": payment_id})
        # Same name as the index create_all makes for a fresh database (unique=True, index=True).
        connection.execute(text("CREATE UNIQUE INDEX ix_payments_public_id ON payments (public_id)"))
    return len(ids)


def add_product_sku(bind: Engine) -> bool:
    """``products.sku`` (store-scoped product code, added after the table): add the nullable column
    and its per-store unique index. Existing products keep no SKU (NULL never clashes) until one is
    set in admin. Returns whether the column was added."""
    inspector = inspect(bind)
    if "products" not in inspector.get_table_names():
        return False
    if "sku" in {column["name"] for column in inspector.get_columns("products")}:
        return False
    with bind.begin() as connection:
        connection.execute(text("ALTER TABLE products ADD COLUMN sku VARCHAR(64)"))
        # A fresh database gets the same rule from the table's UniqueConstraint("store_id", "sku");
        # SQLite can't add a constraint to an existing table, a unique index enforces it identically.
        connection.execute(text("CREATE UNIQUE INDEX uq_product_store_sku ON products (store_id, sku)"))
    return True


def add_product_stock(bind: Engine) -> bool:
    """``products.stock_on_hand`` (units on hand, added after the table): add the column with 0 for
    every existing product and the same ``>= 0`` check a fresh table has. Returns whether it was added."""
    inspector = inspect(bind)
    if "products" not in inspector.get_table_names():
        return False
    if "stock_on_hand" in {column["name"] for column in inspector.get_columns("products")}:
        return False
    with bind.begin() as connection:
        # A column CHECK is allowed in ADD COLUMN (SQLite, PostgreSQL); named like the table's
        # CheckConstraint in app/models.py.
        connection.execute(text(
            "ALTER TABLE products ADD COLUMN stock_on_hand INTEGER NOT NULL DEFAULT 0 "
            "CONSTRAINT ck_product_stock_non_negative CHECK (stock_on_hand >= 0)"))
    return True


CART_LINE_SNAPSHOT_COLUMNS = (
    ("name", "VARCHAR(200) NOT NULL DEFAULT ''"),
    ("sku", "VARCHAR(64)"),
    ("unit_price_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("previous_unit_price_cents", "INTEGER"),
    ("issue", "VARCHAR(20)"),
    ("validated_at", "DATETIME"),
)


def add_cart_line_snapshots(bind: Engine) -> int:
    """``cart_lines`` snapshot columns (name, SKU, price of the product when added, last validation):
    add them and fill each open line from its product, as if added now; ``validated_at`` stays empty,
    so the next read validates them. Returns how many columns were added."""
    inspector = inspect(bind)
    if "cart_lines" not in inspector.get_table_names():
        return 0
    present = {column["name"] for column in inspector.get_columns("cart_lines")}
    missing = [(name, ddl) for name, ddl in CART_LINE_SNAPSHOT_COLUMNS if name not in present]
    if not missing:
        return 0
    with bind.begin() as connection:
        for name, ddl in missing:
            connection.execute(text(f"ALTER TABLE cart_lines ADD COLUMN {name} {ddl}"))
        connection.execute(text(
            "UPDATE cart_lines SET "
            "name = (SELECT products.name FROM products WHERE products.id = cart_lines.product_id), "
            "sku = (SELECT products.sku FROM products WHERE products.id = cart_lines.product_id), "
            "unit_price_cents = (SELECT products.price_cents FROM products WHERE products.id = cart_lines.product_id) "
            "WHERE product_id IN (SELECT id FROM products)"))
    return len(missing)


def add_order_stock_columns(bind: Engine) -> int:
    """``orders.stock_taken_at`` and ``order_lines.short_quantity`` (stock is taken when an order's
    payment succeeds): add them if missing. Older orders keep ``stock_taken_at`` empty. Returns how
    many columns were added."""
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    wanted = [("orders", "stock_taken_at", "DATETIME"), ("order_lines", "short_quantity", "INTEGER NOT NULL DEFAULT 0")]
    missing = [(table, column, ddl) for table, column, ddl in wanted
               if table in tables and column not in {c["name"] for c in inspector.get_columns(table)}]
    if missing:
        with bind.begin() as connection:
            for table, column, ddl in missing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    return len(missing)


def add_bank_transfer_columns(bind: Engine) -> int:
    """Offline bank transfer terminals: ``payment_terminals.kind`` / ``config`` and the payments'
    ``transfer_reference`` (unique per store), ``instructions``, ``decided_by_id``, ``decision_note``.
    Existing terminals are mocks (kind ``mock``). Returns how many columns were added."""
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    wanted = [
        ("payment_terminals", "kind", "VARCHAR(24) NOT NULL DEFAULT 'mock'"),
        ("payment_terminals", "config", "JSON"),
        ("payments", "transfer_reference", "VARCHAR(16)"),
        ("payments", "instructions", "JSON"),
        ("payments", "decided_by_id", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
        ("payments", "decision_note", "VARCHAR(200)"),
    ]
    missing = [(table, column, ddl) for table, column, ddl in wanted
               if table in tables and column not in {c["name"] for c in inspector.get_columns(table)}]
    if missing:
        with bind.begin() as connection:
            for table, column, ddl in missing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            if ("payments", "transfer_reference") in {(t, c) for t, c, _ in missing}:
                connection.execute(text("CREATE UNIQUE INDEX uq_payment_store_transfer_reference "
                                        "ON payments (store_id, transfer_reference)"))
    return len(missing)


def add_low_stock_thresholds(bind: Engine) -> int:
    """Low stock: ``stores.low_stock_threshold`` (existing stores get the default) and the products'
    optional ``low_stock_threshold`` (NULL = the store's). Returns how many columns were added."""
    from app.models import DEFAULT_LOW_STOCK_THRESHOLD

    inspector = inspect(bind)
    wanted = [("stores", "low_stock_threshold", f"INTEGER NOT NULL DEFAULT {DEFAULT_LOW_STOCK_THRESHOLD}"),
              ("products", "low_stock_threshold", "INTEGER")]
    return _add_columns(bind, inspector, wanted)


def add_line_name_translations(bind: Engine) -> int:
    """``cart_lines.name_translations`` / ``order_lines.name_translations`` (the product name in other
    languages, copied with the name). Older lines get none: they show the name as entered."""
    inspector = inspect(bind)
    wanted = [("cart_lines", "name_translations", "JSON"), ("order_lines", "name_translations", "JSON")]
    return _add_columns(bind, inspector, wanted)


def _add_columns(bind: Engine, inspector, wanted: list[tuple[str, str, str]]) -> int:
    tables = set(inspector.get_table_names())
    missing = [(table, column, ddl) for table, column, ddl in wanted
               if table in tables and column not in {c["name"] for c in inspector.get_columns(table)}]
    if missing:
        with bind.begin() as connection:
            for table, column, ddl in missing:
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    return len(missing)


def upgrade(bind: Engine) -> None:
    add_payment_public_ids(bind)
    add_product_sku(bind)
    add_product_stock(bind)
    add_cart_line_snapshots(bind)
    add_order_stock_columns(bind)
    add_bank_transfer_columns(bind)
    add_low_stock_thresholds(bind)
    add_line_name_translations(bind)
