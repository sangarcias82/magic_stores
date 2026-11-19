"""Our own code raises no warnings. The suite used to print 2,600+ DeprecationWarnings because the
models used ``default=datetime.utcnow`` (deprecated since Python 3.12), called on every insert."""

from __future__ import annotations

import time
import warnings
from datetime import timezone

from app.auth import create_user
from app.models import Category, PackImport, PackImportStatus, Product, Store, utc_now


def test_inserting_rows_raises_no_warnings(db_session):
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning fails the test
        store = Store(slug="w", name="W", currency="USD")
        db_session.add(store)
        db_session.flush()
        category = Category(store_id=store.id, slug="c", name="C")
        db_session.add(category)
        db_session.flush()
        db_session.add(Product(store_id=store.id, category_id=category.id, slug="p", name="P", price_cents=1))
        db_session.add(PackImport(started_at=utc_now(), status=PackImportStatus.running, source="cli",
                                  update_existing=True))
        create_user(db_session, "w@example.com", "A-Long-Enough-Passw0rd")
        db_session.commit()
    assert store.created_at is not None


def test_utc_now_is_naive_utc():
    now = utc_now()
    assert now.tzinfo is None
    assert abs(now.replace(tzinfo=timezone.utc).timestamp() - time.time()) < 5  # UTC, like the old default
