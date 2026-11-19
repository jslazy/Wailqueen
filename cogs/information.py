from __future__ import annotations

import platform
from datetime import datetime
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import helpers
from utils.helpers import relative, stamp
from utils.icons import icon
from utils.ui import make_embed


class Information(commands.Cog):
    """Profile, server and bot information."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _uptime(self) -> int:
        return int((discord.utils.utcnow() - self.bot.started_at).total_seconds())  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ ping
    @commands.hybrid_command(name="ping", description="Show the bot latency and uptime.")
    async def ping(self, ctx: commands.Context) -> None:
        embed = make_embed(
            "Pong",
            f"{icon('wifi')} **Gateway latency:** `{round(self.bot.latency * 1000)} ms`\n"
            f"{icon('stopwatch')} **Uptime:** `{helpers.humanize_seconds(self._uptime())}`",
        )
        await ctx.send(embed=embed)

    # --------------------------------------------------------------- botinfo
    @commands.hybrid_command(
        name="botinfo",
        aliases=["bi", "about", "stats"],
        description="Show information and statistics about the bot.",
    )
    async def botinfo(self, ctx: commands.Context) -> None:
        bot_user = self.bot.user
        assert bot_user is not None

        lines = [
            f"**{icon('rocket')} Bot information**",
            f"ID: `{bot_user.id}`",
            f"Username: **{bot_user.name}**",
        ]
        if bot_user.discriminator != "0":
            lines.append(f"Discriminator: **{bot_user.discriminator}**")
        lines.append(f"{icon('calendar')} Created: **{relative(bot_user.created_at)}**")
        lines.append(f"Mention: {bot_user.mention}")

        if ctx.guild and ctx.guild.me:
            me = ctx.guild.me
            lines += [
                "",
                f"**{icon('users')} Member information**",
                f"{icon('user_add')} Added: **{relative(me.joined_at)}**",
                f"{icon('mask')} Roles: {helpers.role_list(helpers.member_roles(me))}",
            ]

        users = sum(guild.member_count or 0 for guild in self.bot.guilds)
        command_count = len({c.qualified_name for c in self.bot.commands if not c.hidden})
        lines += [
            "",
            f"**{icon('chart')} Statistics**",
            f"{icon('globe')} Servers: **{len(self.bot.guilds):,}**",
            f"{icon('users')} Users: **{users:,}**",
            f"{icon('terminal')} Commands: **{command_count}**",
            f"{icon('wifi')} Latency: **{round(self.bot.latency * 1000)} ms**",
            f"{icon('stopwatch')} Uptime: **{helpers.humanize_seconds(self._uptime())}**",
            f"{icon('code')} Python: **{platform.python_version()}** • discord.py: **{discord.__version__}**",
        ]
        if config.SUPPORT_URL:
            lines += ["", f"{icon('heart')} [Support server]({config.SUPPORT_URL})"]

        embed = make_embed(description="\n".join(lines))
        embed.set_author(name=f"Bot: {bot_user}", icon_url=bot_user.display_avatar.url)
        await ctx.send(embed=embed)

    # -------------------------------------------------------------- userinfo
    @commands.hybrid_command(
        name="userinfo",
        aliases=["user", "ui", "whois", "profile"],
        description="Show information about a member.",
    )
    @app_commands.describe(member="The member to look up. Defaults to you.")
    @commands.guild_only()
    async def userinfo(self, ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
        assert ctx.guild is not None
        member = member or ctx.author  # type: ignore[assignment]
        assert isinstance(member, discord.Member)

        roles = helpers.member_roles(member)
        badges = helpers.badge_text(member)

        lines = [
            "**User information**",
            f"ID: `{member.id}`",
            f"Username: **{member.name}**",
            f"Display Name: **{member.display_name}**",
            f"Created: {relative(member.created_at)}",
            f"Mention: {member.mention}",
        ]
        if badges != "None":
            lines.append("Badges: " + " ".join(badges.split("\n")))
        lines += [
            "",
            "**Member information**",
            f"Joined: {relative(member.joined_at)}",
        ]
        if member.premium_since:
            lines.append(f"Boosting since: {relative(member.premium_since)}")
        if member.timed_out_until and member.timed_out_until > discord.utils.utcnow():
            lines.append(f"Timed out until: {relative(member.timed_out_until)}")
        lines += [
            f"Permissions: {helpers.permission_summary(member)}",
            f"Roles: {helpers.role_list(roles, limit=700)}",
            "",
            "**Voice information**",
            "Current voice channel: "
            + (f"**{member.voice.channel.name}**" if member.voice and member.voice.channel else "**Not in voice**"),
        ]

        embed = make_embed(description="\n".join(lines), color=member.color.value or config.ACCENT)
        embed.set_author(name=f"User: {member.name}", icon_url=member.display_avatar.url)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------ serverinfo
    @commands.hybrid_command(
        name="serverinfo",
        aliases=["si", "guildinfo"],
        description="Show information about this server.",
    )
    @commands.guild_only()
    async def serverinfo(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        assert guild is not None

        embed = make_embed(
            guild.name,
            thumbnail=guild.icon.url if guild.icon else None,
            image=guild.banner.url if guild.banner else None,
            footer=f"Server ID: {guild.id}",
        )
        embed.add_field(
            name=f"{icon('gear')} General",
            value=f"{icon('crown')} Owner: <@{guild.owner_id}>\n{icon('calendar')} Created: {stamp(guild.created_at)}",
            inline=False,
        )
        embed.add_field(
            name=f"{icon('chart')} Counts",
            value=(
                f"{icon('users')} Members: **{guild.member_count or 0:,}**\n"
                f"{icon('mask')} Roles: **{max(len(guild.roles) - 1, 0)}**\n"
                f"{icon('chat')} Channels: **{len(guild.text_channels)}** text • {icon('mic')} **{len(guild.voice_channels)}** voice\n"
                f"{icon('sparkles')} Emojis: **{len(guild.emojis)}**"
            ),
            inline=True,
        )
        embed.add_field(
            name=f"{icon('diamond')} Boosts",
            value=(
                f"{icon('star')} Level: **{guild.premium_tier}**\n"
                f"{icon('rocket')} Boosts: **{guild.premium_subscription_count}**\n"
                f"{icon('shield')} Verification: **{guild.verification_level.name.replace('_', ' ').title()}**"
            ),
            inline=True,
        )
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------- avatar
    @commands.hybrid_command(
        name="avatar",
        aliases=["av", "pfp"],
        description="Show a user's avatar in full size.",
    )
    @app_commands.describe(user="The user whose avatar to show. Defaults to you.")
    async def avatar(self, ctx: commands.Context, user: Optional[discord.User] = None) -> None:
        user = user or ctx.author  # type: ignore[assignment]
        assert user is not None
        asset = user.display_avatar

        embed = make_embed(
            f"{icon('image')} Avatar of {user.name}:",
            helpers.format_links(asset),
            image=helpers.asset_url(asset),
        )
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------- banner
    @commands.hybrid_command(
        name="banner",
        aliases=["bn"],
        description="Show a user's profile banner.",
    )
    @app_commands.describe(user="The user whose banner to show. Defaults to you.")
    async def banner(self, ctx: commands.Context, user: Optional[discord.User] = None) -> None:
        target = user or ctx.author
        fetched = await self.bot.fetch_user(target.id)

        if fetched.banner:
            embed = make_embed(
                f"{icon('banner')} Banner of {fetched.name}:",
                helpers.format_links(fetched.banner),
                image=helpers.asset_url(fetched.banner),
            )
        else:
            embed = make_embed(f"{icon('banner')} Banner of {fetched.name}:", "This user does not have a profile banner.")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Information(bot))
