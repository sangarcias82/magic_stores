"""Files that can't be installed *after* the database commit (disk full, a folder changed since it
was checked): the catalog is imported, so the import must say so (status ``succeeded``) and report
the files as problems, never "nothing was imported". Staged copies are always cleaned up."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.models import PackImport, PackImportStatus, Store
from app.services import store_pack_import
from app.services.pack_files import PackFilesError
from app.services.pack_import_log import SOURCE_CLI, import_pack_bytes
from app.services.store_pack_import import STOREFRONT_NOT_COPIED, import_store_pack_file
from tests.ui_text import en_html, ui

PROJECT = Path(__file__).resolve().parent.parent
SOLARES = PROJECT / "fixtures" / "packs" / "solares" / "solares.store-pack.v1.json"
SOLARES_IMAGES = sorted(p.name for p in (SOLARES.parent / "media").iterdir())


def _disk_full(staged):
    raise OSError(28, "No space left on device")


def _stores(db) -> int:
    return db.scalar(select(func.count()).select_from(Store))


def test_images_failing_after_the_commit_are_a_problem_not_a_failure(db_session, content_root, storefronts,
                                                                      monkeypatch):
    monkeypatch.setattr(store_pack_import, "publish_media", _disk_full)
    report = import_store_pack_file(db_session, SOLARES)

    assert _stores(db_session) == 1 and report.products.created == 5  # the catalog is imported
    assert report.media_files is None
    assert report.file_problems == ["media/: images not published: [Errno 28] No space left on device"]
    assert report.missing_media == [f"/content/solares/{name}" for name in SOLARES_IMAGES]  # would be broken
    assert "problem: media/: images not published: [Errno 28] No space left on device" in report.summary()
    assert report.storefront_files == 4  # the other part still installed
    assert list(content_root.iterdir()) == []  # staging removed


def test_storefront_failing_after_the_commit_is_a_problem_not_a_failure(db_session, content_root, storefronts,
                                                                        monkeypatch):
    def changed(staged):
        raise PackFilesError("the storefronts folder changed")

    monkeypatch.setattr(store_pack_import, "publish_storefront", changed)
    report = import_store_pack_file(db_session, SOLARES)

    assert _stores(db_session) == 1 and report.storefront_files is None
    assert report.file_problems == [
        "storefront.copy_from_pack_dir: storefront folder not installed: the storefronts folder changed"
    ]
    assert STOREFRONT_NOT_COPIED not in report.skipped  # it had its folder: not "imported without its folder"
    assert report.media_files == 6 and report.missing_media == []
    assert sorted(p.name for p in storefronts.iterdir()) == ["_default"]  # staging removed


def test_recorded_as_succeeded_with_the_problem(engine, content_root, storefronts, monkeypatch):
    monkeypatch.setattr(store_pack_import, "publish_media", _disk_full)
    db = sessionmaker(bind=engine)()
    outcome = import_pack_bytes(db, SOLARES.read_bytes(), source=SOURCE_CLI, filename=str(SOLARES),
                                pack_dir=SOLARES.parent)
    assert outcome.status == PackImportStatus.succeeded and outcome.exception is None
    record = db.scalar(select(PackImport))
    assert record.status == PackImportStatus.succeeded
    assert record.report["file_problems"] == ["media/: images not published: [Errno 28] No space left on device"]
    assert len(record.report["missing_media"]) == 6


def test_admin_zip_upload_says_imported_and_shows_the_problem(admin_client, content_root, storefronts, monkeypatch):
    monkeypatch.setattr(store_pack_import, "publish_media", _disk_full)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path in sorted(SOLARES.parent.rglob("*")):
            archive.write(path, f"solares/{path.relative_to(SOLARES.parent).as_posix()}")
    response = admin_client.post("/admin/packs", files={"pack": ("solares.zip", buffer.getvalue(), "application/zip")},
                                 follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/admin/stores/solares"
    page = admin_client.get(response.headers["location"]).text
    problem = "media/: images not published: [Errno 28] No space left on device"
    assert en_html("flash.pack_files_problem", problems=problem) in page
    assert en_html("flash.pack_created", name="Solares", slug="solares", categories=2, properties=9, products=5) in page


@pytest.mark.parametrize("lang", ["en", "es"])
def test_problem_message_is_translated(lang):
    text = ui(lang, "flash.pack_files_problem", problems="X")  # the key must exist in both catalogs
    assert "X" in text and "{" not in text
