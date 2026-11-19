from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models import MAX_AMOUNT_CENTS, PropertyType


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
    # True when at least one unit is on hand (Product.stock_on_hand > 0). The count and the SKU
    # are not public.
    in_stock: bool
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
    in_stock: bool  # see ProductOut.in_stock


class SkuProductOut(BaseModel):
    """The product found by a SKU lookup (active or hidden: this is an admin API)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    price_cents: int
    is_active: bool
    category_id: int


class SkuLookupOut(BaseModel):
    """GET /api/stores/{slug}/skus/{sku} (admin session): which product has the SKU, and its stock."""

    store: str
    currency: str
    sku: str  # as stored (upper-case), whatever case was asked for
    stock_on_hand: int
    in_stock: bool
    product: SkuProductOut


class CartAddIn(BaseModel):
    """Body of POST /api/stores/{slug}/cart/lines: a product by id **or** SKU, and how many."""

    model_config = ConfigDict(extra="forbid")

    product_id: Optional[int] = Field(default=None, ge=1, description="The product's id")
    sku: Optional[str] = Field(default=None, min_length=1, max_length=64, description="Or its SKU (any case)")
    quantity: int = Field(default=1, ge=1, le=99, description="How many to add (1 to 99)")

    @model_validator(mode="after")
    def _one_product(self) -> "CartAddIn":
        if (self.product_id is None) == (self.sku is None):
            raise ValueError("give either product_id or sku")
        return self


class CartQuantityIn(BaseModel):
    """Body of PATCH /api/stores/{slug}/cart/lines/{line_id}: the line's new quantity."""

    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(ge=1, le=99)


class CartLineOut(BaseModel):
    id: int
    product_id: int
    sku: Optional[str] = None
    slug: str
    name: str
    name_translations: dict[str, str] = Field(default_factory=dict)  # the name in other languages
    url: str
    image_url: Optional[str] = None  # the product's picture (first image property), shown small
    quantity: int
    unit_price_cents: int
    line_total_cents: int
    unit_price_display: str
    line_total_display: str
    price_changed: bool  # the last validation found a new price (the line now charges it)
    previous_unit_price_cents: Optional[int] = None
    previous_unit_price_display: Optional[str] = None
    issue: Optional[str] = None  # out_of_stock / insufficient_stock / unavailable, from the last validation
    available: bool  # issue is None
    validated_at: Optional[str] = None  # UTC, when the snapshot was last checked


class CartOut(BaseModel):
    store: str
    currency: str
    lines: list[CartLineOut] = Field(default_factory=list)
    line_count: int
    item_count: int
    subtotal_cents: int
    subtotal_display: str


class CheckoutIn(BaseModel):
    """Body of POST /api/stores/{slug}/cart/checkout: the shipping method and the payment terminal,
    chosen separately (the cart holds neither)."""

    model_config = ConfigDict(extra="forbid")

    shipping_method: str = Field(min_length=1, max_length=64, description="Shipping method code, e.g. 'standard'")
    country: Optional[str] = Field(default=None, pattern=r"^[A-Za-z]{2}$", description="ISO 3166-1 alpha-2, e.g. 'MX'")
    terminal: str = Field(min_length=1, max_length=64, description="Payment terminal code, e.g. 'mock_card'")


class OrderLineOut(BaseModel):
    product_id: Optional[int] = None  # None once the product is deleted
    sku: Optional[str] = None
    name: str
    name_translations: dict[str, str] = Field(default_factory=dict)  # the name in other languages
    quantity: int
    unit_price_cents: int
    line_total_cents: int
    unit_price_display: str
    line_total_display: str
    short_quantity: int  # units missing when the payment succeeded (0 = all taken)


class OrderStockOut(BaseModel):
    """The order's stock: taken when its payment succeeded (a pending or failed payment takes none)."""

    taken: bool
    taken_at: Optional[str] = None
    short: bool  # some line couldn't take all its units


class OrderShippingOut(BaseModel):
    method: str
    name: str
    country: Optional[str] = None
    amount_cents: int
    free_shipping: bool


