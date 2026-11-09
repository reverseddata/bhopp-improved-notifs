"""The discord.py layer: posting to the channel, slash commands and the cancel buttons.

Every command acknowledges first and always answers within a time budget, so Discord never
shows "The application did not respond". Only DISCORD_OWNER_ID may use them.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Literal

import discord
from discord import app_commands

from .commands import Commands
from .embeds import Post

log = logging.getLogger(__name__)
MAX_REPLY = 1900
COMMAND_TIMEOUT = 45.0
HISTORY_LIMIT = 50
OnOff = Literal["on", "off"]
Kind = Literal["auto_reply", "afk", "thanks", "cancel_ack", "cancel_done", "cancel_declined"]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_REPLY else text[:MAX_REPLY - 1] + "…"


class CancelButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"bn:cancel:(?P<action>yes|no):(?P<order>[\w-]+)"):
    """Keeps working after a restart: the action and order live in the button's id."""

    def __init__(self, action: str, order_id: str):
        accept = action == "yes"
        super().__init__(discord.ui.Button(
            label="Cancel and refund" if accept else "Keep order",
            style=discord.ButtonStyle.danger if accept else discord.ButtonStyle.secondary,
            custom_id=f"bn:cancel:{action}:{order_id}"))
        self.action, self.order_id = action, order_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], match["order"])

    async def callback(self, interaction: discord.Interaction):
        commands: Commands = interaction.client.bhopp_commands

        async def work():
            text = await commands.watcher.resolve_cancel(self.order_id, self.action == "yes")
            try:
                await interaction.message.edit(view=None)
            except discord.HTTPException as e:
                log.warning("could not remove the buttons: %s", e)
            return text
        await run_command(interaction, commands.allowed(interaction.user.id), work)


def build_view(post: Post) -> discord.ui.View | None:
    if not post.link and not post.cancel_order:
        return None
    view = discord.ui.View(timeout=None)
    if post.cancel_order:
        view.add_item(CancelButton("yes", post.cancel_order))
        view.add_item(CancelButton("no", post.cancel_order))
    if post.link:
        view.add_item(discord.ui.Button(label=post.link[0], url=post.link[1],
                                        style=discord.ButtonStyle.link))
    return view


def allowed_mentions(post: Post, owner_id: str) -> discord.AllowedMentions:
    allowed = post.allowed_mentions(owner_id)
    return discord.AllowedMentions(
        everyone=allowed["everyone"], roles=False, replied_user=False,
        users=[discord.Object(id=int(u)) for u in allowed["users"]])


class ChannelPoster:
    """Sends to the alert channel. Raises on failure; the caller decides about retrying."""

    def __init__(self, client: discord.Client, channel_id: str, owner_id: str):
        self.client, self.channel_id, self.owner_id = client, int(channel_id), owner_id
        self._channel = None

    async def _get(self):
        if self._channel is None:
            self._channel = (self.client.get_channel(self.channel_id)
                             or await self.client.fetch_channel(self.channel_id))
        return self._channel

    async def post(self, post: Post) -> None:
        channel = await self._get()
        kwargs = {}
        view = build_view(post)
        if view is not None:
            kwargs["view"] = view
        await channel.send(content=post.content(self.owner_id) or None,
                           embed=discord.Embed.from_dict(post.embed),
                           allowed_mentions=allowed_mentions(post, self.owner_id), **kwargs)

    async def already_posted(self, order_id: str) -> bool:
        """After a send with an unknown outcome: is the alert in the channel already?"""
        try:
            channel = await self._get()
            async for message in channel.history(limit=HISTORY_LIMIT):
                if message.author.id == self.client.user.id and any(
                        order_id in (e.footer.text or "") for e in message.embeds):
                    return True
        except discord.HTTPException as e:
            log.warning("could not read the channel history: %s", e)
        return False


async def run_command(interaction, allowed: bool, work, *, timeout: float = COMMAND_TIMEOUT):
    """Acknowledge first (Discord's 3 s deadline), then always send something back."""
    if not allowed:
        log.warning("refused a Discord command from user %s", interaction.user.id)
        try:
            await interaction.response.send_message("You are not allowed to use this bot.",
                                                    ephemeral=True)
        except discord.HTTPException as e:
            log.warning("could not refuse a Discord command: %s", e)
        return
    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException as e:
        log.warning("could not acknowledge a Discord command: %s", e)
        return
    try:
        text = await asyncio.wait_for(work(), timeout)
    except asyncio.TimeoutError:
        text = f"That took over {timeout:g}s, so the bot stopped waiting. Try again."
    except Exception as e:  # noqa: BLE001 - the user must hear about it, not see a dead spinner
        log.exception("Discord command failed")
        text = f"That failed: {type(e).__name__}"
    try:
        await interaction.followup.send(_clip(text), ephemeral=True)
    except discord.HTTPException as e:
        log.warning("could not send a Discord reply: %s", e)


