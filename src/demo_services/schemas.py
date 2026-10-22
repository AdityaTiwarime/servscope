"""Request and response bodies shared by the demo services. Money is always in paise (int)."""

from __future__ import annotations

from pydantic import BaseModel, Field

SKU = r"^[a-z]{1,32}$"
# Fake tokens only; real card data never enters the demo. (A pattern, not a password.)
CARD_TOKEN = r"^tok_demo_[a-z]{1,32}$"  # noqa: S105


class Item(BaseModel):
    sku: str = Field(pattern=SKU)
    qty: int = Field(ge=1, le=20)


class Checkout(BaseModel):
    items: list[Item] = Field(min_length=1, max_length=20)
    card_token: str = Field(default="tok_demo_visa", pattern=CARD_TOKEN)


class OrderRequest(Checkout):
    pass


class Order(BaseModel):
    order_id: str
    total_paise: int
    charge_id: str
    status: str = "paid"


class ReservationRequest(BaseModel):
    order_id: str
    items: list[Item] = Field(min_length=1, max_length=20)


class Reservation(BaseModel):
    reservation_id: str


class ChargeRequest(BaseModel):
    order_id: str
    amount_paise: int = Field(gt=0)
    card_token: str = Field(pattern=CARD_TOKEN)


class Charge(BaseModel):
    charge_id: str
    order_id: str
    amount_paise: int
    status: str = "captured"  # or "voided" once reversed


class VoidResult(BaseModel):
    voided: bool  # True when a captured charge was reversed by this call
    charge_id: str | None = None


class Problem(BaseModel):
    """Error body for every demo service (shape of RFC 9457 problem details)."""

    title: str
    status: int
    detail: str = ""
