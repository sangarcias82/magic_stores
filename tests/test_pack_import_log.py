"""Import results in the database (``pack_imports``): one record per attempt, outside the import
transaction, with ``issues`` for expected failures and ``exception`` for unexpected ones."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import PackImport, PackImportStatus, Store, User
from app.routers import admin_packs
from app.services import store_pack_import
from app.services.pack_import_log import SOURCE_CLI, import_pack_bytes, recent_imports
from app.services.store_pack_validation import PACK_FILE_SUFFIX, validate_store_pack
from tests.ui_text import en_html

UPLOAD = "/admin/packs"
PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
CLOTHING = PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}"


def _pack() -> dict:
    return json.loads(CLOTHING.read_text(encoding="utf-8"))


def _bytes(pack: dict) -> bytes:
    return json.dumps(pack).encode("utf-8")


def _upload(client, content: bytes, *, update: bool = False, filename: str = "pack.json"):
    data = {"update_existing": "1"} if update else {}
    return client.post(UPLOAD, files={"pack": (filename, content, "application/json")}, data=data,
                       follow_redirects=False)


def _records(db) -> list[PackImport]:
    db.expire_all()
    return list(db.scalars(select(PackImport).order_by(PackImport.id)))


def _stores(db) -> int:
    return db.scalar(select(func.count()).select_from(Store))


@pytest.fixture()
def disruption(monkeypatch):
    """Make the import fail unexpectedly halfway (after the store and categories are written
    in the transaction, before the products)."""
    def boom(self, store, report):
        raise RuntimeError("simulated disruption")

    monkeypatch.setattr(store_pack_import._Importer, "import_products", boom)


# --------------------------------------------------------------------------- successes


def test_successful_upload_is_recorded(admin_client, db_session):
    raw = CLOTHING.read_bytes()
    pack = json.loads(raw)
    assert _upload(admin_client, raw, filename=CLOTHING.name).status_code == 303

    (record,) = _records(db_session)
    admin = db_session.scalar(select(User).where(User.email == "admin@example.com"))
    store = db_session.scalar(select(Store).where(Store.slug == pack["store"]["slug"]))
    assert record.status == PackImportStatus.succeeded
    assert (record.source, record.filename, record.user_id) == ("upload", CLOTHING.name, admin.id)
    assert (record.file_bytes, record.file_sha256) == (len(raw), hashlib.sha256(raw).hexdigest())
    assert (record.store_slug, record.store_id, record.store_created) == (store.slug, store.id, True)
    assert record.update_existing is False
    assert record.started_at <= record.finished_at
    definitions = pack["categories"][0]["property_definitions"]
    assert record.report["categories"] == {"created": 1, "updated": 0}
    assert record.report["properties"] == {"created": len(definitions), "updated": 0}
    assert record.report["products"] == {"created": 1, "updated": 0}
    assert record.report["values_written"] == len(pack["products"][0]["values"])
    assert record.issues is None and record.exception is None


def test_update_is_recorded_as_not_created(admin_client, db_session):
    raw = CLOTHING.read_bytes()
    _upload(admin_client, raw)
    _upload(admin_client, raw, update=True)
    first, second = _records(db_session)
    assert (first.store_created, second.store_created, second.update_existing) == (True, False, True)
    assert second.report["products"] == {"created": 0, "updated": 1}


# ---------------------------------------------------------------- expected failures


def test_invalid_pack_is_recorded_with_its_issues(admin_client, db_session):
    pack = _pack()
    del pack["store"]["slug"]
    pack["version"] = 1
    response = _upload(admin_client, _bytes(pack))
    assert response.status_code == 400

    (record,) = _records(db_session)
    expected = [{"path": i.path, "message": i.message} for i in validate_store_pack(pack)]
    assert record.status == PackImportStatus.invalid and record.issues == expected
    assert record.store_slug is None and record.exception is None and record.report is None
    assert record.finished_at is not None and _stores(db_session) == 0


def test_not_json_is_recorded_as_invalid(admin_client, db_session):
    _upload(admin_client, b"not json")
    (record,) = _records(db_session)
    assert record.status == PackImportStatus.invalid
    assert record.issues == [{"path": "", "message": "not valid JSON: Expecting value (line 1, column 1)"}]


def test_too_large_file_is_recorded_without_reading_it(admin_client, db_session, monkeypatch):
    monkeypatch.setattr(admin_packs, "MAX_PACK_BYTES", 100)
    assert _upload(admin_client, CLOTHING.read_bytes()).status_code == 413
    (record,) = _records(db_session)
    assert record.status == PackImportStatus.too_large
    assert record.file_bytes is None and record.file_sha256 is None and record.store_slug is None


def test_refused_update_is_recorded(admin_client, db_session):
    raw = CLOTHING.read_bytes()
    _upload(admin_client, raw)
    assert _upload(admin_client, raw).status_code == 409
    record = _records(db_session)[-1]
    assert record.status == PackImportStatus.refused and record.store_slug == _pack()["store"]["slug"]
    assert record.exception is None


def test_conflict_is_recorded_with_its_issues(admin_client, db_session):
    pack = _pack()
    slug = pack["store"]["slug"]
    _upload(admin_client, _bytes(pack))
    shirts_id = admin_client.get(f"/api/stores/{slug}/categories").json()[0]["id"]
    admin_client.post(f"/admin/stores/{slug}/products", data={
        "category_id": str(shirts_id), "slug": "admin-tee", "sku": "ADMIN-TEE", "stock": "3", "name": "Admin tee",
        "price": "5", "prop_size": "M", "prop_color": "Navy", "prop_material": "cotton"})
    pack["categories"][0]["property_definitions"][2]["prop_type"] = "number"  # material
    pack["products"][0]["values"]["material"] = 100

    assert _upload(admin_client, _bytes(pack), update=True).status_code == 409
    record = _records(db_session)[-1]
    assert record.status == PackImportStatus.conflict and record.exception is None
    assert [i["path"] for i in record.issues] == ["categories[0].property_definitions[2].prop_type"]


def test_upload_without_a_file_is_not_recorded(admin_client, db_session):
    assert admin_client.post(UPLOAD, data={}).status_code == 400
    assert _records(db_session) == []


# -------------------------------------------------------------- unexpected disruption


def test_unexpected_error_is_recorded_with_the_exception(admin_client, db_session, disruption):
    response = _upload(admin_client, CLOTHING.read_bytes())
    (record,) = _records(db_session)

    assert response.status_code == 500
    assert en_html("admin.packs.error.unexpected", record=record.id) in response.text
    assert "Traceback" not in response.text  # details stay in the record
    assert record.status == PackImportStatus.failed and record.finished_at is not None
    assert record.exception.startswith("RuntimeError: simulated disruption\n")
    assert "Traceback (most recent call last)" in record.exception and "import_products" in record.exception
    assert record.store_slug == _pack()["store"]["slug"] and record.store_id is None and record.report is None
    assert _stores(db_session) == 0  # the store written before the error was rolled back


def test_unexpected_error_leaves_an_existing_store_untouched(admin_client, db_session, monkeypatch):
    pack = _pack()
    slug = pack["store"]["slug"]
    _upload(admin_client, _bytes(pack))
    before = admin_client.get(f"/api/stores/{slug}").json(), admin_client.get(f"/api/stores/{slug}/categories").json()

    def boom(self, store, report):
        raise RuntimeError("simulated disruption")

    monkeypatch.setattr(store_pack_import._Importer, "import_products", boom)
    pack["store"]["name"] = "Should not be saved"
    pack["categories"][0]["name"] = "Should not be saved either"
    assert _upload(admin_client, _bytes(pack), update=True).status_code == 500
    after = admin_client.get(f"/api/stores/{slug}").json(), admin_client.get(f"/api/stores/{slug}/categories").json()
    assert after == before
    assert [r.status for r in _records(db_session)] == [PackImportStatus.succeeded, PackImportStatus.failed]


def test_unexpected_error_is_logged(db_session, disruption, caplog):
    with caplog.at_level(logging.ERROR, logger="app.services.pack_import_log"):
        outcome = import_pack_bytes(db_session, CLOTHING.read_bytes(), source="upload")
    assert outcome.status == PackImportStatus.failed and not outcome.ok
    assert "store pack import failed unexpectedly" in caplog.text


class ProcessStopped(BaseException):
    """Stands for the process being stopped mid-import (not an ordinary error)."""


def test_import_stopped_midway_stays_running(db_session, monkeypatch):
    def stop(self, store, report):
        raise ProcessStopped()

    monkeypatch.setattr(store_pack_import._Importer, "import_products", stop)
    with pytest.raises(ProcessStopped):
        import_pack_bytes(db_session, CLOTHING.read_bytes(), source="upload")
    (record,) = _records(db_session)
    assert record.status == PackImportStatus.running and record.finished_at is None
    assert _stores(db_session) == 0


def test_failing_to_record_the_result_does_not_undo_the_import(db_session, monkeypatch, caplog):
    def broken_get(*args, **kwargs):
        raise RuntimeError("record storage unavailable")

    monkeypatch.setattr(db_session, "get", broken_get)
    with caplog.at_level(logging.ERROR, logger="app.services.pack_import_log"):
        outcome = import_pack_bytes(db_session, CLOTHING.read_bytes(), source="upload")
    monkeypatch.undo()

    assert outcome.ok and _stores(db_session) == 1
    assert f"could not record the result of store pack import #{outcome.record_id}" in caplog.text
    (record,) = _records(db_session)
    assert record.status == PackImportStatus.running  # the attempt is still visible


# ------------------------------------------------------------------------ other sources


def test_command_line_imports_are_recorded(engine, db_session, capsys):
    from sqlalchemy.orm import sessionmaker

    assert store_pack_import.main([str(CLOTHING)], sessionmaker(bind=engine, autoflush=False)) == 0
    (record,) = _records(db_session)
    assert (record.source, record.filename, record.user_id, record.update_existing) == (
        SOURCE_CLI, str(CLOTHING), None, True
    )
    assert record.status == PackImportStatus.succeeded
    assert f"imported (record #{record.id})" in capsys.readouterr().out


def test_command_line_reports_unexpected_errors(engine, capsys, disruption):
    from sqlalchemy.orm import sessionmaker

    assert store_pack_import.main([str(CLOTHING)], sessionmaker(bind=engine, autoflush=False)) == 1
    out = capsys.readouterr().out
    assert f"{CLOTHING}: FAILED, unexpected error (details in record #1)" in out
    assert "  RuntimeError: simulated disruption" in out


def test_recent_imports_lists_the_latest_first(db_session):
    for _ in range(3):
        import_pack_bytes(db_session, b"{}", source="upload")
    assert [r.id for r in recent_imports(db_session, limit=2)] == [3, 2]
