from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord
from discord.ext import commands

import config
from utils import badges
from utils.icons import icon
from utils.ui import make_embed


class BadgeAdmin(commands.Cog):
    """Badges Discord doesn't give to bots (streaming, hours played, game variety, Nitro tier,
    legacy username, quests...). The bot owner assigns them and /userinfo shows them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        for user_id, key in self.db.execute("SELECT user_id, badge FROM custom_badges"):
            if key in badges.BADGES:
                badges.set_custom(user_id, key)
        for key, emoji in self.db.execute("SELECT badge, emoji FROM badge_emojis"):
            if key in badges.BADGES:
                badges.emoji_override[key] = emoji

    @commands.group(name="badge", invoke_without_command=True)
    @commands.is_owner()
    async def badge(self, ctx: commands.Context) -> None:
        """Owner only: give members badges Discord doesn't expose to bots."""
        p = config.PREFIX
        await ctx.send(embed=make_embed(
            f"{icon('shield')} Custom badges",
            f"`{p}badge add <user> <badge>` e.g. `{p}badge add @user phenomenon`\n"
            f"`{p}badge remove <user> <badge>`\n"
            f"`{p}badge list [user]`\n`{p}badge keys` all badge names\n"
            f"`{p}badge emoji <badge> <emoji>` change the emoji of a badge (custom emojis work)\n"
            f"`{p}badge emoji <badge> reset` back to the default icon\n\n"
            "One badge per family: adding a new streaming / hours played / game variety / Nitro tier "
            "badge replaces the old one. Account age and boost badges are automatic.",
        ))

    @badge.command(name="add")
    @commands.is_owner()
    async def badge_add(self, ctx: commands.Context, user: discord.User, *, badge: str) -> None:
        key = badges.resolve_manual(badge)
        if key is None:
            return await ctx.send(f"{icon('error')} Unknown badge `{badge}`. See `{config.PREFIX}badge keys`.")
        family = badges._KEY_FAMILY.get(key)
        if family:
            self.db.execute(
                f"DELETE FROM custom_badges WHERE user_id=? AND badge IN ({','.join('?' * len(badges.FAMILIES[family]))})",
                (user.id, *badges.FAMILIES[family]),
            )
        self.db.execute(
            "INSERT OR REPLACE INTO custom_badges (user_id, badge, added_by, created_at) VALUES (?,?,?,?)",
            (user.id, key, ctx.author.id, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        badges.set_custom(user.id, key)
        await ctx.send(f"{icon('ok')} {user.mention} now has {badges.badge_emoji(key)} **{badges.BADGES[key].label}**.")

    @badge.command(name="remove")
    @commands.is_owner()
    async def badge_remove(self, ctx: commands.Context, user: discord.User, *, badge: str) -> None:
        key = badges.resolve_manual(badge)
        if key is None or key not in badges.custom.get(user.id, set()):
            return await ctx.send(f"{icon('warn')} {user.mention} doesn't have that custom badge.")
        self.db.execute("DELETE FROM custom_badges WHERE user_id=? AND badge=?", (user.id, key))
        self.db.commit()
        badges.custom[user.id].discard(key)
        await ctx.send(f"{icon('ok')} Removed **{badges.BADGES[key].label}** from {user.mention}.")

    @badge.command(name="emoji")
    @commands.is_owner()
    async def badge_emoji(self, ctx: commands.Context, badge: str, *, emoji: str) -> None:
        """Owner only: set the emoji shown for a badge (normal or custom server emoji)."""
        key = badges.resolve_manual(badge) or (badge if badge in badges.BADGES else None)
        if key is None:
            return await ctx.send(f"{icon('error')} Unknown badge `{badge}`. See `{config.PREFIX}badge keys`.")
        if emoji.strip().lower() in {"reset", "default", "none"}:
            self.db.execute("DELETE FROM badge_emojis WHERE badge=?", (key,))
            self.db.commit()
            badges.emoji_override.pop(key, None)
            return await ctx.send(f"{icon('ok')} {badges.badge_emoji(key)} **{badges.BADGES[key].label}** is back to the default icon.")
        clean = badges.parse_emoji(emoji)
        if clean is None:
            return await ctx.send(f"{icon('error')} That is not an emoji. Send the emoji itself, e.g. `{config.PREFIX}badge emoji {badge} 🔥`.")
        self.db.execute("INSERT OR REPLACE INTO badge_emojis (badge, emoji) VALUES (?,?)", (key, clean))
        self.db.commit()
        badges.emoji_override[key] = clean
        note = ""
        if clean.startswith("<"):
            eid = int(clean.rstrip(">").split(":")[-1])
            if self.bot.get_emoji(eid) is None:
                note = f"\n{icon('warn')} I can't see that emoji (I'm not in its server). It may show as text; use an emoji from a server I'm in."
        await ctx.send(f"{icon('ok')} **{badges.BADGES[key].label}** now uses {clean}{note}")

    @badge.command(name="list")
    @commands.is_owner()
    async def badge_list(self, ctx: commands.Context, user: Optional[discord.User] = None) -> None:
        if user is not None:
            keys = [k for k in badges.MANUAL_KEYS if k in badges.custom.get(user.id, set())]
            text = "\n".join(f"{badges.badge_emoji(k)} {badges.BADGES[k].label}" for k in keys) or "None"
            return await ctx.send(embed=make_embed(f"Custom badges of {user.name}", text))
        lines = [f"<@{uid}>: " + ", ".join(badges.BADGES[k].label for k in badges.MANUAL_KEYS if k in ks)
                 for uid, ks in badges.custom.items() if ks]
        await ctx.send(embed=make_embed("Custom badges", "\n".join(lines)[:4000] or "Nobody has custom badges yet."))

    @badge.command(name="keys")
    @commands.is_owner()
    async def badge_keys(self, ctx: commands.Context) -> None:
        names = lambda ks: ", ".join(f"`{k}`" for k in ks)  # noqa: E731
        singles = ["nitro_basic", "last_meadow", "orb", "quest", "lootbox", "username"]
        embed = make_embed(f"{icon('shield')} Badge names", "Use the name or the key, e.g. `phenomenon` or `stream_10`.")
        embed.add_field(name="Streaming (1-10)", value=", ".join(badges.STREAM_NAMES), inline=False)
        embed.add_field(name="Hours played (1-10)", value=", ".join(badges.GTIME_NAMES), inline=False)
        embed.add_field(name="Game variety (1-10)", value=", ".join(badges.GVAR_NAMES), inline=False)
        embed.add_field(name="Nitro tier", value=", ".join(badges.TIER_NAMES), inline=False)
        embed.add_field(name="Others", value=names(singles), inline=False)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BadgeAdmin(bot))
