from datetime import timedelta

import pytest

from bhopp_notifs.bhopp import BhoppError
from bhopp_notifs.engine import CANCEL_WORD
from conftest import http_500

# ---------------------------------------------------------------- detecting sales


def test_first_run_does_not_alert_or_message_existing_orders(rig):
    rig.prefs.auto_reply = True
    rig.api.add(oid="old")
    rig.tick()
    assert len(rig.poster.posts) == 1 and "alerts are on" in rig.poster.posts[0].embed["title"]
    assert rig.api.sent == []
    rig.tick_later()
    assert len(rig.poster.posts) == 1


def test_new_sale_alerts_once_even_after_a_restart(rig):
    rig.start()
    rig.api.add(oid="o1")
    rig.tick()
    rig.tick_later()
    assert len(rig.poster.titled("New sale")) == 1
    rig.boot()
    rig.tick_later()
    assert len(rig.poster.titled("New sale")) == 1


def test_sale_alert_has_what_a_seller_needs(rig):
    rig.start()
    rig.api.add(oid="o1", qty=2, cents=899)
    rig.tick()
    post = rig.poster.posts[0]
    e = post.embed
    fields = {f["name"]: f["value"] for f in e["fields"]}
    assert "2× Harvester" in e["title"]
    assert e["url"] == "https://bhopp.com/account/messages/conv-o1"
    assert e["thumbnail"]["url"].startswith("https://cdn.bhopp.com/")
    assert "$8.99 each" in e["description"]
    assert fields["💵 Sale price"] == "$17.98"
    assert fields["🏦 BHOPP fee"] == "−$0.899 (5%)"
    assert fields["✅ You earn"] == "**$17.08**"
    assert "Buyer1" in fields["👤 Buyer"]
    assert "Pending" in fields["📋 Status"]
    assert post.link == ("Open order chat", "https://bhopp.com/account/messages/conv-o1")
    assert "o1" in e["footer"]["text"]


@pytest.mark.parametrize("setting,mention", [("owner", "owner"), ("everyone", "everyone"),
                                             ("off", "none")])
def test_ping_follows_the_setting(rig, setting, mention):
    rig.start()
    rig.prefs.ping = setting
    rig.api.add(oid="o1")
    rig.tick()
    assert rig.poster.posts[0].mention == mention


def test_everyone_is_only_ever_used_on_sale_alerts(rig):
    rig.start()
    rig.prefs.ping = "everyone"
    rig.api.add(oid="o1", created=rig.clock() - timedelta(hours=11))
    rig.api.add(oid="o2")
    rig.api.add(oid="o3")
    rig.tick()
    rig.api.set_state("o2", "disputed")
    rig.api.finish("o3", "cancelled")
    rig.tick_later(120)
    others = [p for p in rig.poster.posts if "New sale" not in p.embed.get("title", "")]
    assert len(others) >= 3
    assert all(p.mention != "everyone" for p in others)


def test_buyer_name_cannot_inject_a_mention(rig):
    rig.start()
    rig.prefs.ping = "owner"
    rig.api.add(oid="o1", buyer="@everyone")
    rig.tick()
    post = rig.poster.posts[0]
    assert post.allowed_mentions("123")["everyone"] is False
    assert "@everyone" not in post.content("123").replace("\\@everyone", "")


def test_a_failed_discord_send_is_retried(rig):
    rig.start()
    rig.api.add(oid="o1")
    rig.poster.fail = RuntimeError("discord down")
    rig.tick()
    rig.poster.fail = None
    rig.tick_later()
    assert len(rig.poster.titled("New sale")) == 1


def test_a_send_that_landed_before_a_crash_is_not_repeated(rig):
    rig.start()
    rig.api.add(oid="o1")
    rig.poster.fail = RuntimeError("timed out after Discord took it")
    rig.tick()
    rig.poster.fail = None
    rig.poster.posted_ids.add("o1")
    rig.tick_later()
    assert rig.poster.titled("New sale") == []


