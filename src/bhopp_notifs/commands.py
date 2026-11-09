"""What the slash commands do. Knows nothing about Discord, so it is tested directly."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import embeds
from .bhopp import BhoppError
from .embeds import Post
from .models import Order
from .store import Prefs, Store
from .text import MAX_MESSAGE, format_left, md, money, parse_duration, render

SAMPLE_IMAGE = "https://cdn.bhopp.com/public/bhopp/products/HarvesterImproved.webp"
PLACEHOLDERS = "{buyer} {seller} {items} {total} {order}, and in the AFK message {left} {until}"


def sample_order(now: datetime) -> Order:
    return Order.from_api({
        "id": "test-order", "state": "pending",
        "buyer": {"id": "b", "username": "ExampleBuyer"},
        "seller": {"id": "s", "username": "You"},
        "items": [{"name": "Harvester", "quantity": 2, "unitAmount": 899, "productId": "p"}],
        "totals": {"gross": 179800, "fee": 8990, "feeBps": 500, "net": 170810},
        "conversationId": "", "createdAt": now.isoformat(),
    })


class Commands:
    def __init__(self, owner_id: str, store: Store, prefs: Prefs, watcher, presence):
        self.owner_id, self.store, self.prefs = owner_id, store, prefs
        self.watcher, self.presence = watcher, presence

    def allowed(self, user_id) -> bool:
        return bool(self.owner_id) and str(user_id) == self.owner_id

    @property
    def _now(self) -> datetime:
        return self.watcher.now()

    # --------------------------------------------------------------- settings
    def alerts(self, ping: str | None = None, reminders: str | None = None) -> str:
        if ping:
            self.prefs.ping = ping
        if reminders:
            self.prefs.reminders = reminders == "on"
        who = {"owner": "pings you", "everyone": "pings @everyone", "off": "pings nobody"}
        text = (f"Sale alerts {who[self.prefs.ping]}. Reminders about undelivered orders are "
                f"**{'on' if self.prefs.reminders else 'off'}**.")
        if self.prefs.ping == "everyone":
            text += "\nThe bot needs the Mention Everyone permission in this channel."
        return text

    def _toggle(self, name: str, kind: str, label: str, state: str | None) -> str:
        if state:
            setattr(self.prefs, name, state == "on")
        on = getattr(self.prefs, name)
        return f"{label} is **{'on' if on else 'off'}**. Message:\n> {md(self.prefs.text(kind))}"

    def autoreply(self, state: str | None = None) -> str:
        return self._toggle("auto_reply", "auto_reply", "Auto-reply to new sales", state)

    def thanks(self, state: str | None = None) -> str:
        return self._toggle("thanks", "thanks", "Thank-you after you mark an order delivered", state)

    def online(self, state: str | None = None) -> str:
        if state:
            self.prefs.online = state == "on"
        if not self.prefs.online:
            return "Always online is **off**."
        return ("Always online is **on**. It connects within a minute." if state
                else f"Always online is **on**, {'connected' if self.presence.connected else 'not connected yet'}.")

    def set_message(self, kind: str, text: str | None = None) -> str:
        if text is not None:
            value = text.strip()
            if value.lower() == "default":
                self.prefs.set_text(kind, None)
            elif not value or len(value) > MAX_MESSAGE:
                return f"Not changed: the message must be 1 to {MAX_MESSAGE} characters."
            else:
                self.prefs.set_text(kind, value)
        return (f"**{kind}** message:\n> {md(self.prefs.text(kind))}\n"
                f"Placeholders: {md(PLACEHOLDERS)}. Use `default` to reset it.")

    def cancelmode(self, mode: str | None = None) -> str:
        if mode:
            self.prefs.cancel_mode = mode
        if self.prefs.cancel_mode == "auto":
            return ('When a buyer types "cancel" after an AFK notice, the bot **cancels and '
                    "refunds by itself** (only pending orders, and not if you wrote in that chat).")
        return ('When a buyer types "cancel" after an AFK notice, the bot **asks you first** '
                "with buttons here in Discord.")

    # -------------------------------------------------------------------- AFK
    def afk(self, duration: str) -> str:
        try:
            length = parse_duration(duration)
        except ValueError as e:
            return f"Not changed: {e}."
        now = self._now
        until = self.prefs.start_afk(now, length)
        sample = render(self.prefs.text("afk"), left=format_left(until - now),
                        until=until.strftime("%H:%M UTC on %b %d"), buyer="Buyer",
                        seller="Seller", items="items", total="$0.00", order="000000")
        return (f"💤 AFK until <t:{int(until.timestamp())}:f> (<t:{int(until.timestamp())}:R>). "
                f"New sales get:\n> {md(sample)}\nEnd it early with /back.")

    def back(self) -> str:
        if self.prefs.afk_until is None:
            return "You are not AFK."
        since = self.prefs.end_afk()
        count, _, net = self.store.totals_since(since)
        return ("👋 Welcome back. "
                + (f"{count} sale(s) came in while you were away, worth {money(net)} after fees."
                   if count else "No sales came in while you were away."))

    # ------------------------------------------------------------------- info
    async def orders(self) -> str:
        try:
            rows = await self.watcher.api.open_orders()
        except BhoppError as e:
            return f"Could not read your orders: {e}"
        if not rows:
            return "No open orders."
        rows.sort(key=lambda o: o.created_at or self._now)
        lines = [f"• {embeds.state_label(o.state)} · **{md(o.summary)}** · {md(o.buyer_name)} · "
                 f"{money(o.net)} · {embeds.relative(o.created_at)} · [chat](<{o.chat_url}>)"
                 for o in rows[:15]]
        if len(rows) > 15:
            lines.append(f"…and {len(rows) - 15} more.")
        return f"**{len(rows)} open order(s)**\n" + "\n".join(lines)

    def stats(self) -> str:
        now = self._now
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        def line(label: str, since: datetime | None) -> str:
            n, gross, net = self.store.totals_since(since)
            return f"**{label}:** {n} sale(s), {money(gross)} sold, {money(net)} earned"
        return "\n".join([line("Today (UTC)", today), line("Last 7 days", now - timedelta(days=7)),
                          line("Since the bot started", None),
                          "Cancelled orders are not counted."])

    def status(self) -> str:
        w, p, now = self.watcher, self.prefs, self._now
        lines = []
        if w.last_ok:
            lines.append(f"✅ Last check <t:{int(w.last_ok.timestamp())}:R>, "
                         f"{w.open_count} open order(s).")
        else:
            lines.append("⏳ No successful check yet.")
        if w.last_error:
            lines.append(f"⚠️ Last error: {md(w.last_error[:200])}")
        exp = w.api.expires_at
        if exp:
            lines.append((f"🔑 Token expires <t:{int(exp.timestamp())}:R>."
                          if exp > now else "🔑 **The BHOPP token has expired.**"))
        afk = p.afk_until
        lines.append(f"💤 AFK until <t:{int(afk.timestamp())}:f> "
                     f"({format_left(afk - now)} left)." if afk and afk > now else "💤 Not AFK.")
        lines += [
            f"🔔 Sale pings: **{p.ping}**. Reminders: **{'on' if p.reminders else 'off'}**.",
            f"💬 Auto-reply: **{'on' if p.auto_reply else 'off'}**. "
            f"Thanks message: **{'on' if p.thanks else 'off'}**.",
            f"🛑 Buyer cancels: **{p.cancel_mode}**.",
            "🟢 Always online: " + (
                f"**on** ({'connected' if self.presence.connected else 'not connected'})"
                if p.online else "**off**") + ".",
        ]
        if p.online and self.presence.last_error:
            lines.append(f"⚠️ Online status error: {md(self.presence.last_error)}")
        if w.dry_run:
            lines.append("🧪 **Dry run:** nothing is written to BHOPP.")
        return "\n".join(lines)

    async def test_sale(self) -> str:
        """A sample alert. Pings you at most, never @everyone, whatever the setting."""
        o = sample_order(self._now.astimezone(timezone.utc))
        post = Post(embeds.sale_embed(o, SAMPLE_IMAGE, ("Auto-reply",)),
                    "🧪 **Test alert.** " + embeds.sale_summary(o),
                    "none" if self.prefs.ping == "off" else "owner",
                    link=("Open order chat", o.chat_url))
        await self.watcher.poster.post(post)
        return "Posted a test alert in the sales channel."
