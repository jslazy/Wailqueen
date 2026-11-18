"""Banner / thumbnail images: ONLY THE LINK is saved (no file is downloaded or stored).

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


async def store(conn: Optional[sqlite3.Connection], url: str) -> str:
    """Keep the link only. Returns the value to save in the settings (= the link itself).

    `conn` is unused now; it is still accepted so older call sites keep working."""
    return clean_link(url)


def attach(conn: sqlite3.Connection, value: Optional[str], name: str) -> tuple[Optional[str], Optional[discord.File]]:
    """(url for the embed, file to upload with the message) for a saved banner value.

    `value` is a plain link (normal now), an old `db:` marker, or empty."""
    if not value:
        return None, None
    if value.startswith(MARKER):
        row = conn.execute("SELECT filename, data FROM images WHERE key=?", (value[len(MARKER):],)).fetchone()
        if row is None:
            return None, None
        ext = row[0].rsplit(".", 1)[-1]
        filename = f"{name}.{ext}"
        return f"attachment://{filename}", discord.File(io.BytesIO(row[1]), filename=filename)
    return value, None  # a link
