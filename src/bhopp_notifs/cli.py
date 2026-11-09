"""bhopp-notifs run    the bot
bhopp-notifs check  read-only: is the BHOPP token good, how many open orders are there
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from . import config as config_module
from .bhopp import BhoppApi, BhoppError
from .text import register_secret, redact

log = logging.getLogger("bhopp_notifs")


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


async def check(cfg) -> int:
    api = BhoppApi(cfg.bhopp_token)
    try:
        orders = await api.open_orders()
    except BhoppError as e:
        print(f"BHOPP refused or did not answer: {e}")
        return 1
    finally:
        await api.close()
    print(f"Token works. Expires: {api.expires_at or 'unknown'}. Open orders: {len(orders)}.")
    return 0


async def run(cfg) -> int:
    import discord

    from .commands import Commands
    from .discord_bot import ChannelPoster, attach, make_client
    from .engine import SaleWatcher
    from .presence import Presence
    from .store import Prefs, Store, utcnow

    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.data_dir / "bhopp_notifs.sqlite3")
    prefs = Prefs(store)
    api = BhoppApi(cfg.bhopp_token)
    client = make_client()
    poster = ChannelPoster(client, cfg.channel_id, cfg.owner_id)
    watcher = SaleWatcher(api, store, prefs, poster, dry_run=cfg.dry_run,
                          poll_interval=cfg.poll_interval)

    def online_wanted() -> bool:
        return prefs.online and (api.expires_at is None or api.expires_at > utcnow())

    presence = Presence(lambda: api.token, online_wanted)
    connected = asyncio.Event()
    attach(client, Commands(cfg.owner_id, store, prefs, watcher, presence), cfg.guild_id, connected)

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C raises KeyboardInterrupt instead
            pass

    bot = asyncio.create_task(client.start(cfg.discord_token), name="discord")
    await asyncio.wait({bot, asyncio.create_task(connected.wait()),
                        asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
    code = 0
    tasks = []
    try:
        if bot.done():
            bot.result()
        if not stop.is_set():
            log.info("started%s", " in dry-run mode" if cfg.dry_run else "")
            tasks = [asyncio.create_task(watcher.run(), name="watcher"),
                     asyncio.create_task(presence.run(), name="presence")]
            waiter = asyncio.create_task(stop.wait())
            await asyncio.wait({bot, waiter, *tasks}, return_when=asyncio.FIRST_COMPLETED)
            if bot.done():
                bot.result()
    except discord.LoginFailure:
        log.error("Discord rejected DISCORD_BOT_TOKEN")
        code = 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()
        await api.close()
        store.close()
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bhopp-notifs", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "check"])
    args = parser.parse_args(argv)
    setup_logging()
    try:
        cfg = config_module.load(need_discord=args.command == "run")
    except config_module.ConfigError as e:
        print(f"Config problem: {e}. See .env.example.")
        return 2
    register_secret(cfg.bhopp_token)
    register_secret(cfg.discord_token)
    try:
        return asyncio.run(check(cfg) if args.command == "check" else run(cfg))
    except KeyboardInterrupt:
        return 0
