"""Rebuild a server from the anti-nuke backup: roles, categories, channels and every saved message
(posted back with the original name + profile picture)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

import discord

from utils.nukestore import VOICE_TYPES, Store

log = logging.getLogger("premium-bot.antinuke.restore")

DEFAULT_FILE_LIMIT = 8 * 1024 * 1024  # used when the server upload limit is unknown
Progress = Callable[[str], Awaitable[None]]

def _why(exc: BaseException) -> str:
    bits = [type(exc).__name__, str(getattr(exc, "status", "") or ""), str(getattr(exc, "code", "") or "")]
    text = str(getattr(exc, "text", "") or "")[:80]
    return " ".join(b for b in bits if b) + (f" ({text})" if text else "")


def _chunks(text: str, size: int = 1990) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def _clean_embed(raw: dict) -> Optional[discord.Embed]:
    """Only real (rich) embeds are re-sent. Link previews / gif previews are generated again by Discord
    from the URL inside the message text, and re-sending them makes Discord reject the whole message."""
    if not isinstance(raw, dict) or raw.get("type", "rich") != "rich":
        return None
    if not any(raw.get(k) for k in ("title", "description", "fields", "image", "thumbnail", "author", "footer")):
        return None
    try:
        return discord.Embed.from_dict(raw)
    except Exception:
        return None


async def restore_roles(guild: discord.Guild, store: Store, role_ids: Optional[list[int]] = None,
                        reason: str = "Anti-nuke restore") -> dict[int, int]:
    """Recreate deleted roles. Returns old id -> new id."""
    made: dict[int, int] = {}
    objs = store.objects(guild.id, "role", deleted=True)
    for obj in sorted(objs, key=lambda o: o["data"]["position"]):
        if role_ids is not None and obj["id"] not in role_ids:
            continue
        d = obj["data"]
        if d["managed"] or d["default"]:
            continue
        try:
            role = await guild.create_role(
                name=d["name"], colour=discord.Colour(d["color"]), hoist=d["hoist"], mentionable=d["mentionable"],
                permissions=discord.Permissions(d["permissions"]), reason=reason)
            top = guild.me.top_role.position - 1
            if top > 0:
                try:
                    await role.edit(position=max(1, min(d["position"], top)), reason=reason)
                except discord.HTTPException:
                    pass
            store.set_restored(guild.id, "role", obj["id"], role.id)
            made[obj["id"]] = role.id
        except discord.HTTPException:
            log.warning("Could not restore role %s", d["name"])
    return made


def role_map(guild: discord.Guild, store: Store) -> dict[int, int]:
    """old role id -> role id that exists now (itself, or the restored copy)."""
    mapping = {r.id: r.id for r in guild.roles}
    for obj in store.objects(guild.id, "role"):
        if obj["restored_as"]:
            mapping[obj["id"]] = obj["restored_as"]
        elif obj["data"].get("default"):
            mapping[obj["id"]] = guild.default_role.id  # @everyone of a backup from another server
    return mapping


def _overwrites(guild: discord.Guild, d: dict, roles: dict[int, int]) -> dict:
    result = {}
    for ow in d["overwrites"]:
        if ow["type"] == "role":
            target = guild.get_role(roles.get(ow["id"], 0))
        else:
            target = guild.get_member(ow["id"])
        if target is not None:
            result[target] = discord.PermissionOverwrite.from_pair(
                discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"]))
    return result


async def _create_channel(guild: discord.Guild, d: dict, category, roles: dict[int, int], reason: str):
    kind = discord.ChannelType(d["type"])
    common = {"name": d["name"], "overwrites": _overwrites(guild, d, roles), "reason": reason}
    if kind is discord.ChannelType.category:
        return await guild.create_category(**common)
    if category is not None:
        common["category"] = category
    if kind is discord.ChannelType.voice:
        bitrate = min(d.get("bitrate") or 64000, guild.bitrate_limit)
        return await guild.create_voice_channel(bitrate=int(bitrate), user_limit=d.get("user_limit") or 0, **common)
    if kind is discord.ChannelType.stage_voice:
        return await guild.create_stage_channel(**common)
    if kind is discord.ChannelType.forum:
        return await guild.create_forum(topic=d.get("topic") or None, nsfw=bool(d.get("nsfw")), **common)
    return await guild.create_text_channel(topic=d.get("topic") or None, nsfw=bool(d.get("nsfw")),
                                           slowmode_delay=d.get("slowmode") or 0, **common)


def find_clone(guild: discord.Guild, obj: dict, exclude: frozenset | set = frozenset()) -> Optional[discord.abc.GuildChannel]:
    """A channel with the same name/type that was created after the original was deleted (nuke 'clone').
    A channel that already is the restored copy of another object is never reused."""
    deleted_at = obj.get("deleted_at") or 0
    imported = obj.get("deleted_by") == 0
    for ch in guild.channels:
        if ch.id in exclude:
            continue
        if ch.name == obj["data"]["name"] and ch.type.value == obj["data"]["type"] \
                and (imported or ch.created_at.timestamp() >= deleted_at - 5):
            return ch
    return None


def _new_report() -> dict:
    return {"channels": 0, "messages": 0, "roles": 0, "failed": 0, "error": ""}


async def restore_channels(guild: discord.Guild, store: Store, channel_ids: Optional[list[int]] = None, *,
                           messages: bool = True, progress: Optional[Progress] = None,
                           reason: str = "Anti-nuke restore") -> dict:
    """Recreate deleted channels (categories first) and repost their saved messages.
    Channels that were already recreated but still have saved messages waiting are retried too."""
    report = _new_report()
    stats = {"failed": 0, "error": ""}
    pending = [o for o in store.objects(guild.id, "channel", deleted=True)
               if channel_ids is None or o["id"] in channel_ids]

    retry: list[tuple[dict, discord.abc.GuildChannel]] = []
    if messages:
        for o in store.objects(guild.id, "channel"):
            if not (o["deleted_at"] and o["restored_as"]) or o["data"]["type"] in VOICE_TYPES:
                continue
            if channel_ids is not None and o["id"] not in channel_ids:
                continue
            target = guild.get_channel(o["restored_as"])
            if target is not None and store.pending_message_count(o["id"]) > 0:
                retry.append((o, target))

    if not pending and not retry:
        return report

    if pending:
        roles = role_map(guild, store)
        if any(not guild.get_role(roles.get(ow["id"], 0)) for o in pending for ow in o["data"]["overwrites"]
               if ow["type"] == "role"):
            report["roles"] = len(await restore_roles(guild, store, None, reason))  # roles deleted too
            roles = role_map(guild, store)

        cat_map: dict[int, int] = {}
        for obj in store.objects(guild.id, "channel"):
            if obj["restored_as"]:
                cat_map[obj["id"]] = obj["restored_as"]
        order = sorted(pending, key=lambda o: (o["data"]["type"] != discord.ChannelType.category.value,
                                               o["data"]["position"]))
        for obj in order:
            d = obj["data"]
            channel = find_clone(guild, obj, set(cat_map.values()))
            if channel is None:
                parent_id = d.get("category_id")
                category = guild.get_channel(cat_map.get(parent_id, parent_id) or 0) if parent_id else None
                try:
                    channel = await _create_channel(guild, d, category, roles, reason)
                    try:
                        await channel.edit(position=d["position"], reason=reason)
                    except discord.HTTPException:
                        pass
                    report["channels"] += 1
                except discord.HTTPException:
                    log.warning("Could not restore channel %s", d["name"])
                    continue
            store.set_restored(guild.id, "channel", obj["id"], channel.id)
            cat_map[obj["id"]] = channel.id
            if progress:
                await progress(f"Channel **{d['name']}** restored")
            if messages and isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
                report["messages"] += await repost_messages(guild, store, obj["id"], channel,
                                                            progress=progress, stats=stats)

    for obj, channel in retry:
        if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
            if progress:
                await progress(f"Retrying messages of **{obj['name']}**")
            report["messages"] += await repost_messages(guild, store, obj["id"], channel,
                                                        progress=progress, stats=stats)
    report["failed"], report["error"] = stats["failed"], stats["error"]
    return report


def _load_files(row: dict, limit: int) -> tuple[list[tuple[bytes, str, str]], list[str]]:
    """(files that can be re-uploaded, urls of everything else). Runs in a thread (reads from disk)."""
    files: list[tuple[bytes, str, str]] = []
    links: list[str] = []
    total = 0
    try:
        atts = json.loads(row.get("attachments") or "[]")
    except (TypeError, ValueError):
        atts = []
    for att in atts:
        data = None
        path = Path(att["local"]) if att.get("local") else None
        try:
            if path and path.exists() and len(files) < 10 and total + path.stat().st_size <= limit:
                data = path.read_bytes()
        except OSError:
            data = None
        if data is not None:
            files.append((data, att["filename"], att.get("url", "")))
            total += len(data)
        elif att.get("url"):
            links.append(att["url"])
    return files, links


IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
EMBED_TEXT = 4000


def _safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name or "file")[-100:] or "file"
    return name


def _hook_name(name: str) -> str:
    """Webhook usernames: 1-80 chars and must not contain 'discord' / 'clyde' (Discord rejects them)."""
    name = (name or "Unknown").strip() or "Unknown"
    name = re.sub(r"(?i)discord", lambda m: m.group(0).replace("i", "\u0456").replace("I", "\u0406"), name)
    name = re.sub(r"(?i)clyde", lambda m: m.group(0).replace("c", "\u0441").replace("C", "\u0421"), name)
    return name[:80]


class _Poster:
    """Posts one saved message back into the restored channel AS THE ORIGINAL PERSON: a temporary channel
    webhook is used, so every message shows the original name + profile picture and the original text
    (plus original embeds / files). The webhook is deleted again when the channel is done.
    If the bot may not create webhooks (no Manage Webhooks) it falls back to bot-posted embeds."""

    def __init__(self, channel: discord.abc.Messageable) -> None:
        self.channel = channel
        self.hook: Optional[discord.Webhook] = None
        self.hook_failed = False
        self.error = ""

    @property
    def chunk(self) -> int:
        """Max text per message: webhook messages hold 2000 characters, embed fallback 4000."""
        return 1990 if self.hook is not None else EMBED_TEXT

    async def prepare(self) -> None:
        if self.hook is not None or self.hook_failed:
            return
        try:
            self.hook = await self.channel.create_webhook(  # type: ignore[attr-defined]
                name="Restore", reason="Anti-nuke restore: posting saved messages as their authors")
        except Exception as exc:  # no permission / channel type without webhooks
            self.hook_failed = True
            self.error = _why(exc)
            log.warning("Webhook for restore could not be created (%s): using embeds instead", self.error)

    @staticmethod
    def _embed(author: str, avatar: Optional[str], ts: Optional[float], text: str,
               image: Optional[str]) -> discord.Embed:
        embed = discord.Embed(description=text or None, colour=0x2B2D31)
        if ts:
            embed.timestamp = datetime.fromtimestamp(float(ts), timezone.utc)
        embed.set_author(name=(author or "Unknown")[:256], icon_url=avatar or None)
        if image:
            embed.set_image(url=f"attachment://{image}")
        return embed

    async def close(self) -> None:
        if self.hook is not None:
            try:
                await self.hook.delete(reason="Anti-nuke restore finished")
            except discord.HTTPException:
                pass
            self.hook = None

    async def _via_webhook(self, author: str, avatar: Optional[str], content: str,
                           embeds: list[discord.Embed], files: list[tuple[bytes, str, str]]) -> bool:
        """Same name + same avatar + same text. Tries: with embeds+files, without embeds, links instead of files."""
        steps = [(True, True)]
        if embeds:
            steps.append((False, True))
        if files:
            steps.append((False, False))
        seen: set[tuple] = set()
        for with_embeds, with_files in steps:
            key = (with_embeds and bool(embeds), with_files and bool(files))
            if key in seen:
                continue
            seen.add(key)
            text = content
            if files and not with_files:  # files could not go along: keep their links in the text
                text = (text + "\n" + "\n".join(u for _, _, u in files if u)).strip()
            use_embeds = embeds[:10] if with_embeds else []
            use_files = files if with_files else []
            if not text and not use_embeds and not use_files:
                continue
            kwargs: dict = {"username": _hook_name(author), "allowed_mentions": discord.AllowedMentions.none()}
            if avatar:
                kwargs["avatar_url"] = avatar
            if text:
                kwargs["content"] = text[:2000]
            if use_embeds:
                kwargs["embeds"] = use_embeds
            if use_files:
                kwargs["files"] = [discord.File(io.BytesIO(b), filename=_safe_filename(n)) for b, n, _ in use_files]
            try:
                await self.hook.send(**kwargs)  # type: ignore[union-attr]
                return True
            except discord.NotFound:  # the webhook (or channel) is gone: stop using it
                self.hook, self.hook_failed = None, True
                return False
            except discord.HTTPException as exc:
                self.error = _why(exc)
            except Exception as exc:  # bad data: try the next, simpler step
                self.error = _why(exc)
        return False

    async def deliver(self, author: str, avatar: Optional[str], ts: Optional[float], content: str,
                      extra_embeds: list[discord.Embed], files: list[tuple[bytes, str, str]]) -> bool:
        await self.prepare()
        if self.hook is not None and await self._via_webhook(author, avatar, content, extra_embeds, files):
            return True
        return await self._deliver_embed(author, avatar, ts, content, extra_embeds, files)

    async def _deliver_embed(self, author: str, avatar: Optional[str], ts: Optional[float], content: str,
                             extra_embeds: list[discord.Embed], files: list[tuple[bytes, str, str]]) -> bool:
        """Fallback when no webhook can be used: the bot posts an embed with the author's name + avatar."""
        names = [_safe_filename(n) for _, n, _ in files]
        image = next((n for n in names if n.lower().endswith(IMAGE_EXT)), None)
        plans = [(True, True, True)]  # (original embeds, attachments, picture inside the embed)
        if extra_embeds:
            plans.append((False, True, True))
        if files:
            plans.append((False, False, False))
        plans.append((None, None, None))  # plain text, last resort
        seen: set[tuple] = set()
        for with_embeds, with_files, with_image in plans:
            key = (with_embeds and bool(extra_embeds), with_files and bool(files), with_image and bool(image))
            if with_embeds is not None and key in seen:
                continue
            seen.add(key)
            text = content
            if files and not with_files:
                text = (text + "\n" + "\n".join(u for _, _, u in files if u)).strip()
            try:
                kwargs: dict = {"allowed_mentions": discord.AllowedMentions.none()}
                if with_embeds is None:
                    when = f"<t:{int(ts)}:f>" if ts else ""
                    kwargs["content"] = f"**{author}** {when}\n{text}"[:2000]
                else:
                    embeds = [self._embed(author, avatar, ts, text[:EMBED_TEXT], image if with_image else None)]
                    if with_embeds:
                        embeds += extra_embeds[:9]
                    kwargs["embeds"] = embeds
                    if with_files and files:
                        kwargs["files"] = [discord.File(io.BytesIO(b), filename=n) for (b, _, _), n in zip(files, names)]
                await self.channel.send(**kwargs)  # type: ignore[attr-defined]
                return True
            except discord.HTTPException as exc:
                self.error = _why(exc)
                if getattr(exc, "status", 0) == 404:
                    return False  # the channel is gone
            except Exception as exc:  # bad data: try the next, simpler plan
                self.error = _why(exc)
        return False


