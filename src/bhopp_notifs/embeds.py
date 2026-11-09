"""What gets posted in Discord. Pure functions returning embed dicts, so they are testable."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .models import Order
from .text import clip, md, money

GREEN, GOLD, BLUE, ORANGE, RED, GREY = 0x2ECC71, 0xF1C40F, 0x3498DB, 0xE67E22, 0xE74C3C, 0x95A5A6
MAX_ITEM_LINES = 10
FOOTER = "BHOPP"
STATES = {
    "pending": "🟡 Pending, needs delivery",
    "delivered": "📦 Delivered, waiting for the buyer",
    "completed": "✅ Completed",
    "cancelled": "❌ Cancelled",
    "disputed": "⚠️ Disputed",
}


@dataclass(frozen=True)
class Post:
    """One Discord message. `mention` alone decides who can be pinged."""
    embed: dict
    summary: str = ""
    mention: str = "none"  # none, owner or everyone
    link: tuple[str, str] | None = None  # (label, url) for a link button
    cancel_order: str = ""  # order id: adds the Cancel and refund / Keep order buttons

    def content(self, owner_id: str) -> str:
        prefix = {"owner": f"<@{owner_id}>", "everyone": "@everyone"}.get(self.mention, "")
        return clip(" ".join(p for p in (prefix, self.summary) if p), 2000)

    def allowed_mentions(self, owner_id: str) -> dict:
        return {"everyone": self.mention == "everyone",
                "users": [owner_id] if self.mention == "owner" else [], "roles": False}


def _ts(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _field(name: str, value: str, inline: bool = True) -> dict:
    return {"name": clip(name, 256), "value": clip(value or "—", 1024), "inline": inline}


def state_label(state: str) -> str:
    return STATES.get(state, state.capitalize())


def _footer(order: Order) -> dict:
    return {"text": f"{FOOTER} · Order {order.id}"}


def relative(dt: datetime | None) -> str:
    return f"<t:{int(dt.timestamp())}:R>" if dt else "just now"


def _item_lines(order: Order) -> str:
    lines = [f"**{i.quantity}×** {md(i.name)}"
             + (f" · {money(i.unit)} each" if i.quantity > 1 else f" · {money(i.unit)}")
             for i in order.items[:MAX_ITEM_LINES]]
    if len(order.items) > MAX_ITEM_LINES:
        lines.append(f"…and {len(order.items) - MAX_ITEM_LINES} more")
    return "\n".join(lines) or "No item details"


def _headline(order: Order) -> str:
    if len(order.items) == 1:
        return f"{order.items[0].quantity}× {order.items[0].name}"
    return f"{sum(i.quantity for i in order.items)} items"


def _fee(order: Order) -> str:
    return f"−{money(order.fee)} ({order.fee_bps / 100:g}%)"


def sale_embed(order: Order, image: str = "", bot_actions: tuple[str, ...] = ()) -> dict:
    e = {
        "title": clip(f"💸 New sale: {_headline(order)}", 256),
        "url": order.chat_url,
        "description": clip(f"{_item_lines(order)}\n\n**[Open order chat →]({order.chat_url})**",
                            4096),
        "color": GREEN,
        "fields": [
            _field("💵 Sale price", money(order.gross)),
            _field("🏦 BHOPP fee", _fee(order)),
            _field("✅ You earn", f"**{money(order.net)}**"),
            _field("👤 Buyer", f"**{md(order.buyer_name)}**"),
            _field("📋 Status", state_label(order.state)),
            _field("🕒 Ordered", relative(order.created_at)),
        ],
        "timestamp": _ts(order.created_at),
        "footer": _footer(order),
    }
    if bot_actions:
        e["fields"].append(_field("🤖 Sent to the buyer", ", ".join(bot_actions), False))
    if image.startswith("https://"):
        e["thumbnail"] = {"url": image}
    return e


def sale_summary(order: Order) -> str:
    return f"💸 **New sale:** {md(order.summary)} · you earn **{money(order.net)}**"


def _order_embed(order: Order, title: str, text: str, color: int) -> dict:
    return {
        "title": clip(title, 256), "url": order.chat_url, "color": color,
        "description": clip(text, 4096),
        "fields": [_field("👤 Buyer", f"**{md(order.buyer_name)}**"),
                   _field("🛒 Items", md(order.summary)),
                   _field("✅ You earn", money(order.net))],
        "timestamp": _ts(), "footer": _footer(order),
    }


def completed_embed(order: Order) -> dict:
    return _order_embed(order, f"💰 Order completed: {_headline(order)}",
                        f"**{money(order.net)}** earned.", GOLD)


def cancelled_embed(order: Order) -> dict:
    return _order_embed(order, f"❌ Order cancelled: {_headline(order)}",
                        "This sale was cancelled on BHOPP.", RED)


def disputed_embed(order: Order) -> dict:
    return _order_embed(order, f"⚠️ Order disputed: {_headline(order)}",
                        f"The buyer opened a dispute. **[Open the chat]({order.chat_url})** "
                        "and add your proof of delivery.", RED)


def cancel_request_embed(order: Order, error: str = "", seller_wrote: bool = False) -> dict:
    text = ('The buyer typed "cancel" after the AFK notice. **Cancel and refund** returns the '
            "full amount to them and can't be undone.")
    if seller_wrote:
        text += "\nYou wrote in this chat yourself, so the bot left the decision to you."
    if error:
        text = f"The automatic cancel did not go through: {error}\n\n" + text
    return _order_embed(order, f"🛑 Cancel requested: {_headline(order)}", text, ORANGE)


def cancel_done_embed(order: Order, dry_run: bool = False) -> dict:
    title = "🧪 Dry run, would cancel" if dry_run else "🛑 Cancelled and refunded"
    return _order_embed(order, f"{title}: {_headline(order)}",
                        f"The buyer asked to cancel. {money(order.gross)} goes back to them.", RED)


def reminder_embed(order: Order, age_hours: int, cancel_hours: int) -> dict:
    return _order_embed(
        order, f"⏰ Still not delivered: {_headline(order)}",
        f"This order has been pending for {age_hours} hours. BHOPP lets the buyer cancel it "
        f"themselves {cancel_hours} hours after purchase.", ORANGE)


def message_failed_embed(order: Order, step: str, error: str) -> dict:
    return _order_embed(order, "⚠️ Could not send a chat message",
                        f"The {step.replace('_', ' ')} message was not delivered: {error}\n"
                        f"**[Open the chat]({order.chat_url})** to write it yourself.", ORANGE)


def dry_run_embed(order: Order, step: str, body: str) -> dict:
    return _order_embed(order, f"🧪 Dry run, would send the {step.replace('_', ' ')} message",
                        md(body), GREY)


def system_embed(title: str, message: str, level: str = "info") -> dict:
    color = {"info": BLUE, "ok": GREEN, "warn": ORANGE, "error": RED}.get(level, GREY)
    return {"title": clip(title, 256), "description": clip(message, 4096), "color": color,
            "timestamp": _ts(), "footer": {"text": FOOTER}}
