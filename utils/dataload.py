"""Load / merge bot data (bot.db) from backups, safely.

* prepare_database(): runs before the bot opens bot.db. Repairs the "data folder replaced but old
  -wal file stayed" problem and restores from the bundled seed when the database is missing/broken.
* merge_file(): merges a backup (.zip / .db / .json / .gz) INTO the live database. Nothing is deleted
  or overwritten (unless replace=True for settings); rows that already exist are skipped, new rows
  are added. Running the same file twice adds nothing. Several data folders can be merged one by one.
* auto_import(): at start-up merges files dropped into import/ (or a newer seed/ or data/ folder).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import logging
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.db import SCHEMA

log = logging.getLogger("premium-bot.dataload")

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_BYTES = 300 * 1024 * 1024

# tables where one row is identified by its natural key (existing rows win unless replace=True)
NATURAL = {
    "welcome": ["guild_id"], "guild_settings": ["guild_id"], "command_aliases": ["guild_id", "alias"],
    "custom_badges": ["user_id", "badge"], "badge_emojis": ["badge"], "no_prefix": ["user_id"],
    "ticket_config": ["guild_id"], "automod_config": ["guild_id"], "images": ["key"],
}
# tables with an auto id: a row counts as "the same" when these columns match
IDTABLES = {
    "warnings": ["guild_id", "user_id", "moderator_id", "reason", "created_at"],
    "mod_cases": ["guild_id", "action", "user_id", "moderator_id", "reason", "duration", "created_at"],
    "ticket_categories": ["guild_id", "name"],
    "tickets": ["guild_id", "channel_id", "created_at"],
}
ORDER = ["warnings", "mod_cases", "ticket_categories", "tickets", "giveaways", "giveaway_entries"]


# ---------------------------------------------------------------------------- start-up safety
def _healthy(db: Path, wal: Optional[Path]) -> bool:
    """integrity_check on a COPY (the real files are never touched)."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp) / "t.db"
        shutil.copyfile(db, t)
        if wal is not None and wal.exists():
            shutil.copyfile(wal, Path(tmp) / "t.db-wal")
        try:
            c = sqlite3.connect(t)
            try:
                return c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            finally:
                c.close()
        except sqlite3.Error:
            return False


def _aside(path: Path, folder: str) -> Optional[Path]:
    if not path.exists():
        return None
    target_dir = path.parent / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{path.name}.{int(time.time())}"
    shutil.move(str(path), target)
    return target


def _with_wal(db: Path, wal: Path) -> Optional[bytes]:
    """bot.db + its -wal folded together (on a COPY). None if that combination is damaged."""
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp) / "t.db"
        shutil.copyfile(db, t)
        shutil.copyfile(wal, Path(tmp) / "t.db-wal")
        try:
            c = sqlite3.connect(t)
            try:
                c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return None
            finally:
                c.close()
            return t.read_bytes()
        except sqlite3.Error:
            return None


def prepare_database(db_path: str, seed: Optional[Path] = None) -> list[str]:
    """Call once before the bot opens the database. Returns notes for the log.

    A leftover -wal file next to bot.db is never trusted blindly: it may belong to a bot.db that was
    replaced by another backup. It is moved to data/stale/ and its rows are MERGED into bot.db, so a
    real crash loses nothing and a replaced bot.db is never corrupted."""
    notes: list[str] = []
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    wal, shm = db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")
    rescue: Optional[bytes] = None
    if db.exists() and db.stat().st_size > 0:
        if wal.exists() and wal.stat().st_size > 0:
            rescue = _with_wal(db, wal)
            _aside(wal, "stale")
            notes.append("Found an old -wal file next to bot.db; it was moved to data/stale/")
        else:
            wal.unlink(missing_ok=True)
        shm.unlink(missing_ok=True)
        if not _healthy(db, None):
            _aside(db, "corrupt")
            notes.append("bot.db was damaged and was moved to data/corrupt/")
    elif db.exists():
        db.unlink()  # empty file
    for side in (wal, shm):
        side.unlink(missing_ok=True)
    if not db.exists() and seed is not None and seed.exists():
        shutil.copyfile(seed, db)
        notes.append("Started from the bundled seed/bot.db")
    if rescue is not None and db.exists():
        conn = sqlite3.connect(db)
        try:
            conn.executescript(SCHEMA)
            res = merge_file(conn, db_path, rescue, "old-wal")
            added = sum(c["added"] for c in totals(res).values())
            notes.append(f"Rows from the old -wal file merged back: {added} new")
        except Exception as exc:
            notes.append(f"Could not merge the old -wal file ({type(exc).__name__})")
        finally:
            conn.commit()
            conn.close()
    return notes


