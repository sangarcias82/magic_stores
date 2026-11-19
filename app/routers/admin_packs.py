"""Admin: upload a store pack (.json, contract v1) -> validate -> import.

A .zip of the pack folder is accepted too: it is unpacked safely (app/services/pack_archive.py) and
imported from that folder, so its storefront/ folder is copied and its media/ images are published
to the content app, like the command line does. A .json alone has no folder: nothing to copy or
publish, and the success message lists the pack's images that are missing from the content folder.

Nothing is written unless the whole pack is valid and imports cleanly (the import runs in one
transaction). Every upload of a file is recorded in ``pack_imports`` with its result, including
the exception of an unexpected error (app/services/pack_import_log.py). A pack whose store already exists is only applied when the admin asks for it
("update existing"), because re-import overwrites what the pack names (see
fixtures/packs/README.md).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import translator
from app.models import PackImportStatus, User
from app.routers.admin import FLASH_KEY, admin_title, render_admin
from app.services.pack_import_log import SOURCE_UPLOAD, import_pack_bytes
from app.services.store_pack_import import STOREFRONT_NOT_COPIED
from app.services.store_pack_validation import PackIssue

router = APIRouter(prefix="/admin/packs", tags=["admin"], dependencies=[Depends(require_admin)])

MAX_PACK_MB = 5
MAX_PACK_BYTES = MAX_PACK_MB * 1024 * 1024
MAX_ARCHIVE_MB = 50  # a .zip of the pack folder (with its images)
MAX_ARCHIVE_BYTES = MAX_ARCHIVE_MB * 1024 * 1024
ZIP_MAGIC = b"PK\x03\x04"
MISSING_SHOWN = 5  # missing image paths listed in the message (then "+N more")
# 413 by number: Starlette renamed HTTP_413_REQUEST_ENTITY_TOO_LARGE (deprecated, warns) to
# HTTP_413_CONTENT_TOO_LARGE, which older Starlette versions allowed by requirements.txt lack.
HTTP_413_CONTENT_TOO_LARGE = 413


def _upload_page(request: Request, *, error: Optional[str] = None, issues: Optional[list[PackIssue]] = None,
                 update_existing: bool = False, record_id: Optional[int] = None,
                 status_code: int = 200) -> HTMLResponse:
    t = translator(request)
    return render_admin(
        request,
        "admin/pack_upload.html",
        {
            "page_title": admin_title(t, t("admin.packs.title")),
            "section": "packs",
            "error": error,
            "issues": issues or [],
            "update_existing": update_existing,
            "record_id": record_id,  # import record of a failed attempt (link to its details)
            "max_mb": MAX_PACK_MB,
            "zip_mb": MAX_ARCHIVE_MB,
        },
        status_code=status_code,
    )


@router.get("", response_class=HTMLResponse)
def pack_upload_form(request: Request):
    return _upload_page(request)


@router.post("", response_class=HTMLResponse)
def pack_upload(
    request: Request,
    pack: Optional[UploadFile] = File(None),
    update_existing: Optional[str] = Form(None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    t = translator(request)
    update = update_existing is not None

    outcome = None

    def fail(error: str, status_code: int, issues: Optional[list[PackIssue]] = None) -> HTMLResponse:
        return _upload_page(request, error=error, issues=issues, update_existing=update, status_code=status_code,
                            record_id=outcome.record_id if outcome is not None else None)

    # A browser sends an empty part when no file is chosen; FastAPI turns it into None. The
    # filename check is a safety net for clients / versions that pass an empty UploadFile.
    if pack is None or not pack.filename:
        return fail(t("admin.packs.error.no_file"), status.HTTP_400_BAD_REQUEST)  # nothing to record
    is_zip = pack.file.read(len(ZIP_MAGIC)) == ZIP_MAGIC or pack.filename.lower().endswith(".zip")
    pack.file.seek(0)
    limit, limit_mb = (MAX_ARCHIVE_BYTES, MAX_ARCHIVE_MB) if is_zip else (MAX_PACK_BYTES, MAX_PACK_MB)
    raw = pack.file.read(limit + 1)  # never more than the limit in memory

    # Validate + import, recorded in pack_imports (see app/services/pack_import_log.py).
    outcome = import_pack_bytes(db, raw, source=SOURCE_UPLOAD, filename=pack.filename, user=user,
                                update_existing=update, max_bytes=limit, archive=is_zip,
                                max_pack_bytes=MAX_PACK_BYTES)
    result = outcome.status
    if result == PackImportStatus.too_large:
        return fail(t("admin.packs.error.too_large", max_mb=limit_mb), HTTP_413_CONTENT_TOO_LARGE)
    if result == PackImportStatus.invalid:
        return fail(t("admin.packs.error.invalid"), status.HTTP_400_BAD_REQUEST, outcome.issues)
    if result == PackImportStatus.refused:
        slug = outcome.pack["store"]["slug"]
        if outcome.previous_record_id is not None:  # the very same file is already applied
            return fail(t("admin.packs.error.same_file", slug=slug, record=outcome.previous_record_id),
                        status.HTTP_409_CONFLICT)
        return fail(t("admin.packs.error.exists", slug=slug), status.HTTP_409_CONFLICT)
    if result == PackImportStatus.conflict:
        return fail(t("admin.packs.error.conflict"), status.HTTP_409_CONFLICT, outcome.issues)
    if result != PackImportStatus.succeeded:  # failed: unexpected error, details in the record
        return fail(t("admin.packs.error.unexpected", record=outcome.record_id), status.HTTP_500_INTERNAL_SERVER_ERROR)

    report = outcome.report
    counts = {
        "name": outcome.pack["store"]["name"],
        "slug": report.store_slug,
        "categories": report.categories.created + report.categories.updated,
        "properties": report.properties.created + report.properties.updated,
        "products": report.products.created + report.products.updated,
    }
    message = t("flash.pack_created" if report.store_created else "flash.pack_updated", **counts)
    if STOREFRONT_NOT_COPIED in report.skipped:  # a .json upload has no folder to copy from
        message = f"{message} {t('flash.pack_storefront_skipped')}"
    if report.file_problems:  # imported, but some files could not be installed afterwards
        message = f"{message} {t('flash.pack_files_problem', problems='; '.join(report.file_problems))}"
    if report.missing_media:  # would be broken images on the storefront
        shown = report.missing_media[:MISSING_SHOWN]
        more = len(report.missing_media) - len(shown)
        paths = ", ".join(shown) + (f" (+{more})" if more else "")
        message = f"{message} {t('flash.pack_media_missing', count=len(report.missing_media), paths=paths)}"
    request.session[FLASH_KEY] = message
    return RedirectResponse(f"/admin/stores/{report.store_slug}", status_code=status.HTTP_303_SEE_OTHER)
