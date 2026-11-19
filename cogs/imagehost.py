"""Image host (bot owner only): !imgchannel, !tolink, !linklist.

The owner picks ONE channel (in any server the bot is in). Every image uploaded through the bot's
features (ticket panel banner, giveaway banner / thumbnail, welcome banner) and every `!tolink` is
re-posted there, and only a reference + link is saved in the database / backups (no image data).
"""
from __future__ import annotations

import logging
from typing import Optional

import discord
from discord.ext import commands, tasks

import config
from utils import imagehost
from utils.icons import icon
from utils.images import ImageError
from utils.ui import build_pages, make_embed, send_pages

log = logging.getLogger("premium-bot.imagehost.cog")


class ImageHost(commands.Cog):
    """Owner-only image host."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        self.refresh_loop.start()

    async def cog_unload(self) -> None:
        self.refresh_loop.cancel()

    # Discord image links expire, so they are read again from the host messages every 2 hours
    # (also once right after start-up).
    @tasks.loop(hours=2)
    async def refresh_loop(self) -> None:
        try:
            updated, failed = await imagehost.refresh_links(self.bot)
            if updated or failed:
                log.info("Image links refreshed: %d updated, %d failed", updated, failed)
        except Exception:
            log.exception("Image link refresh failed")

    @refresh_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------- !imgchannel
    @commands.command(name="imgchannel", aliases=["imagehost"])
    @commands.is_owner()
    async def imgchannel(self, ctx: commands.Context, *, target: Optional[str] = None) -> None:
        """Owner only: set the channel that stores every uploaded image (global)."""
        current = imagehost.host_channel_id(self.db)
        if target is None:
            where = f"<#{current}>" if current else "not set"
            return await ctx.send(embed=make_embed(
                f"{icon('image')} Image host",
                f"Current host channel: {where}\n\n"
                f"`{config.PREFIX}imgchannel #channel` set it (any server the bot is in)\n"
                f"`{config.PREFIX}imgchannel off` turn it off\n"
                f"`{config.PREFIX}tolink` (attach an image or reply to one) get a link\n"
                f"`{config.PREFIX}linklist` all saved image links",
            ))
        if target.strip().lower() in ("off", "none", "remove", "reset"):
            imagehost.set_host_channel(self.db, None)
            return await ctx.send(f"{icon('ok')} Image host turned off. Uploads are disabled until you set a channel again.")
        try:
            channel = await commands.TextChannelConverter().convert(ctx, target.strip())
        except commands.BadArgument:
            return await ctx.send(f"{icon('warn')} I could not find that text channel. Mention it or use its ID.")
        me = channel.guild.me
        perms = channel.permissions_for(me)
        missing = [n for n, ok in (("View Channel", perms.view_channel), ("Send Messages", perms.send_messages),
                                   ("Attach Files", perms.attach_files), ("Read Message History", perms.read_message_history)) if not ok]
        if missing:
            return await ctx.send(f"{icon('warn')} In {channel.mention} I am missing: **{', '.join(missing)}**. Nothing changed.")
        imagehost.set_host_channel(self.db, channel.id)
        await ctx.send(
            f"{icon('ok')} Image host set to {channel.mention} (server **{channel.guild.name}**).\n"
            "Every image uploaded in any server is now saved there. Keep that channel private and never delete the messages."
        )

    # ------------------------------------------------------------- !tolink
    @commands.command(name="tolink")
    @commands.is_owner()
    async def tolink(self, ctx: commands.Context) -> None:
        """Owner only: upload an image with this command, or reply to an image, to get its link."""
        msg = ctx.message
        source_msg: Optional[discord.Message] = msg if msg.attachments else None
        if source_msg is None and msg.reference is not None:
            ref = msg.reference.resolved
            if not isinstance(ref, discord.Message) and msg.reference.message_id:
                try:
                    ref = await ctx.channel.fetch_message(msg.reference.message_id)
                except discord.HTTPException:
                    ref = None
            if isinstance(ref, discord.Message):
                source_msg = ref
        if source_msg is None:
            return await ctx.send(f"{icon('warn')} Attach an image to `{config.PREFIX}tolink`, or reply to a message that has an image.")

        attachment = next((a for a in source_msg.attachments if imagehost._is_image(a.filename, a.content_type)), None)
        link: Optional[str] = None
        if attachment is None:
            for e in source_msg.embeds:
                link = (e.image.url if e.image else None) or (e.thumbnail.url if e.thumbnail else None) or (e.url if e.type == "image" else None)
                if link:
                    break
        if attachment is None and not link:
            return await ctx.send(f"{icon('warn')} I found no image in that message.")

        guild_id = ctx.guild.id if ctx.guild else None
        try:
            if attachment is not None:
                marker = await imagehost.host_attachment(self.bot, attachment, guild_id=guild_id, user_id=ctx.author.id)
            else:
                marker = await imagehost.host_url(self.bot, link, guild_id=guild_id, user_id=ctx.author.id)  # type: ignore[arg-type]
        except ImageError as exc:
            return await ctx.send(f"{icon('warn')} {exc}")
        url = self.db.execute(
            "SELECT url FROM image_links WHERE channel_id=? AND message_id=?",
            tuple(int(x) for x in marker[len("host:"):].split(":")),
        ).fetchone()[0]
        embed = make_embed(f"{icon('link')} Image link", f"```{url}```", image=url,
                           footer=f"Saved in {config.PREFIX}linklist • Discord refreshes these links, so the list always has the current one")
        await ctx.send(embed=embed)

    # ------------------------------------------------------------- !linklist
    @commands.command(name="linklist", aliases=["links"])
    @commands.is_owner()
    async def linklist(self, ctx: commands.Context) -> None:
        """Owner only: every image saved in the image host, newest first."""
        rows = self.db.execute(
            "SELECT filename, url, guild_id, user_id, created_at FROM image_links ORDER BY created_at DESC"
        ).fetchall()
        total = len(rows)
        lines = []
        for i, (filename, url, guild_id, user_id, created) in enumerate(rows):
            try:
                when = discord.utils.format_dt(discord.utils.parse_time(created), "R")
            except Exception:
                when = created
            who = f"<@{user_id}>" if user_id else "?"
            lines.append(f"**#{total - i}** • {when} • {who} • server `{guild_id or 'DM'}`\n{url}")
        pages = build_pages(f"{icon('link')} Image links", lines, per_page=5, empty="No images saved yet.")
        await send_pages(ctx, pages)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageHost(bot))
