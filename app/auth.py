"""Session auth: password hashing, current-user lookup, admin guard, admin seed.

Sessions use Starlette's signed-cookie ``SessionMiddleware`` (configured in
``app.main``); the cookie only carries the user id, signed with ``SECRET_KEY``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User, UserRole

SESSION_USER_KEY = "user_id"

# PBKDF2-HMAC-SHA256 (stdlib, available on OpenSSL and LibreSSL builds; no extra dependency).
# Algorithm and iterations are stored in each hash, so the work factor can be raised later
# without invalidating existing passwords. Format: pbkdf2_sha256$<iterations>$<salt>$<digest>.
_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ITERATIONS = 600_000  # OWASP recommendation for PBKDF2-HMAC-SHA256
_SALT_BYTES = 16
_DKLEN = 32


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS, dklen=_DKLEN)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, *params = stored_hash.split("$")
        if algo == _PBKDF2_ALGO:
            iterations, salt_b64, digest_b64 = params
            expected = base64.b64decode(digest_b64)
            candidate = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations), dklen=len(expected)
            )
        elif algo == "scrypt":
            # Hashes stored before the switch to PBKDF2 (scrypt$n$r$p$salt$digest). They can only
            # be checked where hashlib has scrypt (not on LibreSSL builds of Python).
            scrypt = getattr(hashlib, "scrypt", None)
            if scrypt is None:
                return False
            n, r, p, salt_b64, digest_b64 = params
            expected = base64.b64decode(digest_b64)
            candidate = scrypt(
                password.encode("utf-8"), salt=base64.b64decode(salt_b64), n=int(n), r=int(r), p=int(p),
                dklen=len(expected),
            )
        else:
            return False
    except (ValueError, TypeError, OverflowError):  # malformed / corrupted stored hash
        return False
    return hmac.compare_digest(candidate, expected)


# Used to spend the same hashing time when the email is unknown (no user enumeration by timing).
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_user_by_email(db: Session, email: str) -> Optional[User]:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def create_user(db: Session, email: str, password: str, role: UserRole = UserRole.customer) -> User:
    user = User(email=normalize_email(email), password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> Optional[User]:
    user = get_user_by_email(db, email)
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def login_session(request: Request, user: User) -> None:
    request.session.clear()
    request.session[SESSION_USER_KEY] = user.id


def logout_session(request: Request) -> None:
    request.session.clear()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    user_id = request.session.get(SESSION_USER_KEY)
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None:
        # Stale cookie (user deleted / DB reset): drop it.
        request.session.clear()
    return user


class LoginRequired(Exception):
    """Raised by guards when there is no logged-in user; handled as a redirect to /login."""

    def __init__(self, next_path: str = "/") -> None:
        self.next_path = next_path


def require_admin(request: Request, user: Optional[User] = Depends(get_current_user)) -> User:
    if user is None:
        raise LoginRequired(next_path=request.url.path)
    if user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def safe_next_path(value: Optional[str], default: str = "/") -> str:
    """Only allow local absolute paths as post-login redirect targets (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    if any(ord(ch) < 0x20 for ch in value):
        return default
    return value


def seed_admin(db: Session, email: str, password: str) -> User:
    """Create the admin account if it does not exist. Never overwrites an existing password."""
    existing = get_user_by_email(db, email)
    if existing is not None:
        return existing
    return create_user(db, email, password, role=UserRole.admin)
