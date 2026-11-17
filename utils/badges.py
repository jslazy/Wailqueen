"""Profile badges for /userinfo, with icons from https://github.com/mezotv/discord-badges.

* `badge_lines(user)` returns one line per badge ("<icon> Name"), already de-duplicated:
  only ONE account-age badge (the highest one) and ONE boost badge are ever shown.
* `sync(bot)` makes the icons available. For every badge it
    1. uses assets/badges/<key>.png if that file exists (you can drop your own PNG there),
    2. otherwise downloads the SVG from the repo, converts it to a 128x128 PNG and caches it
       in assets/badges/,
  then uploads it once as an *application emoji* (name wl_badge_<key>). Later starts only reuse the
  existing emojis. If anything fails the badge simply shows its unicode fallback, nothing breaks.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import aiohttp
import discord

log = logging.getLogger("premium-bot.badges")

REPO_RAW = "https://raw.githubusercontent.com/mezotv/discord-badges/main/assets/"
CACHE_DIR = Path(__file__).resolve().parent.parent / "assets" / "badges"
EMOJI_PREFIX = "wl_badge_"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MAX_EMOJI_BYTES = 256 * 1024


@dataclass(frozen=True)
class Badge:
    label: str
    path: str       # file inside the repo's assets/ folder
    fallback: str   # unicode emoji used until the custom icon is available


def _b(label: str, path: str, fallback: str) -> Badge:
    return Badge(label, path, fallback)


# --- account age (leveled): level = full years, capped at 10 -----------------------------------
AGE_NAMES = ["seed", "sprout", "bud", "sapling", "blossom", "redwood",
             "sequoia", "bristlecone", "stromatolite", "primordial"]
AGE_MAX = len(AGE_NAMES)  # 10


def _age_label(level: int) -> str:
    name = AGE_NAMES[level - 1].title()
    return f"{name} (10+ years)" if level == AGE_MAX else f"{name} ({level} year{'s' if level > 1 else ''})"


# --- server boost (leveled): months boosting -> badge level ---------------------------------------
BOOST_MONTHS = [1, 2, 3, 6, 9, 12, 15, 18, 24]  # level 1..9


BADGES: dict[str, Badge] = {
    # profile flags
    "staff": _b("Discord Staff", "discord-staff.svg", "🛡️"),
    "partner": _b("Partnered Server Owner", "discord-partner.svg", "🤝"),
    "hypesquad_events": _b("HypeSquad Events", "hype-squad-events.svg", "🎉"),
    "bug_hunter": _b("Discord Bug Hunter", "discord-bug-hunter-green.svg", "🐛"),
    "bug_hunter_gold": _b("Discord Bug Hunter (Gold)", "discord-bug-hunter-gold.svg", "🐞"),
    "hypesquad_bravery": _b("HypeSquad Bravery", "hype-squad-bravery.svg", "💜"),
    "hypesquad_brilliance": _b("HypeSquad Brilliance", "hype-squad-brilliance.svg", "🧡"),
    "hypesquad_balance": _b("HypeSquad Balance", "hype-squad-balance.svg", "💚"),
    "early_supporter": _b("Early Supporter", "discord-early-supporter.svg", "🌟"),
    "bot_dev": _b("Early Verified Bot Developer", "discord-bot-dev.svg", "🤖"),
    "moderator": _b("Moderator Programs Alumni", "discord-mod.svg", "🔨"),
    "active_dev": _b("Active Developer", "active-developer.svg", "💻"),
    "nitro": _b("Discord Nitro", "discord-nitro.svg", "💎"),
    "verified_bot": _b("Verified Bot", "special/verified-bot.svg", "✅"),
    "app": _b("App", "special/app.svg", "🤖"),
}
for _i, _name in enumerate(AGE_NAMES, start=1):
    BADGES[f"age_{_i}"] = _b(_age_label(_i), f"account-age/{_name}.svg", "🌱")
for _i, _months in enumerate(BOOST_MONTHS, start=1):
    BADGES[f"boost_{_i}"] = _b(f"Server Booster ({_months}+ month{'s' if _months > 1 else ''})",
                               f"boosts/discord-boost-{_i}.svg", "🚀")

# --- badges Discord does NOT expose to bots: shown only when the bot owner assigns them (`badge` command)
STREAM_NAMES = ["newcomer", "fledgling", "breakout", "standout", "trendsetter",
                "headliner", "star", "sensation", "visionary", "phenomenon"]
GTIME_NAMES = ["casual", "recreational", "dedicated", "committed", "serious",
               "devoted", "seasoned", "ironclad", "unshakeable", "eternal"]
GVAR_NAMES = ["sampler", "dabbler", "enthusiast", "ranger", "explorer",
              "adventurer", "voyager", "maverick", "polymath", "universalist"]
TIER_NAMES = ["bronze", "silver", "gold", "platinum", "diamond", "emerald", "ruby", "opal"]

for _i, _name in enumerate(STREAM_NAMES, start=1):
    BADGES[f"stream_{_i}"] = _b(f"Streaming: {_name.title()}", f"streaming/{_name}.svg", "📺")
for _i, _name in enumerate(GTIME_NAMES, start=1):
    BADGES[f"gtime_{_i}"] = _b(f"Hours Played: {_name.title()}", f"game-time/{_name}.svg", "🎮")
for _i, _name in enumerate(GVAR_NAMES, start=1):
    BADGES[f"gvar_{_i}"] = _b(f"Game Variety: {_name.title()}", f"game-variety/{_name}.svg", "🕹️")
for _name in TIER_NAMES:
    BADGES[f"tier_{_name}"] = _b(f"Nitro Subscriber: {_name.title()}", f"subscriptions/badges/{_name}.png", "💎")
BADGES.update({
    "nitro_basic": _b("Discord Nitro Basic", "discord-nitro-basic.svg", "💎"),
    "last_meadow": _b("Last Meadow Online", "last-meadow.png", "🌿"),
    "orb": _b("Orbs Apprentice", "orb.svg", "🔮"),
    "quest": _b("Completed a Quest", "quest.png", "📜"),
    "lootbox": _b("A clown, for a limited time", "special/discord-lootbox.svg", "🤡"),
    "username": _b("Originally known as (legacy username)", "username.png", "🏷️"),
})

# One badge per family: assigning a new one replaces the old one.
FAMILIES: dict[str, list[str]] = {
    "streaming": [f"stream_{i}" for i in range(1, 11)],
    "game time": [f"gtime_{i}" for i in range(1, 11)],
    "game variety": [f"gvar_{i}" for i in range(1, 11)],
    "nitro tier": [f"tier_{n}" for n in TIER_NAMES],
}
MANUAL_KEYS: list[str] = FAMILIES["nitro tier"] + [
    k for fam, keys in FAMILIES.items() if fam != "nitro tier" for k in keys] + [
    "nitro_basic", "last_meadow", "orb", "quest", "lootbox", "username"]
_KEY_FAMILY = {k: fam for fam, keys in FAMILIES.items() for k in keys}

# user id -> manually assigned badge keys (filled from the database by cogs/badges.py)
custom: dict[int, set[str]] = {}

_ALIASES: dict[str, str] = {key: key for key in MANUAL_KEYS}
for _names, _prefix in ((STREAM_NAMES, "stream_"), (GTIME_NAMES, "gtime_"), (GVAR_NAMES, "gvar_")):
    _ALIASES.update({name: f"{_prefix}{i}" for i, name in enumerate(_names, start=1)})
_ALIASES.update({name: f"tier_{name}" for name in TIER_NAMES})


def resolve_manual(text: str) -> Optional[str]:
    """'phenomenon', 'stream_10', 'opal', 'orb' ... -> badge key (None if unknown)."""
    return _ALIASES.get(text.strip().lower().replace(" ", "_").replace("-", "_"))


def set_custom(user_id: int, key: str) -> None:
    """Add a manual badge; a badge of the same family is replaced."""
    keys = custom.setdefault(user_id, set())
    family = _KEY_FAMILY.get(key)
    if family:
        keys.difference_update(FAMILIES[family])
    keys.add(key)


_resolved: dict[str, str] = {key: badge.fallback for key, badge in BADGES.items()}

# public_flags attribute -> badge key (shown in this order)
_FLAG_BADGES = (
    ("staff", "staff"),
    ("partner", "partner"),
    ("hypesquad", "hypesquad_events"),
    ("hypesquad_bravery", "hypesquad_bravery"),
    ("hypesquad_brilliance", "hypesquad_brilliance"),
    ("hypesquad_balance", "hypesquad_balance"),
    ("bug_hunter", "bug_hunter"),
    ("bug_hunter_level_2", "bug_hunter_gold"),
    ("early_supporter", "early_supporter"),
    ("verified_bot_developer", "bot_dev"),
    ("discord_certified_moderator", "moderator"),
    ("active_developer", "active_dev"),
)


# ----------------------------------------------------------------------------- badge logic
def account_age_years(created_at, now=None) -> int:
    """Full years since the account was created."""
    now = now or discord.utils.utcnow()
    years = now.year - created_at.year
    if (now.month, now.day) < (created_at.month, created_at.day):
        years -= 1
    return max(years, 0)


def account_age_key(created_at, now=None) -> Optional[str]:
    """Key of the single account-age badge: 10+ years -> Primordial only, never a lower one."""
    level = min(account_age_years(created_at, now), AGE_MAX)
    return f"age_{level}" if level >= 1 else None


def boost_key(premium_since, now=None) -> Optional[str]:
    """Key of the single (highest) boost badge for how long someone has been boosting."""
    if premium_since is None:
        return None
    now = now or discord.utils.utcnow()
    months = (now.year - premium_since.year) * 12 + (now.month - premium_since.month)
    if now.day < premium_since.day:
        months -= 1
    level = sum(1 for needed in BOOST_MONTHS if months >= needed)
    return f"boost_{level}" if level else "boost_1"  # boosting right now = at least level 1


def _has_nitro(user) -> bool:
    """Bots can't read Nitro directly, so this is inferred from Nitro-only things."""
    if getattr(user, "premium_since", None):
        return True
    avatar = getattr(user, "avatar", None)
    if avatar is not None and avatar.is_animated():
        return True
    return getattr(user, "guild_avatar", None) is not None