# ---------------------------------------------------------------------------- reading backups
def _json_to_db(raw: bytes, out: Path) -> Path:
    payload = json.loads(raw.decode("utf-8"))
    tables = payload.get("tables", payload)
    conn = sqlite3.connect(out)
    try:
        conn.executescript(SCHEMA)
        for table, rows in tables.items():
            if not isinstance(rows, list) or not rows:
                continue
            have = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if not have:
                continue
            for row in rows:
                cols = [c for c in row if c in have]
                if cols:
                    conn.execute(f'INSERT OR IGNORE INTO "{table}" ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                                 [row[c] if not isinstance(row[c], (dict, list)) else json.dumps(row[c]) for c in cols])
        conn.commit()
    finally:
        conn.close()
    return out


def extract_sources(raw: bytes, name: str, tmp: Path, depth: int = 0) -> list[tuple[str, Path]]:
    """Every database found in an uploaded file: .db, .json export, .gz, or a .zip holding them."""
    if depth > 3 or len(raw) > MAX_BYTES:
        return []
    found: list[tuple[str, Path]] = []
    n = len(list(tmp.iterdir()))
    if raw.startswith(SQLITE_MAGIC):
        p = tmp / f"src{n}.db"
        p.write_bytes(raw)
        return [(name, p)]
    if raw[:2] == b"\x1f\x8b":
        return extract_sources(gzip.decompress(raw), name.removesuffix(".gz"), tmp, depth + 1)
    if raw[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if sum(i.file_size for i in infos) > MAX_BYTES:
                raise ValueError("zip is too large")
            dbs, jsons = [], []
            for i in infos:
                low = i.filename.lower()
                if "antinuke" in low or low.endswith(("-wal", "-shm")):
                    continue
                (jsons if low.endswith(".json") else dbs).append(i)
            for i in dbs:
                found += extract_sources(zf.read(i), f"{name}:{i.filename}", tmp, depth + 1)
            if not found:  # only readable exports inside: use them
                for i in jsons:
                    found += extract_sources(zf.read(i), f"{name}:{i.filename}", tmp, depth + 1)
        return found
    try:
        p = tmp / f"src{n}.db"
        return [(name, _json_to_db(raw, p))]
    except (ValueError, UnicodeDecodeError, sqlite3.Error, AttributeError):
        return []


# ---------------------------------------------------------------------------- merging
def _cols(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _merge_one(conn: sqlite3.Connection, replace: bool) -> dict[str, dict[str, int]]:
    """`src` is attached. Returns {table: {"added": n, "skipped": n}}."""
    report: dict[str, dict[str, int]] = {}
    src_tables = {r[0] for r in conn.execute("SELECT name FROM src.sqlite_master WHERE type='table'")}

    for table, key in NATURAL.items():
        if table not in src_tables:
            continue
        cols = [c for c in _cols(conn, "main", table) if c in _cols(conn, "src", table)]
        if not cols or not all(k in cols for k in key):
            continue
        total = conn.execute(f'SELECT COUNT(*) FROM src."{table}"').fetchone()[0]
        before = conn.total_changes
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        conn.execute(f'{verb} INTO main."{table}" ({",".join(cols)}) SELECT {",".join(cols)} FROM src."{table}"')
        added = conn.total_changes - before
        report[table] = {"added": added, "skipped": total - added}

    maps: dict[str, dict[int, int]] = {"ticket_categories": {}, "giveaways": {}}
    for table in ORDER:
        if table not in src_tables:
            continue
        have = _cols(conn, "main", table)
        cols = [c for c in _cols(conn, "src", table) if c in have and c != "id"]
        added = skipped = 0
        if table == "giveaway_entries":
            before = conn.total_changes
            rows = conn.execute("SELECT giveaway_id, user_id FROM src.giveaway_entries").fetchall()
            for gid, uid in rows:
                new_gid = maps["giveaways"].get(gid)
                if new_gid is not None:
                    conn.execute("INSERT OR IGNORE INTO main.giveaway_entries (giveaway_id, user_id) VALUES (?,?)",
                                 (new_gid, uid))
            added = conn.total_changes - before
            report[table] = {"added": added, "skipped": len(rows) - added}
            continue
        cur = conn.execute(f'SELECT id, {",".join(cols)} FROM src."{table}" ORDER BY id')
        for row in cur.fetchall():
            sid, values = row[0], dict(zip(cols, row[1:]))
            if table == "tickets" and values.get("category_id") in maps["ticket_categories"]:
                values["category_id"] = maps["ticket_categories"][values["category_id"]]
            if table == "giveaways":
                key = ["guild_id", "message_id"] if values.get("message_id") else ["guild_id", "name", "created_at"]
            else:
                key = IDTABLES[table]
            key = [k for k in key if k in values]
            where = " AND ".join(f'"{k}" IS ?' for k in key)
            hit = conn.execute(f'SELECT id FROM main."{table}" WHERE {where}', [values[k] for k in key]).fetchone()
            if hit:
                skipped += 1
                new_id = hit[0]
            else:
                cur2 = conn.execute(f'INSERT INTO main."{table}" ({",".join(cols)}) VALUES ({",".join("?" * len(cols))})',
                                    [values[c] for c in cols])
                new_id = cur2.lastrowid
                added += 1
            if table in maps:
                maps[table][sid] = new_id
        report[table] = {"added": added, "skipped": skipped}
    return report


def snapshot_before(conn: sqlite3.Connection, db_path: str) -> Optional[Path]:
    """Safety copy of the live database before anything is merged (last 5 are kept)."""
    folder = Path(db_path).parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"before-load-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    dest = sqlite3.connect(target)
    try:
        conn.backup(dest)
    finally:
        dest.close()
    for old in sorted(folder.glob("before-load-*.db"))[:-5]:
        old.unlink(missing_ok=True)
    return target


def merge_file(conn: sqlite3.Connection, db_path: str, raw: bytes, name: str, *, replace: bool = False
               ) -> list[tuple[str, dict[str, dict[str, int]]]]:
    """Merge an uploaded backup into the live database. Raises ValueError if nothing usable is inside."""
    results: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        sources = extract_sources(raw, name, Path(tmpdir))
        sources = [(n, p) for n, p in sources if _is_bot_db(p)]
        if not sources:
            raise ValueError("No WailQueen data (bot.db / bot-data.json) found in that file.")
        conn.commit()
        snapshot_before(conn, db_path)
        for src_name, path in sources:
            conn.commit()
            conn.execute("ATTACH DATABASE ? AS src", (str(path),))
            try:
                rep = _merge_one(conn, replace)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.commit()
                conn.execute("DETACH DATABASE src")
            results.append((src_name, rep))
    return results


def _is_bot_db(path: Path) -> bool:
    try:
        c = sqlite3.connect(path)
        try:
            names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        return bool(names & (set(NATURAL) | set(IDTABLES) | {"giveaways"}))
    except sqlite3.Error:
        return False


def totals(results: list[tuple[str, dict]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for _, rep in results:
        for table, c in rep.items():
            t = out.setdefault(table, {"added": 0, "skipped": 0})
            t["added"] += c["added"]
            t["skipped"] += c["skipped"]
    return out


# ---------------------------------------------------------------------------- start-up auto import
def auto_import(conn: sqlite3.Connection, db_path: str, root: Path) -> list[str]:
    """Merge files from import/ (moved to import/done/ afterwards) and newer seed/ or data/ folders.
    A file is only merged once (its hash is remembered inside the database)."""
    lines: list[str] = []
    live = Path(db_path).resolve()
    candidates: list[tuple[Path, bool]] = []
    for folder, move in ((root / "import", True), (Path(db_path).parent / "import", True)):
        if folder.is_dir():
            candidates += [(p, move) for p in sorted(folder.rglob("*")) if p.is_file() and "done" not in p.parts]
    for folder in (root / "seed", root / "data"):
        for pattern in ("bot.db", "bot-data.json"):
            p = folder / pattern
            if p.is_file() and p.resolve() != live:
                candidates.append((p, False))
    seen: set[str] = set()
    for path, move in candidates:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        if conn.execute("SELECT 1 FROM data_imports WHERE hash=?", (digest,)).fetchone():
            _finish(path, move)
            continue
        try:
            results = merge_file(conn, db_path, raw, path.name)
        except Exception as exc:
            lines.append(f"{path.name}: skipped ({type(exc).__name__}: {exc})")
            continue
        conn.execute("INSERT OR IGNORE INTO data_imports (hash, name, imported_at) VALUES (?,?,?)",
                     (digest, path.name, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        added = sum(c["added"] for c in totals(results).values())
        lines.append(f"{path.name}: {added} new rows added")
        _finish(path, move)
    return lines


def _finish(path: Path, move: bool) -> None:
    if not move:
        return
    done = path.parent / "done"
    done.mkdir(exist_ok=True)
    try:
        shutil.move(str(path), done / f"{int(time.time())}-{path.name}")
    except OSError:
        pass