async def repost_messages(guild: discord.Guild, store: Store, source_channel_id: int,
                          channel: discord.abc.Messageable, *, since: Optional[float] = None,
                          until: Optional[float] = None, progress: Optional[Progress] = None,
                          stats: Optional[dict] = None) -> int:
    """Post saved messages of `source_channel_id` into `channel`, oldest first, with the original
    name + avatar (posted through a temporary webhook; embeds only if no webhook is possible). Restored messages are marked and skipped; failed ones stay queued for a retry.
    `stats` (optional dict) receives 'failed' (count) and 'error' (last reason)."""
    stats = stats if stats is not None else {}
    stats.setdefault("failed", 0)
    stats.setdefault("error", "")
    limit = int(getattr(guild, "filesize_limit", 0) or DEFAULT_FILE_LIMIT)
    poster = _Poster(channel)
    await poster.prepare()  # webhook first, so the text is split to the right size
    sent, after = 0, 0
    try:
        while True:
            rows = await asyncio.to_thread(store.channel_rows, source_channel_id, only_deleted_since=since,
                                           only_deleted_until=until, after_id=after, limit=200)
            if not rows:
                break
            done_ids: list[int] = []
            for row in rows:
                after = row["message_id"]
                try:
                    files, links = await asyncio.to_thread(_load_files, row, limit)
                    text = row["content"] or ""
                    if links:
                        text += ("\n" if text else "") + "\n".join(links)
                    try:
                        raw_embeds = json.loads(row["embeds"] or "[]")
                    except (TypeError, ValueError):
                        raw_embeds = []
                    embeds = [e for e in (_clean_embed(x) for x in raw_embeds) if e][:10]
                    if not text and not embeds and not files:
                        done_ids.append(row["message_id"])
                        continue
                    parts = _chunks(text, poster.chunk)
                    ok = True
                    for n, part in enumerate(parts):
                        last = n == len(parts) - 1
                        posted = await poster.deliver(row["author_name"] or "Unknown", row["author_avatar"],
                                                      row.get("created_at"), part,
                                                      embeds if last else [], files if last else [])
                        if not posted:
                            ok = n > 0  # first part posted = count it done (a retry would duplicate it)
                            break
                    if ok:
                        sent += 1
                        done_ids.append(row["message_id"])
                    else:
                        stats["failed"] += 1
                        stats["error"] = poster.error or stats["error"]
                        log.warning("Could not repost message %s: %s", row["message_id"], poster.error)
                except Exception as exc:
                    stats["failed"] += 1
                    stats["error"] = _why(exc)
                    log.exception("Unexpected error while reposting message %s", row.get("message_id"))
            await asyncio.to_thread(store.mark_restored, done_ids)
            if progress:
                await progress(f"{sent} messages restored in <#{getattr(channel, 'id', 0)}>")
            if len(rows) < 200:
                break
    finally:
        await poster.close()
    return sent