def badge_keys(user) -> list[str]:
    flags = getattr(user, "public_flags", None)
    keys: list[str] = []
    if flags is not None:
        keys += [key for attr, key in _FLAG_BADGES if getattr(flags, attr, False)]
    if getattr(user, "bot", False):
        keys.append("verified_bot" if flags is not None and flags.verified_bot else "app")
    elif _has_nitro(user):
        keys.append("nitro")
    manual = custom.get(getattr(user, "id", 0), set())
    if manual:
        if any(k.startswith("tier_") for k in manual) or "nitro_basic" in manual:
            keys = [k for k in keys if k != "nitro"]  # a Nitro tier badge replaces the plain one
        keys += [k for k in MANUAL_KEYS if k in manual]
    boost = boost_key(getattr(user, "premium_since", None))
    if boost:
        keys.append(boost)
    created = getattr(user, "created_at", None)
    if created is not None and not getattr(user, "bot", False):
        age = account_age_key(created)
        if age:
            keys.append(age)
    return keys


# badge key -> emoji text chosen by the owner with `!badge emoji` (wins over the icon from the repo)
emoji_override: dict[str, str] = {}

_CUSTOM_EMOJI = re.compile(r"<?(a?):([A-Za-z0-9_~]{2,32}):(\d{15,25})>?")


