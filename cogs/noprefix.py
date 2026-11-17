from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.icons import icon
from utils.ui import build_pages, make_embed, send_pages


class NoPrefix(commands.Cog):
    """Members who can run commands without typing the prefix. Bot owner only."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        rows = self.db.execute("SELECT user_id FROM no_prefix").fetchall()
        self.bot.noprefix_ids.update(r[0] for r in rows)  # type: ignore[attr-defined]

    @commands.hybrid_group(
        name="noprefix",
        aliases=["np"],
        invoke_without_command=True,
        description="Let members use commands without the prefix (bot owner only).",
    )
    @commands.is_owner()
    async def noprefix(self, ctx: commands.Context) -> None:
        p = config.PREFIX
        await ctx.send(
            embed=make_embed(
                f"{icon('sparkles')} No prefix",
                f"`{p}noprefix add <member>` • member can type `ping` instead of `{p}ping`\n"
                f"`{p}noprefix remove <member>`\n"
                f"`{p}noprefix list`",
            )
        )

    @noprefix.command(name="add", description="Give a user no-prefix access.")
    @app_commands.describe(user="The user who should not need the prefix.")
    @commands.is_owner()
    async def noprefix_add(self, ctx: commands.Context, user: discord.User) -> None:
        if user.bot:
            return await ctx.send(f"{icon('error')} Bots can't be added.", ephemeral=True)
        if user.id in self.bot.noprefix_ids:  # type: ignore[attr-defined]
            return await ctx.send(f"{icon('warn')} {user.mention} already has no-prefix.", ephemeral=True)
        self.db.execute(
            "INSERT OR IGNORE INTO no_prefix (user_id, added_by, created_at) VALUES (?,?,?)",
            (user.id, ctx.author.id, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        self.bot.noprefix_ids.add(user.id)  # type: ignore[attr-defined]
        await ctx.send(
            f"{icon('ok')} {user.mention} can now use commands without the prefix.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @noprefix.command(name="remove", description="Remove a user's no-prefix access.")
    @app_commands.describe(user="The user to remove.")
    @commands.is_owner()
    async def noprefix_remove(self, ctx: commands.Context, user: discord.User) -> None:
        cur = self.db.execute("DELETE FROM no_prefix WHERE user_id=?", (user.id,))
        self.db.commit()
        self.bot.noprefix_ids.discard(user.id)  # type: ignore[attr-defined]
        if cur.rowcount:
            await ctx.send(
                f"{icon('trash')} {user.mention} now needs the prefix again.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send(f"{icon('warn')} {user.mention} doesn't have no-prefix.", ephemeral=True,
                           allowed_mentions=discord.AllowedMentions.none())

    @noprefix.command(name="list", description="Show everyone who has no-prefix access.")
    @commands.is_owner()
    async def noprefix_list(self, ctx: commands.Context) -> None:
        rows = self.db.execute("SELECT user_id, created_at FROM no_prefix ORDER BY created_at").fetchall()
        lines = [
            f"<@{uid}> (`{uid}`) • {discord.utils.format_dt(datetime.fromisoformat(at), 'R')}"
            for uid, at in rows
        ]
        await send_pages(
            ctx, build_pages(f"{icon('sparkles')} No-prefix users", lines, per_page=15, empty="Nobody has no-prefix yet.")
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(NoPrefix(bot))
