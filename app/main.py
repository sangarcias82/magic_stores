import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import LoginRequired, seed_admin
from app.config import get_settings
from app.db import get_db, init_db
from app.i18n import LanguageCookieMiddleware
from app.routers import (
    admin,
    admin_catalog,
    admin_imports,
    admin_inventory,
    admin_packs,
    admin_payments,
    admin_products,
    admin_shipping,
    admin_terminals,
    admin_theme,
    admin_translations,
    admin_try_payment,
    api,
    auth,
    cart,
    checkout,
    inventory,
    payments,
    shipping,
    web,
)
from app.services.pack_seed import seed_packs
from app.services.payments import ensure_default_terminals
from app.services.translations import bind_translations

logger = logging.getLogger(__name__)

PLACEHOLDER_SECRET = "YOUR_SECRET_KEY_HERE"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _seed_default_admin(application: FastAPI) -> None:
    # Go through get_db (honouring dependency overrides) so tests seed their own DB.
    settings = get_settings()
    provider = application.dependency_overrides.get(get_db, get_db)
    sessions = provider()
    db = next(sessions)
    try:
        seed_admin(db, settings.seed_admin_email, settings.seed_admin_password)
    finally:
        sessions.close()


def _seed_store_packs(application: FastAPI) -> None:
    # SEED_PACKS (default: the solares pack): stores present without anyone importing them.
    provider = application.dependency_overrides.get(get_db, get_db)
    sessions = provider()
    db = next(sessions)
    try:
        seed_packs(db, get_settings().seed_packs)
    finally:
        sessions.close()


def _ensure_payment_terminals(application: FastAPI) -> None:
    # Stores created before payment terminals existed get the default ones (new stores get them on insert).
    provider = application.dependency_overrides.get(get_db, get_db)
    sessions = provider()
    db = next(sessions)
    try:
        ensure_default_terminals(db)
    finally:
        sessions.close()


@asynccontextmanager
async def lifespan(application: FastAPI):
    init_db()
    _seed_default_admin(application)
    _seed_store_packs(application)
    _ensure_payment_terminals(application)
    yield


async def _login_required_handler(_: Request, exc: LoginRequired) -> RedirectResponse:
    return RedirectResponse(f"/login?{urlencode({'next': exc.next_path})}", status_code=303)


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.secret_key == PLACEHOLDER_SECRET:
        logger.warning("SECRET_KEY is the placeholder value; set a real one in .env before deploying.")
    # bind_translations: level 2 of the UI translations (database overrides) for every request.
    application = FastAPI(title=settings.app_name, lifespan=lifespan, dependencies=[Depends(bind_translations)])
    application.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="magic_stores_session",
        same_site="lax",
        https_only=settings.session_https_only,
    )
    application.add_middleware(LanguageCookieMiddleware, https_only=settings.session_https_only)
    application.add_exception_handler(LoginRequired, _login_required_handler)
    # Absolute path: works whatever the current working directory is.
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(api.router)
    application.include_router(shipping.router)
    application.include_router(payments.router)
    application.include_router(inventory.router)
    application.include_router(cart.router)
    application.include_router(checkout.router)
    application.include_router(auth.router)
    application.include_router(admin.router)  # before admin_catalog: /admin/stores/new wins
    application.include_router(admin_catalog.router)
    application.include_router(admin_products.router)
    application.include_router(admin_inventory.router)
    application.include_router(admin_theme.router)
    application.include_router(admin_shipping.router)
    application.include_router(admin_terminals.router)
    application.include_router(admin_try_payment.router)
    application.include_router(admin_payments.router)
    application.include_router(admin_translations.router)
    application.include_router(admin_packs.router)
    application.include_router(admin_imports.router)
    application.include_router(web.router)
    return application


app = create_app()