def parse_emoji(text: str) -> Optional[str]:
    """Clean emoji text -> exact string Discord needs, or None if it is not an emoji.

    Custom emojis are rebuilt as <:name:id> / <a:name:id> even when the closing '>' got lost
    (typed by hand, copied badly, cut by a text box...), so they never turn into plain text.
    Normal unicode emojis (also flags, keycaps, ZWJ sequences) are accepted as they are."""
    text = (text or "").strip().strip("`").strip()
    if not text:
        return None
    match = _CUSTOM_EMOJI.fullmatch(text)
    if match:
        animated, name, eid = match.groups()
        return f"<{animated}:{name}:{eid}>"
    if len(text) > 32 or any(ch.isspace() for ch in text) or text.isascii():
        return None
    return text


def badge_emoji(key: str) -> str:
    return emoji_override.get(key) or _resolved.get(key) or BADGES[key].fallback


def badge_lines(user) -> list[str]:
    return [f"{badge_emoji(key)} {BADGES[key].label}" for key in badge_keys(user)]


# ----------------------------------------------------------------------------- icon loading
async def _svg_to_png(session: aiohttp.ClientSession, key: str, svg: bytes) -> Optional[bytes]:
    """SVG -> 128x128 PNG. Tries cairosvg (if installed), then the free wsrv.nl image service."""
    try:
        import cairosvg  # optional, needs the native cairo library

        loop = asyncio.get_running_loop()
        png = await loop.run_in_executor(
            None, lambda: cairosvg.svg2png(bytestring=svg, output_width=128, output_height=128)
        )
        if png.startswith(PNG_MAGIC) and len(png) <= MAX_EMOJI_BYTES:
            return png
    except Exception:
        pass
    url = (
        "https://wsrv.nl/?url=" + quote(REPO_RAW + BADGES[key].path, safe="")
        + "&output=png&w=128&h=128&fit=contain"
    )
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                png = await resp.read()
                if png.startswith(PNG_MAGIC) and len(png) <= MAX_EMOJI_BYTES:
                    return png
    except Exception:
        pass
    return None


