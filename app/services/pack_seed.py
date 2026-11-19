"""Store packs that are present without anyone importing them: ``SEED_PACKS`` (setting) lists pack
files that the app loads when it starts, through the normal import pipeline (recorded in
``pack_imports`` with source ``startup``; storefront folder copied, ``media/`` published).

A pack is only loaded when its store doesn't exist yet: an existing store, with whatever was
changed in admin since, is never touched (``update_existing=False``), with one exception, the
**payment terminals catch-up** (:func:`catch_up_terminals`): when the existing store lacks a
terminal its pack's ``payment_terminals`` names (e.g. a database loaded before solares had its
``bank_transfer`` terminal), that section is applied once: missing terminals are created as the pack
says, and the named ones get the pack's enabled / disabled state. Once every named terminal exists
that part does nothing, so later admin changes (enabling a mock again, the real bank details) are
kept. Every start also fills **missing translations** of the named terminals from the pack (e.g. the
English name and note of ``bank_transfer`` for a database from before they existed), only for a
language without one and only while the translated text is still the pack's; nothing is replaced.
Problems (a missing file, an invalid pack) are logged and recorded; they never stop the app from starting.

Default: the solares pack (``fixtures/packs/solares/solares.store-pack.v1.json``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PaymentTerminal, Store
from app.services.pack_import_log import SOURCE_STARTUP, ImportOutcome, import_pack_bytes
from app.services.store_pack_import import ImportCounts, import_pack_terminals, write_terminal_translations
from app.services.store_pack_validation import StorePackError, parse_store_pack

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # relative SEED_PACKS paths start here


def seed_pack_paths(value: str) -> list[Path]:
    """``SEED_PACKS`` -> pack file paths (comma-separated; relative to the project folder)."""
    paths = []
    for item in value.split(","):
        item = item.strip()
        if item:
            path = Path(item).expanduser()
            paths.append(path if path.is_absolute() else PROJECT_ROOT / path)
    return paths


def seed_packs(db: Session, value: str) -> list[ImportOutcome]:
    """Load every pack of ``value`` whose store doesn't exist yet. Returns the import outcomes."""
    outcomes = []
    for path in seed_pack_paths(value):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.error("SEED_PACKS: cannot read %s (%s); not loaded", path, exc.strerror)
            continue
        try:
            pack = parse_store_pack(raw)
        except StorePackError:
            pack = None  # the import below records it as invalid, with its issues
        slug = pack["store"]["slug"] if pack is not None else None
        store = db.scalar(select(Store).where(Store.slug == slug)) if slug is not None else None
        if store is not None:
            logger.info("SEED_PACKS: store %s exists; %s not loaded again", slug, path)
            added = catch_up_terminals(db, store, pack)
            if added:
                logger.info("SEED_PACKS: store %s got the payment terminals of %s: %s", slug, path, ", ".join(added))
            filled = fill_terminal_translations(db, store, pack)
            if filled:
                logger.info("SEED_PACKS: store %s got %s missing payment terminal translation(s) from %s",
                            slug, filled, path)
            continue
        outcome = import_pack_bytes(db, raw, source=SOURCE_STARTUP, filename=str(path), update_existing=False,
                                    pack_dir=path.parent)
        if outcome.ok:
            logger.info("SEED_PACKS: %s loaded (record #%s)\n%s", path, outcome.record_id, outcome.report.summary())
        else:
            logger.error("SEED_PACKS: %s not loaded: %s (record #%s)", path, outcome.status.value, outcome.record_id)
        outcomes.append(outcome)
    return outcomes


def catch_up_terminals(db: Session, store: Store, pack: dict) -> list[str]:
    """The existing store lacks a terminal its pack names: apply the pack's ``payment_terminals``
    (create the missing ones, set the named ones' enabled state). Returns the codes created
    (none: the store already has them all, nothing is changed)."""
    wanted = pack.get("payment_terminals") or []
    have = set(db.scalars(select(PaymentTerminal.code).where(PaymentTerminal.store_id == store.id)))
    missing = [t["code"] for t in wanted if t["code"] not in have]
    if not missing:
        return []
    import_pack_terminals(db, store, wanted, ImportCounts())
    db.commit()
    return missing


def fill_terminal_translations(db: Session, store: Store, pack: dict) -> int:
    """Adds the pack's terminal translations the store lacks (see the module docstring). Returns how many."""
    terminals = {t.code: t for t in db.scalars(select(PaymentTerminal).where(PaymentTerminal.store_id == store.id))}
    written = 0
    for data in pack.get("payment_terminals") or []:
        terminal = terminals.get(data["code"])
        if terminal is not None:
            written += write_terminal_translations(db, store, terminal, data, ("name", "note"), replace=False)
    if written:
        db.commit()
    return written
