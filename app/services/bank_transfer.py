"""Offline bank transfer (``TerminalKind.bank_transfer``): the shopper deposits the amount into the
store's account (SPEI in Mexico); an admin sees the deposit and confirms or rejects the payment.
No bank API is called: the store's details live in the terminal's ``config`` (edited in admin, or
set by a store pack), and each payment gets:

- a **reference**: 7 digits, unique in the store (SPEI's "referencia numérica" takes up to 7
  digits), which the shopper writes on the transfer so the deposit can be matched;
- a **concept**: the payment's id (``pay_…``, fits SPEI's 40-character "concepto");
- a copy of the instructions at that moment (``Payment.instructions``): later edits of the account
  details don't change what that shopper was told. The copy includes the note's translations
  (``note_translations``, {language: text}, from the ``translations`` table), so a shopper browsing in
  English reads the English note.

Config fields (``validate_config``): ``bank`` and ``beneficiary`` (required), ``clabe`` (required:
18 digits with a valid control digit), ``account`` (optional, 6 to 20 digits), ``note`` (optional,
shown to the shopper). A terminal whose config isn't complete takes no payments
(``terminal_not_configured``).
"""

from __future__ import annotations

import re
import secrets
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Payment, PaymentTerminal, Translation, TranslationEntity
from app.services.store_admin import FieldError

BANK_MAX = 100
BENEFICIARY_MAX = 200
NOTE_MAX = 500
CLABE_LENGTH = 18
ACCOUNT_MIN, ACCOUNT_MAX = 6, 20
ACCOUNT_RE = re.compile(rf"^[0-9]{{{ACCOUNT_MIN},{ACCOUNT_MAX}}}$")
CONFIG_FIELDS = ("bank", "beneficiary", "clabe", "account", "note")
REFERENCE_DIGITS = 7
CLABE_WEIGHTS = (3, 7, 1)


def clabe_control_digit(first17: str) -> int:
    """The 18th digit of a CLABE: weights 3, 7, 1 over the first 17 digits (each product mod 10),
    then (10 - sum mod 10) mod 10."""
    total = sum((int(digit) * CLABE_WEIGHTS[i % 3]) % 10 for i, digit in enumerate(first17))
    return (10 - total % 10) % 10


def is_valid_clabe(clabe: str) -> bool:
    return (len(clabe) == CLABE_LENGTH and clabe.isascii() and clabe.isdigit()
            and clabe_control_digit(clabe[:17]) == int(clabe[17]))


def _digits(raw: Any) -> str:
    return re.sub(r"[\s-]", "", str(raw or ""))


def validate_config(data: dict[str, Any]) -> tuple[dict[str, str], dict[str, FieldError]]:
    """Cleans the instructions (spaces and hyphens out of the numbers). Returns (config, errors)."""
    errors: dict[str, FieldError] = {}
    config = {
        "bank": " ".join(str(data.get("bank") or "").split()),
        "beneficiary": " ".join(str(data.get("beneficiary") or "").split()),
        "clabe": _digits(data.get("clabe")),
        "account": _digits(data.get("account")),
        "note": str(data.get("note") or "").strip(),
    }
    for field, limit in (("bank", BANK_MAX), ("beneficiary", BENEFICIARY_MAX)):
        if not config[field]:
            errors[field] = ("required", {})
        elif len(config[field]) > limit:
            errors[field] = ("too_long", {"max_len": limit})
    if not config["clabe"]:
        errors["clabe"] = ("required", {})
    elif len(config["clabe"]) != CLABE_LENGTH or not config["clabe"].isdigit() or not config["clabe"].isascii():
        errors["clabe"] = ("clabe_format", {})
    elif not is_valid_clabe(config["clabe"]):
        errors["clabe"] = ("clabe_check", {})
    if config["account"] and not ACCOUNT_RE.match(config["account"]):
        errors["account"] = ("account_format", {})
    if len(config["note"]) > NOTE_MAX:
        errors["note"] = ("too_long", {"max_len": NOTE_MAX})
    return config, errors


def is_configured(terminal: PaymentTerminal) -> bool:
    return not validate_config(terminal.config or {})[1]


def new_reference(db: Session, store_id: int) -> str:
    """A 7-digit reference not used by another payment of the store (1000000..9999999: no leading 0)."""
    low = 10 ** (REFERENCE_DIGITS - 1)
    while True:
        reference = str(low + secrets.randbelow(9 * low))
        taken = db.scalar(select(Payment.id).where(Payment.store_id == store_id, Payment.transfer_reference == reference))
        if taken is None:
            return reference


def note_translations(db: Session, terminal: PaymentTerminal) -> dict[str, str]:
    """The terminal's note in other languages, {language: text} (content translations, field "note")."""
    rows = db.execute(select(Translation.locale, Translation.text).where(
        Translation.entity_type == TranslationEntity.payment_terminal, Translation.entity_id == terminal.id,
        Translation.field == "note")).all()
    return dict(sorted(rows))


def instructions_for(db: Session, terminal: PaymentTerminal, reference: str, concept: str) -> dict[str, Any]:
    """What the shopper is told (kept on the payment)."""
    config, _ = validate_config(terminal.config or {})
    return {**{field: config[field] for field in CONFIG_FIELDS}, "reference": reference, "concept": concept,
            "note_translations": note_translations(db, terminal) if config["note"] else {}}


def shopper_view(payment: Payment) -> Optional[dict[str, Any]]:
    """The deposit instructions of a bank transfer payment (None for other payments)."""
    if not payment.instructions:
        return None
    return {"note_translations": {}, **payment.instructions, "amount_cents": payment.amount_cents,
            "currency": payment.currency}


def note_for(instructions: dict[str, Any], locale: str) -> str:
    """The note in ``locale`` when it has a translation, else as entered."""
    return (instructions.get("note_translations") or {}).get(locale) or instructions.get("note") or ""
