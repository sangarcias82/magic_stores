from __future__ import annotations

from collections.abc import Generator
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


# What an INTEGER column can hold (64-bit signed: SQLite INTEGER, PostgreSQL BIGINT).
DB_INT_MIN, DB_INT_MAX = -(2**63), 2**63 - 1


def fits_db_integer(value: int) -> bool:
    """Can ``value`` be compared with an INTEGER column? A bigger number (typed into a URL or a form)
    can't be any row's id, and SQLite refuses to even look it up (OverflowError, a 500): id getters
    answer None for it, like for any other id that doesn't exist."""
    return DB_INT_MIN <= value <= DB_INT_MAX


def _engine_url() -> str:
    return get_settings().database_url


def create_db_engine(url: Optional[str] = None):
    database_url = url or _engine_url()
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind=None) -> None:
    """Create tables from models. Import models so metadata is registered."""
    from app import models  # noqa: F401
    from app.schema_upgrades import upgrade

    Base.metadata.create_all(bind=bind or engine)
    upgrade(bind or engine)  # tables of older databases that create_all doesn't change
