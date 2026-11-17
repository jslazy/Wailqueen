"""Storage + snapshot helpers for the anti-nuke system (own SQLite file, never touches bot.db)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Iterable, Optional

import discord

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS an_config (
    guild_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    webhook_url TEXT NOT NULL DEFAULT '',
    punishment TEXT NOT NULL DEFAULT 'ban',
    auto_restore INTEGER NOT NULL DEFAULT 1,
    trusted TEXT NOT NULL DEFAULT '',
    webhook_msg_id INTEGER NOT NULL DEFAULT 0,
    last_archive_ts REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS an_objects (
    guild_id INTEGER NOT NULL, kind TEXT NOT NULL, obj_id INTEGER NOT NULL,
    name TEXT, data TEXT NOT NULL, hash TEXT NOT NULL,
    deleted_at REAL, deleted_by INTEGER, restored_as INTEGER,
    PRIMARY KEY (guild_id, kind, obj_id)
);
CREATE TABLE IF NOT EXISTS an_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, ts REAL NOT NULL,
    hash TEXT NOT NULL, data BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_an_snap ON an_snapshots (guild_id, id);
CREATE TABLE IF NOT EXISTS an_messages (
    message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
    author_id INTEGER, author_name TEXT, author_avatar TEXT, is_bot INTEGER DEFAULT 0, webhook_id INTEGER,
    content TEXT, embeds TEXT DEFAULT '[]', attachments TEXT DEFAULT '[]', links TEXT DEFAULT '[]',
    stickers TEXT DEFAULT '[]', msg_type INTEGER DEFAULT 0, reference_id INTEGER,
    created_at REAL, edited_at REAL, deleted_at REAL, restored INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_an_msg_chan ON an_messages (channel_id, message_id);
CREATE INDEX IF NOT EXISTS idx_an_msg_del ON an_messages (guild_id, deleted_at);
CREATE TABLE IF NOT EXISTS an_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, ts REAL NOT NULL,
    type TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_an_events ON an_events (guild_id, id);
CREATE TABLE IF NOT EXISTS an_cooldowns (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (guild_id, user_id, kind)
);
"""

URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
VOICE_TYPES = {discord.ChannelType.voice.value, discord.ChannelType.stage_voice.value}