def build_tree(client: discord.Client, commands: Commands, guild=None):
    """Register the slash commands on a new CommandTree. No network."""
    tree = app_commands.CommandTree(client)

    def go(interaction, fn, *args):
        """Async methods run as they are; database-only ones in a worker thread."""
        if inspect.iscoroutinefunction(fn):
            def work():
                return fn(*args)
        else:
            def work():
                return asyncio.to_thread(fn, *args)
        return run_command(interaction, commands.allowed(interaction.user.id), work)

    @tree.command(name="status", description="Bot health and current settings", guild=guild)
    async def status(interaction: discord.Interaction):
        await go(interaction, commands.status)

    @tree.command(name="alerts", description="Who a new sale pings", guild=guild)
    @app_commands.describe(ping="who gets pinged", reminders="ping when an order sits undelivered")
    async def alerts(interaction: discord.Interaction,
                     ping: Literal["owner", "everyone", "off"] | None = None,
                     reminders: OnOff | None = None):
        await go(interaction, commands.alerts, ping, reminders)

    @tree.command(name="autoreply", description="Auto-reply in the chat of new sales", guild=guild)
    async def autoreply(interaction: discord.Interaction, state: OnOff | None = None):
        await go(interaction, commands.autoreply, state)

    @tree.command(name="thanks", description="Thank the buyer and ask for a review", guild=guild)
    async def thanks(interaction: discord.Interaction, state: OnOff | None = None):
        await go(interaction, commands.thanks, state)

    @tree.command(name="message", description="See or edit an automatic message", guild=guild)
    @app_commands.describe(kind="which message", text="new text, or default to reset it")
    async def message(interaction: discord.Interaction, kind: Kind, text: str | None = None):
        await go(interaction, commands.set_message, kind, text)

    @tree.command(name="afk", description="Tell new buyers you are away", guild=guild)
    @app_commands.describe(duration="for how long: 30m, 2h, 1h30m")
    async def afk(interaction: discord.Interaction, duration: str):
        await go(interaction, commands.afk, duration)

    @tree.command(name="back", description="End AFK early", guild=guild)
    async def back(interaction: discord.Interaction):
        await go(interaction, commands.back)

    @tree.command(name="cancelmode", description='What happens when a buyer types "cancel"',
                  guild=guild)
    @app_commands.describe(mode="ask = buttons in Discord, auto = cancel and refund by itself")
    async def cancelmode(interaction: discord.Interaction,
                         mode: Literal["ask", "auto"] | None = None):
        await go(interaction, commands.cancelmode, mode)

    @tree.command(name="online", description="Always show as online on BHOPP", guild=guild)
    async def online(interaction: discord.Interaction, state: OnOff | None = None):
        await go(interaction, commands.online, state)

    @tree.command(name="orders", description="Your open orders", guild=guild)
    async def orders(interaction: discord.Interaction):
        await go(interaction, commands.orders)

    @tree.command(name="stats", description="Sales and earnings", guild=guild)
    async def stats(interaction: discord.Interaction):
        await go(interaction, commands.stats)

    @tree.command(name="testsale", description="Post a sample sale alert", guild=guild)
    async def testsale(interaction: discord.Interaction):
        await go(interaction, commands.test_sale)

    @tree.error
    async def on_error(interaction: discord.Interaction, error):
        log.warning("Discord command error: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("That failed. Check the options.", ephemeral=True)
            else:
                await interaction.response.send_message("That failed. Check the options.",
                                                        ephemeral=True)
        except discord.HTTPException:
            pass

    return tree


def make_client() -> discord.Client:
    # Slash commands and buttons need no privileged intents.
    return discord.Client(intents=discord.Intents(guilds=True))


def attach(client: discord.Client, commands: Commands, guild_id: str,
           ready: asyncio.Event) -> None:
    """Wire the commands and buttons onto the client. `ready` is set once it is connected."""
    guild = discord.Object(id=int(guild_id)) if guild_id else None
    tree = build_tree(client, commands, guild)
    client.bhopp_commands = commands
    client.add_dynamic_items(CancelButton)

    @client.event
    async def on_ready():
        ready.set()
        try:
            synced = await (tree.sync(guild=guild) if guild else tree.sync())
            log.info("discord: online as %s with %d commands", client.user, len(synced))
        except discord.HTTPException as e:
            log.error("could not register the Discord commands: %s", e)
