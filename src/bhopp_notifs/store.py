"""SQLite: every order seen, every chat message owed, and the settings.

Rows are written before anything is sent, so a restart resumes instead of repeating.
Shared by the poll loop and the slash commands, hence the lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .models import Order, TERMINAL
from .text import MAX_MESSAGE, parse_time, utc_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL, sold_at TEXT NOT NULL,
    gross_e4 INTEGER NOT NULL, net_e4 INTEGER NOT NULL,
    alert TEXT NOT NULL, alert_tries INTEGER NOT NULL DEFAULT 0,
    closed INTEGER NOT NULL DEFAULT 0, reminded INTEGER NOT NULL DEFAULT 0,
    cancel_state TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS orders_open ON orders (closed);
CREATE TABLE IF NOT EXISTS outbox (
    order_id TEXT NOT NULL, step TEXT NOT NULL, conversation_id TEXT NOT NULL,
    body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    message_id TEXT, message_at TEXT, UNIQUE (order_id, step));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass(frozen=True)
class OrderRow:
    order: Order
    state: str
    alert: str
    alert_tries: int
    reminded: bool
    cancel_state: str


@dataclass(frozen=True)
class Step:
    order_id: str
    step: str
    conversation_id: str
    body: str
    status: str
    attempts: int
    message_at: str | None


def _e4(value: Decimal) -> int:
    return int(value * 10000)


class Store:
    def __init__(self, path):
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        with self._lock:
            self.db.close()

    # -------------------------------------------------------------- settings
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
            else:
                self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                                (key, value))
            self.db.commit()

    # ---------------------------------------------------------------- orders
    def known(self, order_id: str) -> bool:
        with self._lock:
            return self.db.execute("SELECT 1 FROM orders WHERE id = ?",
                                   (order_id,)).fetchone() is not None

    def add_order(self, order: Order, *, alert: str = "pending", now: datetime,
                  steps: tuple[tuple[str, str], ...] = ()) -> None:
        """The order and the chat messages it is owed, in one transaction."""
        sold = utc_iso(order.created_at or now)
        with self._lock:
            self.db.execute(
                "INSERT OR IGNORE INTO orders (id, state, data, sold_at, gross_e4, net_e4, alert, "
                "closed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (order.id, order.state, json.dumps(order.raw), sold, _e4(order.gross),
                 _e4(order.net), alert, int(order.state in TERMINAL)))
            for step, body in steps:
                self._queue(order.id, order.conversation_id, step, body)
            self.db.commit()

    def row(self, order_id: str) -> OrderRow | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r) -> OrderRow:
        order = Order.from_api(json.loads(r["data"]))
        return OrderRow(order, r["state"], r["alert"], r["alert_tries"], bool(r["reminded"]),
                        r["cancel_state"])

    def _rows(self, where: str, *args) -> list[OrderRow]:
        with self._lock:
            rows = self.db.execute(f"SELECT * FROM orders WHERE {where} ORDER BY sold_at",
                                   args).fetchall()
        return [self._row(r) for r in rows]

    def open_rows(self) -> list[OrderRow]:
        return self._rows("closed = 0")

    def unalerted(self) -> list[OrderRow]:
        return self._rows("alert IN ('pending', 'sending')")

    def begin_alert(self, order_id: str) -> None:
        self._exec("UPDATE orders SET alert = 'sending', alert_tries = alert_tries + 1 "
                   "WHERE id = ?", order_id)

    def alert_sent(self, order_id: str) -> None:
        self._exec("UPDATE orders SET alert = 'sent' WHERE id = ?", order_id)

    def update_order(self, order: Order) -> None:
        self._exec("UPDATE orders SET state = ?, data = ?, closed = ? WHERE id = ?",
                   order.state, json.dumps(order.raw), int(order.state in TERMINAL), order.id)

    def close_order(self, order_id: str, state: str) -> None:
        self._exec("UPDATE orders SET state = ?, closed = 1 WHERE id = ?", state, order_id)

    def mark_reminded(self, order_id: str) -> None:
        self._exec("UPDATE orders SET reminded = 1 WHERE id = ?", order_id)

    def claim_cancel(self, order_id: str, expected: str, new: str) -> bool:
        """Move the cancel state only if it is still `expected`: one caller wins."""
        with self._lock:
            cur = self.db.execute("UPDATE orders SET cancel_state = ? WHERE id = ? AND "
                                  "cancel_state = ?", (new, order_id, expected))
            self.db.commit()
            return cur.rowcount == 1

    def _exec(self, sql: str, *args) -> None:
        with self._lock:
            self.db.execute(sql, args)
            self.db.commit()

    # ---------------------------------------------------------------- outbox
    def _queue(self, order_id, conversation_id, step, body) -> None:
        self.db.execute("INSERT OR IGNORE INTO outbox (order_id, step, conversation_id, body, "
                        "status) VALUES (?, ?, ?, ?, 'pending')",
                        (order_id, step, conversation_id, body[:MAX_MESSAGE]))

    def queue(self, order: Order, step: str, body: str) -> None:
        with self._lock:
            self._queue(order.id, order.conversation_id, step, body)
            self.db.commit()

    @staticmethod
    def _step(r) -> Step:
        return Step(r["order_id"], r["step"], r["conversation_id"], r["body"], r["status"],
                    r["attempts"], r["message_at"])

    def due_steps(self) -> list[Step]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM outbox WHERE status IN ('pending', 'sending') "
                                   "ORDER BY rowid").fetchall()
        return [self._step(r) for r in rows]

    def step(self, order_id: str, step: str) -> Step | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM outbox WHERE order_id = ? AND step = ?",
                                (order_id, step)).fetchone()
        return self._step(r) if r else None

    def steps_of(self, order_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute(
                "SELECT step FROM outbox WHERE order_id = ? ORDER BY rowid", (order_id,))]

    def begin_step(self, s: Step) -> None:
        self._exec("UPDATE outbox SET status = 'sending', attempts = attempts + 1 "
                   "WHERE order_id = ? AND step = ?", s.order_id, s.step)

    def step_sent(self, s: Step, message_id, message_at: str) -> None:
        self._exec("UPDATE outbox SET status = 'sent', message_id = ?, message_at = ? "
                   "WHERE order_id = ? AND step = ?", message_id, message_at, s.order_id, s.step)

    def step_failed(self, s: Step) -> None:
        self._exec("UPDATE outbox SET status = 'failed' WHERE order_id = ? AND step = ?",
                   s.order_id, s.step)

    def bodies_of(self, order_id: str) -> set[str]:
        with self._lock:
            return {r[0].strip() for r in self.db.execute(
                "SELECT body FROM outbox WHERE order_id = ?", (order_id,))}

    def awaiting_cancel(self, limit: int) -> list[OrderRow]:
        """Open pending orders that were told they may type "cancel"."""
        with self._lock:
            rows = self.db.execute(
                "SELECT o.* FROM orders o JOIN outbox b ON b.order_id = o.id AND b.step = 'afk' "
                "AND b.status = 'sent' WHERE o.closed = 0 AND o.state = 'pending' "
                "AND o.cancel_state = '' ORDER BY o.sold_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    # ----------------------------------------------------------------- stats
    def totals_since(self, since: datetime | None) -> tuple[int, Decimal, Decimal]:
        """(sales, gross, net) excluding cancelled orders."""
        where, args = "state != 'cancelled'", []
        if since is not None:
            where += " AND sold_at >= ?"
            args.append(utc_iso(since))
        with self._lock:
            r = self.db.execute("SELECT COUNT(*), COALESCE(SUM(gross_e4), 0), "
                                f"COALESCE(SUM(net_e4), 0) FROM orders WHERE {where}",
                                args).fetchone()
        return r[0], Decimal(r[1]) / 10000, Decimal(r[2]) / 10000


class Prefs:
    """The settings changed from Discord. Automations that write to BHOPP start off."""

    TEXTS = {
        "auto_reply": ("Hey {buyer}, thanks for buying {items}! I've been notified and will "
                       "deliver as soon as I can."),
        "afk": ('Seller is currently offline for another {left}, if you would like to cancel '
                'please type "cancel"'),
        "thanks": ("Thanks for your order, {buyer}! Once you've confirmed you got everything, "
                   "a good review would really help me out ⭐"),
        "cancel_ack": ("Got your cancel request. The seller has been notified and will confirm "
                       "it as soon as possible."),
        "cancel_done": "Your order has been cancelled and the full amount refunded.",
        "cancel_declined": ("The seller has seen your cancel request and will still deliver "
                            "your order."),
    }

    def __init__(self, store: Store):
        self.store = store

    def _flag(self, key: str, default: bool = False) -> bool:
        return self.store.get(key, "1" if default else "0") == "1"

    def _set_flag(self, key: str, on: bool) -> None:
        self.store.set(key, "1" if on else "0")

    @property
    def ping(self) -> str:
        return self.store.get("ping", "owner")

    @ping.setter
    def ping(self, value: str) -> None:
        self.store.set("ping", value)

    def ping_mention(self) -> str:
        """Who a sale alert pings. The only path that can ever reach @everyone."""
        return {"owner": "owner", "everyone": "everyone"}.get(self.ping, "none")

    @property
    def cancel_mode(self) -> str:
        return self.store.get("cancel_mode", "ask")

    @cancel_mode.setter
    def cancel_mode(self, value: str) -> None:
        self.store.set("cancel_mode", value)

    auto_reply = property(lambda s: s._flag("auto_reply"),
                          lambda s, on: s._set_flag("auto_reply", on))
    thanks = property(lambda s: s._flag("thanks"), lambda s, on: s._set_flag("thanks", on))
    online = property(lambda s: s._flag("online"), lambda s, on: s._set_flag("online", on))
    reminders = property(lambda s: s._flag("reminders", True),
                         lambda s, on: s._set_flag("reminders", on))

    def text(self, kind: str) -> str:
        return self.store.get(f"text:{kind}") or self.TEXTS[kind]

    def set_text(self, kind: str, value: str | None) -> None:
        self.store.set(f"text:{kind}", value)

    # ------------------------------------------------------------------ AFK
    @property
    def afk_until(self) -> datetime | None:
        return parse_time(self.store.get("afk_until"))

    @property
    def afk_since(self) -> datetime | None:
        return parse_time(self.store.get("afk_since"))

    def start_afk(self, now: datetime, length: timedelta) -> datetime:
        until = now + length
        if self.afk_since is None:
            self.store.set("afk_since", utc_iso(now))
        self.store.set("afk_until", utc_iso(until))
        return until

    def end_afk(self) -> datetime | None:
        """Ends AFK; returns when it started, or None if it wasn't on."""
        since = self.afk_since
        self.store.set("afk_until", None)
        self.store.set("afk_since", None)
        return since


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
