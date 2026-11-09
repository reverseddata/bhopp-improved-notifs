"""Money, text, the HTTP client, presence, slash commands and the discord.py layer."""
import asyncio
import base64
import json
from datetime import timedelta, timezone, datetime
from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest

from bhopp_notifs import discord_bot, embeds
from bhopp_notifs.bhopp import BhoppApi, BhoppError
from bhopp_notifs.commands import Commands, sample_order
from bhopp_notifs.embeds import Post
from bhopp_notifs.models import Order
from bhopp_notifs.presence import Presence
from bhopp_notifs.text import (format_left, md, money, parse_duration, redact, register_secret,
                               render)
from conftest import order_dict

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
# ------------------------------------------------------------------ money and text


def test_totals_are_hundredths_of_a_cent_and_unit_prices_are_cents():
    o = Order.from_api({**order_dict(cents=17, qty=1),
                        "totals": {"gross": 1700, "fee": 85, "net": 1615}})
    assert (o.gross, o.fee, o.net) == (Decimal("0.17"), Decimal("0.0085"), Decimal("0.1615"))
    assert o.items[0].unit == Decimal("0.17")


def test_missing_totals_fall_back_to_the_items_and_the_five_percent_fee():
    o = Order.from_api({**order_dict(cents=1000, qty=2), "totals": {}})
    assert (o.gross, o.fee, o.net) == (Decimal(20), Decimal(1), Decimal(19))


@pytest.mark.parametrize("value,text", [("12", "$12.00"), ("0.1615", "$0.1615"),
                                        ("1234.5", "$1,234.50"), ("0.17", "$0.17"),
                                        ("17.081", "$17.08")])
def test_money(value, text):
    assert money(Decimal(value)) == text


@pytest.mark.parametrize("raw,minutes", [("30m", 30), ("2h", 120), ("1h30m", 90), ("1h 30m", 90),
                                         ("90", 90), ("1 day", 1440), ("2 hours", 120),
                                         ("1.5h", 90), ("1 hour and 5 minutes", 65)])
def test_durations(raw, minutes):
    assert parse_duration(raw) == timedelta(minutes=minutes)


@pytest.mark.parametrize("raw", ["", "soon", "2x", "0", "31d", "h", "2h banana", "banana 2h"])
def test_bad_durations(raw):
    with pytest.raises(ValueError):
        parse_duration(raw)


@pytest.mark.parametrize("left,text", [(timedelta(seconds=10), "1m"), (timedelta(minutes=45), "45m"),
                                       (timedelta(hours=2), "2h"),
                                       (timedelta(hours=2, minutes=15), "2h 15m"),
                                       (timedelta(days=1, hours=3), "1d 3h")])
def test_time_left(left, text):
    assert format_left(left) == text


def test_templates_keep_unknown_braces():
    assert render("{buyer} {nope} {", buyer="Al") == "Al {nope} {"


def test_escaping_blocks_markdown_and_mentions():
    assert md("[x](http://a) @everyone") == "\\[x\\]\\(http://a\\) \\@everyone"


def test_secrets_are_redacted():
    register_secret("supersecretvalue")
    jwt = "eyJhbGciOiJIUzI1.eyJ1c2VyIjoiYWJj.abcdefghijklmn"
    out = redact(f"supersecretvalue Bearer abc.def {jwt}")
    assert "supersecretvalue" not in out and "abc.def" not in out and jwt not in out

# ---------------------------------------------------------------------- embeds


def test_sale_embed_is_valid_for_discord_and_within_limits():
    many = {**order_dict(), "items": [
        {"name": f"Item {'x' * 90} {n}", "quantity": 1, "unitAmount": 100} for n in range(40)]}
    e = embeds.sale_embed(Order.from_api(many), "https://cdn.bhopp.com/a.webp", ("AFK notice",))
    assert len(e["description"]) <= 4096 and "more" in e["description"]
    built = discord.Embed.from_dict(e)
    assert built.thumbnail.url and len(built.fields) == 7


def test_non_https_images_are_left_out():
    assert "thumbnail" not in embeds.sale_embed(Order.from_api(order_dict()), "http://x/a.png")


def test_order_without_a_chat_links_to_sales():
    assert sample_order(NOW).chat_url == "https://bhopp.com/seller/sales"

