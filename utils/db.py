"""SQLite storage. Everything is saved per server (guild_id) in data/bot.db."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("premium-bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS welcome (
    guild_id INTEGER PRIMARY KEY,
    channel_id INTEGER,
    content TEXT,
    title TEXT,
    description TEXT,
    color TEXT,
    enabled INTEGER DEFAULT 1,
    banner TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_warnings_guild_user ON warnings (guild_id, user_id);
CREATE TABLE IF NOT EXISTS mod_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    duration INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_guild_action ON mod_cases (guild_id, action, id);
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    log_channel_id INTEGER
);
CREATE TABLE IF NOT EXISTS command_aliases (
    guild_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    command TEXT NOT NULL,
    PRIMARY KEY (guild_id, alias)
);
CREATE TABLE IF NOT EXISTS custom_badges (
    user_id INTEGER NOT NULL,
    badge TEXT NOT NULL,
    added_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, badge)
);
CREATE TABLE IF NOT EXISTS badge_emojis (
    badge TEXT PRIMARY KEY,
    emoji TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_imports (
    hash TEXT PRIMARY KEY,
    name TEXT,
    imported_at TEXT
);
CREATE TABLE IF NOT EXISTS no_prefix (
    user_id INTEGER PRIMARY KEY,
    added_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ticket_config (
    guild_id INTEGER PRIMARY KEY,
    panel_channel_id INTEGER,
    panel_message_id INTEGER,
    panel_text TEXT,
    panel_banner TEXT,
    panel_color TEXT,
    panel_placeholder TEXT,
    welcome_title TEXT,
    welcome_description TEXT,
    welcome_color TEXT,
    support_roles TEXT DEFAULT '',
    admin_roles TEXT DEFAULT '',
    log_open INTEGER,
    log_close INTEGER,
    log_transcript INTEGER,
    max_tickets INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS ticket_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    label TEXT,
    emoji TEXT,
    question TEXT,
    placeholder TEXT,
    channel_category_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ticket_categories_guild ON ticket_categories (guild_id);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    category_id INTEGER,
    category_name TEXT,
    answer TEXT,
    claimed_by INTEGER,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by INTEGER,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_channel ON tickets (channel_id);
CREATE INDEX IF NOT EXISTS idx_tickets_guild_user ON tickets (guild_id, user_id, status);
CREATE TABLE IF NOT EXISTS automod_config (
    guild_id INTEGER PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'clean',
    timeout_seconds INTEGER NOT NULL DEFAULT 300,
    ignored_roles TEXT NOT NULL DEFAULT '',
    custom_words TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS images (
    key TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS giveaways (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT,
    saved INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'draft',
    channel_id INTEGER,
    message_id INTEGER,
    host_id INTEGER,
    end_at TEXT,
    data TEXT NOT NULL DEFAULT '{}',
    winners TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_giveaways_guild ON giveaways (guild_id, status);
CREATE INDEX IF NOT EXISTS idx_giveaways_message ON giveaways (message_id);
CREATE TABLE IF NOT EXISTS giveaway_entries (
    giveaway_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    PRIMARY KEY (giveaway_id, user_id)
);
"""


class Database:
    def __init__(self, path: str, legacy_welcome_db: str = "welcome.db") -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        # No -wal/-shm side files: replacing bot.db with another backup can never mix old and new data.
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.executescript(SCHEMA)
        self._add_banner_column()
        self.conn.commit()
        self._migrate_legacy(legacy_welcome_db)

    def _add_banner_column(self) -> None:
        """Older databases have no banner column yet; add it once."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(welcome)")]
        if "banner" not in cols:
            self.conn.execute("ALTER TABLE welcome ADD COLUMN banner TEXT DEFAULT ''")

    def _migrate_legacy(self, legacy: str) -> None:
        """Copy the old root-level welcome.db into the new database once."""
        old = Path(legacy)
        if not old.exists():
            return
        empty = self.conn.execute("SELECT COUNT(*) FROM welcome").fetchone()[0] == 0
        if empty:
            try:
                self.conn.execute("ATTACH DATABASE ? AS legacy", (str(old),))
                self.conn.execute("INSERT OR IGNORE INTO welcome "
                    "(guild_id, channel_id, content, title, description, color, enabled) "
                    "SELECT guild_id, channel_id, content, title, description, color, enabled "
                    "FROM legacy.welcome")
                self.conn.commit()
                self.conn.execute("DETACH DATABASE legacy")
                log.info("Migrated welcome settings from %s", legacy)
            except sqlite3.Error:
                log.exception("Could not migrate %s", legacy)
                return
        try:
            old.rename(old.with_name(old.name + ".migrated"))
        except OSError:
            pass

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