async def _png_for(session: aiohttp.ClientSession, key: str) -> Optional[bytes]:
    cached = CACHE_DIR / f"{key}.png"
    if cached.exists():
        return cached.read_bytes()
    try:
        async with session.get(REPO_RAW + BADGES[key].path) as resp:
            if resp.status != 200:
                log.warning("Badge %s: download failed (HTTP %s)", key, resp.status)
                return None
            svg = await resp.read()
        if svg.startswith(PNG_MAGIC) and len(svg) <= MAX_EMOJI_BYTES:  # already a PNG (e.g. tier badges)
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(svg)
            except OSError:
                pass
            return svg
    except Exception as exc:
        log.warning("Badge %s: download failed (%s)", key, type(exc).__name__)
        return None
    png = await _svg_to_png(session, key, svg)
    if png is None:
        log.warning("Badge %s: could not convert SVG to PNG", key)
        return None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(png)
    except OSError:
        pass
    return png


async def sync(bot: discord.Client) -> None:
    """Make every badge icon an application emoji and start using it. Never raises."""
    try:
        existing = {e.name: e for e in await bot.fetch_application_emojis()}
        for key in BADGES:
            emoji = existing.get(EMOJI_PREFIX + key)
            if emoji is not None:
                _resolved[key] = str(emoji)

        missing = [key for key in BADGES if EMOJI_PREFIX + key not in existing]
        if not missing:
            log.info("Badge icons ready (%d)", len(BADGES))
            return
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for key in missing:
                png = await _png_for(session, key)
                if png is None:
                    continue
                try:
                    emoji = await bot.create_application_emoji(name=EMOJI_PREFIX + key, image=png)
                except discord.HTTPException:
                    log.warning("Badge %s: emoji upload failed", key)
                    continue
                _resolved[key] = str(emoji)
        ready = sum(1 for v in _resolved.values() if v.startswith("<"))
        log.info("Badge icons ready (%d/%d custom)", ready, len(BADGES))
    except Exception:
        log.exception("Badge icon sync failed, using unicode fallbacks")