class Store:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ------------------------------------------------------------------ config
    def cfg(self, guild_id: int) -> dict[str, Any]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM an_config WHERE guild_id=?", (guild_id,)).fetchone()
            if row is None:
                self.conn.execute("INSERT OR IGNORE INTO an_config (guild_id) VALUES (?)", (guild_id,))
                self.conn.commit()
                row = self.conn.execute("SELECT * FROM an_config WHERE guild_id=?", (guild_id,)).fetchone()
            cols = [d[0] for d in self.conn.execute("SELECT * FROM an_config LIMIT 0").description]
        data = dict(zip(cols, row))
        data["trusted_ids"] = [int(x) for x in data["trusted"].split(",") if x.strip().isdigit()]
        return data

    def set_cfg(self, guild_id: int, **values: Any) -> None:
        self.cfg(guild_id)
        with self.lock:
            sets = ", ".join(f"{k}=?" for k in values)
            self.conn.execute(f"UPDATE an_config SET {sets} WHERE guild_id=?", (*values.values(), guild_id))
            self.conn.commit()

    def enabled_guild_ids(self) -> list[int]:
        with self.lock:
            return [r[0] for r in self.conn.execute("SELECT guild_id FROM an_config WHERE enabled=1")]

    # ------------------------------------------------------------------ objects (channels / roles)
    def known(self, guild_id: int) -> dict[tuple[str, int], tuple[str, Optional[float]]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT kind, obj_id, hash, deleted_at FROM an_objects WHERE guild_id=?", (guild_id,)).fetchall()
        return {(k, i): (h, d) for k, i, h, d in rows}

    def write_objects(self, guild_id: int, items: Iterable[tuple[str, int, str, dict, str]]) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO an_objects (guild_id, kind, obj_id, name, data, hash) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(guild_id, kind, obj_id) DO UPDATE SET name=excluded.name, data=excluded.data, "
                "hash=excluded.hash, deleted_at=NULL",
                [(guild_id, k, i, n, json.dumps(d, ensure_ascii=False), h) for k, i, h, d, n in items])
            self.conn.commit()

    def mark_deleted(self, guild_id: int, kind: str, obj_id: int, by: Optional[int] = None) -> bool:
        """True if this call is the one that marked it (False = it was already marked)."""
        with self.lock:
            cur = self.conn.execute(
                "UPDATE an_objects SET deleted_at=?, deleted_by=? WHERE guild_id=? AND kind=? AND obj_id=? "
                "AND deleted_at IS NULL", (time.time(), by, guild_id, kind, obj_id))
            self.conn.commit()
            return cur.rowcount > 0

    def set_deleted_by(self, guild_id: int, kind: str, obj_id: int, by: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE an_objects SET deleted_by=? WHERE guild_id=? AND kind=? AND obj_id=?",
                              (by, guild_id, kind, obj_id))
            self.conn.commit()

    def get_object(self, guild_id: int, kind: str, obj_id: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute(
                "SELECT name, data, deleted_at, deleted_by, restored_as FROM an_objects "
                "WHERE guild_id=? AND kind=? AND obj_id=?", (guild_id, kind, obj_id)).fetchone()
        if r is None:
            return None
        return {"id": obj_id, "name": r[0], "data": json.loads(r[1]), "deleted_at": r[2],
                "deleted_by": r[3], "restored_as": r[4]}

    def objects(self, guild_id: int, kind: str, *, deleted: Optional[bool] = None) -> list[dict]:
        sql = "SELECT obj_id FROM an_objects WHERE guild_id=? AND kind=?"
        if deleted is True:
            sql += " AND deleted_at IS NOT NULL AND restored_as IS NULL"
        elif deleted is False:
            sql += " AND deleted_at IS NULL"
        with self.lock:
            ids = [r[0] for r in self.conn.execute(sql, (guild_id, kind))]
        return [o for o in (self.get_object(guild_id, kind, i) for i in ids) if o]

    def set_restored(self, guild_id: int, kind: str, obj_id: int, new_id: int) -> None:
        with self.lock:
            self.conn.execute("UPDATE an_objects SET restored_as=? WHERE guild_id=? AND kind=? AND obj_id=?",
                              (new_id, guild_id, kind, obj_id))
            self.conn.commit()

    # ------------------------------------------------------------------ snapshots
    def last_snapshot_hash(self, guild_id: int) -> Optional[str]:
        with self.lock:
            r = self.conn.execute("SELECT hash FROM an_snapshots WHERE guild_id=? ORDER BY id DESC LIMIT 1",
                                  (guild_id,)).fetchone()
        return r[0] if r else None

    def save_snapshot(self, guild_id: int, digest: str, blob: bytes) -> None:
        with self.lock:
            self.conn.execute("INSERT INTO an_snapshots (guild_id, ts, hash, data) VALUES (?,?,?,?)",
                              (guild_id, time.time(), digest, zlib.compress(blob, 6)))
            self.conn.execute(
                "DELETE FROM an_snapshots WHERE guild_id=? AND id NOT IN "
                "(SELECT id FROM an_snapshots WHERE guild_id=? ORDER BY id DESC LIMIT ?)",
                (guild_id, guild_id, config.AN_SNAPSHOT_HISTORY))
            self.conn.commit()

    def snapshot_count(self, guild_id: int) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM an_snapshots WHERE guild_id=?", (guild_id,)).fetchone()[0]

    # ------------------------------------------------------------------ messages
    _UPSERT = (
        "INSERT INTO an_messages (message_id, guild_id, channel_id, author_id, author_name, author_avatar, "
        "is_bot, webhook_id, content, embeds, attachments, links, stickers, msg_type, reference_id, "
        "created_at, edited_at) VALUES (:message_id,:guild_id,:channel_id,:author_id,:author_name,"
        ":author_avatar,:is_bot,:webhook_id,:content,:embeds,:attachments,:links,:stickers,:msg_type,"
        ":reference_id,:created_at,:edited_at) "
        "ON CONFLICT(message_id) DO UPDATE SET content=excluded.content, embeds=excluded.embeds, "
        "links=excluded.links, edited_at=excluded.edited_at, author_name=excluded.author_name")

    def upsert_messages(self, rows: list[dict]) -> None:
        if not rows:
            return
        with self.lock:
            try:
                self.conn.executemany(self._UPSERT, rows)
                self.conn.commit()
                return
            except sqlite3.Error:
                self.conn.rollback()
            # one bad row must never throw away the whole batch: save the good ones one by one
            for row in rows:
                try:
                    self.conn.execute(self._UPSERT, row)
                except sqlite3.Error:
                    continue
            self.conn.commit()

    def pending_message_count(self, channel_id: int) -> int:
        """Saved messages of this (deleted) channel that were not posted back yet."""
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM an_messages WHERE channel_id=? AND restored=0 AND msg_type IN (0,19,20,23)",
                (channel_id,)).fetchone()[0]

    def set_attachments(self, message_id: int, attachments: list[dict]) -> None:
        with self.lock:
            self.conn.execute("UPDATE an_messages SET attachments=? WHERE message_id=?",
                              (json.dumps(attachments), message_id))
            self.conn.commit()

    def mark_messages_deleted(self, ids: Iterable[int], ts: Optional[float] = None) -> None:
        ts = ts or time.time()
        with self.lock:
            self.conn.executemany("UPDATE an_messages SET deleted_at=? WHERE message_id=? AND deleted_at IS NULL",
                                  [(ts, i) for i in ids])
            self.conn.commit()

    def existing_ids(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        with self.lock:
            marks = ",".join("?" * len(ids))
            return {r[0] for r in self.conn.execute(
                f"SELECT message_id FROM an_messages WHERE message_id IN ({marks})", ids)}

    def mark_restored(self, ids: Iterable[int]) -> None:
        with self.lock:
            self.conn.executemany("UPDATE an_messages SET restored=1 WHERE message_id=?", [(i,) for i in ids])
            self.conn.commit()

    def deleted_in_window(self, channel_id: int, start: float, end: float) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM an_messages WHERE channel_id=? AND deleted_at BETWEEN ? AND ? AND restored=0",
                (channel_id, start, end)).fetchone()[0]

    def get_event(self, guild_id: int, event_id: int) -> Optional[dict]:
        with self.lock:
            r = self.conn.execute("SELECT id, ts, type, details FROM an_events WHERE guild_id=? AND id=?",
                                  (guild_id, event_id)).fetchone()
        return {"id": r[0], "ts": r[1], "type": r[2], "details": json.loads(r[3])} if r else None

    def channel_rows(self, channel_id: int, *, only_deleted_since: Optional[float] = None, only_deleted_until: Optional[float] = None,
                     after_id: int = 0, limit: int = 500) -> list[sqlite3.Row]:
        sql = "SELECT * FROM an_messages WHERE channel_id=? AND message_id>? AND restored=0 AND msg_type IN (0,19,20,23)"
        args: list[Any] = [channel_id, after_id]
        if only_deleted_since is not None:
            sql += " AND deleted_at IS NOT NULL AND deleted_at BETWEEN ? AND ?"
            args += [only_deleted_since, only_deleted_until or time.time() + 5]
        sql += " ORDER BY message_id ASC LIMIT ?"
        args.append(limit)
        with self.lock:
            cur = self.conn.execute(sql, args)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def counts(self, guild_id: int) -> dict[str, int]:
        with self.lock:
            q = self.conn.execute
            return {
                "messages": q("SELECT COUNT(*) FROM an_messages WHERE guild_id=?", (guild_id,)).fetchone()[0],
                "deleted_messages": q("SELECT COUNT(*) FROM an_messages WHERE guild_id=? AND deleted_at IS NOT NULL",
                                      (guild_id,)).fetchone()[0],
                "channels": q("SELECT COUNT(*) FROM an_objects WHERE guild_id=? AND kind='channel' AND deleted_at IS NULL",
                              (guild_id,)).fetchone()[0],
                "roles": q("SELECT COUNT(*) FROM an_objects WHERE guild_id=? AND kind='role' AND deleted_at IS NULL",
                           (guild_id,)).fetchone()[0],
                "deleted_channels": q("SELECT COUNT(*) FROM an_objects WHERE guild_id=? AND kind='channel' "
                                      "AND deleted_at IS NOT NULL AND restored_as IS NULL", (guild_id,)).fetchone()[0],
                "snapshots": q("SELECT COUNT(*) FROM an_snapshots WHERE guild_id=?", (guild_id,)).fetchone()[0],
            }

    def channel_message_count(self, channel_id: int) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM an_messages WHERE channel_id=?", (channel_id,)).fetchone()[0]

    # ------------------------------------------------------------------ events / cooldowns
    def add_event(self, guild_id: int, kind: str, details: dict) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO an_events (guild_id, ts, type, details) VALUES (?,?,?,?)",
                                    (guild_id, time.time(), kind, json.dumps(details, ensure_ascii=False)))
            self.conn.commit()
            return cur.lastrowid

    def events(self, guild_id: int, limit: int = 10) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id, ts, type, details FROM an_events WHERE guild_id=? "
                                     "ORDER BY id DESC LIMIT ?", (guild_id, limit)).fetchall()
        return [{"id": i, "ts": t, "type": k, "details": json.loads(d)} for i, t, k, d in rows]

    def get_cooldown(self, guild_id: int, user_id: int, kind: str) -> Optional[float]:
        with self.lock:
            r = self.conn.execute("SELECT ts FROM an_cooldowns WHERE guild_id=? AND user_id=? AND kind=?",
                                  (guild_id, user_id, kind)).fetchone()
        return r[0] if r else None

    def set_cooldown(self, guild_id: int, user_id: int, kind: str, ts: Optional[float] = None) -> None:
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO an_cooldowns (guild_id, user_id, kind, ts) VALUES (?,?,?,?)",
                              (guild_id, user_id, kind, ts or time.time()))
            self.conn.commit()

    # ------------------------------------------------------------------ export
    def export_bytes(self) -> bytes:
        """Consistent copy of the whole anti-nuke database."""
        with self.lock:
            mem = sqlite3.connect(":memory:")
            try:
                self.conn.backup(mem)
                return mem.serialize()
            finally:
                mem.close()

    def export_guild_bytes(self, guild_id: int) -> bytes:
        """Copy of the backup database that contains ONLY this server's data (never another server's)."""
        with self.lock:
            mem = sqlite3.connect(":memory:")
            try:
                self.conn.backup(mem)
                for table in ("an_config", "an_objects", "an_snapshots", "an_messages", "an_events", "an_cooldowns"):
                    mem.execute(f"DELETE FROM {table} WHERE guild_id<>?", (guild_id,))
                mem.commit()
                mem.execute("VACUUM")
                return mem.serialize()
            finally:
                mem.close()

    def close(self) -> None:
        with self.lock:
            self.conn.commit()
            self.conn.close()


