from __future__ import annotations

import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import images
from utils.icons import icon

log = logging.getLogger("premium-bot.welcome")

KEYS = ["channel_id", "content", "title", "description", "color", "enabled", "banner"]


def _channel_ref(channel_id: int, fallback: str) -> str:
    return f"<#{channel_id}>" if channel_id else f"#{fallback}"


def default_description() -> str:
    return config.WELCOME_DESCRIPTION.replace(
        "{protocol}", _channel_ref(config.PROTOCOL_CHANNEL_ID, "protocol")
    ).replace("{info_board}", _channel_ref(config.INFO_BOARD_CHANNEL_ID, "info-board"))


def fmt(text: str, member: discord.Member) -> str:
    return (
        text.replace("{user}", member.mention)
        .replace("{username}", member.display_name)
        .replace("{server}", member.guild.name)
        .replace("{count}", str(member.guild.member_count))
    )


def parse_color(value: str) -> discord.Color:
    value = (value or "random").strip()
    if value.lower() == "random":
        return discord.Color(random.choice(config.RANDOM_COLORS))
    try:
        return discord.Color(int(value.lstrip("#"), 16))
    except ValueError:
        return discord.Color(random.choice(config.RANDOM_COLORS))


class WelcomeEditModal(discord.ui.Modal, title="Edit Welcome Message"):
    def __init__(self, cog: "Welcome", cfg: dict) -> None:
        super().__init__()
        self.cog = cog
        self.content_in = discord.ui.TextInput(
            label="Message text (above the embed)",
            default=cfg["content"],
            max_length=500,
            required=False,
        )
        self.title_in = discord.ui.TextInput(
            label="Embed title", default=cfg["title"], max_length=256
        )
        self.desc_in = discord.ui.TextInput(
            label="Embed description",
            default=cfg["description"],
            style=discord.TextStyle.paragraph,
            max_length=4000,
        )
        self.color_in = discord.ui.TextInput(
            label="Color (random or #HEX)",
            default=cfg["color"],
            max_length=10,
            required=False,
        )
        self.banner_in = discord.ui.TextInput(
            label="Banner image link (shown below embed)",
            placeholder="https://... | empty = keep current | remove = delete banner",
            max_length=1000,
            required=False,
        )
        for item in (
            self.content_in,
            self.title_in,
            self.desc_in,
            self.color_in,
            self.banner_in,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None and isinstance(interaction.user, discord.Member)
        await interaction.response.defer(ephemeral=True)  # downloading the banner can take a few seconds
        changes = dict(
            content=self.content_in.value or "",
            title=self.title_in.value,
            description=self.desc_in.value,
            color=self.color_in.value or "random",
        )
        note = ""
        raw = (self.banner_in.value or "").strip()
        if raw.lower() in ("remove", "none", "delete"):
            changes["banner"] = ""
        elif raw:
            try:
                changes["banner"] = await images.store(self.cog.db, raw)  # saved permanently
            except images.ImageError as exc:
                note = f"\n{icon('warn')} Banner not changed: {exc}"
        self.cog.save_cfg(interaction.guild_id, **changes)
        content, embed, files = self.cog.build_message(interaction.user)
        await interaction.followup.send(f"{icon('ok')} Welcome message updated.{note} Preview:", ephemeral=True)
        await interaction.followup.send(content=content, embed=embed, files=files, ephemeral=True)


class Welcome(commands.Cog):
    """Welcome messages. Setup commands are for admins only."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    # ------------------------------------------------------------- storage
    def get_cfg(self, guild_id: int) -> dict:
        row = self.db.execute(
            "SELECT channel_id, content, title, description, color, enabled, banner "
            "FROM welcome WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        if row:
            return dict(zip(KEYS, row))
        return {
            "channel_id": None,
            "content": config.WELCOME_CONTENT,
            "title": config.WELCOME_TITLE,
            "description": default_description(),
            "color": config.WELCOME_COLOR,
            "enabled": 1,
            "banner": "",
        }

    def save_cfg(self, guild_id: int, **changes) -> None:
        cfg = self.get_cfg(guild_id)
        cfg.update(changes)
        self.db.execute(
            "INSERT OR REPLACE INTO welcome "
            "(guild_id, channel_id, content, title, description, color, enabled, banner) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                guild_id,
                cfg["channel_id"],
                cfg["content"],
                cfg["title"],
                cfg["description"],
                cfg["color"],
                cfg["enabled"],
                cfg.get("banner") or "",
            ),
        )
        self.db.commit()

    # ------------------------------------------------------------- building
    def build_message(self, member: discord.Member, *, with_banner: bool = True) -> tuple[Optional[str], discord.Embed, list[discord.File]]:
        cfg = self.get_cfg(member.guild.id)
        embed = discord.Embed(
            title=fmt(cfg["title"], member),
            description=fmt(cfg["description"], member),
            color=parse_color(cfg["color"]),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        files: list[discord.File] = []
        if with_banner and cfg.get("banner"):
            url, file = images.attach(self.db, cfg["banner"], "welcome_banner")
            if url:
                embed.set_image(url=url)  # large image at the bottom of the embed
            if file:
                files.append(file)
        return (fmt(cfg["content"], member) or None), embed, files

    def resolve_channel(self, guild: discord.Guild, cfg: dict) -> Optional[discord.TextChannel]:
        me = guild.me
        if cfg["channel_id"]:
            channel = guild.get_channel(cfg["channel_id"])
            if isinstance(channel, discord.TextChannel):
                return channel
        for channel in guild.text_channels:
            if "welcome" in channel.name.lower() and channel.permissions_for(me).send_messages:
                return channel
        system = guild.system_channel
        if system and system.permissions_for(me).send_messages:
            return system
        return None

    # --------------------------------------------------------------- events
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        cfg = self.get_cfg(member.guild.id)
        if not cfg["enabled"]:
            return
        channel = self.resolve_channel(member.guild, cfg)
        if channel is None:
            return
        mentions = discord.AllowedMentions(users=[member])
        try:
            content, embed, files = self.build_message(member)
            await channel.send(content=content, embed=embed, files=files, allowed_mentions=mentions)
        except discord.HTTPException:
            # Never lose the welcome because of the banner: retry once without it.
            log.exception("Welcome with banner failed in guild %s, retrying without banner", member.guild.id)
            try:
                content, embed, _ = self.build_message(member, with_banner=False)
                await channel.send(content=content, embed=embed, allowed_mentions=mentions)
            except discord.HTTPException:
                log.exception("Welcome message could not be sent in guild %s", member.guild.id)

    # ------------------------------------------------------- slash commands
    @app_commands.command(name="welcome-setup", description="Choose the welcome channel.")
    @app_commands.describe(channel="Channel where welcome messages are sent.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_setup(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        self.save_cfg(interaction.guild_id, channel_id=channel.id)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"{icon('ok')} Welcome channel set to {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="welcome-edit", description="Edit the welcome message.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_edit(self, interaction: discord.Interaction) -> None:
        cfg = self.get_cfg(interaction.guild_id)  # type: ignore[arg-type]
        await interaction.response.send_modal(WelcomeEditModal(self, cfg))

    @app_commands.command(name="welcome-test", description="Preview the welcome message.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_test(self, interaction: discord.Interaction) -> None:
        assert isinstance(interaction.user, discord.Member)
        content, embed, files = self.build_message(interaction.user)
        await interaction.response.send_message(content=content, embed=embed, files=files, ephemeral=True)

    @app_commands.command(name="welcome-toggle", description="Turn welcome messages on or off.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_toggle(self, interaction: discord.Interaction) -> None:
        new = 0 if self.get_cfg(interaction.guild_id)["enabled"] else 1  # type: ignore[arg-type]
        self.save_cfg(interaction.guild_id, enabled=new)  # type: ignore[arg-type]
        await interaction.response.send_message(
            f"{icon('bell')} Welcome messages are now **{'ON' if new else 'OFF'}**.", ephemeral=True
        )

    @app_commands.command(name="welcome-reset", description="Restore the default welcome message.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def welcome_reset(self, interaction: discord.Interaction) -> None:
        self.save_cfg(
            interaction.guild_id,  # type: ignore[arg-type]
            content=config.WELCOME_CONTENT,
            title=config.WELCOME_TITLE,
            description=default_description(),
            color=config.WELCOME_COLOR,
            banner="",
        )
        await interaction.response.send_message(f"{icon('recycle')} Welcome message reset to the default.", ephemeral=True)

    # ------------------------------------------------------------ banner
    @commands.hybrid_command(
        name="welcomebanner",
        aliases=["wbanner"],
        description="Set the welcome banner: upload an image or give a link. 'remove' deletes it.",
    )
    @app_commands.describe(link="Direct image link, or 'remove'.", image="Or upload the banner image here.")
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def welcomebanner(
        self, ctx: commands.Context, link: Optional[str] = None, image: Optional[discord.Attachment] = None
    ) -> None:
        assert ctx.guild is not None
        if image is None and ctx.message is not None and ctx.message.attachments:
            image = ctx.message.attachments[0]
        if link and link.lower() in ("remove", "none", "delete"):
            self.save_cfg(ctx.guild.id, banner="")
            return await ctx.send(f"{icon('ok')} Welcome banner removed.")
        source = image.url if image is not None else link
        if not source:
            return await ctx.send(
                f"{icon('warn')} Upload an image with the command, or give a direct image link.\n"
                f"Example: `{config.PREFIX}welcomebanner https://.../banner.png`",
                ephemeral=True,
            )
        async with ctx.typing():
            try:
                marker = await images.store(self.db, source)
            except images.ImageError as exc:
                return await ctx.send(f"{icon('warn')} {exc}", ephemeral=True)
        self.save_cfg(ctx.guild.id, banner=marker)
        if isinstance(ctx.author, discord.Member):
            content, embed, files = self.build_message(ctx.author)
            await ctx.send(content=content, embed=embed, files=files)
        await ctx.send(f"{icon('ok')} Welcome banner saved permanently (preview above).")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
