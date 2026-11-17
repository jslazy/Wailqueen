from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import humanize_seconds, parse_duration
from utils.icons import icon
from utils.ui import build_pages, make_embed, send_pages

log = logging.getLogger("premium-bot.moderation")

REASON_LIMIT = 400
LIST_REASON_LIMIT = 150

# Reason marker for prefix commands:  !warn @member ?r spamming
_REASON_MARK = re.compile(r"^\?r(?:\s+|$)", re.IGNORECASE)

# action -> (title shown in the log channel, embed color)
ACTIONS: dict[str, tuple[str, int]] = {
    "warn": ("Warn", 0xF1C40F),
    "timeout": ("Timeout", 0xE67E22),
    "untimeout": ("Timeout removed", 0x2ECC71),
    "kick": ("Kick", 0xE74C3C),
    "ban": ("Ban", 0xC0392B),
    "unban": ("Unban", 0x2ECC71),
    "delwarn": ("Warning deleted", 0x95A5A6),
    "clearwarns": ("Warnings cleared", 0x95A5A6),
}
# action -> icon name shown before the title in the log channel
ACTION_ICONS = {
    "warn": "warn", "timeout": "hourglass", "untimeout": "ok", "kick": "kick", "ban": "error",
    "unban": "unlock", "delwarn": "trash", "clearwarns": "recycle",
}
STORED = {"warn", "timeout", "untimeout", "kick", "ban", "unban"}  # saved in mod_cases


def _clip(reason: Optional[str], limit: int = REASON_LIMIT) -> str:
    """Clean a reason. Accepts both 'spam' and '?r spam'."""
    reason = _REASON_MARK.sub("", (reason or "").strip(), count=1).strip()
    return (reason or "No reason provided")[:limit]


def _when(iso: str) -> str:
    return discord.utils.format_dt(datetime.fromisoformat(iso), "R")