# ---------------------------------------------------------------------- snapshot building
def _overwrites(channel) -> list[dict]:
    out = []
    for target, ow in channel.overwrites.items():
        allow, deny = ow.pair()
        out.append({"id": target.id, "type": "role" if isinstance(target, discord.Role) else "member",
                    "allow": allow.value, "deny": deny.value})
    return sorted(out, key=lambda o: (o["type"], o["id"]))


def channel_data(ch) -> dict:
    return {
        "id": ch.id, "name": ch.name, "type": ch.type.value, "position": ch.position,
        "category_id": getattr(ch, "category_id", None), "topic": getattr(ch, "topic", None),
        "nsfw": getattr(ch, "nsfw", False), "slowmode": getattr(ch, "slowmode_delay", 0),
        "bitrate": getattr(ch, "bitrate", None), "user_limit": getattr(ch, "user_limit", None),
        "rtc_region": str(ch.rtc_region) if getattr(ch, "rtc_region", None) else None,
        "auto_archive": getattr(ch, "default_auto_archive_duration", None),
        "overwrites": _overwrites(ch),
    }


def role_data(r: discord.Role) -> dict:
    return {"id": r.id, "name": r.name, "color": r.colour.value, "hoist": r.hoist, "mentionable": r.mentionable,
            "permissions": r.permissions.value, "position": r.position, "managed": r.managed,
            "default": r.is_default()}