async def restore_wipe(guild: discord.Guild, store: Store, event: dict, progress: Optional[Progress] = None,
                       stats: Optional[dict] = None) -> int:
    """Repost the messages that were mass-deleted from a channel that still exists."""
    d = event["details"]
    channel = guild.get_channel(d["channel_id"])
    if channel is None:
        obj = store.get_object(guild.id, "channel", d["channel_id"])
        channel = guild.get_channel(obj["restored_as"]) if obj and obj["restored_as"] else None
    if channel is None:
        return 0
    return await repost_messages(guild, store, d["channel_id"], channel, since=d["from_ts"] - 2,
                                 until=d["to_ts"] + 5, progress=progress, stats=stats)


async def restore_everything(guild: discord.Guild, store: Store, progress: Optional[Progress] = None) -> dict:
    """Missing roles + channels (with messages) + every recorded message wipe."""
    report = _new_report()
    report["roles"] = len(await restore_roles(guild, store))
    part = await restore_channels(guild, store, None, progress=progress)
    report["channels"], report["messages"] = part["channels"], part["messages"]
    stats = {"failed": part.get("failed", 0), "error": part.get("error", "")}
    for event in store.events(guild.id, 50):
        if event["type"] == "message_wipe" and not event["details"].get("restored"):
            report["messages"] += await restore_wipe(guild, store, event, progress, stats)
    report["failed"], report["error"] = stats["failed"], stats["error"]
    return report
