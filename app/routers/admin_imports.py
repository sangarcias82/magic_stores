"""Admin: store pack import records (table ``pack_imports``), read-only.

- ``/admin/imports``: every attempt, latest first, filterable by status, 50 per page.
- ``/admin/imports/<id>``: one attempt: file, store, user, counts, issues, and the exception
  of an unexpected error.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.i18n import Translator, translator
from app.models import PackImportStatus
from app.routers.admin import admin_title, parse_page, render_admin
from app.services.pack_import_log import SOURCE_CLI, SOURCE_STARTUP, SOURCE_UPLOAD, get_import, list_imports

router = APIRouter(prefix="/admin/imports", tags=["admin"], dependencies=[Depends(require_admin)])

PAGE_SIZE = 50


def status_labels(t: Translator) -> dict[PackImportStatus, str]:
    return {
        PackImportStatus.succeeded: t("admin.imports.status.succeeded"),
        PackImportStatus.invalid: t("admin.imports.status.invalid"),
        PackImportStatus.too_large: t("admin.imports.status.too_large"),
        PackImportStatus.refused: t("admin.imports.status.refused"),
        PackImportStatus.conflict: t("admin.imports.status.conflict"),
        PackImportStatus.failed: t("admin.imports.status.failed"),
        PackImportStatus.running: t("admin.imports.status.running"),
    }


def source_labels(t: Translator) -> dict[str, str]:
    return {SOURCE_UPLOAD: t("admin.imports.source.upload"), SOURCE_CLI: t("admin.imports.source.cli"),
            SOURCE_STARTUP: t("admin.imports.source.startup")}


def _parse_status(raw: Optional[str]) -> Optional[PackImportStatus]:
    try:
        return PackImportStatus(raw) if raw else None
    except ValueError:
        return None  # unknown filter value: show everything


@router.get("", response_class=HTMLResponse)
def import_list(request: Request, status: Optional[str] = None, page: Optional[str] = None,
                db: Session = Depends(get_db)):
    t = translator(request)
    selected = _parse_status(status)
    page_number = parse_page(page)
    rows = list_imports(db, status=selected, offset=(page_number - 1) * PAGE_SIZE, limit=PAGE_SIZE + 1)
    return render_admin(
        request,
        "admin/imports.html",
        {
            "page_title": admin_title(t, t("admin.imports.title")),
            "section": "imports",
            "records": rows[:PAGE_SIZE],
            "has_older": len(rows) > PAGE_SIZE,
            "page": page_number,
            "selected_status": selected.value if selected else "",
            "status_labels": status_labels(t),
            "source_labels": source_labels(t),
        },
    )


@router.get("/{record_id}", response_class=HTMLResponse)
def import_detail(record_id: int, request: Request, db: Session = Depends(get_db)):
    record = get_import(db, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Import record not found")
    t = translator(request)
    return render_admin(
        request,
        "admin/import_detail.html",
        {
            "page_title": admin_title(t, t("admin.imports.record_title", id=record.id)),
            "section": "imports",
            "record": record,
            "status_labels": status_labels(t),
            "source_labels": source_labels(t),
        },
    )