# --------------------------------------------------------------------- http client


class FakeResponse:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self.body


class FakeSession:
    closed = False

    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return FakeResponse(*self.responses.pop(0))

    async def close(self):
        pass


def make_api(*responses, token="tok"):
    session = FakeSession(responses)
    return BhoppApi(token, session_factory=lambda: session), session


def test_open_orders_reads_every_page_with_the_token():
    api, session = make_api((200, json.dumps({"orders": [order_dict("a")], "pages": 2})),
                            (200, json.dumps({"orders": [order_dict("b")], "pages": 2})))
    assert [o.id for o in asyncio.run(api.open_orders())] == ["a", "b"]
    assert session.calls[0][1].endswith("/seller/orders")
    assert session.calls[1][2]["params"] == {"state": "open", "page": 2}
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer tok"


def test_http_errors_carry_the_status_and_not_the_token():
    api, _ = make_api((401, "bad token eyJhbGciOiJIUzI1.eyJ1c2VyIjoiYWJj.abcdefghijklmn"))
    with pytest.raises(BhoppError) as e:
        asyncio.run(api.open_orders())
    assert e.value.status == 401 and "eyJ" not in str(e.value)


def test_unexpected_payload_is_an_error_not_an_empty_list():
    api, _ = make_api((200, json.dumps({"error": "maintenance"})))
    with pytest.raises(BhoppError):
        asyncio.run(api.open_orders())


def test_messages_come_back_oldest_first():
    api, _ = make_api((200, json.dumps({"messages": [{"id": 2, "createdAt": "2026-01-02"},
                                                     {"id": 1, "createdAt": "2026-01-01"}]})))
    assert [m["id"] for m in asyncio.run(api.messages("c"))] == [1, 2]


def test_writes_use_the_documented_routes():
    api, session = make_api((200, json.dumps({"message": {"id": "m"}})),
                            (200, json.dumps({"order": {}})))
    asyncio.run(api.send_message("c1", "hi"))
    asyncio.run(api.cancel_order("o1"))
    assert session.calls[0][:2] == ("POST", api.base + "/conversations/c1/messages")
    assert session.calls[0][2]["json"] == {"body": "hi"}
    assert session.calls[1][:2] == ("PATCH", api.base + "/orders/o1/cancel")


def test_product_image_is_cached_and_public():
    rows = {"products": [{"id": "p1", "name": "Harvester", "avatar": "https://cdn/x.webp"}]}
    api, session = make_api((200, json.dumps(rows)))
    assert asyncio.run(api.product_image("p1", "Harvester")) == "https://cdn/x.webp"
    assert asyncio.run(api.product_image("p1", "Harvester")) == "https://cdn/x.webp"
    assert len(session.calls) == 1
    assert session.calls[0][2]["headers"] == {}


def test_product_image_failure_is_just_no_image():
    api, _ = make_api((500, "boom"))
    assert asyncio.run(api.product_image("p1", "Harvester")) == ""


def test_token_claims():
    body = base64.urlsafe_b64encode(json.dumps({"user": "u1", "exp": 1800000000}).encode()).decode()
    api = BhoppApi(f"h.{body.rstrip('=')}.s")
    assert api.seller_id == "u1" and int(api.expires_at.timestamp()) == 1800000000
    assert BhoppApi("not-a-jwt").expires_at is None

# ------------------------------------------------------------------------ presence


class FakeSio:
    made = []

    def __init__(self):
        self.connected, self.emits, self.args = False, [], None
        FakeSio.made.append(self)

    async def connect(self, url, **kw):
        self.args, self.connected = (url, kw), True

    async def emit(self, event, namespace=None):
        self.emits.append((event, namespace))

    async def disconnect(self):
        self.connected = False


def test_presence_connects_pings_and_follows_the_setting():
    FakeSio.made = []
    want = {"on": True}
    p = Presence(lambda: "tok", lambda: want["on"], FakeSio)

    async def go():
        await p.tick()
        await p.tick()
        assert len(FakeSio.made) == 1 and p.connected
        want["on"] = False
        await p.tick()
    asyncio.run(go())
    sio = FakeSio.made[0]
    assert sio.args[1]["auth"] == {"token": "tok"} and sio.args[1]["namespaces"] == ["/user"]
    assert sio.emits == [("presence:ping", "/user")] * 2 and not sio.connected


