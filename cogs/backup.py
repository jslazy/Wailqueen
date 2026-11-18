from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

import config
from utils import backup as snap
from utils import dataload
from utils.icons import icon
from utils.helpers import humanize_seconds

log = logging.getLogger("premium-bot.backup")

WEBHOOK_LIMIT = 9 * 1024 * 1024   # webhook uploads are capped at 10 MB by default
FLUSH_TIMEOUT = 20                # seconds the shutdown backup may take at most
STATE_FILE = Path(config.DB_PATH).parent / "backup_state.json"


class Backup(commands.Cog):
    """Webhook backup: every BACKUP_WEBHOOK_INTERVAL seconds and once more right before the bot goes
    offline, TWO zips plus a small report are sent to BACKUP_WEBHOOK_URL: the data (bot.db + json) and
    the code (no data, no .env). The database is only read, never changed. Webhooks can't use the
    bot's custom emojis, so normal unicode emojis are used here."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()
        self._closing = False
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.sent_count = 0
        self.state: dict = self._load_state()

    # ------------------------------------------------------------- state (edit mode only)
    @staticmethod
    def _load_state() -> dict:
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.state), "utf-8")
        except OSError:
            log.exception("Could not save %s", STATE_FILE)

    @property
    def conn(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    # ------------------------------------------------------------- lifecycle
    async def cog_load(self) -> None:
        if not config.BACKUP_WEBHOOK_URL:
            log.info("BACKUP_WEBHOOK_URL is empty: webhook backups are off")
            return
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        self.push_webhook.change_interval(seconds=config.BACKUP_WEBHOOK_INTERVAL)
        self.push_webhook.start()
        self._recovery = asyncio.get_running_loop().create_task(self.recover_from_webhook())

    async def cog_unload(self) -> None:
        self.push_webhook.cancel()
        task = getattr(self, "_recovery", None)
        if task is not None:
            task.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    async def final_flush(self) -> None:
        """Last backup right before the bot goes offline. Called from PremiumBot.close()."""
        if self._closing or not config.BACKUP_WEBHOOK_URL:
            return
        self._closing = True
        self.push_webhook.cancel()
        try:
            await asyncio.wait_for(self._push("shutdown"), FLUSH_TIMEOUT)
            log.info("Final backup sent before shutdown")
        except asyncio.TimeoutError:
            log.warning("Final backup timed out after %ss", FLUSH_TIMEOUT)
        except Exception:
            log.exception("Final backup failed")

    # ------------------------------------------------------------- report
    def _report(self, reason: str, names: list[str], sizes: list[int], notes: list[str]) -> discord.Embed:
        """Small embed: one status line, the two files, the database size."""
        bot = self.bot
        now = discord.utils.utcnow()
        shutdown = reason == "shutdown"
        counts = snap.table_counts(self.conn)
        uptime = int((now - bot.started_at).total_seconds())  # type: ignore[attr-defined]
        guilds = len(bot.guilds)
        embed = discord.Embed(
            title=f"\U0001F4BE {config.BOT_NAME} backup",
            description=(
                ("\U0001F534 Going offline (final backup)" if shutdown else "\U0001F7E2 Online")
                + f" \u2022 {discord.utils.format_dt(now, 'R')}\n"
                f"**Servers:** {guilds:,} \u2022 **Uptime:** {humanize_seconds(uptime)} \u2022 **Backup #{self.sent_count + 1}**\n"
                f"**DB:** {sum(counts.values()):,} rows in {len(counts)} tables"
            ),
            color=0xE74C3C if shutdown else 0x2ECC71,
            timestamp=now,
        )
        def label(n: str) -> str:
            return ("\U0001F6E1\uFE0F Anti-nuke" if "-antinuke" in n else "\U0001F4BB Code" if "-code" in n else "\U0001F4C1 Data")

        embed.add_field(
            name="Files",
            value="\n".join(f"{label(n)}: `{n}` ({sizes[i] / 1024:.0f} KB)" for i, n in enumerate(names)) or "none",
            inline=False,
        )
        status = []
        if self.last_error:
            status.append(f"\u26A0\uFE0F {self.last_error}")
        status += [f"\u26A0\uFE0F {n}" for n in notes]
        if status:
            embed.add_field(name="Status", value="\n".join(status)[:500], inline=False)
        embed.set_footer(text="No .env inside \u2022 keep this channel private")
        return embed

    # ------------------------------------------------------------- webhook
    async def _push(self, reason: str = "scheduled") -> None:
        """Send TWO zips + a small report: the data zip and the code zip. Never raises, never logs the URL."""
        url = config.BACKUP_WEBHOOK_URL
        if not url or self.session is None:
            return
        async with self._lock:
            try:
                webhook = discord.Webhook.from_url(url, session=self.session)
                base = config.BOT_NAME.lower().replace(" ", "-")
                an_cog = self.bot.get_cog("AntiNuke")
                an_store = an_cog.store if an_cog is not None else None  # type: ignore[attr-defined]
                data_bytes, _ = snap.data_zip(self.conn, an_store)
                code_bytes, _ = snap.source_zip()
                files_in: list[tuple[str, bytes]] = [(f"{base}-data.zip", data_bytes), (f"{base}-code.zip", code_bytes)]
                if an_store is not None:  # the full anti-nuke database (settings + saved structure + saved messages)
                    an_bytes, _ = await asyncio.to_thread(snap.antinuke_zip, an_store)
                    files_in.append((f"{base}-antinuke.zip", an_bytes))
                notes: list[str] = []
                payload: list[tuple[str, bytes]] = []
                budget = WEBHOOK_LIMIT
                for name, raw in files_in:
                    fitted = snap.fit(name, raw, budget)
                    if fitted is None:
                        notes.append(f"{name} is too large to upload ({len(raw) / 1048576:.1f} MB)")
                    else:
                        payload.append(fitted)
                        budget -= len(fitted[1])  # one message may hold ~10 MB in total
                if an_store is not None and not any(n.endswith("-data.zip") for n, _ in payload):
                    notes.append("data zip missing: anti-nuke settings are NOT in this backup")
                embed = self._report(reason, [p[0] for p in payload], [len(p[1]) for p in payload], notes)

                def files() -> list[discord.File]:
                    return [discord.File(io.BytesIO(data), filename=name) for name, data in payload]

                avatar = self.bot.user.display_avatar.url if self.bot.user else discord.utils.MISSING
                done = False
                if config.BACKUP_WEBHOOK_MODE == "edit" and self.state.get("webhook_message_id"):
                    try:
                        await webhook.edit_message(self.state["webhook_message_id"], embed=embed, attachments=files())
                        done = True
                    except discord.NotFound:
                        pass
                if not done:
                    message = await webhook.send(
                        embed=embed, files=files(), wait=True, username=config.BOT_NAME, avatar_url=avatar
                    )
                    if config.BACKUP_WEBHOOK_MODE == "edit":
                        self.state["webhook_message_id"] = message.id
                        self._save_state()
                self.sent_count += 1
                self.last_ok = discord.utils.utcnow().timestamp()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except discord.HTTPException as exc:
                self.last_error = f"Webhook HTTP {exc.status}"
                log.warning("Webhook backup failed: HTTP %s", exc.status)
            except ValueError:
                self.last_error = "BACKUP_WEBHOOK_URL is not a valid webhook URL"
                log.warning(self.last_error)
            except Exception as exc:
                self.last_error = f"Webhook {type(exc).__name__}"
                log.warning("Webhook backup failed: %s", type(exc).__name__)

    # ------------------------------------------------------------- load a backup
    @commands.hybrid_command(
        name="backupload", aliases=["loadbackup", "dataload"],
        description="Owner: load a backup data file (zip / bot.db / json) and merge it into the bot's data.",
    )
    @commands.is_owner()
    async def backupload(self, ctx: commands.Context, file: Optional[discord.Attachment] = None, mode: str = "merge") -> None:
        """Attach the `-data.zip` from your backup channel (or a bot.db / bot-data.json). You can attach several
        files at once or reply to a message that has them. Everything new is ADDED, nothing is deleted.
        `!backupload replace` also lets the uploaded settings overwrite existing ones."""
        attachments: dict[int, discord.Attachment] = {}
        if file is not None:
            attachments[file.id] = file
        if ctx.message is not None:
            for a in ctx.message.attachments:
                attachments[a.id] = a
            ref = ctx.message.reference
            if ref and ref.message_id:
                try:
                    replied = ref.resolved if isinstance(ref.resolved, discord.Message) else await ctx.channel.fetch_message(ref.message_id)
                    for a in replied.attachments:
                        attachments[a.id] = a
                except discord.HTTPException:
                    pass
        if not attachments:
            return await ctx.send(
                f"{icon('file')} Attach your backup file (the `-data.zip` from the backup channel, a `bot.db` or "
                f"`bot-data.json`) to the message with `{config.PREFIX}backupload`, or reply to a message that has it.\n"
                f"Send several files together to merge all of them. Add `replace` to let uploaded settings overwrite existing ones.")
        replace = mode.lower() in {"replace", "overwrite"}
        if ctx.interaction is not None:
            await ctx.defer()
        lines, grand = [], {"added": 0, "skipped": 0}
        failed: list[str] = []
        for att in attachments.values():
            try:
                raw = await att.read()
            except discord.HTTPException:
                failed.append(f"`{att.filename}`: could not be downloaded")
                continue
            results: list = []
            bot_error: Optional[Exception] = None
            try:
                results = dataload.merge_file(self.conn, config.DB_PATH, raw, att.filename, replace=replace)
            except Exception as exc:
                bot_error = exc
                if not isinstance(exc, ValueError):
                    log.exception("backupload failed for %s", att.filename)
            an_results: list = []
            an_cog = self.bot.get_cog("AntiNuke")
            if an_cog is not None:  # webhook link, trusted users, saved structure... of the anti-nuke system
                try:
                    an_results = await an_cog.merge_backup(raw, att.filename, replace=replace)  # type: ignore[attr-defined]
                except Exception as exc:
                    failed.append(f"`{att.filename}` (anti-nuke): {type(exc).__name__}")
                    log.exception("anti-nuke backupload failed for %s", att.filename)
            if bot_error is not None and not an_results and not (isinstance(bot_error, ValueError) and failed):
                failed.append(f"`{att.filename}`: {bot_error if isinstance(bot_error, ValueError) else type(bot_error).__name__}")
                continue
            for table, c in {**dataload.totals(results), **dataload.totals(an_results)}.items():
                grand["added"] += c["added"]
                grand["skipped"] += c["skipped"]
                if c["added"] or c["skipped"]:
                    lines.append(f"`{table}` +{c['added']}" + (f" ({c['skipped']} already there)" if c["skipped"] else ""))
        reloaded = await self._reload_data_cogs() if grand["added"] or replace else []
        embed = discord.Embed(
            title=f"{icon('ok') if not failed else icon('warn')} Backup loaded",
            color=0x2ECC71 if not failed else 0xE67E22,
            description=f"**{grand['added']:,}** new rows added \u2022 {grand['skipped']:,} already existed",
        )
        if lines:
            embed.add_field(name="Tables", value="\n".join(lines)[:1000], inline=False)
        if failed:
            embed.add_field(name="Could not load", value="\n".join(failed)[:1000], inline=False)
        embed.set_footer(text="A safety copy of the old data is in data/backups/" + (" \u2022 reloaded: " + ", ".join(reloaded) if reloaded else ""))
        await ctx.send(embed=embed)

    async def _reload_data_cogs(self) -> list[str]:
        """Cogs that keep a copy of the data in memory read it again."""
        done = []
        for ext in ("cogs.noprefix", "cogs.badges", "cogs.giveaway", "cogs.tickets"):
            if ext in self.bot.extensions:
                try:
                    await self.bot.reload_extension(ext)
                    done.append(ext.split(".")[1])
                except Exception:
                    log.exception("Could not reload %s", ext)
        return done

    # ------------------------------------------------------------- automatic recovery
    def _is_fresh(self) -> bool:
        """True when the bot starts with completely empty data (a wiped disk / new host): nothing to lose by restoring."""
        counts = snap.table_counts(self.conn)
        rows = sum(n for t, n in counts.items() if t != "data_imports")
        an_rows = dataload.antinuke_row_count(config.AN_DB_PATH)
        return rows == 0 and an_rows == 0

    async def recover_from_webhook(self) -> None:
        """If the bot starts with EMPTY data (the host wiped the disk), take the newest backup from the backup webhook's
        channel and merge it back: all settings, the anti-nuke webhook link, tickets, giveaways... Best effort and only
        ever ADDS data. Needs either the saved message id or Read Message History in the backup channel."""
        await self.bot.wait_until_ready()
        if not config.AUTO_RECOVER or not config.BACKUP_WEBHOOK_URL or self.session is None or not self._is_fresh():
            return
        base = config.BOT_NAME.lower().replace(" ", "-")
        try:
            webhook = discord.Webhook.from_url(config.BACKUP_WEBHOOK_URL, session=self.session)
            message: Optional[discord.WebhookMessage] = None
            if self.state.get("webhook_message_id"):
                try:
                    message = await webhook.fetch_message(self.state["webhook_message_id"])
                except discord.HTTPException:
                    message = None
            if message is None:
                info = await webhook.fetch()
                channel = self.bot.get_channel(info.channel_id) if info.channel_id else None
                if channel is None and info.channel_id:
                    channel = await self.bot.fetch_channel(info.channel_id)
                async for m in channel.history(limit=100):  # type: ignore[union-attr]
                    if m.webhook_id == info.id and any(a.filename.startswith(base) and "-data.zip" in a.filename for a in m.attachments):
                        message = m  # type: ignore[assignment]
                        break
            if message is None:
                log.info("Automatic recovery: no backup message found")
                return
            wanted = [a for a in message.attachments if a.filename.startswith(base) and ("-data.zip" in a.filename or "-antinuke.zip" in a.filename)]
            added = 0
            for att in wanted:
                raw = await att.read()
                try:
                    added += sum(c["added"] for c in dataload.totals(dataload.merge_file(self.conn, config.DB_PATH, raw, att.filename)).values())
                except ValueError:
                    pass
                an_cog = self.bot.get_cog("AntiNuke")
                if an_cog is not None:
                    results = await an_cog.merge_backup(raw, att.filename)  # type: ignore[attr-defined]
                    added += sum(c["added"] for c in dataload.totals(results).values())
            if added:
                await self._reload_data_cogs()
            log.warning("Automatic recovery: data folder was empty, %d rows restored from the backup channel", added)
        except Exception as exc:
            log.warning("Automatic recovery skipped: %s", type(exc).__name__)

    @tasks.loop(seconds=60)
    async def push_webhook(self) -> None:
        await self._push("scheduled")

    @push_webhook.before_loop
    async def _before_push(self) -> None:
        await self.bot.wait_until_ready()
        task = getattr(self, "_recovery", None)
        if task is not None:  # an empty database must never overwrite the good backup before it was restored
            try:
                await asyncio.wait_for(asyncio.shield(task), 60)
            except Exception:
                pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Backup(bot))
