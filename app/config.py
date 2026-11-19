from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Shortest PAYMENT_CALLBACK_SECRET accepted (HMAC key; 32 hex characters = 128 bits).
PAYMENT_CALLBACK_SECRET_MIN = 32
CART_REVALIDATE_MAX_MINUTES = 7 * 24 * 60
# The content app's folder when both projects sit side by side (workspace/magic_stores and
# workspace/magic_stores_content): the same folder the content app serves by default.
DEFAULT_CONTENT_ROOT = str(Path(__file__).resolve().parents[2] / "magic_stores_content" / "content")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Magic Stores"
    database_url: str = "sqlite:///./store_engine.db"
    secret_key: str = "YOUR_SECRET_KEY_HERE"
    # Send the session cookie only over HTTPS. Keep False for local http://127.0.0.1 dev.
    session_https_only: bool = False
    # Dev-only admin account created on startup if missing. Dummy credentials - change them.
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "ChangeMeAdmin123!"
    # Where the separate content app (../magic_stores_content) serves product files. Image values
    # stored as "/content/<store-slug>/<file>" are rendered as CONTENT_BASE_URL + that path.
    # Empty: leave them relative (e.g. a reverse proxy serves /content/ on this same host).
    content_base_url: str = "http://127.0.0.1:8001"
    # Folder the content app serves (its CONTENT_DIR). A pack imported from its folder publishes
    # its media/ files to CONTENT_ROOT/<store-slug>/. Empty: media are not published.
    content_root: str = DEFAULT_CONTENT_ROOT
    # Store packs loaded at startup when their store doesn't exist yet (comma-separated pack files;
    # relative paths are relative to this project). Existing stores are never changed. Empty: none.
    seed_packs: str = "fixtures/packs/solares/solares.store-pack.v1.json"
    # Shared secret of the payment terminals: they sign their succeed / fail callbacks with it
    # (HMAC-SHA256, see app/services/payment_callbacks.py). Empty: callbacks are refused (503).
    payment_callback_secret: str = ""
    # An open cart line keeps the name / SKU / price it had when added; after this many minutes
    # without a check it is validated again (price refreshed = "repriced", quantity checked
    # against the stock). 0 = on every request. See app/services/cart.py.
    cart_revalidate_minutes: int = 30

    @field_validator("content_base_url")
    @classmethod
    def _check_content_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.query or parts.fragment:
            raise ValueError("CONTENT_BASE_URL must be empty or an http(s) URL like http://127.0.0.1:8001")
        return value

    @field_validator("payment_callback_secret")
    @classmethod
    def _check_payment_callback_secret(cls, value: str) -> str:
        value = value.strip()
        if value and len(value) < PAYMENT_CALLBACK_SECRET_MIN:
            raise ValueError(f"PAYMENT_CALLBACK_SECRET must be empty or at least {PAYMENT_CALLBACK_SECRET_MIN} "
                             "characters (e.g. python3 -c 'import secrets; print(secrets.token_hex(32))')")
        return value

    @field_validator("cart_revalidate_minutes")
    @classmethod
    def _check_cart_revalidate_minutes(cls, value: int) -> int:
        if not 0 <= value <= CART_REVALIDATE_MAX_MINUTES:
            raise ValueError(f"CART_REVALIDATE_MINUTES must be between 0 and {CART_REVALIDATE_MAX_MINUTES} (a week)")
        return value

    @field_validator("content_root")
    @classmethod
    def _strip_content_root(cls, value: str) -> str:
        return value.strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
