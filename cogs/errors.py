from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.help import ADMIN_COGS, is_staff
from utils.helpers import pretty_permission
from utils.icons import icon
from utils.ratelimit import AppRateLimited, RateLimited

log = logging.getLogger("premium-bot.errors")


def _tag(error: Exception) -> str:
    """Icon shown before an error message."""
    if isinstance(error, (RateLimited, AppRateLimited, commands.CommandOnCooldown)):
        return icon("hourglass")
    if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
        return icon("lock")
    return icon("warn")


def _is_permission_denial(error: Exception) -> bool:
    """True when the user simply is not allowed to run the command (no permission / not owner / role)."""
    if isinstance(error, (commands.MissingPermissions, commands.NotOwner, commands.MissingRole, commands.MissingAnyRole)):
        return True
    # a plain failed check (custom checks); NOT bot-permission or DM-only problems
    return type(error) is commands.CheckFailure


class Errors(commands.Cog):
    """Global error handling."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.tree.on_error = self.on_app_command_error  # errors of slash-only commands

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original  # type: ignore[assignment]

        if isinstance(error, AppRateLimited):
            message = f"You're using commands too fast. Try again in {error.retry_after:.1f}s."
        elif isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(pretty_permission(p) for p in error.missing_permissions)
            message = f"You need the following permissions to use this command: {perms}"
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(pretty_permission(p) for p in error.missing_permissions)
            message = f"I need the following permissions to run this command: {perms}"
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command can only be used inside a server."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You are not allowed to use this command."
        else:
            log.error("Unhandled slash command error", exc_info=error)
            message = "An unexpected error occurred. Please try again in a moment."

        message = f"{_tag(error)} {message}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not deliver the slash error message")

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, (commands.CommandInvokeError, commands.HybridCommandError)):
            error = error.original  # type: ignore[assignment]

        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, RateLimited):
            if error.silent:  # already told the user once during this window
                return
            try:
                await ctx.send(
                    f"{icon('hourglass')} You're using commands too fast. Try again in {error.retry_after:.1f}s.",
                    delete_after=5,
                )
            except discord.HTTPException:
                pass
            return

        # Anything from an admin-only command used by a normal member (bad arguments, missing bot
        # permission, ...): NO reply either, their message is removed.
        if ctx.interaction is None and ctx.command is not None and ctx.command.cog is not None \
                and ctx.command.cog.qualified_name in ADMIN_COGS and not is_staff(ctx):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            return

        # Admin / owner commands used by normal members: NO reply, and their message is removed.
        # (Slash commands cannot be deleted, so they still get a private "not allowed" notice.)
        if ctx.interaction is None and _is_permission_denial(error):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass  # no Manage Messages permission here: stay silent anyway
            return

        usage = ""
        if ctx.command is not None:
            usage = f"{config.PREFIX}{ctx.command.qualified_name} {ctx.command.signature}".strip()

        if isinstance(error, commands.MissingRequiredArgument):
            message = f"Missing required argument: {error.param.name}\nUsage: `{usage}`"
        elif isinstance(error, commands.BadArgument):
            message = f"{error}\nUsage: `{usage}`"
        elif isinstance(error, commands.MissingPermissions):
            perms = ", ".join(pretty_permission(p) for p in error.missing_permissions)
            message = f"You need the following permissions to use this command: {perms}"
        elif isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join(pretty_permission(p) for p in error.missing_permissions)
            message = f"I need the following permissions to run this command: {perms}"
        elif isinstance(error, commands.CommandOnCooldown):
            message = f"This command is on cooldown. Try again in {error.retry_after:.1f}s."
        elif isinstance(error, commands.NoPrivateMessage):
            message = "This command can only be used inside a server."
        elif isinstance(error, (commands.NotOwner, commands.CheckFailure)):
            message = "You are not allowed to use this command."
        elif isinstance(error, commands.UserInputError):
            message = f"{error}\nUsage: `{usage}`"
        else:
            log.error("Unhandled error in command %s", ctx.command, exc_info=error)
            message = "An unexpected error occurred. Please try again in a moment."

        message = f"{_tag(error)} {message}"
        try:
            await ctx.send(message, ephemeral=True)
        except discord.HTTPException:
            log.warning("Could not deliver the error message for %s", ctx.command)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Errors(bot))
