"""The solares store is present without anyone importing it:

- its storefront and images are in the repository, exactly as an import installs them
  (app/storefronts/solares/ and ../magic_stores_content/content/solares/);
- its catalog rows are loaded when the app starts (SEED_PACKS, default: the solares pack), only
  if the store doesn't exist yet; later starts never change it.

conftest.py pins SEED_PACKS="" for every other test; these tests set it themselves, with temporary
CONTENT_ROOT / STOREFRONTS_ROOT folders (nothing is written to the real ones)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db import get_db
from app.main import create_app
from app.models import PackImport, PackImportStatus, Product, Store
from app.services.pack_import_log import SOURCE_STARTUP
from app.services.pack_seed import PROJECT_ROOT, seed_pack_paths
from tests.ui_text import en, page_text

PROJECT = Path(__file__).resolve().parent.parent
SOLARES_DIR = PROJECT / "fixtures" / "packs" / "solares"
SOLARES = "fixtures/packs/solares/solares.store-pack.v1.json"
CONTENT_PROJECT = PROJECT.parent / "magic_stores_content"


def _files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture()
def seed(monkeypatch):
    def set_seed(value: str) -> None:
        monkeypatch.setenv("SEED_PACKS", value)
        get_settings.cache_clear()

    yield set_seed
    get_settings.cache_clear()


def _start(engine) -> TestClient:
    """The app started against ``engine`` (its lifespan runs the seeding), like `uvicorn app.main:app`."""
    sessions = sessionmaker(bind=engine, autoflush=False)
    app = create_app()

    def override():
        db = sessions()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    return TestClient(app)


def _count(engine, model, *where) -> int:
    with sessionmaker(bind=engine)() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


# ------------------------------------------------------------------ in the repository


def test_solares_storefront_and_images_are_in_the_repository():
    """The copies an import installs are committed, identical to the pack (keep them in sync)."""
    assert _files(PROJECT / "app" / "storefronts" / "solares") == _files(SOLARES_DIR / "storefront")
    assert _files(CONTENT_PROJECT / "content" / "solares") == _files(SOLARES_DIR / "media")


def test_default_setting_seeds_the_solares_pack():
    assert Settings(_env_file=None, _env_prefix="NO_SUCH_PREFIX_").seed_packs == SOLARES
    [path] = seed_pack_paths(SOLARES)
    assert path == PROJECT_ROOT / SOLARES and path.is_file()


# ------------------------------------------------------------------------- at startup


def test_first_start_loads_solares(engine, seed, content_root, storefronts, content_app):
    seed(SOLARES)
    with _start(engine) as client:
        store = client.get("/api/stores/solares")
        assert store.status_code == 200 and store.json()["name"] == "Solares"
        assert len(client.get("/api/stores/solares/products").json()) == 5
        home = client.get("/stores/solares").text
        assert 'data-storefront="solares"' in home  # the pack storefront, not _default
        product = client.get("/stores/solares/p/solar-8-tubos-alta").text
        src = "https://media.example.test/content/solares/tubo-8-alta.jpg"
        assert f'<img src="{src}"' in product
        assert content_app(content_root).get("/content/solares/tubo-8-alta.jpg").status_code == 200
    assert _files(content_root / "solares") == _files(SOLARES_DIR / "media")
    assert _files(storefronts / "solares") == _files(SOLARES_DIR / "storefront")
    with sessionmaker(bind=engine)() as db:
        record = db.scalar(select(PackImport))
        assert (record.source, record.status, record.store_slug) == (SOURCE_STARTUP, PackImportStatus.succeeded,
                                                                    "solares")
        assert record.filename == str(PROJECT_ROOT / SOLARES) and record.report["missing_media"] == []


def test_later_starts_never_change_the_store(engine, seed, content_root, storefronts):
    seed(SOLARES)
    with _start(engine):
        pass
    with sessionmaker(bind=engine)() as db:  # edited in admin after the first start
        db.scalar(select(Store).where(Store.slug == "solares")).name = "Solares (edited)"
        db.delete(db.scalar(select(Product).where(Product.slug == "boiler-gas-40-litros")))
        db.commit()
    with _start(engine) as client:  # restart
        assert client.get("/api/stores/solares").json()["name"] == "Solares (edited)"
        assert len(client.get("/api/stores/solares/products").json()) == 4
    assert _count(engine, PackImport) == 1  # not loaded again, not even attempted


def test_the_imports_screen_shows_the_startup_source(engine, seed, content_root, storefronts):
    seed(SOLARES)
    with _start(engine) as client:
        client.post("/login", data={"email": "admin@example.com", "password": "ChangeMeAdmin123!"})
        assert en("admin.imports.source.startup") in page_text(client.get("/admin/imports?lang=en"))


def test_empty_setting_starts_with_an_empty_catalog(engine, seed):
    seed("")
    with _start(engine) as client:
        assert client.get("/api/stores").json() == []
        assert "No stores yet" in client.get("/?lang=en").text
    assert _count(engine, PackImport) == 0


def test_missing_or_invalid_packs_never_stop_the_app(engine, seed, tmp_path, caplog, content_root, storefronts):
    bad = tmp_path / "bad.store-pack.v1.json"
    bad.write_text('{"version": 1, "store": {"slug": "Bad Slug"}}', encoding="utf-8")
    seed(f"{tmp_path / 'missing.json'}, {bad} ,{SOLARES}")
    with caplog.at_level(logging.INFO, logger="app.services.pack_seed"), _start(engine) as client:
        assert client.get("/api/health").status_code == 200  # the app started
        assert client.get("/api/stores/solares").status_code == 200  # the valid pack after them still loads
    assert f"SEED_PACKS: cannot read {tmp_path / 'missing.json'}" in caplog.text
    assert f"SEED_PACKS: {bad} not loaded: invalid" in caplog.text
    assert _count(engine, PackImport, PackImport.status == PackImportStatus.invalid) == 1


def test_relative_paths_do_not_depend_on_the_current_folder(engine, seed, content_root, storefronts, tmp_path,
                                                             monkeypatch):
    monkeypatch.chdir(tmp_path)  # e.g. uvicorn started from another folder
    seed(SOLARES)
    with _start(engine) as client:
        assert client.get("/api/stores/solares").status_code == 200