def digest_of(data: Any) -> str:
    return hashlib.sha1(json.dumps(data, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def build_snapshot(guild: discord.Guild) -> dict:
    """Whole server structure, read from the gateway cache (no API call, safe to run every second)."""
    return {
        "guild": {
            "id": guild.id, "name": guild.name, "description": guild.description,
            "verification": guild.verification_level.value, "afk_channel": guild.afk_channel.id if guild.afk_channel else None,
            "afk_timeout": guild.afk_timeout, "system_channel": guild.system_channel.id if guild.system_channel else None,
            "icon": guild.icon.url if guild.icon else None, "owner_id": guild.owner_id,
        },
        "roles": [role_data(r) for r in sorted(guild.roles, key=lambda r: r.id)],
        "channels": [channel_data(c) for c in sorted(guild.channels, key=lambda c: c.id)],
        "emojis": [{"id": e.id, "name": e.name, "animated": e.animated, "url": e.url} for e in guild.emojis],
    }


# ---------------------------------------------------------------------- message serialising
def serialize_message(m: discord.Message) -> dict:
    links = list(dict.fromkeys(URL_RE.findall(m.content or "")))
    for e in m.embeds:
        for url in (e.url, getattr(e.image, "url", None), getattr(e.thumbnail, "url", None)):
            if url and url not in links:
                links.append(url)
    return {
        "message_id": m.id, "guild_id": m.guild.id if m.guild else 0, "channel_id": m.channel.id,
        "author_id": m.author.id, "author_name": m.author.display_name[:80],
        "author_avatar": m.author.display_avatar.url, "is_bot": int(m.author.bot),
        "webhook_id": m.webhook_id, "content": m.content or "",
        "embeds": json.dumps([e.to_dict() for e in m.embeds], ensure_ascii=False),
        "attachments": json.dumps([{"id": a.id, "filename": a.filename, "url": a.url, "size": a.size,
                                    "content_type": a.content_type, "local": None} for a in m.attachments]),
        "links": json.dumps(links), "stickers": json.dumps([s.name for s in m.stickers]),
        "msg_type": m.type.value, "reference_id": m.reference.message_id if m.reference else None,
        "created_at": m.created_at.timestamp(), "edited_at": m.edited_at.timestamp() if m.edited_at else None,
    }
