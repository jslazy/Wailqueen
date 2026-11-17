"""Custom icons (assets/icons/*.png) used in embeds and messages.

On startup the PNG files are uploaded once as *application emojis* of the bot (they work in every
server and in DMs, no server slots used). Later starts only reuse what already exists.
If anything fails the bot simply falls back to the plain unicode emoji below, so it never breaks.

Use:  from utils.icons import icon      ->      f"{icon('ok')} Done"
"""
from __future__ import annotations

import logging
from pathlib import Path

import discord

import config

log = logging.getLogger("premium-bot.icons")

ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"
PREFIX = "wl_"  # emoji name prefix inside the Developer Portal

# icon name -> unicode fallback (used until/unless the custom emoji is available)
FALLBACK: dict[str, str] = {
    "ok": "✅", "error": "⛔", "warn": "⚠️", "lock": "🔒", "unlock": "🔓", "hourglass": "⏳",
    "stopwatch": "⏱️", "calendar": "📅", "users": "👥", "user_add": "➕", "crown": "👑",
    "diamond": "💎", "star": "⭐", "shield": "🛡️", "gavel": "🔨", "trash": "🗑️", "recycle": "♻️",
    "link": "🔗", "globe": "🌐", "chat": "💬", "mic": "🎙️", "image": "🖼️", "banner": "🖼️",
    "gear": "⚙️", "chart": "📊", "wifi": "📶", "terminal": "💻", "code": "⌨️", "rocket": "🚀",
    "mask": "🎭", "gamepad": "🎮", "moon": "🌙", "offline": "⚫", "dnd": "⛔", "bell": "🔔",
    "gift": "🎁", "heart": "💙", "sparkles": "✨", "folder": "📁", "file": "📄",
    "clipboard": "📋", "search": "🔎", "left": "◀️", "right": "▶️", "kick": "👢",
    "compass": "🧭", "puzzle": "🧩",
    "save": "💾", "palette": "🎨", "restore": "⏪", "database": "🗄️", "power": "⏻", "bolt": "⚡",
    "webhook": "🔗", "eye": "👁️", "ticket": "🎫", "hand": "🙋",
    "dot_green": "🟢", "dot_red": "🔴", "dot_yellow": "🟡",
}

_resolved: dict[str, str] = dict(FALLBACK)

# config.EMOJI key -> icon name (keeps the /userinfo code untouched)
_CONFIG_EMOJI = {
    "badges": "shield", "none": "error", "roles": "mask", "permissions": "lock",
    "activity": "gamepad", "online": "ok", "idle": "moon", "dnd": "dnd", "offline": "offline",
}


def icon(name: str) -> str:
    """Emoji text for an icon name (custom emoji if loaded, unicode otherwise)."""
    return _resolved.get(name, "")


async def sync(bot: discord.Client) -> None:
    """Upload missing icons as application emojis and start using them. Never raises."""
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
        for name in FALLBACK:
            file = ICON_DIR / f"{name}.png"
            key = f"{PREFIX}{name}"
            emoji = existing.get(key)
            if emoji is None and file.exists():
                try:
                    emoji = await bot.create_application_emoji(name=key, image=file.read_bytes())
                except discord.HTTPException:
                    log.warning("Could not upload icon %s", name)
                    continue
            if emoji is not None:
                _resolved[name] = str(emoji)
        for key, name in _CONFIG_EMOJI.items():
            if key in config.EMOJI and _resolved[name].startswith("<"):
                config.EMOJI[key] = _resolved[name]
        log.info("Icons ready (%d custom)", sum(1 for v in _resolved.values() if v.startswith("<")))
    except Exception:
        log.exception("Icon sync failed, using unicode fallbacks")
