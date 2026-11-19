"""Auth core: password hashing, User model constraints, admin seed on startup."""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.auth import create_user, hash_password, seed_admin, verify_password
from app.models import User, UserRole

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "ChangeMeAdmin123!"


# --- password hashing / model -------------------------------------------------

# The suite hashes with a low iteration count for speed (see conftest); the tests below that
# use the ``production_work_factor`` fixture hash with the real one.


def test_production_work_factor_is_600k(production_work_factor):
    # OWASP recommendation for PBKDF2-HMAC-SHA256; must not be lowered in app code.
    assert production_work_factor == 600_000


def test_password_hash_roundtrip(production_work_factor):
    hashed = hash_password("s3cret!")
    assert hashed != "s3cret!"
    assert "s3cret!" not in hashed  # no plaintext
    assert hashed.startswith("pbkdf2_sha256$600000$")
    assert verify_password("s3cret!", hashed)
    assert not verify_password("wrong", hashed)
    assert not verify_password("S3cret!", hashed)
    assert hash_password("s3cret!") != hashed  # random salt
    assert not verify_password("s3cret!", "garbage")


def test_password_hash_format(production_work_factor):
    algo, iterations, salt_b64, digest_b64 = hash_password("s3cret!").split("$")
    assert algo == "pbkdf2_sha256"
    assert int(iterations) == 600_000
    salt = base64.b64decode(salt_b64)
    assert len(salt) == 16
    # It is plain PBKDF2-HMAC-SHA256 of the password with that salt.
    assert base64.b64decode(digest_b64) == hashlib.pbkdf2_hmac("sha256", b"s3cret!", salt, 600_000, dklen=32)


def test_non_ascii_password():
    hashed = hash_password("contraseña-ñandú-密码")
    assert verify_password("contraseña-ñandú-密码", hashed)
    assert not verify_password("contrasena-nandu-密码", hashed)


def test_hash_with_other_iteration_count_still_verifies():
    # The work factor is stored per hash, so raising it later keeps old hashes valid.
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", b"pw", salt, 1_000, dklen=32)
    stored = f"pbkdf2_sha256$1000${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    assert verify_password("pw", stored)
    assert not verify_password("nope", stored)


@pytest.mark.parametrize(
    "mangle",
    [
        lambda it, salt, dig: f"pbkdf2_sha256$0${salt}${dig}",
        lambda it, salt, dig: f"pbkdf2_sha256$-5${salt}${dig}",
        lambda it, salt, dig: f"pbkdf2_sha256$abc${salt}${dig}",
        lambda it, salt, dig: f"pbkdf2_sha256${'9' * 30}${salt}${dig}",  # overflows a C long
        lambda it, salt, dig: f"pbkdf2_sha256${it}${salt}$",
        lambda it, salt, dig: f"pbkdf2_sha256${it}$!!!${dig}",
        lambda it, salt, dig: f"pbkdf2_sha256${it}${salt}",
        lambda it, salt, dig: f"pbkdf2_sha256${it}${salt}${dig}$extra",
        lambda it, salt, dig: f"md5${it}${salt}${dig}",
        lambda it, salt, dig: "",
        lambda it, salt, dig: "s3cret!",  # a plaintext "hash" is never accepted
    ],
)
def test_malformed_hashes_are_rejected_without_errors(mangle):
    _, iterations, salt, digest = hash_password("s3cret!").split("$")
    assert verify_password("s3cret!", mangle(iterations, salt, digest)) is False


def _legacy_scrypt_hash(password: str) -> str:
    """A hash in the format stored before the switch to PBKDF2."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${2**14}$8$1${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


@pytest.mark.skipif(not hasattr(hashlib, "scrypt"), reason="this Python's hashlib has no scrypt (e.g. LibreSSL)")
def test_legacy_scrypt_hash_still_verifies():
    # Users stored before the switch (e.g. an already seeded admin) can still log in.
    stored = _legacy_scrypt_hash("s3cret!")
    assert verify_password("s3cret!", stored)
    assert not verify_password("wrong", stored)
    assert not verify_password("s3cret!", "scrypt$1$2$3$x$y")


@pytest.mark.skipif(not hasattr(hashlib, "scrypt"), reason="this Python's hashlib has no scrypt (e.g. LibreSSL)")
def test_legacy_scrypt_hash_without_scrypt_support_is_rejected(monkeypatch):
    stored = _legacy_scrypt_hash("s3cret!")
    monkeypatch.delattr(hashlib, "scrypt")
    assert verify_password("s3cret!", stored) is False


def test_auth_works_on_python_without_hashlib_scrypt():
    """Regression: on LibreSSL builds (e.g. macOS system Python) hashlib has no scrypt and
    importing app.auth used to fail. Simulated in a fresh interpreter, where scrypt is removed
    before app.auth is imported."""
    project_root = Path(__file__).resolve().parent.parent
    script = (
        "import hashlib\n"
        "if hasattr(hashlib, 'scrypt'):\n"
        "    del hashlib.scrypt\n"
        "import app.auth as auth\n"
        "h = auth.hash_password('pw')\n"
        "assert h.startswith('pbkdf2_sha256$'), h\n"
        "assert auth.verify_password('pw', h)\n"
        "assert not auth.verify_password('nope', h)\n"
        "assert auth.verify_password('pw', 'scrypt$16384$8$1$c2FsdA==$ZGlnZXN0') is False\n"
        "print('ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(project_root), "DATABASE_URL": "sqlite://"}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=project_root, env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_email_is_unique(db_session):
    create_user(db_session, "dup@example.com", "pw-one")
    with pytest.raises(IntegrityError):
        create_user(db_session, "DUP@example.com ", "pw-two")  # normalized -> same email
    db_session.rollback()


# --- seed ---------------------------------------------------------------------

def test_admin_seeded_on_startup(client, db_session):
    admin = db_session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert admin is not None
    assert admin.role == UserRole.admin
    assert admin.password_hash != ADMIN_PASSWORD
    assert admin.password_hash.startswith("pbkdf2_sha256$")
    assert verify_password(ADMIN_PASSWORD, admin.password_hash)


def test_seed_is_idempotent_and_keeps_password(client, db_session):
    seed_admin(db_session, ADMIN_EMAIL, "SomethingElse!")
    count = db_session.scalar(select(func.count()).select_from(User).where(User.email == ADMIN_EMAIL))
    assert count == 1
    admin = db_session.scalar(select(User).where(User.email == ADMIN_EMAIL))
    assert verify_password(ADMIN_PASSWORD, admin.password_hash)

