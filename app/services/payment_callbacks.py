"""Signed payment-terminal callbacks: the only way the API marks a payment succeeded or failed.

A real payment's outcome must come from the terminal's server, never from a browser. The terminal
sends ``POST /api/stores/<slug>/payments/callback`` with the JSON body
``{"payment": "<payment id>", "status": "succeeded" | "failed", "mock_reference": "…"}`` and two
headers (Stripe-style webhook signature):

    X-Terminal-Timestamp: <unix time in seconds>
    X-Terminal-Signature: sha256=<hex HMAC-SHA256(PAYMENT_CALLBACK_SECRET, "<timestamp>." + raw body)>

:func:`verify` accepts it only if the signature matches (constant-time compare) and the timestamp is
within ``TOLERANCE_SECONDS`` of now, so a captured callback can't be replayed later; the payment id
is inside the signed body, so it can't be pointed at another payment either. Replaying the same
answer within the window is harmless: a payment is answered once (app/services/payments.py).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

TIMESTAMP_HEADER = "X-Terminal-Timestamp"
SIGNATURE_HEADER = "X-Terminal-Signature"
SIGNATURE_PREFIX = "sha256="
TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 8 * 1024


class CallbackRejected(Exception):
    """A callback that isn't proven to come from the terminal. ``str()`` is safe to return."""


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The ``X-Terminal-Signature`` value for ``body`` sent at ``timestamp``."""
    digest = hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return SIGNATURE_PREFIX + digest.hexdigest()


def verify(secret: str, timestamp: Optional[str], signature: Optional[str], body: bytes,
           now: Optional[float] = None) -> None:
    """Raise :class:`CallbackRejected` unless ``signature`` signs ``body`` with ``secret`` and
    ``timestamp`` is recent. ``secret`` must not be empty (callbacks are disabled then)."""
    if not secret:
        raise ValueError("no secret: callbacks are disabled")
    if not timestamp or not signature:
        raise CallbackRejected(f"missing {TIMESTAMP_HEADER} or {SIGNATURE_HEADER} header")
    if not timestamp.isdigit() or len(timestamp) > 12:
        raise CallbackRejected(f"{TIMESTAMP_HEADER} must be a unix time in seconds")
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > TOLERANCE_SECONDS:
        raise CallbackRejected(f"{TIMESTAMP_HEADER} is more than {TOLERANCE_SECONDS} seconds away from now")
    if not hmac.compare_digest(sign(secret, timestamp, body).encode("ascii"), signature.encode("utf-8", "replace")):
        raise CallbackRejected("invalid signature")
