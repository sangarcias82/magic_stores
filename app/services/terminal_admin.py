"""Admin-side management of a store's payment terminals: rename, enable / disable.

Terminals themselves are created automatically (every store has the mock defaults, see
app/services/payments.py) or by a store pack (e.g. solares' ``bank_transfer``); there is no create or
delete here. A bank transfer terminal's deposit instructions (bank, beneficiary, CLABE, account,
note) are edited here too (app/services/bank_transfer.py). The ``code`` can't be changed: the API
and callbacks use it, and its ``mock_`` prefix decides whether the admin simulator may answer the
terminal's payments. Disabling a terminal stops new payments through it (``terminal_disabled``);
payments already pending on it can still get their answer.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import fits_db_integer
from app.models import PaymentTerminal, Store
from app.services.shipping_admin import FieldError

NAME_MAX = 200


def get_terminal(db: Session, store: Store, terminal_id: int) -> Optional[PaymentTerminal]:
    """The store's terminal with this id (another store's terminal is None)."""
    if not fits_db_integer(terminal_id):
        return None
    return db.scalar(
        select(PaymentTerminal).where(PaymentTerminal.store_id == store.id, PaymentTerminal.id == terminal_id)
    )


def clean_name(raw: str) -> tuple[str, Optional[FieldError]]:
    name = " ".join(raw.split())
    if not name:
        return name, ("required", {})
    if len(name) > NAME_MAX:
        return name, ("too_long", {"max_len": NAME_MAX})
    return name, None


def save_terminal(
    db: Session, terminal: PaymentTerminal, name: str, enabled: bool, config: Optional[dict] = None
) -> PaymentTerminal:
    """``config``: a bank transfer terminal's deposit instructions (validated; new payments use them,
    payments already made keep the copy they were created with)."""
    terminal.name = name
    terminal.is_enabled = enabled
    if config is not None:
        terminal.config = config
    db.commit()
    return terminal


def set_enabled(db: Session, terminal: PaymentTerminal, enabled: bool) -> PaymentTerminal:
    """Enable or disable; doing it twice is harmless."""
    terminal.is_enabled = enabled
    db.commit()
    return terminal
