"""Automod for bad words.

!automod                      panel: on/off, mode, timeout time, ignored roles
!automod add <word>           add your own blocked word      !automod remove <word>   !automod list
Modes:  clean   = the message is replaced by the same message with nice words (via webhook, same name + avatar)
        timeout = message deleted + member timed out
        both    = cleaned message + timeout
The bot needs Manage Messages (+ Manage Webhooks for 'clean', Moderate Members for timeouts)."""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import profanity
from utils.icons import icon
from utils.ui import make_embed

log = logging.getLogger("premium-bot.automod")

MODES = {
    "clean": ("Clean", "Replace bad words with nice words"),
    "timeout": ("Timeout", "Delete the message + timeout the member"),
    "both": ("Both", "Clean the message + timeout the member"),
}
WEBHOOK_NAME = f"{config.BOT_NAME} Automod"
MAX_TIMEOUT = 28 * 86400


def parse_seconds(text: str) -> Optional[int]:
    """'30s', '10m', '2h', '1d' (or a plain number = minutes) -> seconds."""
    match = re.fullmatch(r"\s*(\d{1,6})\s*([smhd]?)\s*", (text or "").lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2) or "m"
    seconds = value * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return seconds if 1 <= seconds <= MAX_TIMEOUT else None


def human(seconds: int) -> str:
    for size, name in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{name}"
    return f"{seconds}s"


def safe_name(name: str) -> str:
    name = re.sub(r"(?i)discord", "disc0rd", name)
    name = re.sub(r"(?i)clyde", "cly de", name)
    return (name.strip() or "member")[:80]


class DurationModal(discord.ui.Modal, title="Timeout time"):
    def __init__(self, cog: "Automod", guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=300)
        self.cog, self.guild, self.author_id = cog, guild, author_id
        self.value = discord.ui.TextInput(
            label="Timeout (30s, 10m, 2h, 1d - max 28d)", default=human(cog.get_cfg(guild.id)["timeout_seconds"]), max_length=8
        )
        self.add_item(self.value)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        seconds = parse_seconds(self.value.value)
        if seconds is None:
            return await interaction.response.send_message(f"{icon('warn')} Use `30s`, `10m`, `2h` or `1d` (max 28d).", ephemeral=True)
        self.cog.save_cfg(self.guild.id, timeout_seconds=seconds)
        await interaction.response.edit_message(embed=self.cog.panel_embed(self.guild), view=AutomodView(self.cog, self.author_id, self.guild))


