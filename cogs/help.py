from __future__ import annotations

import difflib
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.icons import icon
from utils.ui import make_embed

CATEGORY_ICONS = {
    "Information": "search", "Moderation": "gavel", "Welcome": "gift", "Giveaway": "gift",
    "Aliases": "link", "Tickets": "clipboard", "Automod": "lock", "Backup": "folder", "NoPrefix": "star", "Help": "compass", "Errors": "warn",
    "BadgeAdmin": "shield", "AntiNuke": "shield",
}


ADMIN_COGS = {"Moderation", "Tickets", "Automod", "Welcome", "Giveaway", "Aliases", "NoPrefix", "Backup", "BadgeAdmin", "AntiNuke"}


def is_staff(ctx: commands.Context) -> bool:
    """True for the bot owner and members with a staff permission. Everyone else must never see
    (or get an answer from) admin commands and the admin help."""
    if ctx.author.id in (getattr(ctx.bot, "owner_ids", None) or ()) or ctx.author.id == getattr(ctx.bot, "owner_id", None):
        return True
    if not isinstance(ctx.author, discord.Member):
        return False
    p = ctx.author.guild_permissions
    return ctx.author.id == ctx.guild.owner_id or any(
        (p.administrator, p.manage_guild, p.manage_messages, p.manage_channels, p.kick_members,
         p.ban_members, p.moderate_members, p.manage_roles)
    )


async def silent_deny(ctx: commands.Context) -> None:
    """Normal member used an admin-only command: no answer at all, just remove their message."""
    if ctx.interaction is not None:
        try:
            await ctx.interaction.response.send_message(f"{icon('lock')} Not allowed.", ephemeral=True)
        except discord.HTTPException:
            pass
        return
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass


