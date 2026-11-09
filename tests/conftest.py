from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db, init_db
from app.main import create_app
from app.models import Category, Product, PropertyDefinition, PropertyType, PropertyValue, Store


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
def client(engine):
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
