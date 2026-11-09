"""The poll loop: finds new sales, alerts Discord, and works the order chats.

Each pass, in order: token and AFK checks, new orders, Discord alerts, order state changes,
chat messages owed, buyers typing "cancel", reminders. Every Discord alert and chat message
is recorded in SQLite before it is sent, so a restart resumes instead of repeating it. If an
earlier send's outcome is unknown, the bot looks first and only sends if it isn't there.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta

from . import embeds
from .bhopp import BhoppError
from .embeds import Post
from .models import Order, TERMINAL
from .store import OrderRow, Prefs, Step, Store, utcnow
from .text import format_left, md, money, parse_time, redact, render, utc_iso

log = logging.getLogger(__name__)

CANCEL_WORD = re.compile(
    r"^\W*(?:please\s+|pls\s+)?cancel(?:\s+(?:it|this|that|the|my|order|please|pls))*\W*$", re.I)
MAX_STEP_ATTEMPTS = 5
MAX_ALERT_TRIES = 5
CHAT_POLL = timedelta(seconds=20)
PAST_POLL = timedelta(seconds=60)
MAX_CHATS_PER_PASS = 25
GREET_MAX_AGE = timedelta(hours=6)     # a late start doesn't greet day-old orders
THANKS_MAX_AGE = timedelta(days=14)
THANKS_STATES = ("delivered", "completed")
GIVE_UP_AGE = timedelta(days=30)       # an order that vanished from every list
BUYER_CAN_CANCEL_HOURS = 12            # from bhopp.com's own order screen
REMIND_HOURS = 10
AUTH_RETRY = 60.0
FAILURES_BEFORE_ALERT = 10
TOKEN_WARNINGS = ((timedelta(hours=24), "24 hours"), (timedelta(hours=3), "3 hours"))
STEP_LABELS = {"afk": "AFK notice", "auto_reply": "Auto-reply"}


class SaleWatcher:
    def __init__(self, api, store: Store, prefs: Prefs, poster, *, dry_run: bool = False,
                 poll_interval: float = 15.0, now=utcnow):
        self.api, self.store, self.prefs, self.poster = api, store, prefs, poster
        self.dry_run = dry_run
        self.poll_interval = poll_interval
        self.now = now
        self.last_ok: datetime | None = None
        self.last_error = ""
        self.open_count = 0
        self.failures = 0
        self._warned_failing = False
        self._last_chat_poll: datetime | None = None
        self._last_past_poll: datetime | None = None
        self._flush_lock = asyncio.Lock()

    # ------------------------------------------------------------------ loop
    async def run(self) -> None:
        while True:
            delay = self.poll_interval
            try:
                await self.tick()
                await self._recovered()
            except BhoppError as e:
                delay = await self._poll_failed(e)
            except Exception as e:  # noqa: BLE001 - one bad pass never stops the alerts
                log.exception("poll pass failed")
                self.last_error = redact(f"{type(e).__name__}: {e}")
            await asyncio.sleep(delay)

    async def tick(self) -> None:
        now = self.now()
        await self._token_warnings(now)
        await self._afk_expiry(now)
        orders = await self.api.open_orders()
        live = {o.id: o for o in orders}
        self.open_count = len(live)
        if self.store.get("baseline") is None:
            await self._baseline(orders, now)
        else:
            self._take_new(orders, now)
            await self._send_alerts()
            await self._track(live, now)
            await self.flush()
            await self._cancel_watch(now)
            await self.flush()
            await self._reminders(now)
        self.last_ok = now
        self.last_error = ""

    async def _post(self, post: Post) -> bool:
        """For alerts that must never stop the pass. False if Discord didn't take it."""
        try:
            await self.poster.post(post)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("could not post to Discord: %s", redact(e))
            return False

    async def _poll_failed(self, e: BhoppError) -> float:
        self.last_error = str(e)
        self.failures += 1
        log.warning("poll failed: %s", e)
        if e.status in (401, 403):
            key = f"auth_alerted:{utc_iso(self.api.expires_at) if self.api.expires_at else ''}"
            if self.store.get(key) is None and await self._post(Post(
                    embeds.system_embed(
                        "🔑 BHOPP rejected the token",
                        "No sales can be seen until BHOPP_TOKEN is replaced. Log in on bhopp.com, "
                        "copy `localStorage.getItem('bhopp.token')` and update the variable.",
                        "error"), mention="owner")):
                self.store.set(key, "1")
            return AUTH_RETRY
        if self.failures >= FAILURES_BEFORE_ALERT and not self._warned_failing:
            self._warned_failing = await self._post(Post(
                embeds.system_embed("⚠️ Can't reach BHOPP",
                                    f"The last {self.failures} checks failed: {e}", "warn"),
                mention="owner"))
        return self.poll_interval

    async def _recovered(self) -> None:
        if self._warned_failing:
            await self._post(Post(embeds.system_embed("✅ BHOPP is reachable again",
                                                      "Sale alerts are back.", "ok")))
        self.failures, self._warned_failing = 0, False

    # ---------------------------------------------------------- token and AFK
    async def _token_warnings(self, now: datetime) -> None:
        exp = self.api.expires_at
        if exp is None or now >= exp:
            return
        for before, label in TOKEN_WARNINGS:
            key = f"token_warned:{int(exp.timestamp())}:{label}"
            if exp - now <= before and self.store.get(key) is None:
                if await self._post(Post(embeds.system_embed(
                        "🔑 BHOPP token expires soon",
                        f"It runs out <t:{int(exp.timestamp())}:R> (under {label}). Log in on "
                        "bhopp.com, copy `localStorage.getItem('bhopp.token')` and update "
                        "BHOPP_TOKEN, or sale alerts will stop.", "warn"), mention="owner")):
                    self.store.set(key, "1")
                break

    async def _afk_expiry(self, now: datetime) -> None:
        until = self.prefs.afk_until
        if until is not None and now >= until:
            await self.post_afk_summary(self.prefs.end_afk(), "⏱️ AFK time is up")

    async def post_afk_summary(self, since: datetime | None, title: str) -> str:
        count, _, net = self.store.totals_since(since)
        text = (f"{count} sale(s) came in while you were away, worth {money(net)} after fees."
                if count else "No sales came in while you were away.")
        await self._post(Post(embeds.system_embed(title, text, "info")))
        return text

    # ------------------------------------------------------------- new orders
    async def _baseline(self, orders: list[Order], now: datetime) -> None:
        """First run ever: what is already open is not news."""
        for o in orders:
            self.store.add_order(o, alert="skipped", now=now)
        self.store.set("baseline", utc_iso(now))
        await self._post(Post(embeds.system_embed(
            "✅ BHOPP sale alerts are on",
            f"{len(orders)} open order(s) were already there, so they were not alerted. "
            "Use /status to see the settings.", "ok")))

    def values(self, o: Order, **extra) -> dict:
        return {"buyer": o.buyer_name, "seller": o.seller_name, "items": o.summary,
                "total": money(o.gross), "order": o.id[-6:], **extra}

    def _first_steps(self, o: Order, now: datetime) -> tuple[tuple[str, str], ...]:
        if o.state != "pending" or (o.created_at and now - o.created_at > GREET_MAX_AGE):
            return ()
        until = self.prefs.afk_until
        if until is not None and until > now:
            text = render(self.prefs.text("afk"),
                          **self.values(o, left=format_left(until - now),
                                        until=until.strftime("%H:%M UTC on %b %d")))
            return (("afk", text),)
        if self.prefs.auto_reply:
            return (("auto_reply", render(self.prefs.text("auto_reply"), **self.values(o))),)
        return ()

    def _take_new(self, orders: list[Order], now: datetime) -> None:
        fresh = [o for o in orders if not self.store.known(o.id)]
        for o in sorted(fresh, key=lambda x: x.created_at or now):
            self.store.add_order(o, now=now, steps=self._first_steps(o, now))

    async def _send_alerts(self) -> None:
        for row in self.store.unalerted():
            try:
                await self._alert(row)
            except Exception as e:  # noqa: BLE001 - stays unsent and is retried next pass
                log.warning("could not alert order %s: %s", row.order.id, redact(e))

    async def _alert(self, row: OrderRow) -> None:
        o = row.order
        if row.alert == "sending" and await self.poster.already_posted(o.id):
            self.store.alert_sent(o.id)
            return
        self.store.begin_alert(o.id)
        if row.alert_tries >= MAX_ALERT_TRIES:  # Discord keeps refusing the embed: plain text
            await self.poster.post(Post(
                {"description": f"{embeds.sale_summary(o)}\n{o.chat_url}",
                 "footer": {"text": f"Order {o.id}"}},
                embeds.sale_summary(o), self.prefs.ping_mention()))
            self.store.alert_sent(o.id)
            return
        first = o.items[0] if o.items else None
        image = await self.api.product_image(first.product_id, first.name) if first else ""
        actions = tuple(STEP_LABELS[s] for s in self.store.steps_of(o.id) if s in STEP_LABELS)
        await self.poster.post(Post(embeds.sale_embed(o, image, actions), embeds.sale_summary(o),
                                    self.prefs.ping_mention(),
                                    link=("Open order chat", o.chat_url)))
        self.store.alert_sent(o.id)

    # ----------------------------------------------------------- state changes
    async def _track(self, live: dict[str, Order], now: datetime) -> None:
        missing = []
        for row in self.store.open_rows():
            current = live.get(row.order.id)
            if current is None:
                missing.append(row)
            elif current.state != row.state:
                await self._changed(row, current, now)
        if missing and (self._last_past_poll is None
                        or now - self._last_past_poll >= PAST_POLL):
            self._last_past_poll = now
            await self._resolve_missing(missing, now)

    async def _resolve_missing(self, missing: list[OrderRow], now: datetime) -> None:
        try:
            past = {o.id: o for o in await self.api.past_orders()}
        except BhoppError as e:
            log.warning("could not read past orders: %s", e)
            return
        for row in missing:
            current = past.get(row.order.id)
            if current is not None:
                if current.state != row.state or current.state in TERMINAL:
                    await self._changed(row, current, now)
            elif row.order.created_at and now - row.order.created_at > GIVE_UP_AGE:
                self.store.close_order(row.order.id, "gone")

    async def _changed(self, row: OrderRow, current: Order, now: datetime) -> None:
        self.store.update_order(current)
        # Thanked when the seller marks it delivered. Straight to completed means that mark
        # fell between two polls; anything else (cancelled, disputed) never gets one.
        delivered_now = current.state == "delivered" or (
            current.state == "completed" and row.state == "pending")
        if (delivered_now and self.prefs.thanks
                and now - (current.created_at or now) <= THANKS_MAX_AGE):
            self.store.queue(current, "thanks",
                             render(self.prefs.text("thanks"), **self.values(current)))
        if current.state == "completed":
            await self._post(Post(embeds.completed_embed(current),
                                  link=("Open order chat", current.chat_url)))
        elif current.state == "cancelled" and row.cancel_state != "done":
            await self._post(Post(embeds.cancelled_embed(current), mention="owner"))
        elif current.state == "disputed":
            await self._post(Post(embeds.disputed_embed(current), mention="owner",
                                  link=("Open order chat", current.chat_url)))

    # ------------------------------------------------------------ chat outbox
    async def flush(self) -> None:
        async with self._flush_lock:
            for s in self.store.due_steps():
                try:
                    await self._deliver(s)
                except BhoppError as e:
                    await self._delivery_failed(s, e)
                except Exception:  # noqa: BLE001 - one bad message never stops the others
                    log.exception("sending %s for order %s failed", s.step, s.order_id)

    async def _deliver(self, s: Step) -> None:
        row = self.store.row(s.order_id)
        if s.step == "thanks" and row.state not in THANKS_STATES:
            self.store.step_failed(s)  # cancelled or disputed while it waited to be sent
            return
        if s.status == "sending" and not self.dry_run:
            seller = row.order.seller_id or self.api.seller_id
            for m in await self.api.messages(s.conversation_id):
                if m.get("sender") == seller and (m.get("body") or "").strip() == s.body.strip():
                    self.store.step_sent(s, m.get("id"), m.get("createdAt") or utc_iso(self.now()))
                    return
        self.store.begin_step(s)
        if self.dry_run:
            if await self._post(Post(embeds.dry_run_embed(row.order, s.step, s.body))):
                self.store.step_sent(s, "dry-run", utc_iso(self.now()))
            return
        sent = await self.api.send_message(s.conversation_id, s.body)
        self.store.step_sent(s, sent.get("id"), sent.get("createdAt") or utc_iso(self.now()))

    async def _delivery_failed(self, s: Step, e: BhoppError) -> None:
        log.warning("sending %s for order %s failed: %s", s.step, s.order_id, e)
        if s.attempts + 1 < MAX_STEP_ATTEMPTS:
            return
        self.store.step_failed(s)
        row = self.store.row(s.order_id)
        await self._post(Post(embeds.message_failed_embed(row.order, s.step, str(e)),
                              mention="owner", link=("Open order chat", row.order.chat_url)))

    # ------------------------------------------------------------ cancel flow
    async def _cancel_watch(self, now: datetime) -> None:
        if self._last_chat_poll is not None and now - self._last_chat_poll < CHAT_POLL:
            return
        self._last_chat_poll = now
        for row in self.store.awaiting_cancel(MAX_CHATS_PER_PASS):
            try:
                await self._check_chat(row)
            except BhoppError as e:
                log.warning("could not read the chat of order %s: %s", row.order.id, e)

    async def _check_chat(self, row: OrderRow) -> None:
        o = row.order
        cutoff = parse_time(self.store.step(o.id, "afk").message_at)
        if cutoff is None:
            return
        after = []
        for m in await self.api.messages(o.conversation_id):
            at = parse_time(m.get("createdAt"))
            if m.get("kind", "text") == "text" and at is not None and at > cutoff:
                after.append(m)
        if not any(m.get("sender") == o.buyer_id and CANCEL_WORD.match(m.get("body") or "")
                   for m in after):
            return
        ours = self.store.bodies_of(o.id)
        seller = o.seller_id or self.api.seller_id
        seller_wrote = any(m.get("sender") == seller and (m.get("body") or "").strip() not in ours
                           for m in after)
        if self.prefs.cancel_mode == "auto" and not seller_wrote:
            await self._auto_cancel(o)
        else:
            await self._ask_owner(o, seller_wrote=seller_wrote)

    async def _ask_owner(self, o: Order, *, seller_wrote: bool = False, error: str = "") -> None:
        # Discord first: if this fails, the next pass sees the "cancel" again.
        await self.poster.post(Post(embeds.cancel_request_embed(o, error, seller_wrote),
                                    f"🛑 **{md(o.buyer_name)}** wants to cancel",
                                    "owner", link=("Open order chat", o.chat_url),
                                    cancel_order=o.id))
        self.store.claim_cancel(o.id, "", "requested")
        self.store.claim_cancel(o.id, "cancelling", "requested")
        self.store.queue(o, "cancel_ack", render(self.prefs.text("cancel_ack"), **self.values(o)))

    async def _auto_cancel(self, o: Order) -> None:
        if not self.store.claim_cancel(o.id, "", "cancelling"):
            return
        try:
            await self._cancel(o)
        except BhoppError as e:
            await self._ask_owner(o, error=str(e))
            return
        await self._cancelled(o)

    async def _cancel(self, o: Order) -> None:
        if not self.dry_run:
            await self.api.cancel_order(o.id)

    async def _cancelled(self, o: Order) -> None:
        self.store.claim_cancel(o.id, "cancelling", "done")
        self.store.queue(o, "cancel_done", render(self.prefs.text("cancel_done"), **self.values(o)))
        await self._post(Post(embeds.cancel_done_embed(o, self.dry_run), mention="owner"))

    async def resolve_cancel(self, order_id: str, accept: bool) -> str:
        """The owner pressed Cancel and refund, or Keep order."""
        row = self.store.row(order_id)
        if row is None:
            return "That order is not known to the bot."
        o = row.order
        if not accept:
            if not self.store.claim_cancel(o.id, "requested", "declined"):
                return "That request was already handled."
            self.store.queue(o, "cancel_declined",
                             render(self.prefs.text("cancel_declined"), **self.values(o)))
            await self.flush()
            return f"Kept the order. {md(o.buyer_name)} was told you will still deliver."
        if not self.store.claim_cancel(o.id, "requested", "cancelling"):
            return "That request was already handled."
        try:
            current = next((x for x in await self.api.open_orders() if x.id == o.id), None)
            if current is None or current.state != "pending":
                self.store.claim_cancel(o.id, "cancelling", "moot")
                state = current.state if current else "no longer open"
                return (f"Nothing was cancelled: the order is {state}. "
                        f"If it still needs cancelling, do it on BHOPP: {o.chat_url}")
            await self._cancel(o)
        except BhoppError as e:
            self.store.claim_cancel(o.id, "cancelling", "requested")
            return (f"BHOPP did not cancel it: {e}. You can press the button again, or "
                    f"cancel it on BHOPP: {o.chat_url}")
        await self._cancelled(o)
        await self.flush()
        return f"Cancelled and refunded {money(o.gross)} to {md(o.buyer_name)}."

    # -------------------------------------------------------------- reminders
    async def _reminders(self, now: datetime) -> None:
        if not self.prefs.reminders:
            return
        for row in self.store.open_rows():
            o = row.order
            if row.reminded or row.state != "pending" or o.created_at is None:
                continue
            hours = (now - o.created_at).total_seconds() / 3600
            if hours >= REMIND_HOURS and await self._post(Post(
                    embeds.reminder_embed(o, int(hours), BUYER_CAN_CANCEL_HOURS), mention="owner",
                    link=("Open order chat", o.chat_url))):
                self.store.mark_reminded(o.id)