class HelpMenu(discord.ui.View):
    """Small embed + 'Pick a category...' dropdown (one page per category)."""

    def __init__(self, author_id: int, home: discord.Embed, pages: dict[str, discord.Embed]) -> None:
        super().__init__(timeout=180)
        self.author_id, self.home, self.pages = author_id, home, pages
        options = [discord.SelectOption(label="Home", value="__home__", emoji=icon("compass") or None)]
        for name in pages:
            options.append(discord.SelectOption(label=name, value=name, emoji=icon(CATEGORY_ICONS.get(name, "puzzle")) or None))
        select = discord.ui.Select(placeholder="Pick a category...", options=options[:25])
        select.callback = self._picked
        self.select = select
        self.add_item(select)
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Run the help command yourself to use this menu.", ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction) -> None:
        value = self.select.values[0]
        await interaction.response.edit_message(embed=self.home if value == "__home__" else self.pages[value], view=self)

    async def on_timeout(self) -> None:
        self.select.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Help(commands.Cog):
    """Command browser. Everyone sees only the commands they are allowed to use."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # --------------------------------------------------------- visibility
    @staticmethod
    def _member_perms(ctx: commands.Context) -> discord.Permissions:
        if isinstance(ctx.author, discord.Member):
            return ctx.author.guild_permissions
        return discord.Permissions.none()

    async def _can_use(self, ctx: commands.Context, cmd: commands.Command) -> bool:
        """True if this user passes the command's checks (permissions etc.)."""
        if cmd.hidden:
            return False
        if cmd.cog is not None and cmd.cog.qualified_name in ADMIN_COGS and not is_staff(ctx):
            return False
        try:
            return await cmd.can_run(ctx)
        except commands.CommandError:
            return False

    async def _visible(self, ctx: commands.Context) -> list[commands.Command]:
        return [c for c in self.bot.commands if await self._can_use(ctx, c)]

    def _slash_only(self, ctx: commands.Context) -> list[app_commands.Command]:
        """Slash-only commands the user has the default permissions for."""
        prefixed = {c.name for c in self.bot.commands}
        perms = self._member_perms(ctx)
        owner = ctx.guild is not None and ctx.author.id == ctx.guild.owner_id
        result = []
        for c in self.bot.tree.get_commands():
            if not isinstance(c, app_commands.Command) or c.name in prefixed:
                continue
            needed = c.default_permissions
            if needed is not None and not (owner or perms.administrator or perms.is_superset(needed)):
                continue
            if c.binding is not None and c.binding.qualified_name in ADMIN_COGS and not is_staff(ctx):
                continue
            if c.guild_only and ctx.guild is None:
                continue
            result.append(c)
        return result

    # ------------------------------------------------------------ command
    @commands.hybrid_command(
        name="help",
        aliases=["h", "commands"],
        description="Show the commands you can use, or details about one.",
    )
    @app_commands.describe(command="A command name to view details for.")
    async def help_command(self, ctx: commands.Context, *, command: Optional[str] = None) -> None:
        if not is_staff(ctx):
            return await silent_deny(ctx)  # members without staff permissions get no help answer at all
        if command:
            await self._send_detail(ctx, command.strip().lstrip(config.PREFIX).lstrip("/"))
        else:
            await self._send_overview(ctx)

    @help_command.autocomplete("command")
    async def help_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        try:
            ctx = await commands.Context.from_interaction(interaction)
            names = {c.qualified_name for c in await self._visible(ctx)}
            names |= {c.name for c in self._slash_only(ctx)}
        except Exception:
            return []
        current = current.lower()
        return [app_commands.Choice(name=n, value=n) for n in sorted(names) if current in n][:25]

    async def _collect(self, ctx: commands.Context, admin_only: bool) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for top in sorted(self.bot.commands, key=lambda c: c.qualified_name):
            family = [top] + (sorted(top.walk_commands(), key=lambda c: c.qualified_name) if isinstance(top, commands.Group) else [])
            for cmd in family:
                category = cmd.cog.qualified_name if cmd.cog else "Other"
                if admin_only != (category in ADMIN_COGS) or not await self._can_use(ctx, cmd):
                    continue
                desc = (cmd.description or cmd.short_doc or "").split("\n")[0][:60]
                usage = f"{config.PREFIX}{cmd.qualified_name} {cmd.signature}".strip()
                grouped.setdefault(category, []).append(f"`{usage}` - {desc}" if desc else f"`{usage}`")
        for cmd in self._slash_only(ctx):
            category = cmd.binding.qualified_name if cmd.binding else "Other"  # type: ignore[union-attr]
            if admin_only == (category in ADMIN_COGS):
                grouped.setdefault(category, []).append(f"`/{cmd.name}` - {cmd.description[:60]}")
        return grouped

    async def _send_menu(self, ctx: commands.Context, title: str, grouped: dict[str, list[str]]) -> None:
        lines = [f"Prefix: `{config.PREFIX}`", "`<x>` = required · `[x]` = optional", ""]
        pages: dict[str, discord.Embed] = {}
        for category in sorted(grouped):
            glyph = icon(CATEGORY_ICONS.get(category, "puzzle"))
            lines.append(f"{glyph} **{category}** - {len(grouped[category])}")
            pages[category] = make_embed(f"{glyph} {category}", "\n".join(grouped[category])[:4000])
        lines += ["", "*Pick a category in the menu below.*"]
        home = make_embed(f"{icon('sparkles')} {title}", "\n".join(lines))
        view = HelpMenu(ctx.author.id, home, pages)
        view.message = await ctx.send(embed=home, view=view)

    async def _send_overview(self, ctx: commands.Context) -> None:
        grouped = await self._collect(ctx, admin_only=False)
        grouped.update({k: v for k, v in (await self._collect(ctx, admin_only=True)).items() if k not in grouped})
        await self._send_menu(ctx, f"{config.BOT_NAME} Help", grouped)

    @commands.hybrid_command(
        name="adminhelp", aliases=["admin", "ahelp"],
        description="Admin commands: moderation, tickets, automod, backup... (staff only)",
    )
    async def adminhelp(self, ctx: commands.Context) -> None:
        if not is_staff(ctx):
            return await silent_deny(ctx)  # normal members get NO reply
        grouped = await self._collect(ctx, admin_only=True)
        if not grouped:
            return await silent_deny(ctx)
        await self._send_menu(ctx, f"{config.BOT_NAME} Admin Help", grouped)

    async def _send_detail(self, ctx: commands.Context, name: str) -> None:
        name = name.lower()
        cmd = self.bot.get_command(name)
        if cmd is not None and cmd.cog is not None and cmd.cog.qualified_name in ADMIN_COGS and not is_staff(ctx):
            return await silent_deny(ctx)
        if cmd is None and ctx.guild is not None:
            target = self.bot.custom_alias(ctx.guild.id, name)  # type: ignore[attr-defined]
            if target:
                cmd = self.bot.get_command(target)

        if cmd is not None and await self._can_use(ctx, cmd):
            usage = f"{config.PREFIX}{cmd.qualified_name} {cmd.signature}".strip()
            lines = [cmd.description or cmd.short_doc or "No description provided.", "", f"{icon('terminal')} **Usage:** `{usage}`"]
            if cmd.aliases:
                lines.append(f"{icon('link')} **Aliases:** " + ", ".join(f"`{a}`" for a in cmd.aliases))
            if ctx.guild is not None:
                rows = self.bot.db.conn.execute(  # type: ignore[attr-defined]
                    "SELECT alias FROM command_aliases WHERE guild_id=? AND command=? ORDER BY alias",
                    (ctx.guild.id, cmd.qualified_name),
                ).fetchall()
                if rows:
                    lines.append(f"{icon('link')} **Server aliases:** " + ", ".join(f"`{r[0]}`" for r in rows))
            if cmd.cog:
                lines.append(f"{icon('folder')} **Category:** {cmd.cog.qualified_name}")
            await ctx.send(embed=make_embed(cmd.qualified_name, "\n".join(lines), footer="<required> • [optional]"))
            return

        slash = next((c for c in self._slash_only(ctx) if c.name == name), None)
        if slash is not None:
            await ctx.send(
                embed=make_embed(
                    f"/{slash.name}",
                    f"{slash.description}\n\n{icon('terminal')} **Usage:** `/{slash.name}`\n{icon('folder')} **Type:** Slash command only",
                )
            )
            return

        known = sorted({c.qualified_name for c in await self._visible(ctx)} | {c.name for c in self._slash_only(ctx)})
        message = f"{icon('warn')} No command named `{name}` was found."
        close = difflib.get_close_matches(name, known, n=3, cutoff=0.5)
        if close:
            message += "\nDid you mean: " + ", ".join(f"`{c}`" for c in close)
        await ctx.send(message, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))