def test_a_malformed_order_does_not_stop_the_others(rig):
    rig.start()
    rig.api.add(oid="o1")
    from bhopp_notifs.bhopp import parse_orders
    assert [o.id for o in parse_orders([{"no": "id"}, rig.api.open["o1"]], "test")] == ["o1"]


# -------------------------------------------------------------------- auto-reply


def test_nothing_is_written_to_bhopp_by_default(rig):
    rig.start()
    rig.api.add(oid="o1")
    rig.tick()
    rig.api.finish("o1")
    rig.tick_later(120)
    assert rig.api.sent == [] and rig.api.cancelled == []


def test_auto_reply_is_sent_once_across_restarts(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.api.add(oid="o1")
    rig.tick()
    rig.tick_later()
    rig.boot()
    rig.tick_later()
    assert len(rig.api.sent) == 1
    conv, body = rig.api.sent[0]
    assert conv == "conv-o1" and "Buyer1" in body and "2× Harvester" in body


def test_unknown_send_outcome_looks_in_the_chat_first(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.api.add(oid="o1")
    rig.api.send_error = BhoppError(0, "TimeoutError")
    rig.tick()
    rig.api.send_error = None
    body = rig.store.step("o1", "auto_reply").body
    rig.api.say("o1", body, sender="seller-id", minutes_after=0)  # it did get through
    rig.tick_later()
    assert rig.api.sent == []
    assert rig.store.step("o1", "auto_reply").status == "sent"


def test_unknown_send_outcome_resends_when_it_never_arrived(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.api.add(oid="o1")
    rig.api.send_error = BhoppError(0, "TimeoutError")
    rig.tick()
    rig.api.send_error = None
    rig.tick_later()
    assert len(rig.api.sent) == 1


def test_a_refused_message_stops_and_warns_once(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.api.add(oid="o1")
    rig.api.send_error = BhoppError(403, "not allowed")
    for _ in range(8):
        rig.tick_later()
    assert len(rig.poster.titled("Could not send")) == 1
    assert rig.store.step("o1", "auto_reply").status == "failed"


def test_late_start_does_not_greet_old_orders(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.api.add(oid="o1", created=rig.clock() - timedelta(hours=7))
    rig.tick()
    assert rig.api.sent == [] and len(rig.poster.titled("New sale")) == 1


def test_delivered_orders_are_not_greeted(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.api.add(oid="o1", state="delivered")
    rig.tick()
    assert rig.api.sent == []


# --------------------------------------------------------------------------- AFK


def test_afk_notice_gives_time_left_and_replaces_the_auto_reply(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.afk(hours=2, minutes=15)
    rig.api.add(oid="o1")
    rig.tick()
    assert rig.bodies() == ['Seller is currently offline for another 2h 15m, if you would like '
                            'to cancel please type "cancel"']
    field = rig.poster.posts[0].embed["fields"][-1]
    assert field["value"] == "AFK notice"


def test_afk_ends_by_itself_and_reports(rig):
    rig.start()
    rig.prefs.auto_reply = True
    rig.afk(minutes=10)
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    rig.poster.posts.clear()
    rig.clock.advance(minutes=11)
    rig.tick()
    assert len(rig.poster.titled("AFK time is up")) == 1
    assert "1 sale(s)" in rig.poster.posts[0].embed["description"]
    assert rig.prefs.afk_until is None
    rig.api.add(oid="o2", created=rig.clock())
    rig.tick_later()
    assert "thanks for buying" in rig.bodies()[-1]


def test_afk_survives_a_restart(rig):
    rig.start()
    rig.afk(hours=1)
    rig.boot()
    rig.api.add(oid="o1")
    rig.tick()
    assert "offline" in rig.bodies()[0]


# ------------------------------------------------------------------------ cancel


@pytest.mark.parametrize("text", ["cancel", "Cancel", " CANCEL!! ", "cancel please",
                                  "please cancel", "cancel my order", "cancel it", '"cancel"'])
def test_cancel_words_match(text):
    assert CANCEL_WORD.match(text)


@pytest.mark.parametrize("text", ["don't cancel", "how do i cancel", "not cancel", "cancelled?",
                                  "cancel after i get it", ""])
def test_other_messages_do_not_match(text):
    assert not CANCEL_WORD.match(text)


def afk_rig(rig, mode="ask"):
    rig.start()
    rig.prefs.cancel_mode = mode
    rig.afk(hours=2)
    rig.sale_with_afk()
    return rig


def request(rig, text="cancel"):
    rig.api.say("o1", text)
    rig.clock.advance(minutes=2)
    rig.tick()


def run(coro):
    import asyncio
    return asyncio.run(coro)


def test_ask_mode_pings_the_owner_and_cancels_nothing(rig):
    afk_rig(rig)
    request(rig)
    post = rig.poster.titled("Cancel requested")[0]
    assert post.mention == "owner" and post.cancel_order == "o1"
    assert rig.api.cancelled == []
    assert "seller has been notified" in rig.bodies()[-1]


def test_cancel_request_is_handled_once(rig):
    afk_rig(rig)
    request(rig)
    rig.api.say("o1", "cancel", minutes_after=2)
    rig.tick_later(120)
    assert len(rig.poster.titled("Cancel requested")) == 1
    assert sum("seller has been notified" in b for b in rig.bodies()) == 1


def test_accepting_cancels_once_and_tells_the_buyer(rig):
    afk_rig(rig)
    request(rig)
    text = run(rig.watcher.resolve_cancel("o1", True))
    assert "Cancelled and refunded $17.98" in text
    assert rig.api.cancelled == ["o1"]
    assert "cancelled and the full amount refunded" in rig.bodies()[-1]
    assert "already handled" in run(rig.watcher.resolve_cancel("o1", True))
    assert rig.api.cancelled == ["o1"]


def test_declining_tells_the_buyer_and_cancels_nothing(rig):
    afk_rig(rig)
    request(rig)
    assert "Kept the order" in run(rig.watcher.resolve_cancel("o1", False))
    assert rig.api.cancelled == []
    assert "still deliver" in rig.bodies()[-1]


def test_accepting_after_delivery_cancels_nothing(rig):
    afk_rig(rig)
    request(rig)
    rig.api.set_state("o1", "delivered")
    assert "Nothing was cancelled" in run(rig.watcher.resolve_cancel("o1", True))
    assert rig.api.cancelled == []


def test_refused_cancel_can_be_retried(rig):
    afk_rig(rig)
    request(rig)
    rig.api.cancel_error = BhoppError(403, "buyers only")
    assert "did not cancel" in run(rig.watcher.resolve_cancel("o1", True))
    rig.api.cancel_error = None
    assert "Cancelled" in run(rig.watcher.resolve_cancel("o1", True))
    assert rig.api.cancelled == ["o1"]


def test_auto_mode_cancels_and_tells_everyone(rig):
    afk_rig(rig, "auto")
    request(rig)
    assert rig.api.cancelled == ["o1"]
    assert "full amount refunded" in rig.bodies()[-1]
    assert rig.poster.titled("Cancelled and refunded")[0].mention == "owner"


def test_auto_mode_falls_back_to_asking_when_bhopp_refuses(rig):
    afk_rig(rig, "auto")
    rig.api.cancel_error = BhoppError(403, "buyers only")
    request(rig)
    post = rig.poster.titled("Cancel requested")[0]
    assert "BHOPP answered 403" in post.embed["description"] and post.cancel_order == "o1"
    assert "seller has been notified" in rig.bodies()[-1]


def test_auto_mode_asks_if_the_seller_wrote_in_the_chat(rig):
    afk_rig(rig, "auto")
    rig.api.say("o1", "back early, delivering now", sender="seller-id")
    request(rig)
    assert rig.api.cancelled == []
    assert "wrote in this chat" in rig.poster.titled("Cancel requested")[0].embed["description"]


def test_ignores_cancel_typed_before_the_notice(rig):
    rig.start()
    rig.afk(hours=2)
    rig.api.say("o1", "cancel", minutes_after=-5)
    rig.sale_with_afk()
    rig.tick_later(120)
    assert rig.store.row("o1").cancel_state == ""
    assert not any("seller has been notified" in b for b in rig.bodies())


def test_ignores_cancel_from_anyone_but_the_buyer(rig):
    afk_rig(rig)
    rig.api.say("o1", "cancel", sender="someone-else")
    rig.tick_later(120)
    assert rig.poster.titled("Cancel requested") == []


def test_orders_without_the_afk_notice_cannot_be_cancelled(rig):
    rig.start()
    rig.prefs.cancel_mode = "auto"
    rig.api.add(oid="o1")
    rig.tick()
    request(rig)
    assert rig.api.cancelled == [] and rig.poster.titled("Cancel requested") == []


def test_cancel_after_delivery_is_ignored(rig):
    afk_rig(rig, "auto")
    rig.api.set_state("o1", "delivered")
    rig.tick_later(120)
    request(rig)
    assert rig.api.cancelled == []


def test_dry_run_never_writes_to_bhopp(rig):
    rig.start()
    rig.boot(dry_run=True)
    rig.prefs.cancel_mode = "auto"
    rig.afk(hours=2)
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    assert rig.api.sent == []
    assert len(rig.poster.titled("Dry run, would send the afk")) == 1
    request(rig)
    assert rig.api.cancelled == [] and rig.api.sent == []
    assert len(rig.poster.titled("Dry run, would cancel")) == 1


# ---------------------------------------------------------------- after the sale


def test_thanks_goes_out_once_when_marked_delivered(rig):
    rig.start()
    rig.prefs.thanks = True
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    assert rig.api.sent == []
    rig.api.set_state("o1", "delivered")
    rig.tick_later()
    assert len(rig.bodies()) == 1 and "good review" in rig.bodies()[0]
    rig.api.finish("o1")
    rig.tick_later(120)
    rig.boot()
    rig.tick_later(120)
    assert len(rig.bodies()) == 1
    assert len(rig.poster.titled("Order completed")) == 1


def test_no_new_thanks_at_completion_if_delivery_was_already_seen(rig):
    rig.start()
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    rig.api.set_state("o1", "delivered")
    rig.tick_later()
    rig.prefs.thanks = True  # turned on after the delivery: too late for this order
    rig.api.finish("o1")
    rig.tick_later(120)
    assert rig.api.sent == []


def test_thanks_when_delivery_and_completion_fall_between_two_polls(rig):
    rig.start()
    rig.prefs.thanks = True
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    rig.api.finish("o1")
    rig.tick_later(120)
    assert len(rig.bodies()) == 1


@pytest.mark.parametrize("state", ["cancelled", "disputed"])
@pytest.mark.parametrize("open_list", [True, False])
def test_no_thanks_when_cancelled_or_disputed(rig, state, open_list):
    rig.start()
    rig.prefs.thanks = True
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    if open_list:
        rig.api.set_state("o1", state)
    else:
        rig.api.finish("o1", state)
    rig.tick_later(120)
    assert rig.api.sent == []
    assert rig.store.step("o1", "thanks") is None


def test_queued_thanks_is_dropped_if_the_order_is_cancelled_first(rig):
    rig.start()
    rig.prefs.thanks = True
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    rig.api.set_state("o1", "delivered")
    rig.api.send_error = BhoppError(500, "boom")
    rig.tick_later()
    rig.api.send_error = None
    rig.api.finish("o1", "cancelled")
    rig.tick_later(120)
    rig.tick_later(120)
    assert rig.api.sent == []
    assert rig.store.step("o1", "thanks").status == "failed"
    assert rig.poster.titled("Could not send") == []


def test_cancelled_order_pings_the_owner(rig):
    rig.start()
    rig.api.add(oid="o2", created=rig.clock())
    rig.tick()
    rig.api.finish("o2", "cancelled")
    rig.tick_later(120)
    assert rig.poster.titled("Order cancelled")[0].mention == "owner"


def test_no_thanks_for_very_old_orders(rig):
    rig.start()
    rig.prefs.thanks = True
    rig.api.add(oid="o1", created=rig.clock() - timedelta(days=15))
    rig.tick()
    rig.api.finish("o1")
    rig.tick_later(120)
    assert rig.api.sent == []


def test_dispute_pings_the_owner(rig):
    rig.start()
    rig.api.add(oid="o1")
    rig.tick()
    rig.api.set_state("o1", "disputed")
    rig.tick_later()
    assert rig.poster.titled("disputed")[0].mention == "owner"


def test_an_empty_open_list_changes_nothing(rig):
    rig.start()
    rig.api.add(oid="o1")
    rig.tick()
    saved = rig.api.open
    rig.api.open = {}
    rig.tick_later(120)
    rig.api.open = saved
    rig.tick_later(120)
    assert rig.store.row("o1").state == "pending"
    assert rig.poster.titled("cancelled") == []


def test_past_orders_are_not_read_every_pass(rig):
    rig.start()
    rig.api.add(oid="o1")
    rig.tick()
    rig.api.open = {}
    rig.tick_later(1)
    rig.tick_later(1)
    rig.tick_later(1)
    assert rig.api.past_reads == 1


def test_undelivered_order_reminds_once(rig):
    rig.start()
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    rig.clock.advance(hours=10, minutes=1)
    rig.tick()
    rig.tick_later()
    posts = rig.poster.titled("Still not delivered")
    assert len(posts) == 1 and posts[0].mention == "owner"


def test_reminders_can_be_turned_off(rig):
    rig.start()
    rig.prefs.reminders = False
    rig.api.add(oid="o1", created=rig.clock())
    rig.tick()
    rig.clock.advance(hours=11)
    rig.tick()
    assert rig.poster.titled("Still not delivered") == []


# ----------------------------------------------------------------- site trouble


def test_token_warning_comes_once_per_threshold(rig):
    rig.start()
    rig.api.expires_at = rig.clock() + timedelta(hours=23)
    rig.tick()
    rig.tick_later()
    assert len(rig.poster.titled("token expires")) == 1
    rig.clock.advance(hours=21)
    rig.tick()
    assert len(rig.poster.titled("token expires")) == 2
    rig.boot()
    rig.tick_later()
    assert len(rig.poster.titled("token expires")) == 2


def test_rejected_token_warns_once(rig):
    rig.start()
    rig.api.open_error = BhoppError(401, "expired")
    delays = [run(rig.watcher._poll_failed(rig.api.open_error)) for _ in range(3)]
    assert len(rig.poster.titled("rejected the token")) == 1
    assert delays[0] == 60.0


def test_outage_warns_once_after_many_failures_and_reports_recovery(rig):
    rig.start()
    for _ in range(12):
        run(rig.watcher._poll_failed(http_500()))
    assert len(rig.poster.titled("Can't reach")) == 1
    run(rig.watcher._recovered())
    assert len(rig.poster.titled("reachable again")) == 1


def test_failed_poll_raises_for_the_loop_and_sends_nothing(rig):
    rig.start()
    rig.api.open_error = http_500()
    with pytest.raises(BhoppError):
        rig.tick()
    assert rig.poster.posts == []


def test_stats_leave_out_cancelled_orders(rig):
    rig.start()
    rig.api.add(oid="o1", created=rig.clock())
    rig.api.add(oid="o2", created=rig.clock())
    rig.tick()
    rig.api.finish("o2", "cancelled")
    rig.tick_later(120)
    count, gross, _ = rig.store.totals_since(rig.clock() - timedelta(days=1))
    assert count == 1 and str(gross) == "17.98"
