"""Read-only snapshots of the bot database. Nothing here ever writes to the live database."""
from __future__ import annotations

import gzip
import io
import json
import sqlite3
import zipfile
from pathlib import Path
from datetime import datetime, timezone


def snapshot_db(conn: sqlite3.Connection) -> bytes:
    """A consistent copy of the whole database as bytes (a normal .db file)."""
    mem = sqlite3.connect(":memory:")
    try:
        conn.backup(mem)  # SQLite online-backup API: safe while the bot is running
        return mem.serialize()
    finally:
        mem.close()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def export_json(conn: sqlite3.Connection) -> bytes:
    """Every table as readable JSON (rows as objects)."""
    cursor = conn.cursor()
    data: dict[str, list] = {}
    for table in table_counts(conn):
        cursor.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cursor.description]
        data[table] = [dict(zip(cols, row)) for row in cursor.fetchall()]
    payload = {"exported_at": datetime.now(timezone.utc).isoformat(), "tables": data}
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str).encode("utf-8")


def fit(name: str, data: bytes, limit: int) -> tuple[str, bytes] | None:
    """(filename, bytes) that fits `limit`: as is, gzipped if needed, or None if still too big."""
    if len(data) <= limit:
        return name, data
    packed = gzip.compress(data, compresslevel=6)
    return (name + ".gz", packed) if len(packed) <= limit else None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {"data", "seed", "__pycache__", ".cache", ".local", ".git", ".venv", "venv", "node_modules"}
_SKIP_SUFFIXES = {".zip", ".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite", ".migrated"}


def _wanted(path: Path) -> bool:
    rel = path.relative_to(PROJECT_ROOT)
    if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
        return False
    name = path.name
    if name == "backup_state.json" or name.startswith(".env") and name != ".env.example" or path.suffix in _SKIP_SUFFIXES:
        return False
    return path.is_file()


def source_files() -> list[Path]:
    return sorted(p for p in PROJECT_ROOT.rglob("*") if _wanted(p))


def source_zip() -> tuple[bytes, int]:
    """ZIP 1: the whole bot CODE (no data/ folder, no .env, no database)."""
    files = source_files()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in files:
            zf.write(path, path.relative_to(PROJECT_ROOT).as_posix())
    return buf.getvalue(), len(files)


def data_zip(conn: sqlite3.Connection) -> tuple[bytes, int]:
    """ZIP 2: only the bot DATA: a fresh copy of data/bot.db and data/bot-data.json (no code, no .env)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("data/bot.db", snapshot_db(conn))
        zf.writestr("data/bot-data.json", export_json(conn))
    return buf.getvalue(), 2
