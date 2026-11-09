"""Fakes for BHOPP and Discord. Nothing in the tests touches the network."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bhopp_notifs.bhopp import BhoppError
from bhopp_notifs.engine import SaleWatcher
from bhopp_notifs.models import Order
from bhopp_notifs.store import Prefs, Store

START = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = START

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def order_dict(oid="o1", state="pending", created=START, qty=2, cents=899, buyer="Buyer1"):
    gross = qty * cents * 100
    return {"id": oid, "state": state, "buyer": {"id": "buyer-id", "username": buyer},
            "seller": {"id": "seller-id", "username": "SellerName"},
            "items": [{"offerId": "of1", "productId": "p1", "name": "Harvester", "quantity": qty,
                       "unitAmount": cents}],
            "totals": {"gross": gross, "fee": gross * 5 // 100, "feeBps": 500,
                       "net": gross - gross * 5 // 100, "currency": "USD"},
            "conversationId": f"conv-{oid}", "createdAt": created.isoformat()}


class FakeApi:
    expires_at = None
    seller_id = "seller-id"
    token = "fake"

    def __init__(self, clock):
        self.clock = clock
        self.open, self.past = {}, {}
        self.chats: dict[str, list[dict]] = {}
        self.sent: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.open_error = None
        self.send_error = None
        self.cancel_error = None
        self.past_reads = 0
        self.counter = 0

    def add(self, **kw) -> dict:
        d = order_dict(**kw)
        self.open[d["id"]] = d
        return d

    def set_state(self, oid, state):
        self.open[oid]["state"] = state

    def finish(self, oid, state="completed"):
        d = self.open.pop(oid)
        d["state"] = state
        self.past[oid] = d

    def say(self, oid, body, sender="buyer-id", minutes_after=1):
        self.counter += 1
        at = self.clock() + timedelta(minutes=minutes_after)
        self.chats.setdefault(f"conv-{oid}", []).append(
            {"id": f"m{self.counter}", "sender": sender, "kind": "text", "body": body,
             "createdAt": at.isoformat()})

    async def open_orders(self):
        if self.open_error:
            raise self.open_error
        return [Order.from_api(d) for d in self.open.values()]

    async def past_orders(self):
        self.past_reads += 1
        return [Order.from_api(d) for d in self.past.values()]

    async def messages(self, conversation_id):
        return list(self.chats.get(conversation_id, []))

    async def send_message(self, conversation_id, body):
        if self.send_error:
            raise self.send_error
        self.sent.append((conversation_id, body))
        self.counter += 1
        msg = {"id": f"m{self.counter}", "sender": "seller-id", "kind": "text", "body": body,
               "createdAt": self.clock().isoformat()}
        self.chats.setdefault(conversation_id, []).append(msg)
        return msg

    async def cancel_order(self, order_id):
        if self.cancel_error:
            raise self.cancel_error
        self.cancelled.append(order_id)
        return {}

    async def product_image(self, product_id, name):
        return "https://cdn.bhopp.com/x.webp"


class FakePoster:
    def __init__(self):
        self.posts = []
        self.fail = None
        self.posted_ids = set()

    async def post(self, post):
        if self.fail:
            raise self.fail
        self.posts.append(post)

    async def already_posted(self, order_id):
        return order_id in self.posted_ids

    def titled(self, text):
        return [p for p in self.posts if text in p.embed.get("title", "")]


class Rig:
    def __init__(self, path):
        self.path = path
        self.clock = Clock()
        self.api = FakeApi(self.clock)
        self.poster = FakePoster()
        self.boot()

    def boot(self, **kw):
        """A fresh process on the same database."""
        self.store = Store(self.path)
        self.prefs = Prefs(self.store)
        self.watcher = SaleWatcher(self.api, self.store, self.prefs, self.poster,
                                   now=self.clock, **kw)

    def tick(self):
        asyncio.run(self.watcher.tick())

    def start(self):
        """Past the first-run baseline, with no orders yet."""
        self.tick()
        self.poster.posts.clear()

    def tick_later(self, seconds=30):
        self.clock.advance(seconds=seconds)
        self.tick()

    def bodies(self):
        return [b for _, b in self.api.sent]

    def afk(self, **kw):
        self.prefs.start_afk(self.clock(), timedelta(**kw))

    def sale_with_afk(self, oid="o1"):
        self.api.add(oid=oid, created=self.clock())
        self.tick()
        assert any("offline" in b for b in self.bodies())
        self.poster.posts.clear()


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path / "t.sqlite3")


def http_500():
    return BhoppError(500, "boom")