def test_presence_never_raises_and_reports_without_the_token():
    class Bad(FakeSio):
        async def connect(self, url, **kw):
            raise RuntimeError("Bearer abcdefghijk refused")
    p = Presence(lambda: "tok", lambda: True, Bad)
    asyncio.run(p.tick())
    assert "abcdefghijk" not in p.last_error and not p.connected

# ------------------------------------------------------------------------- commands


def commands(rig):
    presence = SimpleNamespace(connected=False, last_error="")
    return Commands("42", rig.store, rig.prefs, rig.watcher, presence)


def test_only_the_owner_is_allowed(rig):
    c = commands(rig)
    assert c.allowed(42) and c.allowed("42") and not c.allowed(43)
    assert not Commands("", rig.store, rig.prefs, rig.watcher, None).allowed("")


def test_alerts_setting(rig):
    c = commands(rig)
    assert "pings you" in c.alerts()
    assert "@everyone" in c.alerts("everyone") and rig.prefs.ping == "everyone"
    assert "pings nobody" in c.alerts("off")
    c.alerts(reminders="off")
    assert rig.prefs.reminders is False


def test_automations_start_off_and_toggle(rig):
    c = commands(rig)
    assert "**off**" in c.autoreply() and "**off**" in c.thanks() and "**off**" in c.online()
    assert "**on**" in c.autoreply("on") and rig.prefs.auto_reply
    assert "**on**" in c.thanks("on") and rig.prefs.thanks
    assert "**on**" in c.online("on") and rig.prefs.online
    assert rig.prefs.cancel_mode == "ask"


def test_message_edit_and_reset(rig):
    c = commands(rig)
    c.set_message("thanks", "Cheers {buyer}")
    assert rig.prefs.text("thanks") == "Cheers {buyer}"
    c.set_message("thanks", "default")
    assert "good review" in rig.prefs.text("thanks")
    assert "Not changed" in c.set_message("thanks", "x" * 1001)


def test_afk_and_back(rig):
    c = commands(rig)
    assert "Not changed" in c.afk("soon") and rig.prefs.afk_until is None
    text = c.afk("2h")
    assert "another 2h" in text and rig.prefs.afk_until == rig.clock() + timedelta(hours=2)
    assert "Not AFK" not in c.status()
    assert "Welcome back" in c.back() and rig.prefs.afk_until is None
    assert c.back() == "You are not AFK."


def test_status_shows_problems(rig):
    rig.api.expires_at = rig.clock() - timedelta(hours=1)
    rig.watcher.last_error = "BHOPP answered 500"
    text = commands(rig).status()
    assert "has expired" in text and "BHOPP answered 500" in text


def test_orders_list(rig):
    rig.api.add(oid="o1")
    text = asyncio.run(commands(rig).orders())
    assert "1 open order" in text and "conv-o1" in text


def test_orders_list_reports_failures(rig):
    rig.api.open_error = BhoppError(500, "boom")
    assert "Could not read" in asyncio.run(commands(rig).orders())


def test_test_sale_never_pings_everyone(rig):
    rig.prefs.ping = "everyone"
    asyncio.run(commands(rig).test_sale())
    assert rig.poster.posts[0].mention == "owner"
    rig.prefs.ping = "off"
    asyncio.run(commands(rig).test_sale())
    assert rig.poster.posts[1].mention == "none"

# -------------------------------------------------------------------- discord layer


@pytest.mark.parametrize("mention,everyone,users,prefix", [
    ("none", False, [], ""), ("owner", False, [42], "<@42>"), ("everyone", True, [], "@everyone")])
def test_discord_mentions_match_the_post(mention, everyone, users, prefix):
    post = Post({}, "hello", mention)
    allowed = discord_bot.allowed_mentions(post, "42")
    assert allowed.everyone is everyone and allowed.roles is False
    assert [u.id for u in allowed.users] == users
    assert post.content("42") == (f"{prefix} hello".strip())


