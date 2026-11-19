"""Owner image host.

Every image uploaded through a bot feature (ticket panel banner, giveaway banner / thumbnail, welcome
banner, !tolink) is re-posted by the bot into ONE channel chosen by the bot owner (`!imgchannel`).
Settings and backups keep only a reference (`host:<channel>:<message>`) and the link in `image_links`;
no image bytes are stored in the database.

Discord image links expire after a while, so the link is read again from the host message every
couple of hours (see cogs/imagehost.py). As long as the host message exists, the image never breaks.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import aiohttp
import discord

import config
from utils import images
from utils.icons import icon
from utils.images import ImageError

log = logging.getLogger("premium-bot.imagehost")

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_BYTES = 25 * 1024 * 1024


# ------------------------------------------------------------------ host channel setting
def host_channel_id(conn) -> Optional[int]:
    row = conn.execute("SELECT channel_id FROM image_host WHERE id=1").fetchone()
    return int(row[0]) if row else None


def set_host_channel(conn, channel_id: Optional[int]) -> None:
    if channel_id is None:
        conn.execute("DELETE FROM image_host WHERE id=1")
    else:
        conn.execute("INSERT OR REPLACE INTO image_host (id, channel_id) VALUES (1, ?)", (channel_id,))
    conn.commit()


async def _host_channel(bot) -> discord.abc.Messageable:
    cid = host_channel_id(bot.db.conn)
    if not cid:
        raise ImageError(f"The image host is not set up yet. The bot owner must run `{config.PREFIX}imgchannel #channel` first.")
    channel = bot.get_channel(cid)
    if channel is None:
        try:
            channel = await bot.fetch_channel(cid)
        except discord.HTTPException:
            raise ImageError("I can't reach the image host channel. Tell the bot owner.") from None
    return channel  # type: ignore[return-value]


# ------------------------------------------------------------------ hosting
def _is_image(filename: str, content_type: Optional[str]) -> bool:
    if content_type and content_type.lower().startswith("image/"):
        return True
    return (filename or "").lower().endswith(IMAGE_EXT)


def _safe_name(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", (name or "image").rsplit("/", 1)[-1])[:80] or "image"
    return name if name.lower().endswith(IMAGE_EXT) else name + ".png"


async def host_bytes(bot, data: bytes, filename: str, *, guild_id: Optional[int], user_id: int) -> str:
    """Post the image in the host channel and return the value to save in the settings."""
    channel = await _host_channel(bot)
    guild = getattr(channel, "guild", None)
    limit = min(MAX_BYTES, guild.filesize_limit) if guild is not None else MAX_BYTES
    if len(data) > limit:
        raise ImageError(f"That image is too big (max {limit // 1024 // 1024} MB).")
    try:
        msg = await channel.send(
            content=f"Uploaded by `{user_id}` • server `{guild_id or 'DM'}`",
            file=discord.File(io.BytesIO(data), filename=_safe_name(filename)),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        raise ImageError("I can't send files in the image host channel. Tell the bot owner.") from None
    except discord.HTTPException as exc:
        raise ImageError(f"Discord refused the image: {exc.text}") from None
    if not msg.attachments:
        raise ImageError("Discord did not return a link for the image.")
    conn = bot.db.conn
    conn.execute(
        "INSERT OR REPLACE INTO image_links (channel_id, message_id, filename, url, guild_id, user_id, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (msg.channel.id, msg.id, msg.attachments[0].filename, msg.attachments[0].url, guild_id, user_id,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return images.host_marker(msg.channel.id, msg.id)


async def host_attachment(bot, attachment: discord.Attachment, *, guild_id: Optional[int], user_id: int) -> str:
    if not _is_image(attachment.filename, attachment.content_type):
        raise ImageError("That file is not an image (png, jpg, gif or webp).")
    if attachment.size > MAX_BYTES:
        raise ImageError("That image is too big (max 25 MB).")
    try:
        data = await attachment.read()
    except discord.HTTPException:
        raise ImageError("I could not download that image. Try uploading it again.") from None
    return await host_bytes(bot, data, attachment.filename, guild_id=guild_id, user_id=user_id)


async def host_url(bot, url: str, *, guild_id: Optional[int], user_id: int) -> str:
    """Download an image from a link (used by !tolink for image embeds) and host it."""
    url = images.clean_link(url)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise ImageError(f"Could not download that image (HTTP {resp.status}).")
                ctype = resp.headers.get("Content-Type", "")
                name = urlparse(url).path.rsplit("/", 1)[-1] or "image"
                if not _is_image(name, ctype):
                    raise ImageError("That link is not an image.")
                data = await resp.content.read(MAX_BYTES + 1)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        raise ImageError("Could not download that image.") from None
    if len(data) > MAX_BYTES:
        raise ImageError("That image is too big (max 25 MB).")
    return await host_bytes(bot, data, name, guild_id=guild_id, user_id=user_id)


# ------------------------------------------------------------------ keep links fresh
async def refresh_links(bot, rows: Optional[list[tuple[int, int]]] = None) -> tuple[int, int]:
    """Read the host messages again and store their current links. Returns (updated, failed)."""
    conn = bot.db.conn
    if rows is None:
        rows = [(r[0], r[1]) for r in conn.execute("SELECT channel_id, message_id FROM image_links")]
    updated = failed = 0
    for channel_id, message_id in rows:
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                failed += 1
                continue
        try:
            msg = await channel.fetch_message(message_id)
        except discord.HTTPException:
            failed += 1
            continue
        if msg.attachments:
            conn.execute(
                "UPDATE image_links SET url=? WHERE channel_id=? AND message_id=?",
                (msg.attachments[0].url, channel_id, message_id),
            )
            updated += 1
        await asyncio.sleep(0.3)
    conn.commit()
    return updated, failed


# ------------------------------------------------------------------ button flow (upload only)
async def ask_image(
    interaction: discord.Interaction, bot, *, author_id: int, what: str, timeout: float = 60
) -> Optional[str]:
    """Used by the 'Upload' buttons. Must be called before the interaction has been answered.

    Returns the value to save, "" if the person typed `remove`, or None when nothing changed
    (an explanation was already sent)."""
    if host_channel_id(bot.db.conn) is None:
        await interaction.response.send_message(
            f"{icon('warn')} Image upload is not available yet: the bot owner has to set the image host "
            f"with `{config.PREFIX}imgchannel #channel`.", ephemeral=True,
        )
        return None
    await interaction.response.send_message(
        f"{icon('image')} Upload the **{what}** image in this channel within {int(timeout)} seconds "
        f"(send the image file, links are not accepted).\nType `remove` to delete the current one.",
        ephemeral=True,
    )

    def check(m: discord.Message) -> bool:
        return m.author.id == author_id and m.channel.id == interaction.channel_id

    try:
        msg = await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        await interaction.followup.send(f"{icon('hourglass')} Timed out, nothing changed.", ephemeral=True)
        return None

    try:
        if msg.content.strip().lower() == "remove" and not msg.attachments:
            return ""
        if not msg.attachments:
            await interaction.followup.send(
                f"{icon('warn')} I need an uploaded image file (links are not accepted). Nothing changed.", ephemeral=True
            )
            return None
        try:
            return await host_attachment(bot, msg.attachments[0], guild_id=interaction.guild_id, user_id=author_id)
        except ImageError as exc:
            await interaction.followup.send(f"{icon('warn')} {exc}", ephemeral=True)
            return None
    finally:
        try:
            await msg.delete()  # keep the channel clean (needs Manage Messages, optional)
        except discord.HTTPException:
            pass
