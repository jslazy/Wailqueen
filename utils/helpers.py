"""Formatting helpers shared by the cogs."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Iterable, Optional

import discord

import config

KEY_PERMISSIONS = (
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "manage_messages",
    "manage_webhooks",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mention_everyone",
)

PERMISSION_NAMES = {"moderate_members": "Timeout Members", "manage_guild": "Manage Server"}

STATUS_NAMES = {
    discord.Status.online: "Online",
    discord.Status.idle: "Idle",
    discord.Status.dnd: "Do Not Disturb",
    discord.Status.offline: "Offline",
    discord.Status.invisible: "Offline",
}

STATUS_KEYS = {
    discord.Status.online: "online",
    discord.Status.idle: "idle",
    discord.Status.dnd: "dnd",
    discord.Status.offline: "offline",
    discord.Status.invisible: "offline",
}


def pretty_permission(name: str) -> str:
    return PERMISSION_NAMES.get(name, name.replace("_", " ").title())


def relative(dt) -> str:
    return discord.utils.format_dt(dt, "R") if dt else "Unknown"


def stamp(dt) -> str:
    if dt is None:
        return "Unknown"
    return f"{discord.utils.format_dt(dt, 'D')} ({discord.utils.format_dt(dt, 'R')})"


def humanize_seconds(total: int) -> str:
    total = max(int(total), 0)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def role_list(roles: Iterable[discord.Role], limit: int = 900) -> str:
    roles = list(roles)
    if not roles:
        return "None"
    shown, used = [], 0
    for index, role in enumerate(roles):
        mention = role.mention
        if used + len(mention) + 1 > limit:
            shown.append(f"+{len(roles) - index} more")
            break
        shown.append(mention)
        used += len(mention) + 1
    return " ".join(shown)


def member_roles(member: discord.Member) -> list[discord.Role]:
    return [role for role in reversed(member.roles) if not role.is_default()]


def permission_summary(member: discord.Member) -> str:
    if member.id == member.guild.owner_id:
        return "Server Owner"
    perms = member.guild_permissions
    if perms.administrator:
        return "Administrator"
    names = [pretty_permission(name) for name in KEY_PERMISSIONS if getattr(perms, name)]
    return ", ".join(names) if names else "No key permissions"


def badge_text(user: discord.abc.User) -> str:
    """One line per badge, with icons from the discord-badges repo (see utils/badges.py).

    Only the highest account-age badge is shown (10+ years = Primordial only)."""
    from utils import badges

    lines = badges.badge_lines(user)
    return "\n".join(lines) if lines else "None"


def activity_text(member: discord.Member) -> str:
    if not member.activities:
        return "None"
    parts = []
    for activity in member.activities:
        if isinstance(activity, discord.CustomActivity):
            if activity.name:
                parts.append(activity.name)
        elif isinstance(activity, discord.Spotify):
            parts.append(f"Listening to {activity.title} by {activity.artist}")
        else:
            parts.append(f"{activity.type.name.title()} {activity.name}")
    return "\n".join(parts) if parts else "None"


def asset_url(asset: discord.Asset, fmt: Optional[str] = None, size: int = 1024) -> str:
    try:
        sized = asset.with_size(size)
        if fmt:
            sized = sized.with_format(fmt)  # type: ignore[arg-type]
        return sized.url
    except ValueError:
        return asset.url


def format_links(asset: discord.Asset) -> str:
    formats = ("gif", "png", "webp") if asset.is_animated() else ("png", "jpg", "webp")
    parts, seen = [], set()
    for fmt in formats:
        url = asset_url(asset, fmt)
        if url not in seen:
            seen.add(url)
            parts.append(f"[{fmt.upper()}]({url})")
    return " | ".join(parts)


_DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text: str) -> Optional[timedelta]:
    """Turn '10m', '2h30m', '1d' into a timedelta. Returns None if invalid."""
    text = text.strip().lower().replace(" ", "")
    matches = _DURATION_RE.findall(text)
    if not matches or "".join(f"{n}{u}" for n, u in matches) != text:
        return None
    seconds = sum(int(n) * _UNIT_SECONDS[u.lower()] for n, u in matches)
    return timedelta(seconds=seconds) if seconds > 0 else None
