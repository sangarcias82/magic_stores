"""Store pack imports with a result record (table ``pack_imports``, model ``PackImport``).

``import_pack_bytes`` is the one pipeline behind the admin upload and the command line:

    size check -> strict parse + validation -> "store exists" guard -> import (one transaction)

and it records every attempt:

1. A ``running`` row is committed *before* the import starts.
2. The import runs in its own transaction (all-or-nothing, see store_pack_import).
3. The row is finished in a new transaction with the outcome:

   ============  ====================================================  =====================
   status        when                                                  details in
   ============  ====================================================  =====================
   succeeded     imported                                              ``report`` (counts)
   invalid       not a valid pack (JSON / contract)                    ``issues``
   too_large     file over the size limit                              -
   refused       the store exists and updating was not allowed         -
   conflict      valid pack that conflicts with the current data       ``issues``
   failed        unexpected error (a bug, the database going away...)  ``exception``
   running       never finished: the process stopped mid-import        -
   ============  ====================================================  =====================

Because the record is written outside the import transaction, it survives the import's
rollback. ``exception`` holds "Type: message" and the traceback of an unexpected error; the
import itself was rolled back, so nothing of it was written. Expected failures never fill it.

If the record itself can't be written (e.g. the database is down), the import result is still
returned and the problem is logged with ``logging`` instead.
"""

from __future__ import annotations

import hashlib
import logging
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Union

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import fits_db_integer
from app.models import PackImport, PackImportStatus, Store, User, utc_now
from app.services.pack_archive import extract_pack_archive
from app.services.store_pack_validation import PackIssue, StorePackError, parse_store_pack
from app.services.store_pack_import import ImportReport, StorePackImportError, import_store_pack

logger = logging.getLogger(__name__)

EXCEPTION_MAX_CHARS = 20_000
SOURCE_UPLOAD = "upload"
SOURCE_CLI = "cli"
SOURCE_STARTUP = "startup"  # SEED_PACKS, loaded when the app starts (app/services/pack_seed.py)


@dataclass
class ImportOutcome:
    """What happened: the result ``record`` plus, when available, the parsed pack and the
    import report. ``record_id`` stays usable even if the record object is expired."""

    record_id: Optional[int]
    status: PackImportStatus
    pack: Optional[dict[str, Any]] = None
    report: Optional[ImportReport] = None
    issues: Optional[list[PackIssue]] = None
    exception: Optional[str] = None
    # For "refused": the earlier successful import of this very file (same SHA-256) into the
    # same store, if any - i.e. a re-upload of a pack that is already applied.
    previous_record_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == PackImportStatus.succeeded


def report_counts(report: ImportReport) -> dict[str, Any]:
    """JSON-friendly summary of an import report (stored in ``PackImport.report``)."""
    return {
        "store_created": report.store_created,
        "categories": {"created": report.categories.created, "updated": report.categories.updated},
        "properties": {"created": report.properties.created, "updated": report.properties.updated},
        "products": {"created": report.products.created, "updated": report.products.updated},
        "shipping_methods": {"created": report.shipping_methods.created, "updated": report.shipping_methods.updated},
        "payment_terminals": {"created": report.payment_terminals.created, "updated": report.payment_terminals.updated},
        "values_written": report.values_written,
        "values_removed": report.values_removed,
        "translations_written": report.translations_written,
        "stock_set": report.stock_set,  # products whose stock the pack changed
        "stock_unchanged": report.stock_unchanged,  # products that already had the pack's stock
        "skipped": list(report.skipped),
        "storefront_files": report.storefront_files,  # None: no storefront folder copied
        "media_files": report.media_files,  # None: no media/ images published
        "missing_media": list(report.missing_media),  # /content/... images the pack uses that aren't there
        "file_problems": list(report.file_problems),  # files not installed after the commit
    }


def format_exception(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}\n" + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(text) > EXCEPTION_MAX_CHARS:
        text = text[: EXCEPTION_MAX_CHARS - 20] + "\n... (truncated)"
    return text


# ------------------------------------------------------------------------------- pipeline


def import_pack_bytes(
    db: Session,
    raw: bytes,
    *,
    source: str,
    filename: Optional[str] = None,
    user: Optional[User] = None,
    update_existing: bool = True,
    max_bytes: Optional[int] = None,
    pack_dir: Union[str, Path, None] = None,
    archive: bool = False,
    max_pack_bytes: Optional[int] = None,
) -> ImportOutcome:
    """Validate and import the bytes of a pack file, recording the attempt in ``pack_imports``.

    ``update_existing=False`` refuses a pack whose store already exists. ``max_bytes``: reject
    larger files (pass at most ``max_bytes + 1`` bytes to detect that without reading it all).
    ``pack_dir``: the folder of the pack file (command line), so ``storefront.copy_from_pack_dir``
    can be copied and ``media/`` published. ``archive=True``: ``raw`` is a .zip of the pack folder
    (admin upload); it is unpacked into a temporary folder that becomes ``pack_dir``, and
    ``max_pack_bytes`` limits the pack file inside it (``max_bytes`` limits the zip). The record
    keeps the zip's size and hash.
    Unexpected errors don't propagate: they are recorded (status ``failed``) and returned."""
    too_large = max_bytes is not None and len(raw) > max_bytes
    sha256 = None if too_large else hashlib.sha256(raw).hexdigest()
    record = PackImport(
        started_at=utc_now(),
        status=PackImportStatus.running,
        source=source,
        filename=filename[:255] if filename else None,
        file_bytes=None if too_large else len(raw),
        file_sha256=sha256,
        update_existing=update_existing,
        user_id=user.id if user is not None else None,
    )
    db.add(record)
    db.commit()  # 1. the attempt exists before the import starts
    record_id = record.id

    outcome = _run(db, raw, too_large=too_large, update_existing=update_existing, sha256=sha256, pack_dir=pack_dir,
                   archive=archive, max_pack_bytes=max_pack_bytes)
    outcome.record_id = record_id
    _finish(db, record_id, outcome)  # 3. its own transaction, after the import's commit / rollback
    return outcome