class AutomodView(discord.ui.View):
    def __init__(self, cog: "Automod", author_id: int, guild: discord.Guild) -> None:
        super().__init__(timeout=600)
        self.cog, self.author_id, self.guild = cog, author_id, guild
        cfg = cog.get_cfg(guild.id)
        self.toggle.label = "Disable" if cfg["enabled"] else "Enable"
        self.toggle.style = discord.ButtonStyle.danger if cfg["enabled"] else discord.ButtonStyle.success
        mode = discord.ui.Select(
            placeholder="Mode", row=1,
            options=[discord.SelectOption(label=v[0], value=k, description=v[1], default=(k == cfg["mode"])) for k, v in MODES.items()],
        )
        mode.callback = self._mode(mode)
        roles = discord.ui.RoleSelect(
            placeholder="Ignored roles (not checked)", min_values=0, max_values=10, row=2,
            default_values=[r for r in (guild.get_role(i) for i in cfg["ignored"]) if r is not None],
        )
        roles.callback = self._roles(roles)
        self.add_item(mode)
        self.add_item(roles)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the person who opened this can use it.", ephemeral=True)
            return False
        return True

    async def _redraw(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(embed=self.cog.panel_embed(self.guild), view=AutomodView(self.cog, self.author_id, self.guild))
        self.stop()

    def _mode(self, select: discord.ui.Select):
        async def callback(interaction: discord.Interaction) -> None:
            self.cog.save_cfg(self.guild.id, mode=select.values[0])
            await self._redraw(interaction)
        return callback

    def _roles(self, select: discord.ui.RoleSelect):
        async def callback(interaction: discord.Interaction) -> None:
            self.cog.save_cfg(self.guild.id, ignored_roles=",".join(str(r.id) for r in select.values))
            await self._redraw(interaction)
        return callback

    @discord.ui.button(label="Enable", row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = self.cog.get_cfg(self.guild.id)
        self.cog.save_cfg(self.guild.id, enabled=0 if cfg["enabled"] else 1)
        await self._redraw(interaction)

    @discord.ui.button(label="Timeout time", emoji=icon("stopwatch"), style=discord.ButtonStyle.secondary, row=0)
    async def timeout_time(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(DurationModal(self.cog, self.guild, self.author_id))


class Automod(commands.Cog):
    """Bad-word automod: replaces bad words with nice ones or times the member out."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._hooks: dict[int, discord.Webhook] = {}

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    # ------------------------------------------------------------- storage
    def get_cfg(self, guild_id: int) -> dict:
        row = self.db.execute(
            "SELECT enabled, mode, timeout_seconds, ignored_roles, custom_words FROM automod_config WHERE guild_id=?", (guild_id,)
        ).fetchone()
        enabled, mode, seconds, ignored, words = row if row else (0, "clean", 300, "", "")
        return {
            "enabled": bool(enabled), "mode": mode if mode in MODES else "clean", "timeout_seconds": seconds,
            "ignored": [int(x) for x in ignored.split(",") if x.strip().isdigit()],
            "words": tuple(w for w in words.split("\n") if w.strip()),
        }

    def save_cfg(self, guild_id: int, **changes) -> None:
        allowed = {"enabled", "mode", "timeout_seconds", "ignored_roles", "custom_words"}
        if set(changes) - allowed:
            raise ValueError("unknown automod setting")
        self.db.execute("INSERT OR IGNORE INTO automod_config (guild_id) VALUES (?)", (guild_id,))
        cols = ", ".join(f"{k}=?" for k in changes)
        self.db.execute(f"UPDATE automod_config SET {cols} WHERE guild_id=?", (*changes.values(), guild_id))
        self.db.commit()

    def panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.get_cfg(guild.id)
        roles = " ".join(f"<@&{i}>" for i in cfg["ignored"]) or "None"
        lines = [
            f"**Status:** {icon('dot_green') + ' On' if cfg['enabled'] else icon('dot_red') + ' Off'}",
            f"**Mode:** {MODES[cfg['mode']][0]} - {MODES[cfg['mode']][1]}",
            f"**Timeout:** {human(cfg['timeout_seconds'])}",
            f"**Ignored roles:** {roles}",
            f"**Your words:** {len(cfg['words'])} • **Built-in:** {len(profanity.SUB_WORDS) + len(profanity.WORD_WORDS)}",
            "",
            f"`{config.PREFIX}automod add <word>` • `remove <word>` • `list`",
        ]
        return make_embed(f"{icon('lock')} Automod", "\n".join(lines))

    # ------------------------------------------------------------- the filter
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self.check(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content != after.content:
            await self.check(after)

    async def check(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id or not message.content:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            return
        cfg = self.get_cfg(guild.id)
        if not cfg["enabled"] or any(r.id in cfg["ignored"] for r in member.roles):
            return
        spans = profanity.find_spans(message.content, cfg["words"])
        if not spans or not message.channel.permissions_for(guild.me).manage_messages:
            return
        mode = cfg["mode"]
        try:
            await message.delete()
        except discord.HTTPException:
            return
        if mode in ("clean", "both"):
            await self.repost(message, profanity.clean_text(message.content, spans))
        if mode in ("timeout", "both"):
            seconds = cfg["timeout_seconds"]
            try:
                await member.timeout(timedelta(seconds=seconds), reason="Automod: blocked word")
                note = f"{member.mention} bad words are not allowed. You are timed out for {human(seconds)}."
            except discord.HTTPException:
                note = f"{member.mention} bad words are not allowed here."
            try:
                await message.channel.send(note, delete_after=8, allowed_mentions=discord.AllowedMentions(users=[member]))
            except discord.HTTPException:
                pass

    async def webhook_for(self, channel: discord.abc.GuildChannel) -> Optional[discord.Webhook]:
        hook = self._hooks.get(channel.id)
        if hook is not None:
            return hook
        me = channel.guild.me
        if not channel.permissions_for(me).manage_webhooks:
            return None
        try:
            for existing in await channel.webhooks():  # type: ignore[attr-defined]
                if existing.user and existing.user.id == me.id and existing.token:
                    self._hooks[channel.id] = existing
                    return existing
            hook = await channel.create_webhook(name=WEBHOOK_NAME)  # type: ignore[attr-defined]
        except discord.HTTPException:
            return None
        self._hooks[channel.id] = hook
        return hook

    async def repost(self, message: discord.Message, cleaned: str) -> None:
        """Send the cleaned text with the author's name + avatar (webhook), or as the bot if that is not possible."""
        channel = message.channel
        thread = channel if isinstance(channel, discord.Thread) else None
        parent = channel.parent if thread is not None else channel  # type: ignore[union-attr]
        files = []
        for att in message.attachments[:10]:
            if att.size <= 8 * 1024 * 1024:
                try:
                    files.append(await att.to_file())
                except discord.HTTPException:
                    pass
        hook = await self.webhook_for(parent) if parent is not None else None
        none = discord.AllowedMentions.none()
        try:
            if hook is not None:
                extra = {"thread": thread} if thread is not None else {}
                await hook.send(
                    content=cleaned[:2000], username=safe_name(message.author.display_name),
                    avatar_url=message.author.display_avatar.url, files=files, allowed_mentions=none, **extra,
                )
                return
        except discord.NotFound:
            self._hooks.pop(parent.id, None)  # webhook was deleted by hand
        except discord.HTTPException:
            pass
        try:
            await channel.send(f"**{message.author.display_name}:** {cleaned}"[:2000], files=files, allowed_mentions=none)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- commands
    @commands.hybrid_group(name="automod", description="Bad-word automod settings.", fallback="panel", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        await ctx.send(embed=self.panel_embed(ctx.guild), view=AutomodView(self, ctx.author.id, ctx.guild))

    @automod.command(name="add", description="Add your own blocked word.")
    @app_commands.describe(word="The word to block.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod_add(self, ctx: commands.Context, *, word: str) -> None:
        assert ctx.guild is not None
        word = re.sub(r"[^a-z0-9]", "", word.lower())
        if len(word) < 2:
            return await ctx.send(f"{icon('warn')} Give a word with at least 2 letters.", ephemeral=True)
        cfg = self.get_cfg(ctx.guild.id)
        if word in cfg["words"]:
            return await ctx.send(f"{icon('warn')} `{word}` is already blocked.", ephemeral=True)
        self.save_cfg(ctx.guild.id, custom_words="\n".join((*cfg["words"], word)))
        await ctx.send(f"{icon('ok')} `{word}` is blocked now (also as fu#k-style spellings).")

    @automod.command(name="remove", description="Remove one of your blocked words.")
    @app_commands.describe(word="The word to unblock.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod_remove(self, ctx: commands.Context, *, word: str) -> None:
        assert ctx.guild is not None
        word = re.sub(r"[^a-z0-9]", "", word.lower())
        cfg = self.get_cfg(ctx.guild.id)
        if word not in cfg["words"]:
            return await ctx.send(f"{icon('warn')} `{word}` is not in your list (built-in words cannot be removed).", ephemeral=True)
        self.save_cfg(ctx.guild.id, custom_words="\n".join(w for w in cfg["words"] if w != word))
        await ctx.send(f"{icon('ok')} `{word}` removed.")

    @automod.command(name="list", description="Show your own blocked words.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def automod_list(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        words = self.get_cfg(ctx.guild.id)["words"]
        text = ", ".join(f"||{w}||" for w in words) or "No own words yet."
        await ctx.send(embed=make_embed(f"{icon('lock')} Your blocked words", f"{text}\n\nBuilt-in list: {len(profanity.SUB_WORDS) + len(profanity.WORD_WORDS)} words"), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Automod(bot))
