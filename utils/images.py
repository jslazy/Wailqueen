"""Banner / thumbnail images: ONLY A LINK / REFERENCE is saved (no image file is kept in the database).

New images go through the owner's image host (utils/imagehost.py) and are saved as `host:<channel>:<message>`;
the matching link lives in the `image_links` table and is refreshed automatically.

`store()` checks that the value is an http(s) link and returns it unchanged, so the settings (and
therefore every backup) simply hold the link. Old banners that were saved as a file (`db:<key>`)
keep working: `attach()` still reads them from the `images` table.
"""
from __future__ import annotations

import io
import logging
import sqlite3
from typing import Optional

import discord

log = logging.getLogger("premium-bot.images")

MARKER = "db:"        # old style: image bytes kept inside the database (still readable)
HOST_PREFIX = "host:"  # image on the owner's image host: "host:<channel_id>:<message_id>"
MAX_LINK = 1000


class ImageError(Exception):
    """Message is safe to show to the user."""


def clean_link(url: str) -> str:
    """Validate a link and return it. Raises ImageError with a readable reason."""
    url = (url or "").strip().strip("<>")
    if not url.lower().startswith(("http://", "https://")):
        raise ImageError("The link must start with http:// or https://")
    if len(url) > MAX_LINK or any(c.isspace() for c in url):
        raise ImageError("That link is not valid (too long or contains spaces).")
    return url


def host_marker(channel_id: int, message_id: int) -> str:
    return f"{HOST_PREFIX}{channel_id}:{message_id}"


def resolve(conn: sqlite3.Connection, value: Optional[str]) -> Optional[str]:
    """Plain link for a saved value: host marker -> its current link, a normal link -> itself.
    Empty or old `db:` values give None (use `attach()` for those)."""
    if not value:
        return None
    if value.startswith(HOST_PREFIX):
        try:
            channel_id, message_id = (int(x) for x in value[len(HOST_PREFIX):].split(":"))
        except ValueError:
            return None
        row = conn.execute(
            "SELECT url FROM image_links WHERE channel_id=? AND message_id=?", (channel_id, message_id)
        ).fetchone()
        return row[0] if row else None
    if value.startswith(MARKER):
        return None
    return value


async def store(conn: Optional[sqlite3.Connection], url: str) -> str:
    """Keep the link only. Returns the value to save in the settings (= the link itself).

    `conn` is unused now; it is still accepted so older call sites keep working."""
    return clean_link(url)


def attach(conn: sqlite3.Connection, value: Optional[str], name: str) -> tuple[Optional[str], Optional[discord.File]]:
    """(url for the embed, file to upload with the message) for a saved banner value.

    `value` is a plain link (normal now), an old `db:` marker, or empty."""
    if not value:
        return None, None
    if value.startswith(HOST_PREFIX):
        return resolve(conn, value), None
    if value.startswith(MARKER):
        row = conn.execute("SELECT filename, data FROM images WHERE key=?", (value[len(MARKER):],)).fetchone()
        if row is None:
            return None, None
        ext = row[0].rsplit(".", 1)[-1]
        filename = f"{name}.{ext}"
        return f"attachment://{filename}", discord.File(io.BytesIO(row[1]), filename=filename)
    return value, None  # a link
