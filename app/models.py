"""Generic catalog schema. Domain fields (tubes, sizes, brands) live in PropertyDefinition rows, not columns."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PropertyType(str, enum.Enum):
    text = "text"
    number = "number"
    boolean = "boolean"
    enum = "enum"
    image = "image"
    category_ref = "category_ref"


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    locale_default: Mapped[str] = mapped_column(String(16), default="en")
    # Theme as data (colors, logo URL placeholders) — no hard-coded brand CSS per vertical
    theme: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    categories: Mapped[list[Category]] = relationship(back_populates="store", cascade="all, delete-orphan")
    products: Mapped[list[Product]] = relationship(back_populates="store", cascade="all, delete-orphan")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("store_id", "slug", name="uq_category_store_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    store: Mapped[Store] = relationship(back_populates="categories")
    parent: Mapped[Optional[Category]] = relationship(remote_side="Category.id")
    property_definitions: Mapped[list[PropertyDefinition]] = relationship(
        back_populates="category",
        cascade="all, delete-orphan",
        foreign_keys="PropertyDefinition.category_id",
    )
    products: Mapped[list[Product]] = relationship(back_populates="category")


class PropertyDefinition(Base):
    __tablename__ = "property_definitions"
    __table_args__ = (UniqueConstraint("category_id", "key", name="uq_prop_def_category_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(200))
    prop_type: Mapped[PropertyType] = mapped_column(
        Enum(PropertyType, native_enum=False), default=PropertyType.text
    )
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    # For enum: {"options": ["S", "M", "L"]}; for category_ref: optional hints
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ref_category_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )

    category: Mapped[Category] = relationship(
        back_populates="property_definitions",
        foreign_keys=[category_id],
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("store_id", "slug", name="uq_product_store_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), index=True)
    slug: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price_cents: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    store: Mapped[Store] = relationship(back_populates="products")
    category: Mapped[Category] = relationship(back_populates="products")
    property_values: Mapped[list[PropertyValue]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )


class PropertyValue(Base):
    __tablename__ = "property_values"
    __table_args__ = (UniqueConstraint("product_id", "definition_id", name="uq_prop_val_product_def"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    definition_id: Mapped[int] = mapped_column(
        ForeignKey("property_definitions.id", ondelete="CASCADE"), index=True
    )
    # Typed payload: {"value": ...} or {"media_path": "..."} or {"category_id": N}
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    product: Mapped[Product] = relationship(back_populates="property_values")
    definition: Mapped[PropertyDefinition] = relationship()


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    alt_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