class Moderation(commands.Cog):
    """Warn, timeout, kick and ban tools with logs and lists. Use ?r before the reason."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    # ------------------------------------------------------------- helpers
    def _check_target(self, ctx: commands.Context, target: discord.Member) -> Optional[str]:
        """Return an error message if the author/bot may not act on target."""
        guild = ctx.guild
        assert guild is not None
        if target.id == ctx.author.id:
            return "You can't use this on yourself."
        if target.id == self.bot.user.id:  # type: ignore[union-attr]
            return "I can't use this on myself."
        if target.id == guild.owner_id:
            return "You can't use this on the server owner."
        if ctx.author.id != guild.owner_id and target.top_role >= ctx.author.top_role:  # type: ignore[union-attr]
            return "That member's top role is equal to or higher than yours."
        if target.top_role >= guild.me.top_role:
            return "That member's top role is equal to or higher than mine, so I can't do that."
        return None

    async def _dm(self, user: Union[discord.Member, discord.User], text: str) -> None:
        try:
            await user.send(text)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _fail(self, ctx: commands.Context, message: str) -> None:
        await ctx.send(f"{icon('error')} {message}", ephemeral=True)

    # ---------------------------------------------------------------- logs
    def _log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        row = self.db.execute(
            "SELECT log_channel_id FROM guild_settings WHERE guild_id=?", (guild.id,)
        ).fetchone()
        if not row or not row[0]:
            return None
        channel = guild.get_channel(row[0])
        if not isinstance(channel, discord.TextChannel):
            return None
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return None
        return channel

    async def _record(
        self,
        guild: discord.Guild,
        action: str,
        user_id: int,
        moderator_id: int,
        reason: str,
        duration: Optional[int] = None,
        note: Optional[str] = None,
    ) -> Optional[int]:
        """Save the case (for list commands) and post it to the log channel."""
        case_id = None
        if action in STORED:
            cur = self.db.execute(
                "INSERT INTO mod_cases (guild_id, action, user_id, moderator_id, reason, duration, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (guild.id, action, user_id, moderator_id, reason, duration, datetime.now(timezone.utc).isoformat()),
            )
            self.db.commit()
            case_id = cur.lastrowid

        channel = self._log_channel(guild)
        if channel is None:
            return case_id

        title, color = ACTIONS[action]
        title = f"{icon(ACTION_ICONS[action])} {title}"
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name=f"{icon('users')} User", value=f"<@{user_id}>\n`{user_id}`", inline=True)
        embed.add_field(name=f"{icon('gavel')} Moderator", value=f"<@{moderator_id}>\n`{moderator_id}`", inline=True)
        if duration:
            embed.add_field(name=f"{icon('stopwatch')} Duration", value=humanize_seconds(duration), inline=True)
        embed.add_field(name=f"{icon('clipboard')} Reason", value=reason[:1024], inline=False)
        if note:
            embed.add_field(name=f"{icon('file')} Details", value=note[:1024], inline=False)
        if case_id:
            embed.set_footer(text=f"Case #{case_id}")
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Could not post to the log channel of guild %s", guild.id)
        return case_id

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """Log bans / kicks / timeouts done by hand (not through this bot)."""
        try:
            if entry.user_id is None or entry.user_id == self.bot.user.id:  # type: ignore[union-attr]
                return
            A = discord.AuditLogAction
            action: Optional[str] = None
            duration: Optional[int] = None
            if entry.action is A.ban:
                action = "ban"
            elif entry.action is A.unban:
                action = "unban"
            elif entry.action is A.kick:
                action = "kick"
            elif entry.action is A.member_update:
                after = getattr(entry.after, "timed_out_until", None)
                before = getattr(entry.before, "timed_out_until", None)
                now = discord.utils.utcnow()
                if after is not None and after > now:
                    action = "timeout"
                    duration = int((after - now).total_seconds())
                elif before is not None and after is None:
                    action = "untimeout"
            target_id = getattr(entry.target, "id", None)
            if action is None or target_id is None:
                return
            await self._record(
                entry.guild, action, target_id, entry.user_id, _clip(entry.reason), duration
            )
        except Exception:
            log.exception("Failed to handle an audit log entry")

    @commands.hybrid_command(name="logchannel", description="Set the channel for moderation logs.")
    @app_commands.describe(channel="Channel where warn/timeout/kick/ban logs are sent.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def logchannel(self, ctx: commands.Context, channel: discord.TextChannel) -> None:
        assert ctx.guild is not None
        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            return await self._fail(ctx, f"I need Send Messages and Embed Links in {channel.mention}.")
        self.db.execute(
            "INSERT INTO guild_settings (guild_id, log_channel_id) VALUES (?,?) "
            "ON CONFLICT(guild_id) DO UPDATE SET log_channel_id=excluded.log_channel_id",
            (ctx.guild.id, channel.id),
        )
        self.db.commit()
        await ctx.send(f"{icon('ok')} Moderation logs will be sent to {channel.mention}.")

    @commands.hybrid_command(name="logoff", description="Turn moderation logs off.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def logoff(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        self.db.execute("UPDATE guild_settings SET log_channel_id=NULL WHERE guild_id=?", (ctx.guild.id,))
        self.db.commit()
        await ctx.send(f"{icon('ok')} Moderation logs are now off.")

    # ----------------------------------------------------------------- warn
    @commands.hybrid_command(name="warn", description="Warn a member. Prefix: !warn @member ?r reason")
    @app_commands.describe(member="Member to warn.", reason="Why they are being warned.")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        assert ctx.guild is not None
        if member.bot:
            return await self._fail(ctx, "You can't warn bots.")
        error = self._check_target(ctx, member)
        if error:
            return await self._fail(ctx, error)

        reason = _clip(reason)
        cur = self.db.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?,?,?,?,?)",
            (ctx.guild.id, member.id, ctx.author.id, reason, datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()
        total = self.db.execute(
            "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id)
        ).fetchone()[0]

        await self._dm(member, f"{icon('warn')} You were warned in **{ctx.guild.name}**.\nReason: {reason}")
        await ctx.send(
            embed=make_embed(
                f"{icon('warn')} Member warned",
                f"{member.mention} has been warned.\n**Reason:** {reason}\n"
                f"**Warning ID:** `{cur.lastrowid}` • **Total warnings:** {total}",
                footer=f"By {ctx.author.name}",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._record(
            ctx.guild, "warn", member.id, ctx.author.id, reason,
            note=f"Warning ID `{cur.lastrowid}` • Total warnings: {total}",
        )

    @commands.hybrid_command(name="warnings", aliases=["warns"], description="Show a member's warnings.")
    @app_commands.describe(member="Member to look up.")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warnings(self, ctx: commands.Context, member: discord.Member) -> None:
        assert ctx.guild is not None
        rows = self.db.execute(
            "SELECT id, moderator_id, reason, created_at FROM warnings "
            "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
            (ctx.guild.id, member.id),
        ).fetchall()
        if not rows:
            return await ctx.send(embed=make_embed(f"{icon('warn')} Warnings", f"{member.mention} has no warnings."))

        lines = [f"`#{wid}` {_when(created)} by <@{mod_id}>\n{reason}" for wid, mod_id, reason, created in rows]
        await ctx.send(
            embed=make_embed(f"{icon('warn')} Warnings for {member.display_name}", "\n\n".join(lines), footer="Latest 10 shown"),
        )

    @commands.hybrid_command(
        name="warnlist", aliases=["wl"], description="Full warning list of the server (or of one member)."
    )
    @app_commands.describe(member="Only show this member's warnings.")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def warnlist(self, ctx: commands.Context, member: Optional[discord.User] = None) -> None:
        assert ctx.guild is not None
        if member is not None:
            rows = self.db.execute(
                "SELECT id, user_id, moderator_id, reason, created_at FROM warnings "
                "WHERE guild_id=? AND user_id=? ORDER BY id DESC",
                (ctx.guild.id, member.id),
            ).fetchall()
            title = f"{icon('warn')} Warn list • {member.name}"
        else:
            rows = self.db.execute(
                "SELECT id, user_id, moderator_id, reason, created_at FROM warnings "
                "WHERE guild_id=? ORDER BY id DESC",
                (ctx.guild.id,),
            ).fetchall()
            title = f"{icon('warn')} Warn list"
        lines = [
            f"`#{wid}` <@{uid}> • {_when(created)} • by <@{mod_id}>\n{reason[:LIST_REASON_LIMIT]}"
            for wid, uid, mod_id, reason, created in rows
        ]
        await send_pages(ctx, build_pages(title, lines, empty="No warnings."))

    @commands.hybrid_command(name="delwarn", description="Delete one warning by its ID.")
    @app_commands.describe(warning_id="The warning ID shown in /warnings.", reason="Reason.")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def delwarn(self, ctx: commands.Context, warning_id: int, *, reason: Optional[str] = None) -> None:
        assert ctx.guild is not None
        row = self.db.execute(
            "SELECT user_id FROM warnings WHERE id=? AND guild_id=?", (warning_id, ctx.guild.id)
        ).fetchone()
        if row is None:
            return await self._fail(ctx, f"No warning with ID `{warning_id}` in this server.")
        self.db.execute("DELETE FROM warnings WHERE id=? AND guild_id=?", (warning_id, ctx.guild.id))
        self.db.commit()
        await ctx.send(f"{icon('trash')} Warning `#{warning_id}` deleted.")
        await self._record(
            ctx.guild, "delwarn", row[0], ctx.author.id, _clip(reason), note=f"Warning ID `{warning_id}`"
        )

    @commands.hybrid_command(name="clearwarns", description="Remove all warnings of a member.")
    @app_commands.describe(member="Member whose warnings to clear.", reason="Reason.")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def clearwarns(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        assert ctx.guild is not None
        cur = self.db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
        self.db.commit()
        await ctx.send(f"{icon('recycle')} Cleared {cur.rowcount} warning(s) for {member.mention}.")
        await self._record(
            ctx.guild, "clearwarns", member.id, ctx.author.id, _clip(reason), note=f"{cur.rowcount} warning(s) removed"
        )

    # -------------------------------------------------------------- timeout
    @commands.hybrid_command(
        name="timeout", aliases=["mute"], description="Timeout a member. Prefix: !timeout @member 10m ?r reason"
    )
    @app_commands.describe(member="Member to timeout.", duration="Length: 30s, 10m, 2h, 1d (max 28d).", reason="Reason.")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(
        self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: Optional[str] = None
    ) -> None:
        assert ctx.guild is not None
        delta = parse_duration(duration)
        if delta is None:
            return await self._fail(ctx, "Invalid duration. Examples: `30s`, `10m`, `2h`, `1d`, `1h30m`.")
        if delta.days > config.MAX_TIMEOUT_DAYS or (delta.days == config.MAX_TIMEOUT_DAYS and delta.seconds):
            return await self._fail(ctx, f"The maximum timeout is {config.MAX_TIMEOUT_DAYS} days.")
        error = self._check_target(ctx, member)
        if error:
            return await self._fail(ctx, error)
        if member.guild_permissions.administrator:
            return await self._fail(ctx, "Administrators can't be timed out.")

        reason = _clip(reason)
        seconds = int(delta.total_seconds())
        await member.timeout(delta, reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        await self._dm(
            member,
            f"{icon('hourglass')} You were timed out in **{ctx.guild.name}** for {humanize_seconds(seconds)}.\nReason: {reason}",
        )
        await ctx.send(
            embed=make_embed(
                f"{icon('hourglass')} Member timed out",
                f"{member.mention} is timed out for **{humanize_seconds(seconds)}**.\n**Reason:** {reason}",
                footer=f"By {ctx.author.name}",
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self._record(ctx.guild, "timeout", member.id, ctx.author.id, reason, duration=seconds)

    @commands.hybrid_command(name="untimeout", aliases=["unmute"], description="Remove a member's timeout.")
    @app_commands.describe(member="Member to un-timeout.", reason="Reason.")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        assert ctx.guild is not None
        if not member.is_timed_out():
            return await self._fail(ctx, "That member is not timed out.")
        reason = _clip(reason)
        await member.timeout(None, reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        await ctx.send(f"{icon('ok')} Timeout removed for {member.mention}.", allowed_mentions=discord.AllowedMentions.none())
        await self._record(ctx.guild, "untimeout", member.id, ctx.author.id, reason)

    @commands.hybrid_command(
        name="timeoutlist", aliases=["tl"], description="Members who are timed out right now."
    )
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    async def timeoutlist(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        members = sorted(
            (m for m in ctx.guild.members if m.is_timed_out()),
            key=lambda m: m.timed_out_until,  # type: ignore[arg-type,return-value]
        )
        lines = []
        for m in members:
            row = self.db.execute(
                "SELECT reason, moderator_id FROM mod_cases "
                "WHERE guild_id=? AND user_id=? AND action='timeout' ORDER BY id DESC LIMIT 1",
                (ctx.guild.id, m.id),
            ).fetchone()
            ends = discord.utils.format_dt(m.timed_out_until, "R")  # type: ignore[arg-type]
            text = f"{m.mention} (`{m.id}`) • ends {ends}"
            if row:
                text += f"\nBy <@{row[1]}> • {row[0][:LIST_REASON_LIMIT]}"
            lines.append(text)
        await send_pages(ctx, build_pages(f"{icon('hourglass')} Timeout list", lines, empty="Nobody is timed out right now."))

    # ----------------------------------------------------------------- kick
    @commands.hybrid_command(name="kick", description="Kick a member. Prefix: !kick @member ?r reason")
    @app_commands.describe(member="Member to kick.", reason="Reason.")
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = None) -> None:
        assert ctx.guild is not None
        error = self._check_target(ctx, member)
        if error:
            return await self._fail(ctx, error)

        reason = _clip(reason)
        await self._dm(member, f"{icon('kick')} You were kicked from **{ctx.guild.name}**.\nReason: {reason}")
        await ctx.guild.kick(member, reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        await ctx.send(
            embed=make_embed(
                f"{icon('kick')} Member kicked",
                f"**{member}** (`{member.id}`) has been kicked.\n**Reason:** {reason}",
                footer=f"By {ctx.author.name}",
            )
        )
        await self._record(ctx.guild, "kick", member.id, ctx.author.id, reason)

    @commands.hybrid_command(name="kicklist", aliases=["kl"], description="Kick history of the server.")
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def kicklist(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        rows = self.db.execute(
            "SELECT id, user_id, moderator_id, reason, created_at FROM mod_cases "
            "WHERE guild_id=? AND action='kick' ORDER BY id DESC",
            (ctx.guild.id,),
        ).fetchall()
        lines = [
            f"`#{cid}` <@{uid}> (`{uid}`) • {_when(created)} • by <@{mod_id}>\n{reason[:LIST_REASON_LIMIT]}"
            for cid, uid, mod_id, reason, created in rows
        ]
        await send_pages(ctx, build_pages(f"{icon('kick')} Kick list", lines, empty="No kicks recorded yet."))

    # ------------------------------------------------------------------ ban
    @commands.hybrid_command(name="ban", description="Ban a member or a user ID. Prefix: !ban @user ?r reason")
    @app_commands.describe(user="Member or user ID to ban.", reason="Reason.")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, user: discord.User, *, reason: Optional[str] = None) -> None:
        assert ctx.guild is not None
        member = ctx.guild.get_member(user.id)
        if member is not None:
            error = self._check_target(ctx, member)
            if error:
                return await self._fail(ctx, error)
        elif user.id == ctx.author.id:
            return await self._fail(ctx, "You can't use this on yourself.")

        reason = _clip(reason)
        await self._dm(user, f"{icon('error')} You were banned from **{ctx.guild.name}**.\nReason: {reason}")
        await ctx.guild.ban(user, reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        await ctx.send(
            embed=make_embed(
                f"{icon('error')} Member banned",
                f"**{user}** (`{user.id}`) has been banned.\n**Reason:** {reason}",
                footer=f"By {ctx.author.name}",
            )
        )
        await self._record(ctx.guild, "ban", user.id, ctx.author.id, reason)

    @commands.hybrid_command(name="unban", description="Unban a user by ID.")
    @app_commands.describe(user_id="ID of the banned user.", reason="Reason.")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: str, *, reason: Optional[str] = None) -> None:
        assert ctx.guild is not None
        if not user_id.isdigit():
            return await self._fail(ctx, "Please give the numeric user ID.")
        reason = _clip(reason)
        try:
            await ctx.guild.unban(discord.Object(id=int(user_id)), reason=f"{ctx.author} ({ctx.author.id}): {reason}")
        except discord.NotFound:
            return await self._fail(ctx, "That user is not banned.")
        await ctx.send(f"{icon('unlock')} Unbanned `{user_id}`.")
        await self._record(ctx.guild, "unban", int(user_id), ctx.author.id, reason)

    @commands.hybrid_command(name="banlist", aliases=["bl"], description="Everyone who is banned right now.")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def banlist(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        if ctx.interaction is not None:
            await ctx.defer()
        entries = [entry async for entry in ctx.guild.bans(limit=None)]
        lines = []
        for entry in entries:
            reason = (entry.reason or "No reason provided")[:LIST_REASON_LIMIT]
            lines.append(f"**{entry.user}** (`{entry.user.id}`)\n{reason}")
        await send_pages(ctx, build_pages(f"{icon('error')} Ban list", lines, empty="Nobody is banned."))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
