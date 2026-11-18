import asyncio
import shutil
import logging
import signal
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import badges, icons
from utils import dataload
from utils.db import Database
from utils.ratelimit import AppRateLimited, RateLimited, RateLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("premium-bot")

EXTENSIONS = (
    "cogs.information",
    "cogs.moderation",
    "cogs.welcome",
    "cogs.tickets",
    "cogs.giveaway",
    "cogs.automod",
    "cogs.aliases",
    "cogs.noprefix",
    "cogs.backup",
    "cogs.antinuke",
    "cogs.badges",
    "cogs.help",
    "cogs.errors",
)


_base_prefix = commands.when_mentioned_or(config.PREFIX)


def get_prefix(bot: "PremiumBot", message: discord.Message) -> list[str]:
    """Normal prefixes, plus an empty one for users who have no-prefix access."""
    prefixes = list(_base_prefix(bot, message))
    if message.author.id in bot.noprefix_ids:
        prefixes.append("")  # last, so "!ping" still uses the real prefix
    return prefixes


class BotTree(app_commands.CommandTree):
    """Applies the rate limit to every slash command."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bot: PremiumBot = self.client  # type: ignore[assignment]
        if await bot.is_owner(interaction.user):
            return True
        retry_after, _ = bot.limiter.hit(interaction.user.id)
        if retry_after:
            raise AppRateLimited(retry_after)
        return True


class PremiumBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True          # Server Members Intent (Developer Portal)
        intents.presences = True        # Presence Intent (Developer Portal)
        intents.message_content = True  # Message Content Intent (Developer Portal)

        super().__init__(
            command_prefix=get_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,
            allowed_mentions=discord.AllowedMentions.none(),
            tree_cls=BotTree,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{config.PREFIX}help",
            ),
        )
        self.noprefix_ids: set[int] = set()  # filled by cogs.noprefix
        self.started_at = discord.utils.utcnow()
        self.db = Database(config.DB_PATH)
        self.limiter = RateLimiter(config.RATE_LIMIT_COMMANDS, config.RATE_LIMIT_PERIOD)
        self._shutdown_started = False
        self._background: set[asyncio.Task] = set()

        @self.before_invoke
        async def rate_limit(ctx: commands.Context) -> None:
            # Slash invocations are limited in BotTree.interaction_check instead.
            if ctx.interaction is not None or await self.is_owner(ctx.author):
                return
            retry_after, first = self.limiter.hit(ctx.author.id)
            if retry_after:
                raise RateLimited(retry_after, silent=not first)

    # ---------------------------------------------------- custom aliases
    def custom_alias(self, guild_id: int, name: str) -> Optional[str]:
        """Command name behind a server's custom alias (set with the alias command)."""
        row = self.db.conn.execute(
            "SELECT command FROM command_aliases WHERE guild_id=? AND alias=?",
            (guild_id, name.lower()),
        ).fetchone()
        return row[0] if row else None

    async def get_context(self, origin, /, *, cls=commands.Context):
        ctx = await super().get_context(origin, cls=cls)
        if (
            isinstance(origin, discord.Message)
            and ctx.prefix is not None
            and ctx.command is None
            and ctx.guild is not None
            and ctx.invoked_with
        ):
            target = self.custom_alias(ctx.guild.id, ctx.invoked_with)
            if target:
                ctx.command = self.get_command(target)
        return ctx

    async def setup_hook(self) -> None:
        try:  # data dropped into import/ (or a newer seed/ / data/ folder) is merged before anything reads it
            for line in dataload.auto_import(self.db.conn, config.DB_PATH, config.BASE_DIR, config.AN_DB_PATH):
                log.info("Data import: %s", line)
        except Exception:
            log.exception("Automatic data import failed (the bot keeps its current data)")
        await icons.sync(self)  # custom icons (assets/icons) -> application emojis
        self._spawn(badges.sync(self))  # badge icons for userinfo (runs in the background, once)
        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.info("Loaded extension %s", extension)

        if config.AUTO_SYNC:
            try:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands", len(synced))
            except discord.HTTPException:
                log.exception("Slash command sync failed")

        self.limiter_cleanup.start()

        # A normal stop signal (docker stop, hosting panels) should also run the final backup.
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, lambda: self._spawn(self.close()))
        except (NotImplementedError, RuntimeError, ValueError):
            pass  # not supported here (e.g. Windows); Ctrl+C still works

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @tasks.loop(minutes=5)
    async def limiter_cleanup(self) -> None:
        self.limiter.cleanup()

    async def close(self) -> None:
        if self._shutdown_started:  # close() can be triggered twice (signal + normal exit)
            return await super().close()
        self._shutdown_started = True
        antinuke = self.get_cog("AntiNuke")
        if antinuke is not None:
            await antinuke.final_flush()  # last messages + structure are saved before going offline
        backup = self.get_cog("Backup")
        if backup is not None:
            await backup.final_flush()  # last backup BEFORE the bot goes offline
        self.db.close()  # all server data is flushed to disk here
        await super().close()

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id if self.user else "unknown")


def main() -> None:
    if not config.TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and set it.")
    for note in dataload.prepare_database(config.DB_PATH, config.BASE_DIR / "seed" / "bot.db"):
        print(f"[data] {note}")
    for note in dataload.prepare_antinuke_database(config.AN_DB_PATH):
        print(f"[data] {note}")
    PremiumBot().run(config.TOKEN)


if __name__ == "__main__":
    main()
