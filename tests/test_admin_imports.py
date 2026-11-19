"""Admin UI for the import records (/admin/imports): list, filter, pages, detail (report,
issues, exception). UI text is compared with the catalogs (``es_html(key)`` / ``en_html(key)``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from markupsafe import escape
from sqlalchemy import select

from app.models import PackImport, PackImportStatus, User
from app.routers import admin_imports
from app.routers.admin import MAX_PAGE
from app.services import store_pack_import
from app.services.pack_import_log import import_pack_bytes
from app.services.store_pack_validation import PACK_FILE_SUFFIX
from tests.ui_text import en_html, es_html

PACKS_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "packs"
CLOTHING = PACKS_DIR / f"clothing-demo{PACK_FILE_SUFFIX}"


def _admin(db) -> User:
    return db.scalar(select(User).where(User.email == "admin@example.com"))


def _import(db, raw: bytes, **kwargs):
    kwargs.setdefault("source", "upload")
    return import_pack_bytes(db, raw, **kwargs)


@pytest.fixture()
def default_lang_admin(default_lang_client):
    response = default_lang_client.post(
        "/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"}, follow_redirects=False
    )
    assert response.status_code == 303
    return default_lang_client


@pytest.fixture()
def three_records(admin_client, db_session):
    """#1 succeeded (upload by the admin), #2 invalid, #3 failed (unexpected error)."""
    admin = _admin(db_session)
    _import(db_session, CLOTHING.read_bytes(), filename=CLOTHING.name, user=admin)
    _import(db_session, b'{"version": 2}', filename="future.json", user=admin)
    original = store_pack_import._Importer.import_products

    def boom(self, store, report):
        raise RuntimeError("simulated <disruption>")

    store_pack_import._Importer.import_products = boom
    try:
        _import(db_session, CLOTHING.read_bytes(), filename="again.json", source="cli", update_existing=True)
    finally:
        store_pack_import._Importer.import_products = original
    return admin_client


# ---------------------------------------------------------------------------------- list


def test_list_is_spanish_by_default_and_in_the_menu(default_lang_admin, db_session):
    _import(db_session, CLOTHING.read_bytes(), filename=CLOTHING.name, user=_admin(db_session))
    page = default_lang_admin.get("/admin/imports")
    assert page.status_code == 200 and '<html lang="es">' in page.text
    assert es_html("admin.imports.title") in page.text and es_html("admin.imports.status.succeeded") in page.text
    assert es_html("admin.imports.source.upload") in page.text
    assert f'href="/admin/imports" aria-current="page">{es_html("admin.nav.imports")}</a>' in page.text
    assert es_html("admin.nav.imports") in default_lang_admin.get("/admin").text  # dashboard link


def test_list_shows_every_attempt_latest_first(three_records, db_session):
    html = three_records.get("/admin/imports").text
    assert html.index('href="/admin/imports/3"') < html.index('href="/admin/imports/2"') < html.index(
        'href="/admin/imports/1"')
    for status in ("succeeded", "invalid", "failed"):
        assert en_html(f"admin.imports.status.{status}") in html
    assert en_html("admin.imports.source.cli") in html and "admin@example.com" in html
    assert CLOTHING.name in html and "future.json" in html and "<code>clothing-demo</code>" in html


def test_list_can_be_filtered_by_status(three_records):
    html = three_records.get("/admin/imports?status=invalid").text
    assert 'href="/admin/imports/2"' in html
    assert 'href="/admin/imports/1"' not in html and 'href="/admin/imports/3"' not in html
    assert '<option value="invalid" selected>' in html
    # An unknown status shows everything instead of failing.
    assert 'href="/admin/imports/1"' in three_records.get("/admin/imports?status=bogus").text


def test_list_is_paginated(admin_client, db_session, monkeypatch):
    monkeypatch.setattr(admin_imports, "PAGE_SIZE", 2)
    for _ in range(5):
        _import(db_session, b"{}")
    first = admin_client.get("/admin/imports").text
    assert 'href="/admin/imports/5"' in first and 'href="/admin/imports/4"' in first
    assert 'href="/admin/imports/3"' not in first
    assert f'href="/admin/imports?page=2">{en_html("admin.imports.older")}</a>' in first
    assert en_html("admin.imports.newer") not in first

    last = admin_client.get("/admin/imports?page=3").text
    assert 'href="/admin/imports/1"' in last and en_html("admin.imports.older") not in last
    assert f'href="/admin/imports?page=2">{en_html("admin.imports.newer")}</a>' in last
    assert 'href="/admin/imports/5"' in admin_client.get("/admin/imports?page=abc").text  # bad page -> 1