def _run(db: Session, raw: bytes, *, too_large: bool, update_existing: bool, sha256: Optional[str],
         pack_dir: Union[str, Path, None] = None, archive: bool = False,
         max_pack_bytes: Optional[int] = None) -> ImportOutcome:
    if too_large:
        return ImportOutcome(None, PackImportStatus.too_large)
    if not archive:
        return _run_pack(db, raw, update_existing=update_existing, sha256=sha256, pack_dir=pack_dir)
    try:
        with extract_pack_archive(raw, max_pack_bytes=max_pack_bytes) as (pack_raw, folder):
            return _run_pack(db, pack_raw, update_existing=update_existing, sha256=sha256, pack_dir=folder)
    except StorePackError as exc:  # not a usable zip: recorded like any invalid pack
        return ImportOutcome(None, PackImportStatus.invalid, issues=exc.issues)
    except Exception as exc:  # e.g. the disk is full while unpacking
        logger.exception("store pack archive could not be unpacked")
        return ImportOutcome(None, PackImportStatus.failed, exception=format_exception(exc))


def _run_pack(db: Session, raw: bytes, *, update_existing: bool, sha256: Optional[str],
              pack_dir: Union[str, Path, None]) -> ImportOutcome:
    pack = None
    try:
        try:
            pack = parse_store_pack(raw)
        except StorePackError as exc:
            return ImportOutcome(None, PackImportStatus.invalid, issues=exc.issues)
        slug = pack["store"]["slug"]
        if not update_existing and db.scalar(select(Store.id).where(Store.slug == slug)) is not None:
            previous = db.scalar(
                select(PackImport.id)
                .where(PackImport.store_slug == slug, PackImport.file_sha256 == sha256,
                       PackImport.status == PackImportStatus.succeeded)
                .order_by(PackImport.id.desc())
                .limit(1)
            )
            return ImportOutcome(None, PackImportStatus.refused, pack=pack, previous_record_id=previous)
        try:
            report = import_store_pack(db, pack, pack_dir=pack_dir)  # 2. all-or-nothing
        except StorePackImportError as exc:
            return ImportOutcome(None, PackImportStatus.conflict, pack=pack, issues=exc.issues)
    except Exception as exc:  # unexpected: the import rolled back; keep the details
        db.rollback()
        logger.exception("store pack import failed unexpectedly")
        return ImportOutcome(None, PackImportStatus.failed, pack=pack, exception=format_exception(exc))
    return ImportOutcome(None, PackImportStatus.succeeded, pack=pack, report=report)


def _finish(db: Session, record_id: int, outcome: ImportOutcome) -> None:
    try:
        record = db.get(PackImport, record_id)
        record.status = outcome.status
        record.finished_at = utc_now()
        if outcome.pack is not None:
            record.store_slug = outcome.pack["store"]["slug"]
        if outcome.issues:
            record.issues = [{"path": issue.path, "message": issue.message} for issue in outcome.issues]
        record.exception = outcome.exception
        if outcome.report is not None:
            record.report = report_counts(outcome.report)
            record.store_created = outcome.report.store_created
            record.store_id = db.scalar(select(Store.id).where(Store.slug == outcome.report.store_slug))
        db.commit()
    except Exception:  # the import result stands; only its record is missing details
        db.rollback()
        logger.exception("could not record the result of store pack import #%s", record_id)


def recent_imports(db: Session, limit: int = 50) -> list[PackImport]:
    """Latest import attempts first."""
    return list_imports(db, limit=limit)


def list_imports(db: Session, *, status: Optional[PackImportStatus] = None, offset: int = 0,
                 limit: int = 50) -> list[PackImport]:
    """Import records, latest first, optionally of one status (with their user loaded)."""
    query = select(PackImport).options(selectinload(PackImport.user)).order_by(PackImport.id.desc())
    if status is not None:
        query = query.where(PackImport.status == status)
    return list(db.scalars(query.offset(offset).limit(limit)))


def get_import(db: Session, record_id: int) -> Optional[PackImport]:
    if not fits_db_integer(record_id):
        return None
    return db.scalar(select(PackImport).options(selectinload(PackImport.user)).where(PackImport.id == record_id))