def test_all_commands_register(rig):
    async def go():
        client = discord_bot.make_client()
        tree = discord_bot.build_tree(client, commands(rig))
        return {c.name for c in tree.get_commands()}
    assert asyncio.run(go()) == {
        "status", "alerts", "autoreply", "thanks", "message", "afk", "back", "cancelmode",
        "online", "orders", "stats", "testsale"}


def test_cancel_buttons_are_persistent_and_carry_the_order():
    async def go():
        view = discord_bot.build_view(Post({}, cancel_order="abc123",
                                           link=("Open", "https://bhopp.com/x")))
        assert view.timeout is None
        return view.to_components()
    rows = asyncio.run(go())
    ids = [c.get("custom_id") for c in rows[0]["components"]]
    assert ids[:2] == ["bn:cancel:yes:abc123", "bn:cancel:no:abc123"]
    assert rows[0]["components"][2]["url"] == "https://bhopp.com/x"
    assert discord_bot.CancelButton.__discord_ui_compiled_template__.fullmatch(ids[0])


def test_no_view_when_there_is_nothing_to_show():
    assert asyncio.run(_view(Post({}))) is None


async def _view(post):
    return discord_bot.build_view(post)


def test_the_bot_starts_polls_and_shuts_down(tmp_path, monkeypatch):
    """The real startup wiring, with Discord's connection and the two loops stubbed out."""
    from pathlib import Path

    from bhopp_notifs import cli
    from bhopp_notifs.config import Config
    from bhopp_notifs.engine import SaleWatcher

    polled = []

    def client_without_network():
        client = real_make_client()

        async def start(token):
            await client.on_ready()
            await asyncio.sleep(60)
        client.start = start
        return client

    async def fake_sync(self, *, guild=None):
        return []

    async def one_pass(self):
        polled.append(self.poll_interval)

    async def idle(self):
        await asyncio.sleep(60)

    real_make_client = discord_bot.make_client
    monkeypatch.setattr(discord_bot, "make_client", client_without_network)
    monkeypatch.setattr(discord.app_commands.CommandTree, "sync", fake_sync)
    monkeypatch.setattr(SaleWatcher, "run", one_pass)
    monkeypatch.setattr(Presence, "run", idle)
    cfg = Config("tok", "disc", "1", "2", "3", 15.0, Path(tmp_path), True)
    assert asyncio.run(cli.run(cfg)) == 0
    assert polled == [15.0] and (tmp_path / "bhopp_notifs.sqlite3").exists()


def test_missing_settings_are_explained(monkeypatch, tmp_path, capsys):
    from bhopp_notifs import cli

    monkeypatch.chdir(tmp_path)
    for name in ("BHOPP_TOKEN", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_OWNER_ID"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["check"]) == 2
    assert "BHOPP_TOKEN is missing" in capsys.readouterr().out


class FakeInteraction:
    def __init__(self):
        self.user = SimpleNamespace(id=7)
        self.sent, self.deferred = [], False
        self.response = SimpleNamespace(send_message=self._send, defer=self._defer)
        self.followup = SimpleNamespace(send=self._follow)

    async def _send(self, text, ephemeral):
        self.sent.append(text)

    async def _defer(self, ephemeral, thinking):
        self.deferred = True

    async def _follow(self, text, ephemeral):
        self.sent.append(text)
        assert ephemeral


def test_refused_user_gets_an_answer_and_no_work_runs():
    i, ran = FakeInteraction(), []

    async def work():
        ran.append(1)
    asyncio.run(discord_bot.run_command(i, False, work))
    assert ran == [] and "not allowed" in i.sent[0] and not i.deferred


def test_command_is_acknowledged_then_answered():
    i = FakeInteraction()

    async def work():
        return "done"
    asyncio.run(discord_bot.run_command(i, True, work))
    assert i.deferred and i.sent == ["done"]


def test_slow_or_broken_commands_still_answer():
    async def slow():
        await asyncio.sleep(5)

    async def broken():
        raise RuntimeError("secret detail")
    i = FakeInteraction()
    asyncio.run(discord_bot.run_command(i, True, slow, timeout=0.01))
    assert "stopped waiting" in i.sent[0]
    j = FakeInteraction()
    asyncio.run(discord_bot.run_command(j, True, broken))
    assert j.sent == ["That failed: RuntimeError"]