@pytest.mark.parametrize("page", ["99999999999999999999", "-99999999999999999999", str(MAX_PAGE + 1)])
def test_huge_page_numbers_never_fail(admin_client, db_session, page):
    """A page number too large for the database's OFFSET used to be a 500 (OverflowError): it is capped."""
    _import(db_session, b"{}")
    response = admin_client.get(f"/admin/imports?page={page}")
    assert response.status_code == 200
    if page.startswith("-"):  # below 1: page 1
        assert 'href="/admin/imports/1"' in response.text
    else:  # capped to the last allowed page: empty, with a way back
        assert 'href="/admin/imports/1"' not in response.text
        assert f'href="/admin/imports?page={MAX_PAGE - 1}">{en_html("admin.imports.newer")}</a>' in response.text


def test_pagination_keeps_the_status_filter(admin_client, db_session, monkeypatch):
    monkeypatch.setattr(admin_imports, "PAGE_SIZE", 1)
    for _ in range(2):
        _import(db_session, b"{}")
    assert 'href="/admin/imports?page=2&amp;status=invalid"' in admin_client.get("/admin/imports?status=invalid").text


def test_empty_list(admin_client):
    assert en_html("admin.imports.empty") in admin_client.get("/admin/imports").text


# -------------------------------------------------------------------------------- detail


def test_detail_of_a_successful_import(three_records, db_session):
    record = db_session.get(PackImport, 1)
    html = three_records.get("/admin/imports/1").text
    assert en_html("admin.imports.record_title", id=1) in html and en_html("admin.imports.status.succeeded") in html
    assert record.file_sha256 in html and "admin@example.com" in html
    assert 'href="/admin/stores/clothing-demo"' in html and 'href="/stores/clothing-demo"' in html
    assert en_html("admin.imports.field.store_created") in html
    report = record.report
    assert f"<td>{report['properties']['created']}</td>" in html
    assert en_html("admin.imports.report.values", written=report["values_written"], removed=0) in html
    assert en_html("admin.imports.exception") not in html


def test_detail_of_an_invalid_pack_lists_its_issues(three_records, db_session):
    (issue,) = db_session.get(PackImport, 2).issues
    html = three_records.get("/admin/imports/2").text
    assert en_html("admin.imports.issues") in html
    assert f"<code>{escape(issue['path'])}</code>: {escape(issue['message'])}" in html


def test_detail_of_an_unexpected_error_shows_the_exception_escaped(three_records, db_session):
    html = three_records.get("/admin/imports/3").text
    assert en_html("admin.imports.status.failed") in html and en_html("admin.imports.failed_hint") in html
    assert en_html("admin.imports.exception") in html
    assert "RuntimeError: simulated &lt;disruption&gt;" in html and "<disruption>" not in html
    assert "Traceback (most recent call last)" in html


def test_detail_of_an_unfinished_import(admin_client, db_session):
    db_session.add(PackImport(started_at=db_session.get(User, 1).created_at, status=PackImportStatus.running,
                              source="cli"))
    db_session.commit()
    html = admin_client.get("/admin/imports/1").text
    assert en_html("admin.imports.status.running") in html and en_html("admin.imports.running_hint") in html


def test_detail_is_spanish_by_default(default_lang_admin, db_session):
    _import(db_session, b'{"version": 2}')
    html = default_lang_admin.get("/admin/imports/1").text
    assert es_html("admin.imports.record_title", id=1) in html and es_html("admin.imports.status.invalid") in html


@pytest.mark.parametrize("record_id", ["999", "99999999999999999999", "-99999999999999999999"])
def test_unknown_record_is_404(admin_client, record_id):
    assert admin_client.get(f"/admin/imports/{record_id}").status_code == 404  # huge: no longer a 500


def test_pages_require_an_admin(client, db_session):
    _import(db_session, b"{}")
    for url in ("/admin/imports", "/admin/imports/1"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")


# ------------------------------------------------------------------- link from uploads


def test_upload_error_links_to_its_record(admin_client):
    response = admin_client.post("/admin/packs", files={"pack": ("bad.json", b"not json", "application/json")})
    assert response.status_code == 400
    assert f'href="/admin/imports/1">{en_html("admin.packs.view_record", id=1)}</a>' in response.text
    assert en_html("admin.imports.status.invalid") in admin_client.get("/admin/imports/1").text


def test_upload_without_a_file_has_no_record_link(admin_client):
    response = admin_client.post("/admin/packs", data={})
    assert response.status_code == 400 and "/admin/imports/" not in response.text
