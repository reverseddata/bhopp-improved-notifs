"""BHOPP's API, as bhopp.com itself calls it. Undocumented, so it may change."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import datetime, timezone

import aiohttp

from . import USER_AGENT
from .models import Order
from .text import redact

log = logging.getLogger(__name__)
BASE = "https://api.sublimelistings.com:5000"
MAX_PAGES = 20
PAST_PAGES = 3
IMAGE_TIMEOUT = 5.0


class BhoppError(RuntimeError):
    """status 0 means no answer: a write may or may not have reached the site."""

    def __init__(self, status: int, text: str):
        super().__init__(redact(f"BHOPP answered {status}: {text[:200]}" if status
                                else f"no answer from BHOPP: {text[:200]}"))
        self.status = status


def _claims(token: str) -> dict:
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except Exception:  # noqa: BLE001 - not a JWT: no expiry or id known
        return {}


def parse_orders(rows, source: str) -> list[Order]:
    out = []
    for row in rows or []:
        try:
            out.append(Order.from_api(row))
        except (ValueError, TypeError, ArithmeticError, AttributeError) as e:
            log.warning("skipped a malformed order in %s: %s", source, e)
    return out


class BhoppApi:
    def __init__(self, token: str, *, base: str = BASE, session_factory=None,
                 timeout: float = 15.0):
        self.token = (token or "").strip().strip('"')
        self.base = base.rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session_factory = session_factory or (
            lambda: aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}))
        self._session = None
        self._images: dict[str, str] = {}

    @property
    def expires_at(self) -> datetime | None:
        exp = _claims(self.token).get("exp")
        return datetime.fromtimestamp(int(exp), tz=timezone.utc) if exp else None

    @property
    def seller_id(self) -> str:
        return str(_claims(self.token).get("user") or "")

    async def close(self):
        if self._session is not None:
            await self._session.close()

    async def _call(self, method: str, path: str, *, auth: bool = True, **kw):
        if self._session is None or getattr(self._session, "closed", False):
            self._session = self._session_factory()
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        try:
            async with self._session.request(method, self.base + path, headers=headers,
                                             timeout=self.timeout, **kw) as r:
                text = await r.text()
                if r.status >= 400:
                    raise BhoppError(r.status, text)
                return json.loads(text) if text else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
            raise BhoppError(0, type(e).__name__) from None

    async def _orders(self, state: str, max_pages: int) -> list[Order]:
        rows, page, pages = [], 1, 1
        while page <= pages and page <= max_pages:
            data = await self._call("GET", "/seller/orders",
                                    params={"state": state, "page": page})
            if not isinstance(data, dict) or not isinstance(data.get("orders"), list):
                raise BhoppError(0, "unexpected /seller/orders payload")
            rows += data["orders"]
            pages = int(data.get("pages") or 1)
            page += 1
        return parse_orders(rows, state)

    async def open_orders(self) -> list[Order]:
        return await self._orders("open", MAX_PAGES)

    async def past_orders(self) -> list[Order]:
        return await self._orders("past", PAST_PAGES)

    async def messages(self, conversation_id: str) -> list[dict]:
        """Oldest first (BHOPP answers newest first)."""
        data = await self._call("GET", f"/conversations/{conversation_id}/messages")
        return sorted(data.get("messages") or [], key=lambda m: m.get("createdAt") or "")

    async def send_message(self, conversation_id: str, body: str) -> dict:
        data = await self._call("POST", f"/conversations/{conversation_id}/messages",
                                json={"body": body})
        return data.get("message") or {}

    async def cancel_order(self, order_id: str) -> dict:
        """Refunds the buyer in full and can't be undone."""
        return (await self._call("PATCH", f"/orders/{order_id}/cancel")).get("order") or {}

    async def product_image(self, product_id: str, name: str) -> str:
        """The item's picture, or "" if it can't be found quickly. Never raises."""
        key = product_id or name
        if key in self._images:
            return self._images[key]
        try:
            data = await asyncio.wait_for(
                self._call("GET", "/products", auth=False, params={"search": name, "page": 1}),
                IMAGE_TIMEOUT)
        except (BhoppError, asyncio.TimeoutError):
            return ""
        rows = (data.get("products") or []) if isinstance(data, dict) else []
        hit = next((p for p in rows if product_id and p.get("id") == product_id), None) or next(
            (p for p in rows if str(p.get("name", "")).lower() == name.lower()), None)
        url = str((hit or {}).get("avatar") or "")
        url = url if url.startswith("https://") else ""
        self._images[key] = url
        return url
