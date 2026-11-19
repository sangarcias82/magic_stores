from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

# --- Test bootstrap: runs before any test module (e.g. test_auth.py) is imported. -----------
# 1. `app` must be *this* project's package however pytest is started: from the project folder,
#    from a parent folder, from an IDE, or without pytest.ini's `pythonpath`. Otherwise `app`
#    may be missing ("No module named 'app'") or another package named `app` may shadow it
#    ("No module named 'app.db'").
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if sys.path[:1] != [str(PROJECT_ROOT)]:
    sys.path.insert(0, str(PROJECT_ROOT))
for _name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
    _file = getattr(sys.modules[_name], "__file__", None)
    if not _file or not Path(_file).resolve().is_relative_to(PROJECT_ROOT):
        del sys.modules[_name]  # a foreign `app` imported earlier; ours is imported below
# 2. Settings are fixed for tests, whatever a local .env says (environment variables take
#    priority over .env): a changed admin password or SESSION_HTTPS_ONLY=true in .env must not
#    break the suite, and the app's global engine (used by the startup hook) is in-memory, so
#    tests never touch the configured database or create store_engine.db in the current folder.
#    Each test still gets its own engine through the `engine` fixture below.
TEST_SETTINGS = {
    "DATABASE_URL": "sqlite://",
    "SECRET_KEY": "test-only-secret-key",
    "SESSION_HTTPS_ONLY": "false",
    "SEED_ADMIN_EMAIL": "admin@example.com",
    "SEED_ADMIN_PASSWORD": "ChangeMeAdmin123!",
    "CONTENT_BASE_URL": "http://127.0.0.1:8001",
    # Packs imported from their folder publish media/ images here, never into the real
    # ../magic_stores_content/content (tests that check publishing use their own folder).
    "CONTENT_ROOT": tempfile.mkdtemp(prefix="magic-stores-test-content-"),
    # No store loaded at startup: each test starts from an empty catalog (tests/test_seed_packs.py
    # sets it when it checks the seeding).
    "SEED_PACKS": "",
}
atexit.register(shutil.rmtree, TEST_SETTINGS["CONTENT_ROOT"], True)
os.environ.update(TEST_SETTINGS)
# 3. UI language of the `client` fixture (and everything built on it, e.g. `admin_client`): each
#    request is sent with ?lang=en. The app defaults to Spanish; the suites assert English UI text.
TEST_UI_LANG = "en"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.db import Base, get_db, init_db  # noqa: E402
from app.i18n import QUERY_PARAM as LANG_PARAM  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Category, Product, PropertyDefinition, PropertyType, PropertyValue, Store  # noqa: E402

# 4. Fast password hashing for the test session. Production hashes use 600,000 PBKDF2 iterations
#    (~0.3 s each, on purpose); every test seeds the admin at app startup and many tests sign in,
#    which made the suite ~3x slower. Each hash stores its own iteration count, so hashing,
#    verification, sign-in and seeding behave exactly the same. tests/test_auth.py pins the
#    production value and exercises it for real (fixture ``production_work_factor``).
import app.auth as _auth  # noqa: E402

PRODUCTION_PBKDF2_ITERATIONS = _auth._PBKDF2_ITERATIONS
TEST_PBKDF2_ITERATIONS = 1_000
_auth._PBKDF2_ITERATIONS = TEST_PBKDF2_ITERATIONS


@pytest.fixture()
def production_work_factor(monkeypatch) -> int:
    """Hash with the real production iteration count for one test; returns that count.
    (Test modules must not import this conftest: it would run a second time, after the patch.)"""
    monkeypatch.setattr(_auth, "_PBKDF2_ITERATIONS", PRODUCTION_PBKDF2_ITERATIONS)
    return PRODUCTION_PBKDF2_ITERATIONS


