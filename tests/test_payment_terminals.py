"""Mock payment terminals per store (app/models.py PaymentTerminal, app/services/payments.py,
GET /api/stores/{slug}/payment/terminals)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.main import create_app
from app.models import PaymentTerminal, Store
from app.services.payments import ensure_default_terminals
from app.services.store_pack_import import import_store_pack

PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
URL = "/api/stores/{slug}/payment/terminals"

BOTH_ENABLED = [
    {"code": "mock_card", "name": "Mock card", "kind": "mock", "enabled": True, "sort_order": 0},
    {"code": "mock_transfer", "name": "Mock bank transfer", "kind": "mock", "enabled": True, "sort_order": 1},
]


def _terminals(client, slug: str) -> list[dict]:
    response = client.get(URL.format(slug=slug))
    assert response.status_code == 200, response.text
    return response.json()


def _disable(db_session, slug: str, code: str) -> None:
    store_id = db_session.scalar(select(Store.id).where(Store.slug == slug))
    db_session.execute(
        update(PaymentTerminal)
        .where(PaymentTerminal.store_id == store_id, PaymentTerminal.code == code)
        .values(is_enabled=False)
    )
    db_session.commit()


# --- The two required cases -----------------------------------------------------------------


def test_fixture_store_has_two_terminals(client, clothing_fixture):
    assert _terminals(client, "clothing-demo") == BOTH_ENABLED


def test_disabled_terminal_is_listed_and_marked(client, db_session, clothing_fixture):
    _disable(db_session, "clothing-demo", "mock_transfer")

    terminals = {t["code"]: t for t in _terminals(client, "clothing-demo")}

    assert terminals["mock_transfer"]["enabled"] is False  # still listed, clearly off
    assert terminals["mock_card"]["enabled"] is True


# --- Every way a store comes to exist gets them -------------------------------------------------


def test_store_created_in_admin_has_terminals(admin_client, demo_store):
    assert _terminals(admin_client, demo_store) == BOTH_ENABLED


def test_store_imported_from_a_pack_has_terminals(client, db_session):
    pack = json.loads((PACKS_DIR / "clothing-demo.store-pack.v1.json").read_text(encoding="utf-8"))
    import_store_pack(db_session, pack)
    assert _terminals(client, pack["store"]["slug"]) == BOTH_ENABLED


def test_reimporting_a_pack_keeps_terminals_as_they_are(client, db_session):
    pack = json.loads((PACKS_DIR / "clothing-demo.store-pack.v1.json").read_text(encoding="utf-8"))
    import_store_pack(db_session, pack)
    _disable(db_session, "clothing-demo", "mock_card")

    import_store_pack(db_session, pack)  # updates the existing store: no new insert, no new terminals

    assert [(t["code"], t["enabled"]) for t in _terminals(client, "clothing-demo")] == [
        ("mock_card", False),
        ("mock_transfer", True),
    ]


def test_startup_adds_missing_terminals_to_existing_stores(engine, db_session, clothing_fixture):
    # A store from before terminals existed: it has none, or only some of them.
    store_id = clothing_fixture.id  # the fixture returns the Store
    db_session.execute(delete(PaymentTerminal).where(PaymentTerminal.code == "mock_transfer"))
    db_session.commit()
    _disable(db_session, "clothing-demo", "mock_card")

    sessions = sessionmaker(bind=engine, autoflush=False)
    app = create_app()

    def override():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as started:  # the lifespan runs ensure_default_terminals
        terminals = _terminals(started, "clothing-demo")

    assert [(t["code"], t["enabled"]) for t in terminals] == [("mock_card", False), ("mock_transfer", True)]
    assert db_session.scalar(select(PaymentTerminal.store_id).where(PaymentTerminal.code == "mock_transfer")) == store_id
    assert ensure_default_terminals(db_session) == 0  # idempotent


def test_unknown_store_is_404(client):
    assert client.get(URL.format(slug="nope")).status_code == 404


def test_deleting_a_store_deletes_its_terminals(db_session, clothing_fixture):
    store = db_session.scalar(select(Store).where(Store.slug == "clothing-demo"))
    db_session.delete(store)
    db_session.commit()
    assert db_session.scalars(select(PaymentTerminal)).all() == []