class OrderOut(BaseModel):
    id: str  # "ord_…"
    status: str  # awaiting_payment / paid / payment_failed: from the payment
    currency: str
    lines: list[OrderLineOut]
    stock: OrderStockOut
    item_count: int
    subtotal_cents: int
    shipping: OrderShippingOut
    total_cents: int
    subtotal_display: str
    shipping_display: str
    total_display: str
    created_at: str


class BankTransferInstructionsOut(BaseModel):
    """Where and how to deposit a bank transfer payment (the copy kept on the payment)."""

    bank: str
    beneficiary: str
    clabe: str  # 18 digits
    account: str = ""  # optional
    note: str = ""  # the store's own instructions (as entered)
    note_translations: dict[str, str] = Field(default_factory=dict)  # the note in other languages: {"en": "…"}
    reference: str  # 7 digits, to write on the transfer
    concept: str  # the payment id
    amount_cents: int
    currency: str
    amount_display: str


class OrderPaymentOut(BaseModel):
    id: str  # "pay_…"
    status: str
    terminal: str
    kind: str  # the terminal's kind: "mock" or "bank_transfer"
    amount_cents: int
    currency: str
    transfer_reference: Optional[str] = None
    instructions: Optional[BankTransferInstructionsOut] = None


class CheckoutOut(BaseModel):
    """A checkout's answer (and GET of an order): the order and its payment, side by side."""

    order: OrderOut
    payment: OrderPaymentOut


class HealthOut(BaseModel):
    status: str
    app: str


class ShippingMethodOut(BaseModel):
    """A store's active shipping method, as listed by GET /api/stores/{slug}/shipping/methods."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    sort_order: int
    rate_cents: int
    free_shipping_threshold_cents: Optional[int] = None
    countries: list[str] = Field(default_factory=list)


class ShippingQuoteIn(BaseModel):
    """Body of POST /api/stores/{slug}/shipping/quote."""

    method: str = Field(min_length=1, max_length=64, description="Shipping method code, e.g. 'standard'")
    subtotal_cents: Optional[int] = Field(default=None, ge=0, description="Order subtotal in cents")
    country: Optional[str] = Field(
        default=None, pattern=r"^[A-Za-z]{2}$", description="ISO 3166-1 alpha-2 destination, e.g. 'ES'"
    )


class ShippingQuoteMethodOut(BaseModel):
    code: str
    name: str


class ShippingQuoteOut(BaseModel):
    store: str
    currency: str
    method: ShippingQuoteMethodOut
    country: Optional[str] = None
    subtotal_cents: Optional[int] = None
    amount_cents: int
    free_shipping: bool


class PaymentTerminalOut(BaseModel):
    """One of a store's payment terminals, as listed by GET /api/stores/{slug}/payment/terminals."""

    code: str
    name: str
    kind: str  # "mock" or "bank_transfer" (offline: pay by transfer, an admin confirms it)
    enabled: bool
    sort_order: int


class PaymentCreateIn(BaseModel):
    """Body of POST /api/stores/{slug}/payments."""

    terminal: str = Field(min_length=1, max_length=64, description="Payment terminal code, e.g. 'mock_card'")
    amount_cents: int = Field(gt=0, le=MAX_AMOUNT_CENTS, description="Amount in cents, in the store's currency")


class PaymentCallbackIn(BaseModel):
    """Signed body of POST /api/stores/{slug}/payments/callback (app/services/payment_callbacks.py)."""

    model_config = ConfigDict(extra="forbid")

    payment: str = Field(min_length=1, max_length=40, description="The payment's id, e.g. 'pay_3q0V…'")
    status: Literal["succeeded", "failed"]
    mock_reference: Optional[str] = Field(
        default=None, min_length=1, max_length=64, description="What the terminal answered, e.g. 'AUTH-123'"
    )


class PaymentOut(BaseModel):
    id: str  # the public id ("pay_…"), never the sequential database id
    store: str
    terminal: str
    amount_cents: int
    currency: str
    status: str
    mock_reference: Optional[str] = None
    transfer_reference: Optional[str] = None  # bank transfer: the deposit's 7-digit reference
    created_at: datetime
    updated_at: datetime