@pytest.fixture()
def storefronts(tmp_path, monkeypatch) -> Path:
    """STOREFRONTS_ROOT -> a temporary folder holding a copy of the real ``_default``: pack imports
    copy storefront folders there, never into the real app/storefronts/. Returns that folder."""
    import app.services.storefront as sf

    root = tmp_path / "storefronts"
    shutil.copytree(sf.STOREFRONTS_ROOT / sf.DEFAULT_KEY, root / sf.DEFAULT_KEY)
    monkeypatch.setattr(sf, "STOREFRONTS_ROOT", root)
    sf.templates_for.cache_clear()
    yield root
    sf.templates_for.cache_clear()


@pytest.fixture()
def content_root(tmp_path, monkeypatch) -> Path:
    """CONTENT_ROOT -> an empty temporary folder (pack imports publish media/ there) and
    CONTENT_BASE_URL -> "https://media.example.test" (not the default, so tests prove it is used)."""
    from app.config import get_settings

    root = tmp_path / "content"
    root.mkdir()
    monkeypatch.setenv("CONTENT_ROOT", str(root))
    monkeypatch.setenv("CONTENT_BASE_URL", "https://media.example.test")
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


@pytest.fixture()
def content_app():
    """Factory: a test client of the real content app (../magic_stores_content) serving a folder."""
    content_project = PROJECT_ROOT.parent / "magic_stores_content"
    if not (content_project / "content_app").is_dir():
        pytest.skip("magic_stores_content is not next to this project")
    if str(content_project) not in sys.path:
        sys.path.insert(0, str(content_project))
    from content_app.main import create_app

    return lambda root: TestClient(create_app(root))


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    # Enable FK checks on SQLite
    @event.listens_for(eng, "connect")
    def _set_sqlite_fk(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    init_db(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)


@pytest.fixture()
def db_session(engine):
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def default_lang_client(engine):
    """Client with no UI language chosen: pages use the app default (Spanish, see app/i18n.py).
    Used by tests/test_i18n.py."""
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    app = create_app()

    def _override_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _add_test_ui_lang(request) -> None:
    """httpx request hook: call the endpoint with ?lang=en, keeping the URL's own query
    (client-level ``params`` would replace it). Redirects that are followed get it too."""
    if LANG_PARAM not in request.url.params:
        request.url = request.url.copy_add_param(LANG_PARAM, TEST_UI_LANG)


@pytest.fixture()
def client(default_lang_client):
    """Client that calls every endpoint with ``?lang=en``: the suites assert English UI text.
    The Spanish default is covered by tests/test_i18n.py.
    (Same object as ``default_lang_client``: don't request both in one test.)"""
    hooks = default_lang_client.event_hooks
    default_lang_client.event_hooks = {**hooks, "request": [*hooks.get("request", []), _add_test_ui_lang]}
    return default_lang_client


@pytest.fixture()
def clothing_fixture(db_session):
    """Arbitrary vertical (clothing) via ORM — proves schema is data-driven."""
    store = Store(
        slug="clothing-demo",
        name="Demo Clothing",
        currency="USD",
        locale_default="en",
        theme={"primary": "#222222", "secondary": "#fafafa", "accent": "#b00020"},
    )
    db_session.add(store)
    db_session.flush()

    category = Category(
        store_id=store.id,
        slug="shirts",
        name="Shirts",
        description="Test fixture (not a business seed)",
    )
    db_session.add(category)
    db_session.flush()

    size = PropertyDefinition(
        category_id=category.id,
        key="size",
        label="Size",
        prop_type=PropertyType.enum,
        required=True,
        sort_order=1,
        config={"options": ["S", "M", "L"]},
    )
    color = PropertyDefinition(
        category_id=category.id,
        key="color",
        label="Color",
        prop_type=PropertyType.text,
        required=True,
        sort_order=2,
    )
    db_session.add_all([size, color])
    db_session.flush()

    product = Product(
        store_id=store.id,
        category_id=category.id,
        slug="linen-shirt-m",
        name="Linen shirt",
        price_cents=49900,
    )
    db_session.add(product)
    db_session.flush()

    db_session.add_all(
        [
            PropertyValue(product_id=product.id, definition_id=size.id, value={"value": "M"}),
            PropertyValue(product_id=product.id, definition_id=color.id, value={"value": "Sand"}),
        ]
    )
    db_session.commit()
    return store


@pytest.fixture()
def electronics_fixture(db_session, clothing_fixture):
    """Second property set in the same DB without migrations."""
    store = clothing_fixture
    category = Category(
        store_id=store.id,
        slug="chargers",
        name="Chargers",
    )
    db_session.add(category)
    db_session.flush()
    voltage = PropertyDefinition(
        category_id=category.id,
        key="voltage",
        label="Voltage",
        prop_type=PropertyType.number,
        sort_order=1,
    )
    watts = PropertyDefinition(
        category_id=category.id,
        key="watts",
        label="Watts",
        prop_type=PropertyType.number,
        sort_order=2,
    )
    db_session.add_all([voltage, watts])
    db_session.flush()
    product = Product(
        store_id=store.id,
        category_id=category.id,
        slug="charger-65w",
        name="65W charger",
        price_cents=89900,
    )
    db_session.add(product)
    db_session.flush()
    db_session.add_all(
        [
            PropertyValue(product_id=product.id, definition_id=voltage.id, value={"value": 20}),
            PropertyValue(product_id=product.id, definition_id=watts.id, value={"value": 65}),
        ]
    )
    db_session.commit()
    return store


ADMIN_EMAIL = TEST_SETTINGS["SEED_ADMIN_EMAIL"]
ADMIN_PASSWORD = TEST_SETTINGS["SEED_ADMIN_PASSWORD"]
DEMO_STORE = {"slug": "demo-widgets", "name": "Demo Widgets", "currency": "USD"}


@pytest.fixture()
def admin_client(client):
    """``client`` logged in as the seeded admin."""
    response = client.post(
        "/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


@pytest.fixture()
def demo_store(admin_client):
    """The demo-widgets store, created through the admin UI. Returns its slug."""
    response = admin_client.post("/admin/stores", data=DEMO_STORE, follow_redirects=False)
    assert response.status_code == 303
    return DEMO_STORE["slug"]


# Property definitions of the "widgets" category: one per engine type (+ a second enum / text).
WIDGET_PROPERTIES = [
    {"key": "color", "label": "Color", "prop_type": "enum", "options": "Red\nBlue\nGreen"},
    {"key": "size", "label": "Size", "prop_type": "enum", "options": "S\nM\nL", "required": "1"},
    {"key": "weight_g", "label": "Weight (g)", "prop_type": "number"},
    {"key": "waterproof", "label": "Waterproof", "prop_type": "boolean"},
    {"key": "photo", "label": "Photo", "prop_type": "image"},
    {"key": "brand", "label": "Brand", "prop_type": "category_ref"},  # ref_category_id = Brands
    {"key": "notes", "label": "Notes", "prop_type": "text"},
]


@pytest.fixture()
def widgets_category(admin_client, demo_store):
    """demo-widgets categories: Brands > Acme, and Widgets with WIDGET_PROPERTIES.
    Returns a namespace with the category ids."""
    from types import SimpleNamespace

    base = f"/admin/stores/{demo_store}/categories"

    def create(**data):
        response = admin_client.post(base, data=data, follow_redirects=False)
        assert response.status_code == 303, response.text
        return int(response.headers["location"].rsplit("/", 1)[1])

    brands = create(slug="brands", name="Brands")
    acme = create(slug="acme", name="Acme", parent_id=str(brands))
    widgets = create(slug="widgets", name="Widgets")
    for definition in WIDGET_PROPERTIES:
        data = dict(definition)
        if data["prop_type"] == "category_ref":
            data["ref_category_id"] = str(brands)
        response = admin_client.post(f"{base}/{widgets}/properties", data=data, follow_redirects=False)
        assert response.status_code == 303, response.text
    return SimpleNamespace(id=widgets, brands_id=brands, acme_id=acme)
