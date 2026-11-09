from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models import PropertyType


class ThemeOut(BaseModel):
    primary: str = "#1a5f4a"
    secondary: str = "#f4f1ea"
    accent: str = "#c45c26"
    logo_url: Optional[str] = None


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    currency: str
    locale_default: str
    theme: dict[str, Any] = Field(default_factory=dict)


class PropertyDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    key: str
    label: str
    prop_type: PropertyType
    required: bool
    sort_order: int
    config: dict[str, Any] = Field(default_factory=dict)
    ref_category_id: Optional[int] = None


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: Optional[str] = None
    sort_order: int
    parent_id: Optional[int] = None
    property_definitions: list[PropertyDefinitionOut] = Field(default_factory=list)


class PropertyValueOut(BaseModel):
    key: str
    label: str
    prop_type: PropertyType
    value: dict[str, Any]


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: Optional[str] = None
    price_cents: int
    is_active: bool
    category_id: int
    properties: list[PropertyValueOut] = Field(default_factory=list)


class ProductListItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    price_cents: int
    category_id: int
    is_active: bool


class HealthOut(BaseModel):
    status: str
    app: str
