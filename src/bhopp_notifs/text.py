"""Small text helpers: secret redaction, Discord escaping, money, durations, templates."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[\w\-.~+/=]+"),
    re.compile(r"(?i)((?:token|authorization|secret|password)\"?\s*[:=]\s*\"?)[^\s\",;]+"),
    re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}"),  # JWTs (the BHOPP token)
]
_KNOWN_SECRETS: set[str] = set()
_MD_SPECIAL = re.compile(r"([\\*_~`|\[\]()<>#@])")
_TEMPLATE = re.compile(r"\{(buyer|seller|items|total|order|left|until)\}")
_DURATION_PART = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m)(?![a-z])", re.I)
MAX_MESSAGE = 1000
MIN_AFK, MAX_AFK = timedelta(minutes=1), timedelta(days=30)


def register_secret(value) -> None:
    v = str(value or "").strip()
    if len(v) >= 8:
        _KNOWN_SECRETS.add(v)


def redact(text) -> str:
    out = str(text)
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        out = out.replace(secret, "[redacted]")
    for pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", out)
    return out


def md(text) -> str:
    """Escape Discord markdown, so a name from the site can't inject links or mentions."""
    return _MD_SPECIAL.sub(r"\\\1", str(text))


def clip(text, limit: int) -> str:
    s = redact(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def money(value: Decimal) -> str:
    """$12.00. Under a dollar keeps 1/100 cents ($0.1615), where rounding would mislead."""
    d = Decimal(value)
    two = d.quantize(Decimal("0.01"))
    if d == two or abs(d) >= 1:
        return f"${two:,}"
    return f"${d.quantize(Decimal('0.0001')).normalize():,f}"


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_duration(raw: str) -> timedelta:
    """"2h", "1h30m", "90 minutes", "1 day"; a bare number is minutes. Raises ValueError."""
    s = (raw or "").strip().lower()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        total = timedelta(minutes=float(s))
    else:
        total, pos = timedelta(), 0
        for m in _DURATION_PART.finditer(s):
            if s[pos:m.start()].strip(" ,") not in ("", "and"):
                raise ValueError(f"could not read {raw!r}")
            n, unit = float(m.group(1)), m.group(2)[0]
            total += (timedelta(days=n) if unit == "d"
                      else timedelta(hours=n) if unit == "h" else timedelta(minutes=n))
            pos = m.end()
        if pos == 0 or s[pos:].strip():
            raise ValueError(f"could not read {raw!r}; try 30m, 2h or 1h30m")
    if not MIN_AFK <= total <= MAX_AFK:
        raise ValueError("the duration must be between 1 minute and 30 days")
    return total


def format_left(left: timedelta) -> str:
    """2h 15m, 1d 3h, 45m. Rounded up, so it never says 0."""
    minutes = max(1, int((left.total_seconds() + 59) // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    return f"{mins}m"


def render(template: str, **values) -> str:
    """Fill known {placeholders}; anything else in braces is left alone, never an error."""
    return clip(_TEMPLATE.sub(lambda m: str(values.get(m.group(1), m.group(0))), template),
                MAX_MESSAGE)
