"""Settings from the environment (or .env). Live toggles are in the database instead."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    bhopp_token: str
    discord_token: str
    channel_id: str
    owner_id: str
    guild_id: str
    poll_interval: float
    data_dir: Path
    dry_run: bool


def _id(name: str, required: bool = True) -> str:
    value = os.environ.get(name, "").strip()
    if required and not value:
        raise ConfigError(f"{name} is missing")
    if value and not value.isdigit():
        raise ConfigError(f"{name} must be a number (a Discord ID)")
    return value


def load(*, need_discord: bool = True) -> Config:
    load_dotenv(Path.cwd() / ".env")
    token = os.environ.get("BHOPP_TOKEN", "").strip().strip('"')
    if not token:
        raise ConfigError("BHOPP_TOKEN is missing")
    discord_token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if need_discord and not discord_token:
        raise ConfigError("DISCORD_BOT_TOKEN is missing")
    try:
        poll = float(os.environ.get("POLL_INTERVAL_SECONDS", "15") or 15)
    except ValueError:
        raise ConfigError("POLL_INTERVAL_SECONDS must be a number") from None
    if poll < 5:
        raise ConfigError("POLL_INTERVAL_SECONDS must be at least 5")
    return Config(token, discord_token, _id("DISCORD_CHANNEL_ID", need_discord),
                  _id("DISCORD_OWNER_ID", need_discord), _id("DISCORD_GUILD_ID", False), poll,
                  Path(os.environ.get("DATA_DIR", "data")),
                  os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on"))
