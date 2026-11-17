"""Anti-nuke: live backup of the whole server + nuke protection + one-click restore.

* Every second (AN_TICK_SECONDS) the server structure (roles, categories, channels, permissions) is
  read from the gateway cache and saved when it changed; every message (also bots / webhooks), image,
  file and link is saved the moment it is sent (real-time events -> database, flushed every second).
  Old history is back-filled in the background when protection is turned on.
* The backup is mirrored to the webhook that is set in the panel (live status message + alerts +
  full archive every few minutes).
* Deleting a channel (voice channels excluded) starts a 2 hour cooldown for that person. A second
  delete inside the cooldown = punishment + automatic restore. Mass bans / kicks / role or channel
  spam / bot adds / webhook spam / message wipes are punished too.
* `!antinuke` opens the panel. Only the server owner, the bot owner and trusted users can use it.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import re
import shutil
import tempfile
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import nukeimport
from utils import nukerestore as restore
from utils.icons import icon
from utils.nukestore import VOICE_TYPES, Store, build_snapshot, channel_data, digest_of, role_data, serialize_message

log = logging.getLogger("premium-bot.antinuke")

WEBHOOK_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+")
ARCHIVE_PART = 8 * 1024 * 1024
PUNISH = {"ban": "Ban the person", "kick": "Kick the person", "strip": "Remove all their roles"}
EVENT_TITLES = {
    "channel_delete": "Channel deleted", "channel_blocked": "Channel delete BLOCKED", "channel_missing": "Channel missing",
    "message_wipe": "Messages wiped", "punished": "Nuker punished", "nuke_clone": "Channel nuked (clone)",
    "role_delete": "Role deleted", "limit": "Nuke limit reached", "bot_added": "Bot added", "restore": "Restore finished",
}


def ts_fmt(ts: float, style: str = "R") -> str:
    return f"<t:{int(ts)}:{style}>"


class AntiNuke(commands.Cog):
    """Server backup + nuke protection. Owner / trusted users only."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = Store(config.AN_DB_PATH)
        self.session: Optional[aiohttp.ClientSession] = None
        self.enabled: set[int] = set()
        self.msg_queue: list[dict] = []
        self.del_queue: list[int] = []
        self.known: dict[int, dict] = {}
        self.digest: dict[int, str] = {}
        self.state: dict[int, dict] = defaultdict(lambda: {
            "missing": {}, "alerts": [], "last_push": 0.0, "last_tick": 0.0, "snap_gz": b"", "archiving": False,
            "wh_retry": 0.0, "wh_error": "", "snap_hash": None, "archive_msgs": [], "backfill": None, "restore": None,
        })
        self.hits: dict[tuple, deque] = defaultdict(deque)
        self.victims: dict[tuple, list[int]] = defaultdict(list)
        self.wipes: dict[int, dict] = {}
        self.punished_at: dict[tuple, float] = {}
        self.dl_sem = asyncio.Semaphore(4)
        self.restoring: dict[int, int] = defaultdict(int)  # guild id -> restores running (create-listener stays out)
        self.restore_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.upload_wait: set[tuple[int, int]] = set()  # (channel, user) that is about to upload an antinuke.zip
        self.tasks_: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        self.enabled = set(self.store.enabled_guild_ids())
        self.tick.change_interval(seconds=config.AN_TICK_SECONDS)
        self.tick.start()
        self._spawn(self._startup())

    async def cog_unload(self) -> None:
        self.tick.cancel()
        for t in list(self.tasks_):
            t.cancel()
        await self.flush()
        if self.session and not self.session.closed:
            await self.session.close()
        self.store.close()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks_.add(task)
        task.add_done_callback(self.tasks_.discard)
        return task

    async def _startup(self) -> None:
        await self.bot.wait_until_ready()
        for gid in list(self.enabled):
            guild = self.bot.get_guild(gid)
            if guild is not None:
                self._spawn(self.backfill(guild))

    async def final_flush(self) -> None:
        """Called by PremiumBot.close(): last messages + structure go to the database / webhook."""
        try:
            await self.flush()
            for gid in list(self.enabled):
                guild = self.bot.get_guild(gid)
                if guild is not None:
                    await asyncio.wait_for(self._tick_guild(guild, force=True), 15)
        except Exception:
            log.exception("Final anti-nuke flush failed")

    # ------------------------------------------------------------------ access
    async def can_manage(self, guild: discord.Guild, user: discord.abc.User) -> bool:
        if user.id == guild.owner_id or await self.bot.is_owner(user):
            return True
        return user.id in self.store.cfg(guild.id)["trusted_ids"]

    def exempt(self, guild: discord.Guild, user_id: int, cfg: Optional[dict] = None) -> bool:
        cfg = cfg or self.store.cfg(guild.id)
        return user_id in (guild.owner_id, self.bot.user.id) or user_id in cfg["trusted_ids"]  # type: ignore[union-attr]

    @asynccontextmanager
    async def _restoring(self, guild: discord.Guild):
        """One restore per server at a time. While it runs, on_guild_channel_create ignores the channels the
        restore itself creates (before, both reposted the same messages at once and fought over the webhook)."""
        self.restoring[guild.id] += 1
        try:
            async with self.restore_locks[guild.id]:
                await self.flush()  # messages from the last second must be in the database first
                yield
        finally:
            self.restoring[guild.id] -= 1

    # ------------------------------------------------------------------ message capture
    async def flush(self) -> None:
        rows, self.msg_queue = self.msg_queue, []
        dels, self.del_queue = self.del_queue, []
        if rows:
            await asyncio.to_thread(self.store.upsert_messages, rows)
        if dels:
            await asyncio.to_thread(self.store.mark_messages_deleted, dels)

    def _capture(self, message: discord.Message) -> None:
        self.msg_queue.append(serialize_message(message))
        if message.attachments:
            self._spawn(self._download(message))

    async def _download(self, message: discord.Message) -> None:
        saved = []
        base = Path(config.AN_FILES_DIR) / str(message.guild.id)  # type: ignore[union-attr]
        async with self.dl_sem:
            for a in message.attachments:
                entry = {"id": a.id, "filename": a.filename, "url": a.url, "size": a.size,
                         "content_type": a.content_type, "local": None}
                path = base / f"{a.id}_{re.sub(r'[^A-Za-z0-9._-]', '_', a.filename)[:80]}"
                try:
                    if path.exists():
                        entry["local"] = str(path)
                    elif a.size <= config.AN_MAX_FILE_MB * 1024 * 1024:
                        data = await a.read()
                        base.mkdir(parents=True, exist_ok=True)
                        await asyncio.to_thread(path.write_bytes, data)
                        entry["local"] = str(path)
                except (discord.HTTPException, OSError):
                    pass
                saved.append(entry)
        await self.flush()  # make sure the row exists before the file paths are attached
        await asyncio.to_thread(self.store.set_attachments, message.id, saved)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.attachments and (message.channel.id, message.author.id) in self.upload_wait:
            return  # an antinuke.zip upload: never store the archive inside the backup itself
        if message.guild and message.guild.id in self.enabled:
            self._capture(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.guild and after.guild.id in self.enabled:
            self._capture(after)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id in self.enabled:
            self.del_queue.append(payload.message_id)
            self._wipe_hit(payload.guild_id, payload.channel_id, 1)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id in self.enabled:
            self.del_queue.extend(payload.message_ids)
            self._wipe_hit(payload.guild_id, payload.channel_id, len(payload.message_ids))

    async def backfill(self, guild: discord.Guild) -> None:
        """Save the old history of every text channel (stops at what is already saved)."""
        st = self.state[guild.id]
        chans = [c for c in guild.text_channels if c.permissions_for(guild.me).read_message_history
                 and c.permissions_for(guild.me).view_channel]
        st["backfill"] = {"done": 0, "total": len(chans), "messages": 0, "running": True}
        try:
            for ch in chans:
                if guild.id not in self.enabled:
                    break
                batch: list[discord.Message] = []
                try:
                    async for m in ch.history(limit=None, oldest_first=False):
                        batch.append(m)
                        if len(batch) < 100:
                            continue
                        known_count = await self._backfill_batch(batch, st)
                        batch = []
                        if known_count >= 100:  # a whole batch was already saved: older history is saved too
                            break
                    if batch:
                        await self._backfill_batch(batch, st)
                except discord.HTTPException:
                    log.warning("Backfill of #%s failed", ch.name)
                st["backfill"]["done"] += 1
                await asyncio.sleep(0.2)
        finally:
            if st["backfill"]:
                st["backfill"]["running"] = False

    async def _backfill_batch(self, batch: list[discord.Message], st: dict) -> int:
        known = await asyncio.to_thread(self.store.existing_ids, [m.id for m in batch])
        new = [m for m in batch if m.id not in known]
        for m in new:
            self._capture(m)
        st["backfill"]["messages"] += len(new)
        await self.flush()
        return len(known)

    # ------------------------------------------------------------------ 1 second backup loop
    @tasks.loop(seconds=1)
    async def tick(self) -> None:
        try:
            await self.flush()
        except Exception:
            log.exception("Message flush failed")
        for gid in list(self.enabled):
            guild = self.bot.get_guild(gid)
            if guild is None or guild.unavailable:
                continue
            try:
                await self._tick_guild(guild)
            except Exception:
                log.exception("Anti-nuke tick failed for %s", gid)

    @tick.before_loop
    async def _before_tick(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild, *, force: bool = False) -> None:
        gid, st, now = guild.id, self.state[guild.id], time.time()
        snap = build_snapshot(guild)
        digest = digest_of(snap)
        changed = force or digest != self.digest.get(gid)
        if changed or st["missing"]:
            if gid not in self.known:
                self.known[gid] = await asyncio.to_thread(self.store.known, gid)
            known = self.known[gid]
            items, present = [], set()
            for kind, objs in (("channel", snap["channels"]), ("role", snap["roles"])):
                for obj in objs:
                    key = (kind, obj["id"])
                    present.add(key)
                    st["missing"].pop(key, None)
                    h = digest_of(obj)
                    if key not in known or known[key][0] != h or known[key][1] is not None:
                        items.append((kind, obj["id"], h, obj, obj["name"]))
                        known[key] = (h, None)
            if items:
                await asyncio.to_thread(self.store.write_objects, gid, items)
            for key, (_, deleted) in list(known.items()):
                if deleted is None and key not in present:
                    first = st["missing"].setdefault(key, now)
                    if now - first >= 10:  # the gateway handler normally got it first; this catches downtime
                        st["missing"].pop(key, None)
                        marked = await asyncio.to_thread(self.store.mark_deleted, gid, key[0], key[1])
                        known[key] = (known[key][0], now)
                        if marked:
                            obj = await asyncio.to_thread(self.store.get_object, gid, key[0], key[1])
                            if key[0] == "channel" and obj and obj["data"]["type"] not in VOICE_TYPES:
                                await self._event(guild, "channel_missing", {
                                    "channel_id": key[1], "name": obj["name"], "type": obj["data"]["type"],
                                    "messages": await asyncio.to_thread(self.store.channel_message_count, key[1])})
            if digest != self.digest.get(gid) or force:
                self.digest[gid] = digest
                if st["snap_hash"] is None:
                    st["snap_hash"] = await asyncio.to_thread(self.store.last_snapshot_hash, gid)
                if st["snap_hash"] != digest:
                    blob = json.dumps(snap, ensure_ascii=False).encode()
                    await asyncio.to_thread(self.store.save_snapshot, gid, digest, blob)
                    st["snap_hash"] = digest
                st["snap_gz"] = gzip.compress(json.dumps(snap, ensure_ascii=False, indent=1).encode(), 6)
        st["last_tick"] = now
        cfg = self.store.cfg(gid)
        await self._mirror(guild, cfg, changed, force=force)
        if cfg["webhook_url"] and not st["archiving"] and now - cfg["last_archive_ts"] >= config.AN_ARCHIVE_MINUTES * 60:
            st["archiving"] = True
            self.store.set_cfg(gid, last_archive_ts=now)
            self._spawn(self._archive(guild, cfg))

    # ------------------------------------------------------------------ events + webhook mirror
    async def _event(self, guild: discord.Guild, kind: str, details: dict, *, alert: bool = True) -> int:
        details = {**details}
        eid = await asyncio.to_thread(self.store.add_event, guild.id, kind, details)
        if alert:
            self.state[guild.id]["alerts"].append(self._event_embed(kind, details, eid))
        return eid

    def _event_embed(self, kind: str, d: dict, eid: int) -> discord.Embed:
        red = kind in {"channel_blocked", "punished", "nuke_clone", "limit", "message_wipe"}
        e = discord.Embed(title=f"{'🚨' if red else '📦'} {EVENT_TITLES.get(kind, kind)}",
                          color=0xE74C3C if red else 0x3498DB, timestamp=discord.utils.utcnow())
        lines = []
        if d.get("name"):
            lines.append(f"**Channel/Role:** {d['name']}")
        if d.get("channel_id"):
            lines.append(f"**Channel ID:** `{d['channel_id']}`")
        if d.get("by"):
            lines.append(f"**By:** <@{d['by']}> (`{d['by']}`)")
        if d.get("user_id"):
            lines.append(f"**User:** <@{d['user_id']}> (`{d['user_id']}`)")
        if "messages" in d:
            lines.append(f"**Messages in backup:** {d['messages']:,}")
        if d.get("count"):
            lines.append(f"**Messages deleted:** {d['count']:,}")
        if d.get("cooldown"):
            lines.append(f"**Cooldown:** {d['cooldown']}")
        if d.get("reason"):
            lines.append(f"**Reason:** {d['reason']}")
        if d.get("result"):
            lines.append(f"**Result:** {d['result']}")
        if kind in {"channel_delete", "channel_missing", "message_wipe", "nuke_clone"}:
            lines.append("\n*Everything is saved. Open the `antinuke` panel > **Backup Details** to restore it.*")
        e.description = "\n".join(lines) or "-"
        e.set_footer(text=f"Event #{eid}")
        return e

    async def _mirror(self, guild: discord.Guild, cfg: dict, changed: bool, *, force: bool = False) -> None:
        url = cfg["webhook_url"]
        st = self.state[guild.id]
        now = time.time()
        if not url or now < st["wh_retry"]:
            return
        due = force or changed or st["alerts"] or now - st["last_push"] >= 30
        if not due or (now - st["last_push"] < 1 and not force):
            return
        st["last_push"] = now
        try:
            hook = discord.Webhook.from_url(url, session=self.session)  # type: ignore[arg-type]
            avatar = self.bot.user.display_avatar.url if self.bot.user else discord.utils.MISSING
            for _ in range(min(len(st["alerts"]), 5)):
                await hook.send(embed=st["alerts"].pop(0), username=f"{config.BOT_NAME} Anti-Nuke", avatar_url=avatar)
            counts = await asyncio.to_thread(self.store.counts, guild.id)
            embed = discord.Embed(title=f"💾 Live backup • {guild.name}", color=0x2ECC71, timestamp=discord.utils.utcnow())
            embed.description = (
                f"🟢 **Backing up every {config.AN_TICK_SECONDS}s**\n"
                f"🕒 Last check: {ts_fmt(st['last_tick'])}\n"
                f"📁 Channels: **{counts['channels']}** • Roles: **{counts['roles']}**\n"
                f"💬 Messages saved: **{counts['messages']:,}** (deleted but kept: {counts['deleted_messages']:,})\n"
                f"🗄️ Structure versions: **{counts['snapshots']}** • Deleted channels waiting: **{counts['deleted_channels']}**")
            embed.set_footer(text="structure.json.gz = whole server structure • full archive every "
                                  f"{config.AN_ARCHIVE_MINUTES} min")
            files = [discord.File(io.BytesIO(st["snap_gz"]), filename="structure.json.gz")] if st["snap_gz"] else []
            done = False
            if cfg["webhook_msg_id"]:
                try:
                    await hook.edit_message(cfg["webhook_msg_id"], embed=embed, attachments=files)
                    done = True
                except discord.NotFound:
                    pass
            if not done:
                msg = await hook.send(embed=embed, files=files, wait=True, username=f"{config.BOT_NAME} Anti-Nuke",
                                      avatar_url=avatar)
                self.store.set_cfg(guild.id, webhook_msg_id=msg.id)
            st["wh_error"] = ""
        except (discord.HTTPException, ValueError) as exc:
            st["wh_error"] = f"{type(exc).__name__} {getattr(exc, 'status', '')}".strip()
            st["wh_retry"] = now + 30
            log.warning("Anti-nuke webhook failed: %s", st["wh_error"])

    async def _archive(self, guild: discord.Guild, cfg: dict) -> None:
        """Zip of the whole backup database + files saved since the last archive, sent in 8 MB parts."""
        st = self.state[guild.id]
        try:
            since = cfg["last_archive_ts"] - config.AN_ARCHIVE_MINUTES * 60 - 60

            def build() -> bytes:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                    zf.writestr("antinuke.db", self.store.export_guild_bytes(guild.id))
                    base = Path(config.AN_FILES_DIR) / str(guild.id)
                    if base.exists():
                        for p in base.iterdir():
                            if p.is_file() and p.stat().st_mtime >= since:
                                zf.write(p, f"files/{p.name}")
                return buf.getvalue()

            data = await asyncio.to_thread(build)
            hook = discord.Webhook.from_url(cfg["webhook_url"], session=self.session)  # type: ignore[arg-type]
            for old in st["archive_msgs"]:
                try:
                    await hook.delete_message(old)
                except discord.HTTPException:
                    pass
            st["archive_msgs"] = []
            parts = [data[i:i + ARCHIVE_PART] for i in range(0, len(data), ARCHIVE_PART)] or [b""]
            for n, part in enumerate(parts, 1):
                name = f"antinuke-{guild.id}.zip" + (f".part{n}" if len(parts) > 1 else "")
                msg = await hook.send(
                    content=f"📦 Full archive {n}/{len(parts)} • {ts_fmt(time.time(), 'f')}",
                    file=discord.File(io.BytesIO(part), filename=name), wait=True,
                    username=f"{config.BOT_NAME} Anti-Nuke")
                st["archive_msgs"].append(msg.id)
        except Exception as exc:
            log.warning("Archive failed: %s", type(exc).__name__)
        finally:
            st["archiving"] = False

    # ------------------------------------------------------------------ nuke detection
    def _hit(self, guild_id: int, user_id: int, kind: str) -> bool:
        limit, window = config.AN_LIMITS[kind]
        q = self.hits[(guild_id, user_id, kind)]
        now = time.monotonic()
        q.append(now)
        while q and now - q[0] > window:
            q.popleft()
        return len(q) >= limit

    async def punish(self, guild: discord.Guild, user_id: int, reason: str) -> bool:
        if self.exempt(guild, user_id):
            return False
        key = (guild.id, user_id)
        if time.time() - self.punished_at.get(key, 0) < 20:
            return True
        self.punished_at[key] = time.time()
        mode = self.store.cfg(guild.id)["punishment"]
        member = guild.get_member(user_id)
        ok = False
        try:
            if mode == "kick" and member:
                await member.kick(reason=reason)
                ok = True
            elif mode == "strip" and member:
                roles = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
                await member.remove_roles(*roles, reason=reason)
                ok = True
            else:
                await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_days=0)
                ok = True
        except discord.HTTPException:
            try:  # could not do the chosen action: at least take the roles away
                if member:
                    roles = [r for r in member.roles if not r.is_default() and not r.managed and r < guild.me.top_role]
                    await member.remove_roles(*roles, reason=reason)
                    ok = True
            except discord.HTTPException:
                ok = False
        await self._event(guild, "punished", {"user_id": user_id, "reason": reason,
                                              "result": f"{mode} {'done' if ok else 'FAILED (my role is too low)'}"})
        return ok

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        guild = entry.guild
        if guild.id not in self.enabled or entry.user_id is None:
            return
        try:
            await self._on_audit(guild, entry)
        except Exception:
            log.exception("Anti-nuke audit handler failed")

    async def _on_audit(self, guild: discord.Guild, entry: discord.AuditLogEntry) -> None:
        A, uid, cfg = discord.AuditLogAction, entry.user_id, None
        cfg = self.store.cfg(guild.id)
        exempt = self.exempt(guild, uid, cfg)
        target_id = getattr(entry.target, "id", None)

        if entry.action is A.channel_delete and target_id:
            await self._channel_deleted(guild, cfg, uid, target_id, exempt)
        elif entry.action is A.role_delete and target_id:
            await asyncio.to_thread(self.store.mark_deleted, guild.id, "role", target_id, uid)
            await asyncio.to_thread(self.store.set_deleted_by, guild.id, "role", target_id, uid)
            if not exempt and self._hit(guild.id, uid, "role_delete"):
                await self.punish(guild, uid, "Anti-nuke: deleting roles too fast")
                if cfg["auto_restore"]:
                    ids = [o["id"] for o in self.store.objects(guild.id, "role", deleted=True)
                           if o["deleted_by"] == uid and time.time() - (o["deleted_at"] or 0) < 120]
                    made = await restore.restore_roles(guild, self.store, ids)
                    await self._event(guild, "role_delete", {"by": uid, "result": f"{len(made)} role(s) restored"})
        elif entry.action is A.channel_create and not exempt and self._hit(guild.id, uid, "channel_create"):
            await self.punish(guild, uid, "Anti-nuke: creating channels too fast")
            for obj in self.store.objects(guild.id, "channel", deleted=False):
                ch = guild.get_channel(obj["id"])
                if ch and ch.id == target_id:
                    try:
                        await ch.delete(reason="Anti-nuke: channel spam")
                    except discord.HTTPException:
                        pass
        elif entry.action is A.role_create and not exempt and self._hit(guild.id, uid, "role_create"):
            await self.punish(guild, uid, "Anti-nuke: creating roles too fast")
            role = guild.get_role(target_id or 0)
            if role:
                try:
                    await role.delete(reason="Anti-nuke: role spam")
                except discord.HTTPException:
                    pass
        elif entry.action is A.ban and target_id:
            self.victims[(guild.id, uid)].append(target_id)
            if not exempt and self._hit(guild.id, uid, "ban"):
                await self.punish(guild, uid, "Anti-nuke: mass ban")
                if cfg["auto_restore"]:
                    for victim in self.victims.pop((guild.id, uid), []):
                        try:
                            await guild.unban(discord.Object(id=victim), reason="Anti-nuke: mass ban reverted")
                        except discord.HTTPException:
                            pass
        elif entry.action is A.kick and not exempt and self._hit(guild.id, uid, "kick"):
            await self.punish(guild, uid, "Anti-nuke: mass kick")
        elif entry.action is A.webhook_create and not exempt and self._hit(guild.id, uid, "webhook_create"):
            await self.punish(guild, uid, "Anti-nuke: creating webhooks too fast")
            try:
                await (await self.bot.fetch_webhook(target_id or 0)).delete(reason="Anti-nuke: webhook spam")
            except discord.HTTPException:
                pass
        elif entry.action is A.bot_add and not exempt and target_id:
            member = guild.get_member(target_id)
            result = "not removed"
            if member:
                try:
                    await member.kick(reason="Anti-nuke: bots can only be added by the owner / trusted users")
                    result = "bot kicked"
                except discord.HTTPException:
                    pass
            await self._event(guild, "bot_added", {"by": uid, "user_id": target_id, "result": result})
        elif entry.action in (A.message_bulk_delete, A.message_delete):
            chan = target_id if entry.action is A.message_bulk_delete else getattr(getattr(entry.extra, "channel", None), "id", None)
            wipe = self.wipes.get(chan or 0)
            if wipe is not None:
                wipe["by"] = uid

    async def _channel_deleted(self, guild: discord.Guild, cfg: dict, uid: int, channel_id: int, exempt: bool) -> None:
        await asyncio.to_thread(self.store.mark_deleted, guild.id, "channel", channel_id, uid)
        await asyncio.to_thread(self.store.set_deleted_by, guild.id, "channel", channel_id, uid)
        key = ("channel", channel_id)
        if key in self.known.get(guild.id, {}):
            self.known[guild.id][key] = (self.known[guild.id][key][0], time.time())
        obj = await asyncio.to_thread(self.store.get_object, guild.id, "channel", channel_id)
        if obj is None or obj["data"]["type"] in VOICE_TYPES:
            return  # voice channels: not counted, no cooldown
        d = obj["data"]
        info = {"channel_id": channel_id, "name": obj["name"], "type": d["type"], "by": uid,
                "messages": await asyncio.to_thread(self.store.channel_message_count, channel_id)}
        if exempt:
            await self._event(guild, "channel_delete", {**info, "cooldown": "none (owner / trusted)"})
            return
        last = await asyncio.to_thread(self.store.get_cooldown, guild.id, uid, "channel_delete")
        left = config.AN_CHANNEL_DELETE_COOLDOWN - (time.time() - last) if last else 0
        if left > 0:
            await self._event(guild, "channel_blocked", {
                **info, "cooldown": f"{int(left // 60)} min left of 2 hours",
                "reason": "Second channel deleted inside the 2 hour cooldown"})
            await self.punish(guild, uid, "Anti-nuke: deleted a channel during the 2 hour delete cooldown")
            if cfg["auto_restore"]:
                await asyncio.sleep(2)
                async with self._restoring(guild):
                    report = await restore.restore_channels(guild, self.store, [channel_id])
                result = f"{report['channels']} channel(s), {report['messages']} messages restored"
                if report.get("failed"):
                    result += f" • {report['failed']} failed ({report.get('error') or 'unknown'}) - retry in Backup Details"
                await self._event(guild, "restore", {"name": obj["name"], "result": result})
        else:
            await asyncio.to_thread(self.store.set_cooldown, guild.id, uid, "channel_delete")
            await self._event(guild, "channel_delete", {**info, "cooldown": "started: 2 hours until their next delete"})

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        """Nuke bots 'clone' a channel (delete + create the same name). Put the old messages back."""
        guild = channel.guild
        if guild.id not in self.enabled or isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return
        if self.restoring.get(guild.id):
            return  # this channel is being created by a restore that already reposts the messages itself
        cfg = self.store.cfg(guild.id)
        now = time.time()
        for obj in self.store.objects(guild.id, "channel", deleted=True):
            if obj["name"] == channel.name and obj["data"]["type"] == channel.type.value \
                    and now - (obj["deleted_at"] or 0) < 180 and not self.exempt(guild, obj["deleted_by"] or 0, cfg):
                msgs = await asyncio.to_thread(self.store.channel_message_count, obj["id"])
                await self._event(guild, "nuke_clone", {"channel_id": obj["id"], "name": obj["name"], "by": obj["deleted_by"],
                                                        "messages": msgs})
                if cfg["auto_restore"] and isinstance(channel, discord.TextChannel):
                    async with self._restoring(guild):
                        fresh = await asyncio.to_thread(self.store.get_object, guild.id, "channel", obj["id"])
                        if fresh is None or fresh["restored_as"]:
                            break  # somebody else restored it while we waited
                        self.store.set_restored(guild.id, "channel", obj["id"], channel.id)
                        stats: dict = {}
                        sent = await restore.repost_messages(guild, self.store, obj["id"], channel, stats=stats)
                    result = f"{sent} messages restored"
                    if stats.get("failed"):
                        result += f" • {stats['failed']} failed ({stats.get('error') or 'unknown'}) - retry in Backup Details"
                    await self._event(guild, "restore", {"name": obj["name"], "result": result})
                break

    def _wipe_hit(self, guild_id: int, channel_id: int, count: int) -> None:
        """Many messages deleted from one channel in a few seconds = wipe."""
        now = time.time()
        w = self.wipes.get(channel_id)
        if w is None or now - w["last"] > 10:
            w = self.wipes[channel_id] = {"guild": guild_id, "from": now, "last": now, "count": 0, "by": None, "fired": False}
        w["last"] = now
        w["count"] += count
        limit = config.AN_LIMITS["message_wipe"][0]
        if w["count"] >= limit and not w["fired"]:
            w["fired"] = True
            self._spawn(self._wipe_finish(guild_id, channel_id, w))

    async def _wipe_finish(self, guild_id: int, channel_id: int, w: dict) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        while time.time() - w["last"] < 4:  # wait until the deleting stops
            await asyncio.sleep(1)
        await self.flush()
        w["to"] = w["last"]
        if w["by"] is None:  # the audit log entry may arrive late: look for it
            try:
                async for e in guild.audit_logs(limit=10):
                    if e.action in (discord.AuditLogAction.message_bulk_delete, discord.AuditLogAction.message_delete) \
                            and (discord.utils.utcnow() - e.created_at).total_seconds() < 60:
                        w["by"] = e.user_id
                        break
            except discord.HTTPException:
                pass
        channel = guild.get_channel(channel_id)
        cfg = self.store.cfg(guild_id)
        saved = await asyncio.to_thread(self.store.deleted_in_window, channel_id, w["from"] - 2, w["to"] + 5)
        await self._event(guild, "message_wipe", {
            "channel_id": channel_id, "name": channel.name if channel else str(channel_id), "by": w["by"],
            "count": w["count"], "messages": saved, "from_ts": w["from"], "to_ts": w["to"]})
        big = w["count"] >= config.AN_WIPE_PUNISH_AT  # small purges by moderators are only logged
        if big and w["by"] and not self.exempt(guild, w["by"], cfg):
            await self.punish(guild, w["by"], "Anti-nuke: mass message delete")
            if cfg["auto_restore"] and channel is not None:
                sent = await restore.restore_wipe(guild, self.store, {"details": {
                    "channel_id": channel_id, "from_ts": w["from"], "to_ts": w["to"]}})
                await self._event(guild, "restore", {"name": channel.name, "result": f"{sent} messages restored"})
        self.wipes.pop(channel_id, None)

    # ------------------------------------------------------------------ controls used by the panel
    async def enable(self, guild: discord.Guild, on: bool) -> None:
        self.store.set_cfg(guild.id, enabled=int(on))
        if on:
            self.enabled.add(guild.id)
            self._spawn(self.backfill(guild))
        else:
            self.enabled.discard(guild.id)

    async def run_restore(self, guild: discord.Guild, channel: Optional[discord.abc.Messageable], what: str) -> None:
        st = self.state[guild.id]
        st["restore"] = {"text": "starting...", "running": True}

        async def progress(text: str) -> None:
            st["restore"]["text"] = text

        try:
            kind, _, ident = what.partition(":")
            async with self._restoring(guild):
                if kind == "ch":
                    rep = await restore.restore_channels(guild, self.store, [int(ident)], progress=progress)
                elif kind == "role":
                    made = await restore.restore_roles(guild, self.store, [int(ident)])
                    rep = {"roles": len(made), "channels": 0, "messages": 0}
                elif kind == "wipe":
                    event = self.store.get_event(guild.id, int(ident))
                    stats: dict = {}
                    rep = {"roles": 0, "channels": 0,
                           "messages": await restore.restore_wipe(guild, self.store, event, progress, stats) if event else 0,
                           "failed": stats.get("failed", 0), "error": stats.get("error", "")}
                else:
                    rep = await restore.restore_everything(guild, self.store, progress)
            text = f"{rep['roles']} roles, {rep['channels']} channels, {rep['messages']:,} messages restored"
            if rep.get("failed"):
                text += (f"\n⚠️ {rep['failed']:,} message(s) could not be posted ({rep.get('error') or 'unknown'}). "
                         "They are kept - open **Backup Details** and pick the channel again to retry.")
        except Exception as exc:
            log.exception("Restore failed")
            text = f"failed: {type(exc).__name__}"
        st["restore"] = {"text": text, "running": False}
        await self._event(guild, "restore", {"result": text})
        if channel is not None:
            try:
                await channel.send(f"{icon('ok')} **Restore finished:** {text}")
            except discord.HTTPException:
                pass

    async def backup_now(self, guild: discord.Guild) -> str:
        await self.flush()
        await self._tick_guild(guild, force=True)
        counts = await asyncio.to_thread(self.store.counts, guild.id)
        return (f"{counts['channels']} channels, {counts['roles']} roles, {counts['messages']:,} messages saved "
                f"({counts['snapshots']} structure versions).")

    # ------------------------------------------------------------------ antinuke.zip: refresh + upload
    async def send_archive(self, interaction: discord.Interaction) -> None:
        """Build a fresh FULL antinuke.zip (database + every saved file) and send it to the person (ephemeral).
        The webhook archive is refreshed as well (full, not only the newest files)."""
        guild = interaction.guild
        assert guild is not None
        await self.flush()
        if guild.id in self.enabled:
            try:
                await asyncio.wait_for(self._tick_guild(guild, force=True), 20)
            except Exception:
                log.exception("Structure backup before the archive failed")
        path = await asyncio.to_thread(nukeimport.build_full_archive, self.store, guild.id, Path(config.AN_FILES_DIR))
        try:
            size = min(ARCHIVE_PART, max(1024 * 1024, int(getattr(guild, "filesize_limit", 0) or ARCHIVE_PART) - 256 * 1024))
            total = path.stat().st_size
            parts = max(1, -(-total // size))
            if self.store.cfg(guild.id)["webhook_url"]:
                self.store.set_cfg(guild.id, last_archive_ts=0)  # the webhook posts a fresh full archive in a second
            if parts > 25:
                return await interaction.followup.send(
                    f"{icon('warn')} The archive is {total / 1048576:,.0f} MB - too big to send here. "
                    "The backup webhook keeps the full archive.", ephemeral=True)
            await interaction.followup.send(
                f"{icon('ok')} **antinuke.zip** refreshed ({total / 1048576:,.1f} MB"
                + (f", {parts} parts - upload all parts together in one message" if parts > 1 else "") + "). "
                "Keep it private: it contains every saved message and file of this server.", ephemeral=True)
            with open(path, "rb") as fh:
                for i in range(1, parts + 1):
                    name = "antinuke.zip" if parts == 1 else f"antinuke.zip.part{i}"
                    await interaction.followup.send(file=discord.File(io.BytesIO(fh.read(size)), filename=name),
                                                    ephemeral=True)
        finally:
            path.unlink(missing_ok=True)

    async def upload_archive(self, interaction: discord.Interaction) -> None:
        """Wait for an antinuke.zip attachment, merge it into the backup and restore everything that is missing."""
        guild, user = interaction.guild, interaction.user
        assert guild is not None
        key = (interaction.channel_id or 0, user.id)
        if key in self.upload_wait:
            return await interaction.response.send_message(f"{icon('hourglass')} Already waiting for your file.", ephemeral=True)
        await interaction.response.send_message(
            f"{icon('file')} Send your **antinuke.zip** now: attach it to a message in this channel (within 3 minutes). "
            "A zip that came in parts (`.part1`, `.part2`...) - attach all parts to the same message. "
            "I delete your message right after reading it.", ephemeral=True)

        def check(m: discord.Message) -> bool:
            return m.author.id == user.id and m.channel.id == interaction.channel_id and bool(m.attachments)

        self.upload_wait.add(key)
        msgs: list[discord.Message] = []
        try:
            try:
                msgs.append(await self.bot.wait_for("message", check=check, timeout=180))
            except asyncio.TimeoutError:
                return await interaction.followup.send(f"{icon('error')} Timed out - press the button again.", ephemeral=True)
            if any(nukeimport.PART_RE.search(a.filename) for a in msgs[0].attachments):
                while True:  # parts may also come in several messages
                    try:
                        msgs.append(await self.bot.wait_for("message", check=check, timeout=20))
                    except asyncio.TimeoutError:
                        break
            tmpdir = Path(tempfile.mkdtemp(prefix="antinuke_up_"))
            try:
                saved: list[tuple[str, Path]] = []
                for m in msgs:
                    for a in m.attachments:
                        if a.size > 300 * 1024 * 1024:
                            await interaction.followup.send(f"{icon('error')} `{a.filename}` is too big.", ephemeral=True)
                            continue
                        dest = tmpdir / f"{len(saved)}_{re.sub(r'[^A-Za-z0-9._-]', '_', a.filename)[:80]}"
                        await a.save(dest)
                        saved.append((a.filename, dest))
                for m in msgs:  # the archive holds the whole server: do not leave it in the channel
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
                if not saved:
                    return await interaction.followup.send(f"{icon('error')} No usable file received.", ephemeral=True)
                await interaction.followup.send(f"{icon('hourglass')} Reading the archive...", ephemeral=True)
                archives = await asyncio.to_thread(nukeimport.group_uploads, saved)
                allow_other = await self.bot.is_owner(user)
                total = {"messages": 0, "channels_missing": 0, "roles_missing": 0, "files": 0}
                for arc in archives:
                    try:
                        rep = await asyncio.to_thread(
                            nukeimport.import_archive, self.store, arc, guild.id, Path(config.AN_FILES_DIR),
                            channel_ids={c.id for c in guild.channels}, role_ids={r.id for r in guild.roles},
                            allow_other_server=allow_other)
                    except nukeimport.ArchiveError as exc:
                        await interaction.followup.send(f"{icon('error')} {exc}", ephemeral=True)
                        continue
                    except Exception:
                        log.exception("Archive import failed")
                        await interaction.followup.send(f"{icon('error')} Could not read this archive.", ephemeral=True)
                        continue
                    for k in total:
                        total[k] += rep.get(k, 0)
                    self.known.pop(guild.id, None)  # structure cache: reload it from the merged database
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
        finally:
            self.upload_wait.discard(key)

        if not (total["messages"] or total["channels_missing"] or total["roles_missing"] or total["files"]):
            return await interaction.followup.send(f"{icon('ok')} Nothing new in this archive - everything in it was already saved.", ephemeral=True)
        line = (f"{icon('ok')} Archive loaded: **{total['messages']:,}** messages, **{total['files']:,}** files, "
                f"**{total['channels_missing']}** missing channel(s), **{total['roles_missing']}** missing role(s).")
        st = self.state[guild.id]
        if st["restore"] and st["restore"]["running"]:
            return await interaction.followup.send(line + f"\n{icon('hourglass')} A restore is already running - "
                                                   "open **Backup details** afterwards for the rest.", ephemeral=True)
        await interaction.followup.send(line + f"\n{icon('restore')} Restoring everything that is missing now. "
                                        "I post here when it is finished.", ephemeral=True)
        self._spawn(self.run_restore(guild, interaction.channel, "all"))  # type: ignore[arg-type]

    # ------------------------------------------------------------------ panel
    def panel_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.store.cfg(guild.id)
        st = self.state[guild.id]
        counts = self.store.counts(guild.id)
        on = guild.id in self.enabled
        e = discord.Embed(title=f"{icon('shield')} Anti-Nuke panel", color=0x2ECC71 if on else 0xE74C3C)
        e.description = (
            f"{icon('dot_green') if on else icon('dot_red')} **Protection:** {'ON' if on else 'OFF'}\n"
            f"{icon('webhook')} **Backup webhook:** {'connected' if cfg['webhook_url'] else 'not set'}"
            + (f" • {icon('warn')} `{st['wh_error']}`" if st["wh_error"] else "") + "\n"
            f"{icon('stopwatch')} **Last backup:** {ts_fmt(st['last_tick']) if st['last_tick'] else 'not yet'} "
            f"(every {config.AN_TICK_SECONDS}s)\n"
            f"{icon('gavel')} **Punishment:** {PUNISH[cfg['punishment']]}\n"
            f"{icon('recycle')} **Auto restore:** {'on' if cfg['auto_restore'] else 'off'}\n"
            f"{icon('users')} **Trusted users:** {len(cfg['trusted_ids'])}")
        e.add_field(name=f"{icon('database')} Backup", value=(
            f"Channels **{counts['channels']}** • Roles **{counts['roles']}**\n"
            f"Messages **{counts['messages']:,}** (deleted, still kept: {counts['deleted_messages']:,})\n"
            f"Structure versions **{counts['snapshots']}**"), inline=True)
        e.add_field(name=f"{icon('lock')} Rules", value=(
            "Delete a channel (not VC) = **2 hour cooldown**\nSecond delete in 2h = punish + restore\n"
            "Mass ban/kick • role/channel/webhook spam • bot add • message wipes"), inline=True)
        bf = st["backfill"]
        if bf:
            e.add_field(name=f"{icon('clipboard')} History backfill",
                        value=f"{bf['done']}/{bf['total']} channels • {bf['messages']:,} messages"
                              + (" • running" if bf["running"] else " • done"), inline=False)
        if st["restore"]:
            e.add_field(name=f"{icon('restore')} Restore", value=st["restore"]["text"][:200], inline=False)
        if counts["deleted_channels"]:
            e.add_field(name=f"{icon('warn')} Deleted channels waiting", value=f"**{counts['deleted_channels']}** "
                        "- open **Backup Details** to restore", inline=False)
        e.set_footer(text="Everything is saved in real time. Only the owner / trusted users can use this panel.")
        return e

    async def open_panel(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        view = PanelView(self, ctx.guild, ctx.author.id)
        view.message = await ctx.send(embed=self.panel_embed(ctx.guild), view=view)

    @commands.hybrid_command(name="antinuke", aliases=["an"], description="Anti-nuke backup & protection panel (owner / trusted only).")
    @commands.guild_only()
    async def antinuke(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        if not await self.can_manage(ctx.guild, ctx.author):
            raise commands.CheckFailure("not allowed")  # errors cog: silent for prefix commands
        await self.open_panel(ctx)


# ====================================================================== views
class Gate(discord.ui.View):
    """Base: only people allowed to manage anti-nuke can press anything."""

    cog: AntiNuke
    guild: discord.Guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if await self.cog.can_manage(self.guild, interaction.user):
            return True
        await interaction.response.send_message(f"{icon('lock')} Not allowed.", ephemeral=True)
        return False


class PanelView(Gate):
    def __init__(self, cog: AntiNuke, guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=900)
        self.cog, self.guild, self.author_id = cog, guild, author_id
        self.message: Optional[discord.Message] = None
        self._sync()

    def _sync(self) -> None:
        on = self.guild.id in self.cog.enabled
        self.toggle.label = "Disable protection" if on else "Enable protection"
        self.toggle.style = discord.ButtonStyle.danger if on else discord.ButtonStyle.success
        self.toggle.emoji = icon("power") or None

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.cog.panel_embed(self.guild), view=self)

    @discord.ui.button(label="Enable protection", row=0)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.enable(self.guild, self.guild.id not in self.cog.enabled)
        await self._refresh(interaction)

    @discord.ui.button(label="Set webhook", style=discord.ButtonStyle.primary, row=0, emoji=icon("webhook") or None)
    async def set_webhook(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(WebhookModal(self))

    @discord.ui.button(label="Backup now", style=discord.ButtonStyle.secondary, row=0, emoji=icon("save") or None)
    async def backup_now(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        if self.guild.id not in self.cog.enabled:
            return await interaction.followup.send(f"{icon('warn')} Enable protection first.", ephemeral=True)
        text = await self.cog.backup_now(self.guild)
        await interaction.followup.send(f"{icon('ok')} Backup done: {text}", ephemeral=True)

    @discord.ui.button(label="Backup details & restore", style=discord.ButtonStyle.primary, row=1, emoji=icon("database") or None)
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = DetailsView(self.cog, self.guild)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, row=1, emoji=icon("gear") or None)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = SettingsView(self.cog, self.guild, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=1, emoji=icon("recycle") or None)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._refresh(interaction)

    @discord.ui.button(label="Upload antinuke.zip", style=discord.ButtonStyle.success, row=2, emoji=icon("folder") or None)
    async def upload_zip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.upload_archive(interaction)

    @discord.ui.button(label="Refresh antinuke.zip", style=discord.ButtonStyle.secondary, row=2, emoji=icon("file") or None)
    async def refresh_zip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.send_archive(interaction)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class WebhookModal(discord.ui.Modal, title="Backup webhook"):
    url = discord.ui.TextInput(label="Webhook link (leave empty to remove)", required=False, max_length=300,
                               placeholder="https://discord.com/api/webhooks/...")

    def __init__(self, panel: PanelView) -> None:
        super().__init__()
        self.panel = panel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog, guild = self.panel.cog, self.panel.guild
        value = (self.url.value or "").strip()
        if not value:
            cog.store.set_cfg(guild.id, webhook_url="", webhook_msg_id=0)
            return await interaction.response.send_message(f"{icon('ok')} Webhook removed.", ephemeral=True)
        if not WEBHOOK_RE.fullmatch(value):
            return await interaction.response.send_message(f"{icon('error')} That is not a Discord webhook link.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            hook = discord.Webhook.from_url(value, session=cog.session)  # type: ignore[arg-type]
            await hook.send(f"✅ **{config.BOT_NAME} Anti-Nuke** connected. Live backups will appear here.",
                            username=f"{config.BOT_NAME} Anti-Nuke")
        except (discord.HTTPException, ValueError):
            return await interaction.followup.send(f"{icon('error')} Discord refused that webhook. Check the link.", ephemeral=True)
        cog.store.set_cfg(guild.id, webhook_url=value, webhook_msg_id=0, last_archive_ts=0)
        cog.state[guild.id]["wh_retry"] = 0
        await interaction.followup.send(f"{icon('ok')} Webhook saved. The backup is now mirrored there every second.", ephemeral=True)


class DetailsView(Gate):
    def __init__(self, cog: AntiNuke, guild: discord.Guild) -> None:
        super().__init__(timeout=600)
        self.cog, self.guild = cog, guild
        self.chans = cog.store.objects(guild.id, "channel", deleted=True)
        self.chans = [c for c in self.chans if c["data"]["type"] not in VOICE_TYPES]
        self.roles = [r for r in cog.store.objects(guild.id, "role", deleted=True)
                      if not r["data"]["managed"] and not r["data"]["default"]]
        self.wipes = [e for e in cog.store.events(guild.id, 50) if e["type"] == "message_wipe"][:10]
        # channels that are back already, but still have saved messages that were not posted (failed / interrupted)
        self.retry = []
        for o in cog.store.objects(guild.id, "channel"):
            if o["deleted_at"] and o["restored_as"] and o["data"]["type"] not in VOICE_TYPES \
                    and guild.get_channel(o["restored_as"]) is not None:
                left = cog.store.pending_message_count(o["id"])
                if left:
                    self.retry.append((o, left))
        options = []
        for c in self.chans[:10]:
            options.append(discord.SelectOption(
                label=f"#{c['name']}"[:100], value=f"ch:{c['id']}", emoji=icon("chat") or None,
                description=f"Deleted channel • {cog.store.channel_message_count(c['id']):,} messages saved"))
        for o, left in self.retry[:5]:
            options.append(discord.SelectOption(
                label=f"Retry messages: #{o['name']}"[:100], value=f"ch:{o['id']}", emoji=icon("restore") or None,
                description=f"Channel is back • {left:,} messages still to post"))
        for r in self.roles[:5]:
            options.append(discord.SelectOption(label=f"@{r['name']}"[:100], value=f"role:{r['id']}",
                                                emoji=icon("mask") or None, description="Deleted role"))
        for e in self.wipes[:5]:
            d = e["details"]
            options.append(discord.SelectOption(label=f"Wipe #{d.get('name', '?')}"[:100], value=f"wipe:{e['id']}",
                                                emoji=icon("trash") or None,
                                                description=f"{d.get('count', 0):,} messages deleted"))
        self.pick.options = options[:25] or [discord.SelectOption(label="Nothing to restore", value="none")]
        self.pick.disabled = not options
        self.restore_all.emoji = icon("restore") or None

    def embed(self) -> discord.Embed:
        e = discord.Embed(title=f"{icon('database')} Backup details", color=0x3498DB)
        lines = []
        for c in self.chans[:10]:
            by = f" by <@{c['deleted_by']}>" if c["deleted_by"] else ""
            lines.append(f"{icon('chat')} **#{c['name']}** deleted {ts_fmt(c['deleted_at'] or 0)}{by} • "
                         f"{self.cog.store.channel_message_count(c['id']):,} messages saved")
        for r in self.roles[:5]:
            lines.append(f"{icon('mask')} **@{r['name']}** deleted {ts_fmt(r['deleted_at'] or 0)}")
        for o, left in self.retry[:5]:
            lines.append(f"{icon('restore')} **#{o['name']}** is back • **{left:,}** saved messages still to post")
        e.add_field(name="Deleted (restorable)", value="\n".join(lines)[:1024] or "Nothing is missing.", inline=False)
        ev = []
        for x in self.cog.store.events(self.guild.id, 8):
            d = x["details"]
            ev.append(f"{ts_fmt(x['ts'])} **{EVENT_TITLES.get(x['type'], x['type'])}** "
                      f"{d.get('name') or ''} {('by <@%s>' % d['by']) if d.get('by') else ''}".strip())
        e.add_field(name="Recent events", value="\n".join(ev)[:1024] or "No events yet.", inline=False)
        counts = self.cog.store.counts(self.guild.id)
        e.set_footer(text=f"{counts['messages']:,} messages • {counts['channels']} channels • "
                          f"{counts['roles']} roles • {counts['snapshots']} structure versions saved")
        return e

    @discord.ui.select(placeholder="Pick what to restore...", row=0, options=[discord.SelectOption(label="-", value="none")])
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        what = select.values[0]
        if what == "none":
            return await interaction.response.defer()
        await interaction.response.send_message(f"{icon('warn')} Restore this now?", view=ConfirmView(self.cog, self.guild, what),
                                                ephemeral=True)

    @discord.ui.button(label="Restore everything missing", style=discord.ButtonStyle.danger, row=1)
    async def restore_all(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(
            f"{icon('warn')} Restore **all** deleted channels, roles and wiped messages?",
            view=ConfirmView(self.cog, self.guild, "all"), ephemeral=True)


class ConfirmView(Gate):
    def __init__(self, cog: AntiNuke, guild: discord.Guild, what: str) -> None:
        super().__init__(timeout=60)
        self.cog, self.guild, self.what = cog, guild, what
        self.yes.emoji = icon("ok") or None
        self.no.emoji = icon("error") or None

    @discord.ui.button(label="Restore", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if self.cog.state[self.guild.id]["restore"] and self.cog.state[self.guild.id]["restore"]["running"]:
            return await interaction.response.edit_message(content=f"{icon('hourglass')} A restore is already running.", view=None)
        await interaction.response.edit_message(content=f"{icon('restore')} Restore started. I will post here when it is done.", view=None)
        self.cog._spawn(self.cog.run_restore(self.guild, interaction.channel, self.what))  # type: ignore[arg-type]

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


class SettingsView(Gate):
    def __init__(self, cog: AntiNuke, guild: discord.Guild, user_id: int) -> None:
        super().__init__(timeout=600)
        self.cog, self.guild = cog, guild
        cfg = cog.store.cfg(guild.id)
        self.punish_select.options = [
            discord.SelectOption(label=text, value=key, default=key == cfg["punishment"]) for key, text in PUNISH.items()]
        self._sync(cfg)

    def _sync(self, cfg: dict) -> None:
        self.auto.label = f"Auto restore: {'ON' if cfg['auto_restore'] else 'OFF'}"
        self.auto.style = discord.ButtonStyle.success if cfg["auto_restore"] else discord.ButtonStyle.secondary
        self.auto.emoji = icon("recycle") or None

    def embed(self) -> discord.Embed:
        cfg = self.cog.store.cfg(self.guild.id)
        trusted = ", ".join(f"<@{i}>" for i in cfg["trusted_ids"]) or "nobody"
        return discord.Embed(title=f"{icon('gear')} Anti-nuke settings", color=0x3498DB, description=(
            f"**Punishment:** {PUNISH[cfg['punishment']]}\n**Trusted users** (never punished, can open this panel): {trusted}\n"
            "Only the server owner / bot owner can change the trusted list."))

    @discord.ui.select(placeholder="Punishment for nukers", row=0, options=[discord.SelectOption(label="-", value="-")])
    async def punish_select(self, interaction: discord.Interaction, select: discord.ui.Select) -> None:
        self.cog.store.set_cfg(self.guild.id, punishment=select.values[0])
        await interaction.response.edit_message(embed=self.embed())

    @discord.ui.button(label="Auto restore", row=1)
    async def auto(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = self.cog.store.cfg(self.guild.id)
        self.cog.store.set_cfg(self.guild.id, auto_restore=int(not cfg["auto_restore"]))
        self._sync(self.cog.store.cfg(self.guild.id))
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.select(cls=discord.ui.UserSelect, placeholder="Trusted users (replaces the list)", min_values=0,
                       max_values=10, row=2)
    async def trusted(self, interaction: discord.Interaction, select: discord.ui.UserSelect) -> None:
        if interaction.user.id != self.guild.owner_id and not await self.cog.bot.is_owner(interaction.user):
            return await interaction.response.send_message(f"{icon('lock')} Only the server owner can do this.", ephemeral=True)
        ids = ",".join(str(u.id) for u in select.values if not u.bot)
        self.cog.store.set_cfg(self.guild.id, trusted=ids)
        await interaction.response.edit_message(embed=self.embed(), view=self)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiNuke(bot))
