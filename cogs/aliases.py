from __future__ import annotations

import re

from discord import app_commands
from discord.ext import commands

import config
from utils.icons import icon
from utils.ui import build_pages, make_embed, send_pages

MAX_ALIASES = 50
_ALIAS_RE = re.compile(r"^[a-z0-9_-]{1,20}$")


class Aliases(commands.Cog):
    """Admins can give any command an extra short name, for their server only."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    def _resolve(self, guild_id: int, name: str) -> commands.Command | None:
        """Find a real command from its name, built-in alias or a custom alias of this server."""
        name = name.strip().lstrip(config.PREFIX).lstrip("/").lower()
        command = self.bot.get_command(name)
        if command is None:
            target = self.bot.custom_alias(guild_id, name)  # type: ignore[attr-defined]
            if target:
                command = self.bot.get_command(target)
        return command

    @commands.hybrid_group(
        name="alias",
        invoke_without_command=True,
        description="Add a short name for any command (admins only).",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def alias(self, ctx: commands.Context) -> None:
        p = config.PREFIX
        await ctx.send(
            embed=make_embed(
                f"{icon('link')} Command aliases",
                f"`{p}alias add <command> <new name>` • example: `{p}alias add warn w`\n"
                f"`{p}alias remove <name>`\n"
                f"`{p}alias list`",
            )
        )

    @alias.command(name="add", description="Give a command a new short name.")
    @app_commands.describe(command="The command, e.g. warn.", new_alias="The new short name, e.g. w.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def alias_add(self, ctx: commands.Context, command: str, new_alias: str) -> None:
        assert ctx.guild is not None
        new_alias = new_alias.strip().lstrip(config.PREFIX).lower()
        if not _ALIAS_RE.match(new_alias):
            return await ctx.send(
                f"{icon('warn')} The alias must be 1-20 characters: letters, numbers, `-` or `_`.", ephemeral=True
            )
        target = self._resolve(ctx.guild.id, command)
        if target is None or target.parent is not None:
            return await ctx.send(f"{icon('warn')} `{command}` is not a command I know.", ephemeral=True)
        if self.bot.get_command(new_alias) is not None:
            return await ctx.send(f"{icon('warn')} `{new_alias}` is already a built-in command or alias.", ephemeral=True)
        existing = self.bot.custom_alias(ctx.guild.id, new_alias)  # type: ignore[attr-defined]
        if existing:
            return await ctx.send(
                f"{icon('warn')} `{new_alias}` already points to `{existing}`. Remove it first.", ephemeral=True
            )
        count = self.db.execute(
            "SELECT COUNT(*) FROM command_aliases WHERE guild_id=?", (ctx.guild.id,)
        ).fetchone()[0]
        if count >= MAX_ALIASES:
            return await ctx.send(f"{icon('warn')} This server already has {MAX_ALIASES} aliases.", ephemeral=True)

        self.db.execute(
            "INSERT INTO command_aliases (guild_id, alias, command) VALUES (?,?,?)",
            (ctx.guild.id, new_alias, target.qualified_name),
        )
        self.db.commit()
        await ctx.send(f"{icon('ok')} Done. `{config.PREFIX}{new_alias}` now runs `{config.PREFIX}{target.qualified_name}`.")

    @alias.command(name="remove", description="Remove a custom alias.")
    @app_commands.describe(name="The alias to remove.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def alias_remove(self, ctx: commands.Context, name: str) -> None:
        assert ctx.guild is not None
        name = name.strip().lstrip(config.PREFIX).lower()
        cur = self.db.execute("DELETE FROM command_aliases WHERE guild_id=? AND alias=?", (ctx.guild.id, name))
        self.db.commit()
        if cur.rowcount:
            await ctx.send(f"{icon('trash')} Alias `{name}` removed.")
        else:
            await ctx.send(f"{icon('warn')} `{name}` is not a custom alias of this server.", ephemeral=True)

    @alias.command(name="list", description="Show this server's custom aliases.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def alias_list(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        rows = self.db.execute(
            "SELECT alias, command FROM command_aliases WHERE guild_id=? ORDER BY command, alias",
            (ctx.guild.id,),
        ).fetchall()
        lines = [f"`{config.PREFIX}{alias}` → `{config.PREFIX}{command}`" for alias, command in rows]
        await send_pages(
            ctx, build_pages(f"{icon('link')} Custom aliases", lines, per_page=15, empty="No custom aliases yet.")
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Aliases(bot))
