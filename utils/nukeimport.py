"""Import / export of the anti-nuke archive (antinuke.zip).

* build_full_archive(): zip with the whole backup database of ONE server + every saved picture / file.
* import_archive(): merges such a zip (or a bare antinuke.db) into the live backup database. Nothing is
  deleted or overwritten: messages / channels / roles that are already known are skipped. Channels and roles that
  no longer exist in the server are marked as deleted, so the restore brings them (and their messages) back.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Optional

from utils.nukestore import Store

SQLITE_MAGIC = b"SQLite format 3\x00"
MAX_TOTAL = 4 * 1024 * 1024 * 1024      # uncompressed size of an uploaded archive
PART_RE = re.compile(r"\.part(\d+)$", re.IGNORECASE)
MSG_COLS = ("message_id", "guild_id", "channel_id", "author_id", "author_name", "author_avatar", "is_bot", "webhook_id",
            "content", "embeds", "attachments", "links", "stickers", "msg_type", "reference_id", "created_at",
            "edited_at", "deleted_at", "restored")
MSG_DEFAULTS = {"is_bot": 0, "embeds": "[]", "attachments": "[]", "links": "[]", "stickers": "[]", "msg_type": 0,
                "restored": 0}


class ArchiveError(Exception):
    """Readable problem with an uploaded archive (shown to the user)."""


# ---------------------------------------------------------------------------------------------- export
def build_full_archive(store: Store, guild_id: int, files_dir: Path) -> Path:
    """Zip (on disk, caller deletes it) with antinuke.db of this server + files/<all saved files>."""
    tmp = tempfile.NamedTemporaryFile(prefix="antinuke_", suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("antinuke.db", store.export_guild_bytes(guild_id))
        base = files_dir / str(guild_id)
        if base.exists():
            for p in base.iterdir():
                if p.is_file():
                    zf.write(p, f"files/{p.name}", compress_type=zipfile.ZIP_STORED)
    return Path(tmp.name)


def split_file(path: Path, part_size: int) -> list[bytes]:
    """Read a file in parts of `part_size` bytes (generator-like list is fine: parts are small)."""
    out = []
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(part_size)
            if not chunk:
                break
            out.append(chunk)
    return out


# ---------------------------------------------------------------------------------------------- upload helpers
def group_uploads(files: list[tuple[str, Path]]) -> list[Path]:
    """Uploaded files -> archives. `x.zip.part1`, `x.zip.part2` are joined (in order) into one archive."""
    parts: dict[str, list[tuple[int, Path]]] = {}
    singles: list[Path] = []
    for name, path in files:
        m = PART_RE.search(name)
        if m:
            parts.setdefault(name[:m.start()].lower(), []).append((int(m.group(1)), path))
        else:
            singles.append(path)
    for key, items in parts.items():
        items.sort()
        joined = Path(tempfile.mkstemp(prefix="antinuke_join_", suffix=".zip")[1])
        with open(joined, "wb") as out:
            for _, p in items:
                with open(p, "rb") as fh:
                    shutil.copyfileobj(fh, out)
        singles.append(joined)
    return singles


def _extract(archive: Path, workdir: Path, files_target: Path) -> tuple[Path, int]:
    """-> (path of antinuke.db, number of files copied into files_target)."""
    with open(archive, "rb") as fh:
        head = fh.read(16)
    if head.startswith(SQLITE_MAGIC):
        return archive, 0
    if not zipfile.is_zipfile(archive):
        raise ArchiveError("This is not an antinuke.zip (or a part is missing). Send the whole zip / all parts.")
    copied = 0
    with zipfile.ZipFile(archive) as zf:
        infos = zf.infolist()
        if sum(i.file_size for i in infos) > MAX_TOTAL:
            raise ArchiveError("The archive is too big to import.")
        db_info = next((i for i in infos if Path(i.filename).name.lower() == "antinuke.db"), None) \
            or next((i for i in infos if i.filename.lower().endswith(".db")), None)
        if db_info is None:
            raise ArchiveError("No `antinuke.db` inside this zip. Use the zip made by the anti-nuke backup.")
        db_path = workdir / "antinuke_import.db"
        with zf.open(db_info) as src, open(db_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        files_target.mkdir(parents=True, exist_ok=True)
        for info in infos:
            if info.is_dir() or not info.filename.replace("\\", "/").startswith("files/"):
                continue
            name = Path(info.filename.replace("\\", "/")).name  # basename only: no path tricks
            if not name or name in (".", ".."):
                continue
            dest = files_target / name
            if dest.exists():
                continue
            with zf.open(info) as src, open(dest, "wb") as dst:
                shutil.copyfileobj(src, dst)
            copied += 1
    return db_path, copied


# ---------------------------------------------------------------------------------------------- import
def import_archive(store: Store, archive: Path, guild_id: int, files_dir: Path, *, channel_ids: set[int],
                   role_ids: set[int], allow_other_server: bool = False) -> dict:
    """Merge one archive into the live backup database of `guild_id`. Blocking: run it in a thread.
    channel_ids / role_ids = what exists in the server right now."""
    workdir = Path(tempfile.mkdtemp(prefix="antinuke_import_"))
    try:
        target_files = files_dir / str(guild_id)
        db_path, files_copied = _extract(archive, workdir, target_files)
        try:
            src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise ArchiveError("The database inside the archive is damaged.") from exc
        src.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"an_objects", "an_messages"} <= tables:
                raise ArchiveError("This database is not an anti-nuke backup.")
            ids = {r[0] for r in src.execute("SELECT DISTINCT guild_id FROM an_objects")} | \
                  {r[0] for r in src.execute("SELECT DISTINCT guild_id FROM an_messages")}
            if guild_id in ids:
                source = guild_id
            elif len(ids) == 1 and allow_other_server:
                source = next(iter(ids))  # backup of another / deleted server: moved into this one (bot owner only)
            else:
                raise ArchiveError("This archive belongs to another server" + (f" (`{', '.join(map(str, sorted(ids)))}`)" if ids else "")
                                   + ". Only the bot owner can load it into a different server.")
            return _merge(store, src, source, guild_id, target_files, channel_ids, role_ids, files_copied)
        finally:
            src.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _merge(store: Store, src: sqlite3.Connection, source: int, guild_id: int, target_files: Path,
           channel_ids: set[int], role_ids: set[int], files_copied: int) -> dict:
    now = time.time()
    res = {"messages": 0, "channels_missing": 0, "roles_missing": 0, "objects": 0, "files": files_copied,
           "moved_server": source != guild_id}
    reset_channels: set[int] = set()
    with store.lock:
        live = {(k, i) for k, i in store.conn.execute(
            "SELECT kind, obj_id FROM an_objects WHERE guild_id=?", (guild_id,))}
        for row in src.execute("SELECT * FROM an_objects WHERE guild_id=?", (source,)):
            key = (row["kind"], row["obj_id"])
            if key in live:
                continue
            exists_now = row["obj_id"] in (channel_ids if row["kind"] == "channel" else role_ids)
            restored_as = row["restored_as"]
            still_there = restored_as in (channel_ids if row["kind"] == "channel" else role_ids) if restored_as else False
            if restored_as and not still_there:
                restored_as = None  # the restored copy is gone too: it can be restored again
                if row["kind"] == "channel":
                    reset_channels.add(row["obj_id"])
            if exists_now:
                deleted_at, restored_as = None, None
            else:
                deleted_at = row["deleted_at"] or now
                data = json.loads(row["data"])
                skip = row["kind"] == "role" and (data.get("managed") or data.get("default"))
                if not skip:
                    res["channels_missing" if row["kind"] == "channel" else "roles_missing"] += int(restored_as is None)
            store.conn.execute(
                "INSERT OR IGNORE INTO an_objects (guild_id, kind, obj_id, name, data, hash, deleted_at, deleted_by, "
                "restored_as) VALUES (?,?,?,?,?,?,?,?,?)",
                (guild_id, row["kind"], row["obj_id"], row["name"], row["data"], row["hash"], deleted_at,
                 None if exists_now else 0, restored_as))  # deleted_by 0 = "loaded from an archive"
            res["objects"] += 1

        cols = {d[0] for d in src.execute("SELECT * FROM an_messages LIMIT 0").description}
        before = store.conn.total_changes
        cur = src.execute("SELECT * FROM an_messages WHERE guild_id=?", (source,))
        sql = (f"INSERT OR IGNORE INTO an_messages ({','.join(MSG_COLS)}) VALUES ({','.join(':' + c for c in MSG_COLS)})")
        while True:
            batch = cur.fetchmany(2000)
            if not batch:
                break
            out = []
            for r in batch:
                d = {c: (r[c] if c in cols else MSG_DEFAULTS.get(c)) for c in MSG_COLS}
                d["guild_id"] = guild_id
                for k, v in MSG_DEFAULTS.items():
                    if d[k] is None:
                        d[k] = v
                if d["channel_id"] in reset_channels:
                    d["restored"] = 0
                d["attachments"] = _relink(d["attachments"], target_files)
                out.append(d)
            store.conn.executemany(sql, out)
        res["messages"] = store.conn.total_changes - before
        store.conn.commit()
    return res


def _relink(raw: Optional[str], target_files: Path) -> str:
    """Point every attachment to the file copy that now lives on this host (or None when it was not saved)."""
    try:
        atts = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return "[]"
    for a in atts:
        local = a.get("local")
        if local:
            p = target_files / Path(str(local).replace("\\", "/")).name
            a["local"] = str(p) if p.exists() else None
    return json.dumps(atts)
