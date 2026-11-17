"""Permanent image storage for banners.

Discord CDN links expire (and random links die), which made welcome / giveaway banners
"glitch" after a while. Here the image is downloaded ONCE when the banner is saved and kept
inside the bot database (so it is also part of the backups). When a message is sent, the
stored bytes are attached again, so the banner can never break.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Optional

import aiohttp
import discord

log = logging.getLogger("premium-bot.images")

MAX_BYTES = 3 * 1024 * 1024  # stored in the database, keep it light
MARKER = "db:"


class ImageError(Exception):
    """Message is safe to show to the user."""


def _sniff(data: bytes) -> Optional[str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def download(url: str) -> tuple[bytes, str]:
    """Download an image link. Raises ImageError with a readable reason."""
    if not url.lower().startswith(("http://", "https://")):
        raise ImageError("The link must start with http:// or https://")
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0 (WailQueen bot)"}) as resp:
                if resp.status != 200:
                    raise ImageError(f"The link could not be opened (HTTP {resp.status}). It may have expired.")
                data = await resp.content.read(MAX_BYTES + 1)
    except ImageError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError):
        raise ImageError("The link could not be downloaded. Use a direct image link or upload the image.")
    if len(data) > MAX_BYTES:
        raise ImageError(f"Image is too big (max {MAX_BYTES // (1024 * 1024)} MB).")
    ext = _sniff(data)
    if ext is None:
        raise ImageError("That link is not a direct image (png / jpg / gif / webp). Page links do not work.")
    return data, ext


def save_bytes(conn: sqlite3.Connection, data: bytes, ext: str) -> str:
    key = hashlib.sha1(data).hexdigest()[:20]
    conn.execute(
        "INSERT OR IGNORE INTO images (key, filename, data) VALUES (?,?,?)", (key, f"image.{ext}", data)
    )
    conn.commit()
    return MARKER + key


async def store(conn: sqlite3.Connection, url: str) -> str:
    """Download `url` and keep it forever. Returns the marker to save in the settings."""
    data, ext = await download(url)
    return save_bytes(conn, data, ext)


def attach(conn: sqlite3.Connection, value: Optional[str], name: str) -> tuple[Optional[str], Optional[discord.File]]:
    """(url for the embed, file to upload with the message) for a saved banner value.

    `value` is either a marker made by store(), an old plain link, or empty."""
    if not value:
        return None, None
    if value.startswith(MARKER):
        row = conn.execute("SELECT filename, data FROM images WHERE key=?", (value[len(MARKER):],)).fetchone()
        if row is None:
            return None, None
        ext = row[0].rsplit(".", 1)[-1]
        filename = f"{name}.{ext}"
        import io

        return f"attachment://{filename}", discord.File(io.BytesIO(row[1]), filename=filename)
    return value, None  # old plain link
