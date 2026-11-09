"""BHOPP's order, in dollars. unitAmount is in cents; totals are in 1/100 of a cent."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .text import parse_time

SITE = "https://bhopp.com"
TERMINAL = ("completed", "cancelled")
DEFAULT_FEE_BPS = 500


def _totals(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)) / 10000
    except ArithmeticError:
        return None


@dataclass(frozen=True)
class Item:
    name: str
    quantity: int
    unit: Decimal
    product_id: str = ""


@dataclass(frozen=True)
class Order:
    id: str
    state: str
    buyer_id: str
    buyer_name: str
    seller_id: str
    seller_name: str
    items: tuple[Item, ...]
    gross: Decimal
    fee: Decimal
    net: Decimal
    fee_bps: int
    conversation_id: str
    created_at: datetime | None
    raw: dict = field(default_factory=dict, compare=False, hash=False, repr=False)

    @classmethod
    def from_api(cls, d: dict) -> "Order":
        if not isinstance(d, dict) or not d.get("id"):
            raise ValueError("order without an id")
        items = tuple(
            Item(str(i.get("name") or "Item"), max(1, int(i.get("quantity") or 1)),
                 Decimal(str(i.get("unitAmount") or 0)) / 100, str(i.get("productId") or ""))
            for i in d.get("items") or [])
        totals = d.get("totals") or {}
        bps = int(totals.get("feeBps") or DEFAULT_FEE_BPS)
        gross = _totals(totals.get("gross"))
        if gross is None:
            gross = sum((i.unit * i.quantity for i in items), Decimal(0))
        fee = _totals(totals.get("fee"))
        if fee is None:
            fee = gross * bps / 10000
        net = _totals(totals.get("net"))
        if net is None:
            net = gross - fee
        buyer, seller = d.get("buyer") or {}, d.get("seller") or {}
        return cls(str(d["id"]), str(d.get("state") or "pending"), str(buyer.get("id") or ""),
                   str(buyer.get("username") or "Unknown buyer"), str(seller.get("id") or ""),
                   str(seller.get("username") or "Seller"), items, gross, fee, net, bps,
                   str(d.get("conversationId") or ""), parse_time(d.get("createdAt")), d)

    @property
    def chat_url(self) -> str:
        return (f"{SITE}/account/messages/{self.conversation_id}" if self.conversation_id
                else f"{SITE}/seller/sales")

    @property
    def summary(self) -> str:
        return ", ".join(f"{i.quantity}× {i.name}" for i in self.items) or "an item"
