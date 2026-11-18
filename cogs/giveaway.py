"""Fully customisable giveaways.

!giveaway edit            opens the editor panel (everything can be changed, banner included)
!giveaway edit <name|id>  edit a saved giveaway, or a running one live
!giveaway load            pick a saved / previous giveaway and continue from it
!giveaway start <time> <winners> <prize>   quick start with the default look
!giveaway end | reroll | list | saved | delete

Giveaways are stored in the database, so running giveaways survive restarts and are
ended automatically when the time is over.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import images
from utils.helpers import humanize_seconds, parse_duration
from utils.icons import icon

log = logging.getLogger("premium-bot.giveaway")

JOIN_ID = "gw:join"
LIST_ID = "gw:list"
MAX_SAVED = 15
MIN_SECONDS = 10
MAX_SECONDS = 60 * 86400

STYLES = {
    "green": discord.ButtonStyle.success,
    "blurple": discord.ButtonStyle.primary,
    "grey": discord.ButtonStyle.secondary,
    "gray": discord.ButtonStyle.secondary,
    "red": discord.ButtonStyle.danger,
}

DEFAULTS: dict[str, Any] = {
    "title": f"{icon('gift')} GIVEAWAY {icon('gift')}",
    "description": "Click the button below to enter!",
    "prize": "Mystery Prize",
    "winners": 1,
    "duration": 3600,
    "color": "#F1C40F",
    "footer": "",
    "banner": "",
    "thumbnail": "",
    "content": "",       # message text above the embed
    "ping_role": 0,      # role pinged when the giveaway starts
    "req_role": 0,       # role needed to enter
    "channel_id": 0,
    "join_emoji": icon("gift"),
    "join_label": "Join Giveaway",
    "join_style": "green",
    "list_emoji": icon("users"),
    "emoji_prize": icon("gift"),
    "emoji_winners": icon("crown"),
    "emoji_host": icon("star"),
    "emoji_ends": icon("hourglass"),
}


# ------------------------------------------------------------------ helpers
def valid_emoji(text: str) -> Optional[str]:
    """Return the emoji if Discord can show it on a button, else None."""
    text = (text or "").strip()
    if not text or len(text) > 60:
        return None
    partial = discord.PartialEmoji.from_str(text)
    if partial.id:
        return str(partial)
    if text.isascii():  # plain words such as ":tada:" or "abc" are not emojis
        return None
    return text


def parse_color(value: str) -> int:
    value = (value or "").strip()
    if value.lower() == "random":
        return random.choice(config.RANDOM_COLORS)
    try:
        return int(value.lstrip("#"), 16) & 0xFFFFFF
    except ValueError:
        return 0xF1C40F


def now() -> datetime:
    return datetime.now(timezone.utc)


def merged(raw: Optional[str]) -> dict[str, Any]:
    data = copy.deepcopy(DEFAULTS)
    try:
        data.update(json.loads(raw or "{}"))
    except (ValueError, TypeError):
        pass
    return data


def resolve_role(guild: discord.Guild, text: str) -> Optional[discord.Role]:
    text = (text or "").strip()
    match = re.fullmatch(r"<@&(\d+)>|(\d{15,25})", text)
    if match:
        return guild.get_role(int(match.group(1) or match.group(2)))
    lowered = text.lower().lstrip("@")
    return next((r for r in guild.roles if r.name.lower() == lowered), None)


def render(
    cog: "Giveaway",
    guild: discord.Guild,
    data: dict[str, Any],
    *,
    host_id: int,
    end_at: datetime,
    label: str,
    ended: bool = False,
    winners: Optional[list[int]] = None,
) -> tuple[discord.Embed, list[discord.File]]:
    """The giveaway embed (+ banner/thumbnail files) from the saved settings."""
    title = data["title"] + (" • ENDED" if ended else "")
    lines = []
    if data["description"]:
        lines += [data["description"], ""]
    lines.append(f"{data['emoji_prize']} **Prize:** {data['prize']}")
    lines.append(f"{data['emoji_winners']} **Winners:** {data['winners']}")
    lines.append(f"{data['emoji_host']} **Hosted by:** <@{host_id}>")
    stamp = discord.utils.format_dt(end_at, "R") + f" ({discord.utils.format_dt(end_at, 'f')})"
    lines.append(f"{data['emoji_ends']} **{'Ended' if ended else 'Ends'}:** {stamp}")
    if data["req_role"]:
        lines.append(f"{icon('lock')} **Required role:** <@&{data['req_role']}>")
    if ended:
        lines.append("")
        if winners:
            lines.append(f"{data['emoji_winners']} **Winner{'s' if len(winners) > 1 else ''}:** " + ", ".join(f"<@{w}>" for w in winners))
        else:
            lines.append(f"{icon('warn')} **No valid entries, no winner.**")

    embed = discord.Embed(title=title[:256], description="\n".join(lines)[:4000], color=parse_color(data["color"]))
    embed.set_footer(text=data["footer"] or f"Giveaway {label}")
    files: list[discord.File] = []
    url, file = images.attach(cog.db, data.get("banner"), "banner")
    if url:
        embed.set_image(url=url)
    if file:
        files.append(file)
    url, file = images.attach(cog.db, data.get("thumbnail"), "thumb")
    if url:
        embed.set_thumbnail(url=url)
    if file:
        files.append(file)
    return embed, files


# --------------------------------------------------------- persistent buttons
class GiveawayView(discord.ui.View):
    """Join + Participants buttons under every giveaway (survive restarts)."""

    def __init__(
        self,
        *,
        data: Optional[dict[str, Any]] = None,
        count: int = 0,
        ended: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        data = data or DEFAULTS
        self.join.label = data["join_label"][:80] or None
        self.join.emoji = valid_emoji(data["join_emoji"]) or None
        self.join.style = STYLES.get(data["join_style"], discord.ButtonStyle.success)
        self.join.disabled = ended
        self.entries.emoji = valid_emoji(data["list_emoji"]) or None
        self.entries.label = str(count)

    @discord.ui.button(label="Join Giveaway", custom_id=JOIN_ID, style=discord.ButtonStyle.success)
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: Giveaway = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        await cog.handle_join(interaction)

    @discord.ui.button(label="0", custom_id=LIST_ID, style=discord.ButtonStyle.secondary)
    async def entries(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cog: Giveaway = interaction.client.get_cog("Giveaway")  # type: ignore[assignment]
        await cog.handle_list(interaction)


class LeaveView(discord.ui.View):
    def __init__(self, cog: "Giveaway", giveaway_id: int, user_id: int) -> None:
        super().__init__(timeout=60)
        self.cog, self.giveaway_id, self.user_id = cog, giveaway_id, user_id

    @discord.ui.button(label="Leave giveaway", style=discord.ButtonStyle.danger)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.cog.db.execute(
            "DELETE FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (self.giveaway_id, self.user_id)
        )
        self.cog.db.commit()
        self.cog.dirty.add(self.giveaway_id)
        await interaction.response.edit_message(content=f"{icon('ok')} You left the giveaway.", view=None)


# ----------------------------------------------------------------- the panel
class TextModal(discord.ui.Modal):
    """Small helper: a modal with ready-made inputs and a callback that gets the values."""

    def __init__(self, title: str, inputs: list[discord.ui.TextInput], on_done) -> None:
        super().__init__(title=title)
        self.on_done = on_done
        self.inputs = inputs
        for item in inputs:
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.on_done(interaction, {i.label: (i.value or "").strip() for i in self.inputs})


def field(label: str, value: Any = "", *, long: bool = False, required: bool = False, max_length: int = 100, placeholder: str = "") -> discord.ui.TextInput:
    return discord.ui.TextInput(
        label=label,
        default=(str(value) if value not in (None, 0) else "")[:max_length] or None,
        style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
        required=required,
        max_length=max_length,
        placeholder=placeholder or None,
    )


class Editor(discord.ui.View):
    """The giveaway panel. The embed on top is a live preview of what members will see."""

    def __init__(
        self,
        cog: "Giveaway",
        author: discord.Member,
        data: dict[str, Any],
        *,
        saved_id: Optional[int] = None,
        saved_name: str = "",
        live_id: Optional[int] = None,
    ) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.author = author
        self.data = data
        self.saved_id = saved_id
        self.saved_name = saved_name
        self.live_id = live_id          # editing a running giveaway
        self.dur_text = ""              # duration text shown in the modal (live mode)
        self.message: Optional[discord.Message] = None
        self.start.label = "Update giveaway" if live_id else "Start giveaway"
        if live_id:
            self.remove_item(self.channel_select)  # a running giveaway cannot change channel

    # ---------------------------------------------------------------- view
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                f"{icon('lock')} This panel belongs to {self.author.mention}. Run `{config.PREFIX}giveaway edit` yourself.",
                ephemeral=True,
            )
            return False
        return True

    def _end_at(self) -> datetime:
        if self.live_id:
            row = self.cog.row(self.live_id)
            if row and row["end_at"]:
                return datetime.fromisoformat(row["end_at"])
        return now() + timedelta(seconds=int(self.data["duration"]))

    def panel(self) -> tuple[str, discord.Embed, list[discord.File]]:
        embed, files = render(
            self.cog, self.author.guild, self.data,
            host_id=self.author.id, end_at=self._end_at(), label="preview" if not self.live_id else f"#{self.live_id}",
        )
        channel = self.data.get("channel_id")
        where = f"<#{channel}>" if channel else "this channel"
        mode = f"editing **running giveaway #{self.live_id}**" if self.live_id else (
            f"editing saved giveaway **{self.saved_name}**" if self.saved_id else "new giveaway"
        )
        text = (
            f"{icon('gift')} **Giveaway panel** • {mode}\n"
            f"Preview below. Channel: {where} • Duration: **{humanize_seconds(int(self.data['duration']))}**"
        )
        return text, embed, files

    async def refresh(self, interaction: discord.Interaction) -> None:
        text, embed, files = self.panel()
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, embed=embed, attachments=files, view=self)
        else:
            await interaction.response.edit_message(content=text, embed=embed, attachments=files, view=self)

    async def refresh_message(self) -> None:
        """Refresh the panel from outside its own interaction (used by the load menu)."""
        if self.message is not None:
            text, embed, files = self.panel()
            await self.message.edit(content=text, embed=embed, attachments=files, view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _bad(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.send_message(f"{icon('warn')} {text}", ephemeral=True)

    # ---------------------------------------------------------------- row 0
    @discord.ui.button(label="Basic", emoji=icon("clipboard"), style=discord.ButtonStyle.primary, row=0)
    async def basic(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        d = self.data
        self.dur_text = humanize_seconds(int(d["duration"])).replace(" ", "")
        if self.live_id:
            row = self.cog.row(self.live_id)
            left = int((datetime.fromisoformat(row["end_at"]) - now()).total_seconds()) if row else 0
            self.dur_text = humanize_seconds(max(left, 0)).replace(" ", "")
        inputs = [
            field("Title", d["title"], max_length=200, required=True),
            field("Prize", d["prize"], max_length=200, required=True),
            field("Winners", d["winners"], max_length=2, required=True),
            field("Duration (10m, 2h30m, 1d)" if not self.live_id else "Time left (change = new end time)", self.dur_text, max_length=12, required=True),
        ]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            vals = list(v.values())
            if not vals[2].isdigit() or not 1 <= int(vals[2]) <= 50:
                return await self._bad(i, "Winners must be a number from 1 to 50. Nothing changed.")
            dur = parse_duration(vals[3].replace(" ", ""))
            if dur is None or not MIN_SECONDS <= dur.total_seconds() <= MAX_SECONDS:
                return await self._bad(i, "Duration must look like `30m`, `2h`, `1d12h` (10 seconds to 60 days). Nothing changed.")
            d.update(title=vals[0], prize=vals[1], winners=int(vals[2]))
            if self.live_id:
                if vals[3].replace(" ", "") != self.dur_text:
                    self.cog.db.execute(
                        "UPDATE giveaways SET end_at=? WHERE id=?",
                        ((now() + dur).isoformat(), self.live_id),
                    )
                    self.cog.db.commit()
            else:
                d["duration"] = int(dur.total_seconds())
            await self.refresh(i)

        await interaction.response.send_modal(TextModal("Basic settings", inputs, done))

    @discord.ui.button(label="Description", emoji=icon("file"), style=discord.ButtonStyle.primary, row=0)
    async def description(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        d = self.data
        inputs = [
            field("Description (above the details)", d["description"], long=True, max_length=1500),
            field("Footer text (empty = default)", d["footer"], max_length=200),
            field("Message above the embed", d["content"], long=True, max_length=500),
        ]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            vals = list(v.values())
            d.update(description=vals[0], footer=vals[1], content=vals[2])
            await self.refresh(i)

        await interaction.response.send_modal(TextModal("Description", inputs, done))

    @discord.ui.button(label="Colour", emoji=icon("palette"), style=discord.ButtonStyle.primary, row=0)
    async def colour(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        inputs = [field("Colour (#HEX or random)", self.data["color"], max_length=10, required=True)]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            value = list(v.values())[0]
            if value.lower() != "random" and not re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
                return await self._bad(i, "Colour must be like `#F1C40F` or `random`. Nothing changed.")
            self.data["color"] = value if value.lower() == "random" else "#" + value.lstrip("#")
            await self.refresh(i)

        await interaction.response.send_modal(TextModal("Embed colour", inputs, done))

    @discord.ui.button(label="Banner", emoji=icon("banner"), style=discord.ButtonStyle.primary, row=0)
    async def banner(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._image(interaction, "banner")

    @discord.ui.button(label="Thumbnail", emoji=icon("image"), style=discord.ButtonStyle.primary, row=0)
    async def thumbnail(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._image(interaction, "thumbnail")

    async def _image(self, interaction: discord.Interaction, key: str) -> None:
        """Wait for an uploaded image or a link in the channel. Only the link is saved."""
        await interaction.response.send_message(
            f"{icon('image')} Send the **{key}** image in this channel within 60 seconds: **upload it** or **paste a direct link**.\n"
            f"Type `remove` to delete the current {key}.",
            ephemeral=True,
        )

        def check(m: discord.Message) -> bool:
            return m.author.id == self.author.id and m.channel.id == interaction.channel_id

        try:
            msg = await self.cog.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            return await interaction.followup.send(f"{icon('hourglass')} Timed out, nothing changed.", ephemeral=True)

        text = msg.content.strip()
        source = msg.attachments[0].url if msg.attachments else text.split()[0] if text else ""
        try:
            if text.lower() == "remove" and not msg.attachments:
                self.data[key] = ""
            elif source:
                self.data[key] = await images.store(self.cog.db, source)
            else:
                return await interaction.followup.send(f"{icon('warn')} No image found. Nothing changed.", ephemeral=True)
        except images.ImageError as exc:
            return await interaction.followup.send(f"{icon('warn')} {exc}", ephemeral=True)
        finally:
            try:
                await msg.delete()  # keep the channel clean (needs Manage Messages, optional)
            except discord.HTTPException:
                pass
        await self.refresh_message()
        await interaction.followup.send(f"{icon('ok')} {key.title()} saved.", ephemeral=True)

    # ---------------------------------------------------------------- row 1
    @discord.ui.button(label="Button & emojis", emoji=icon("puzzle"), style=discord.ButtonStyle.secondary, row=1)
    async def button_look(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        d = self.data
        inputs = [
            field("Join button emoji", d["join_emoji"], max_length=60, placeholder="any emoji or a custom emoji"),
            field("Join button text", d["join_label"], max_length=80, required=True),
            field("Join button colour", d["join_style"], max_length=8, placeholder="green / blurple / grey / red"),
            field("Participants button emoji", d["list_emoji"], max_length=60),
        ]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            vals = list(v.values())
            problems = []
            emoji = valid_emoji(vals[0]) if vals[0] else d["join_emoji"]
            list_emoji = valid_emoji(vals[3]) if vals[3] else d["list_emoji"]
            if emoji is None:
                problems.append("join emoji")
            if list_emoji is None:
                problems.append("participants emoji")
            if vals[2].lower() not in STYLES:
                problems.append("button colour")
            if problems:
                return await self._bad(
                    i, "Invalid " + ", ".join(problems) + ". Paste the real emoji (not `:name:`). Nothing changed."
                )
            d.update(join_emoji=emoji, join_label=vals[1], join_style=vals[2].lower(), list_emoji=list_emoji)
            await self.refresh(i)

        await interaction.response.send_modal(TextModal("Buttons", inputs, done))

    @discord.ui.button(label="Line emojis", emoji=icon("sparkles"), style=discord.ButtonStyle.secondary, row=1)
    async def line_emojis(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        d = self.data
        inputs = [
            field("Prize emoji", d["emoji_prize"], max_length=60),
            field("Winners emoji", d["emoji_winners"], max_length=60),
            field("Host emoji", d["emoji_host"], max_length=60),
            field("Ends emoji", d["emoji_ends"], max_length=60),
        ]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            keys = ["emoji_prize", "emoji_winners", "emoji_host", "emoji_ends"]
            new = {}
            for key, value in zip(keys, v.values()):
                if value:
                    checked = valid_emoji(value)
                    if checked is None:
                        return await self._bad(i, f"`{value}` is not an emoji. Paste the real emoji. Nothing changed.")
                    new[key] = checked
            d.update(new)
            await self.refresh(i)

        await interaction.response.send_modal(TextModal("Emojis in the embed", inputs, done))

    @discord.ui.button(label="Requirements", emoji=icon("lock"), style=discord.ButtonStyle.secondary, row=1)
    async def requirements(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        d = self.data
        guild = self.author.guild
        req = guild.get_role(d["req_role"]) if d["req_role"] else None
        ping = guild.get_role(d["ping_role"]) if d["ping_role"] else None
        inputs = [
            field("Role needed to enter (empty = anyone)", req.name if req else "", max_length=100, placeholder="role name, ID or @mention"),
            field("Role to ping when it starts", ping.name if ping else "", max_length=100, placeholder="role name, ID or @mention"),
        ]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            vals = list(v.values())
            result = {}
            for key, text in zip(("req_role", "ping_role"), vals):
                if not text:
                    result[key] = 0
                    continue
                role = resolve_role(guild, text)
                if role is None:
                    return await self._bad(i, f"Role `{text}` was not found. Nothing changed.")
                result[key] = role.id
            d.update(result)
            await self.refresh(i)

        await interaction.response.send_modal(TextModal("Requirements & ping", inputs, done))

    # ---------------------------------------------------------------- row 2
    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text, discord.ChannelType.news],
        placeholder="Giveaway channel (default: where you ran the command)",
        min_values=1, max_values=1, row=2,
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        self.data["channel_id"] = select.values[0].id
        await self.refresh(interaction)

    # ---------------------------------------------------------------- row 3
    @discord.ui.button(label="Start giveaway", emoji=icon("rocket"), style=discord.ButtonStyle.success, row=3)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        if self.live_id:
            ok, text = await self.cog.update_live(self.live_id, self.data)
        else:
            ok, text = await self.cog.launch(self.author, self.data, interaction.channel)
        if not ok:
            return await interaction.followup.send(f"{icon('warn')} {text}", ephemeral=True)
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.edit_original_response(content=f"{icon('ok')} {text}", embed=None, attachments=[], view=self)
        self.stop()

    @discord.ui.button(label="Save", emoji=icon("save"), style=discord.ButtonStyle.secondary, row=3)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        inputs = [field("Name of this saved giveaway", self.saved_name or self.data["prize"][:40], max_length=40, required=True)]

        async def done(i: discord.Interaction, v: dict[str, str]) -> None:
            name = list(v.values())[0]
            saved_id, error = self.cog.save_template(self.author.guild.id, name, self.data, self.saved_id)
            if error:
                return await self._bad(i, error)
            self.saved_id, self.saved_name = saved_id, name
            await self.refresh(i)
            await i.followup.send(f"{icon('ok')} Saved as **{name}**. Load it any time with `{config.PREFIX}giveaway load`.", ephemeral=True)

        await interaction.response.send_modal(TextModal("Save giveaway", inputs, done))

    @discord.ui.button(label="Load", emoji=icon("folder"), style=discord.ButtonStyle.secondary, row=3)
    async def load(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = LoadView(self.cog, self.author, self.author.guild.id, panel=self)
        if not view.has_options:
            return await self._bad(interaction, "Nothing saved yet. Use **Save** first, or start a giveaway.")
        await interaction.response.send_message("Pick a saved or previous giveaway to load into this panel:", view=view, ephemeral=True)

    @discord.ui.button(label="Reset", emoji=icon("recycle"), style=discord.ButtonStyle.secondary, row=3)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        keep = {"channel_id": self.data.get("channel_id", 0)}
        self.data.clear()
        self.data.update(copy.deepcopy(DEFAULTS))
        self.data.update(keep)
        self.saved_id, self.saved_name = None, ""
        await self.refresh(interaction)

    @discord.ui.button(label="Close", emoji=icon("error"), style=discord.ButtonStyle.danger, row=3)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(view=self)
        self.stop()


class LoadView(discord.ui.View):
    """Drop-down with saved giveaways and the last previous giveaways."""

    def __init__(self, cog: "Giveaway", author: discord.Member, guild_id: int, panel: Optional[Editor] = None) -> None:
        super().__init__(timeout=120)
        self.cog, self.author, self.panel = cog, author, panel
        options: list[discord.SelectOption] = []
        for row in cog.db.execute(
            "SELECT id, name FROM giveaways WHERE guild_id=? AND saved=1 ORDER BY name LIMIT ?", (guild_id, MAX_SAVED)
        ):
            options.append(discord.SelectOption(label=f"{row[1]}"[:100], value=f"t:{row[0]}", description="Saved giveaway", emoji=icon("save")))
        for row in cog.db.execute(
            "SELECT id, status, data FROM giveaways WHERE guild_id=? AND saved=0 AND status IN ('active','ended') "
            "ORDER BY id DESC LIMIT ?", (guild_id, 25 - len(options) if len(options) < 25 else 0),
        ):
            prize = merged(row[2])["prize"]
            options.append(
                discord.SelectOption(label=f"#{row[0]} {prize}"[:100], value=f"g:{row[0]}", description=f"Previous giveaway ({row[1]})", emoji=icon("stopwatch"))
            )
        self.has_options = bool(options)
        if options:
            select = discord.ui.Select(placeholder="Choose a giveaway...", options=options[:25])
            select.callback = self.picked
            self.select = select
            self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This menu is not for you.", ephemeral=True)
            return False
        return True

    async def picked(self, interaction: discord.Interaction) -> None:
        kind, _, raw = self.select.values[0].partition(":")
        row = self.cog.row(int(raw))
        if row is None or row["guild_id"] != self.author.guild.id:
            return await interaction.response.edit_message(content=f"{icon('warn')} That giveaway no longer exists.", view=None)
        data = merged(row["data"])
        saved_id, saved_name = (row["id"], row["name"]) if kind == "t" else (None, "")
        if self.panel is None:
            editor = Editor(self.cog, self.author, data, saved_id=saved_id, saved_name=saved_name or "")
            text, embed, files = editor.panel()
            await interaction.response.edit_message(content=f"{icon('ok')} Loaded. Panel opened below.", view=None)
            editor.message = await interaction.channel.send(content=text, embed=embed, files=files, view=editor)  # type: ignore[union-attr]
            return
        self.panel.data.clear()
        self.panel.data.update(data)
        self.panel.saved_id, self.panel.saved_name = saved_id, saved_name or ""
        await interaction.response.edit_message(content=f"{icon('ok')} Loaded into the panel. You can edit it now.", view=None)
        await self.panel.refresh_message()


# ----------------------------------------------------------------------- cog
class Giveaway(commands.Cog):
    """Fully customisable giveaways with save / load / edit. Admin commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.dirty: set[int] = set()

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    async def cog_load(self) -> None:
        self.bot.add_view(GiveawayView())  # makes the buttons of old giveaways work after a restart
        self.loop.start()

    async def cog_unload(self) -> None:
        self.loop.cancel()

    # ------------------------------------------------------------- storage
    def row(self, giveaway_id: int) -> Optional[dict]:
        cur = self.db.execute(
            "SELECT id, guild_id, name, saved, status, channel_id, message_id, host_id, end_at, data, winners "
            "FROM giveaways WHERE id=?", (giveaway_id,),
        )
        r = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], r)) if r else None

    def find(self, guild_id: int, ref: int) -> Optional[dict]:
        r = self.db.execute(
            "SELECT id FROM giveaways WHERE guild_id=? AND saved=0 AND (id=? OR message_id=?) ORDER BY id DESC LIMIT 1",
            (guild_id, ref, ref),
        ).fetchone()
        return self.row(r[0]) if r else None

    def entries(self, giveaway_id: int) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (giveaway_id,))]

    def save_template(self, guild_id: int, name: str, data: dict, saved_id: Optional[int]) -> tuple[Optional[int], Optional[str]]:
        same = self.db.execute(
            "SELECT id FROM giveaways WHERE guild_id=? AND saved=1 AND lower(name)=lower(?)", (guild_id, name)
        ).fetchone()
        target = same[0] if same else saved_id
        if target is None:
            count = self.db.execute("SELECT COUNT(*) FROM giveaways WHERE guild_id=? AND saved=1", (guild_id,)).fetchone()[0]
            if count >= MAX_SAVED:
                return None, f"You can keep {MAX_SAVED} saved giveaways. Delete one with `{config.PREFIX}giveaway delete <name>`."
            cur = self.db.execute(
                "INSERT INTO giveaways (guild_id, name, saved, status, data, created_at) VALUES (?,?,1,'draft',?,?)",
                (guild_id, name, json.dumps(data), now().isoformat()),
            )
            target = cur.lastrowid
        else:
            self.db.execute("UPDATE giveaways SET name=?, data=? WHERE id=?", (name, json.dumps(data), target))
        self.db.commit()
        return target, None

    # -------------------------------------------------------------- launch
    async def launch(
        self, host: discord.Member, data: dict[str, Any], here: Optional[discord.abc.Messageable],
        *, duration: Optional[int] = None,
    ) -> tuple[bool, str]:
        guild = host.guild
        channel = guild.get_channel(int(data.get("channel_id") or 0)) if data.get("channel_id") else here
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return False, "I can't send giveaways in that channel."
        perms = channel.permissions_for(guild.me)
        if not (perms.send_messages and perms.embed_links):
            return False, f"I need Send Messages and Embed Links in {channel.mention}."
        seconds = int(duration if duration is not None else data["duration"])
        end_at = now() + timedelta(seconds=seconds)
        cur = self.db.execute(
            "INSERT INTO giveaways (guild_id, saved, status, channel_id, host_id, end_at, data, created_at) "
            "VALUES (?,0,'active',?,?,?,?,?)",
            (guild.id, channel.id, host.id, end_at.isoformat(), json.dumps(data), now().isoformat()),
        )
        gid = cur.lastrowid
        self.db.commit()
        embed, files = render(self, guild, data, host_id=host.id, end_at=end_at, label=f"#{gid}")
        text = " ".join(x for x in (f"<@&{data['ping_role']}>" if data["ping_role"] else "", data["content"]) if x) or None
        try:
            message = await channel.send(
                content=text, embed=embed, files=files, view=GiveawayView(data=data),
                allowed_mentions=discord.AllowedMentions(roles=True, everyone=False, users=False),
            )
        except discord.HTTPException:
            log.exception("Could not send giveaway %s", gid)
            self.db.execute("DELETE FROM giveaways WHERE id=?", (gid,))
            self.db.commit()
            return False, "Discord refused the giveaway message (check emojis, banner and my permissions)."
        self.db.execute("UPDATE giveaways SET message_id=? WHERE id=?", (message.id, gid))
        self.db.commit()
        return True, f"Giveaway **#{gid}** started in {channel.mention}. [Jump to it]({message.jump_url})"

    async def _message(self, row: dict) -> Optional[discord.Message]:
        guild = self.bot.get_guild(row["guild_id"])
        channel = guild.get_channel_or_thread(row["channel_id"]) if guild and row["channel_id"] else None
        if channel is None or not row["message_id"]:
            return None
        try:
            return await channel.fetch_message(row["message_id"])  # type: ignore[union-attr]
        except discord.HTTPException:
            return None

    async def update_live(self, giveaway_id: int, data: dict[str, Any]) -> tuple[bool, str]:
        row = self.row(giveaway_id)
        if row is None or row["status"] != "active":
            return False, "That giveaway has already ended."
        message = await self._message(row)
        guild = self.bot.get_guild(row["guild_id"])
        if message is None or guild is None:
            return False, "I can't find the giveaway message anymore."
        row = self.row(giveaway_id)  # end time may have been changed in the panel
        self.db.execute("UPDATE giveaways SET data=? WHERE id=?", (json.dumps(data), giveaway_id))
        self.db.commit()
        embed, files = render(
            self, guild, data, host_id=row["host_id"], end_at=datetime.fromisoformat(row["end_at"]), label=f"#{giveaway_id}"
        )
        try:
            await message.edit(embed=embed, attachments=files, view=GiveawayView(data=data, count=len(self.entries(giveaway_id))))
        except discord.HTTPException:
            return False, "Discord refused the update (check emojis / banner)."
        return True, f"Giveaway **#{giveaway_id}** updated. [Jump to it]({message.jump_url})"

    # ------------------------------------------------------------ joining
    async def handle_join(self, interaction: discord.Interaction) -> None:
        row = self.db.execute(
            "SELECT id FROM giveaways WHERE message_id=? AND saved=0", (interaction.message.id if interaction.message else 0,)
        ).fetchone()
        info = self.row(row[0]) if row else None
        if info is None or info["status"] != "active":
            return await interaction.response.send_message(f"{icon('hourglass')} This giveaway has ended.", ephemeral=True)
        data = merged(info["data"])
        member = interaction.user
        if isinstance(member, discord.Member) and data["req_role"] and not any(r.id == data["req_role"] for r in member.roles):
            return await interaction.response.send_message(
                f"{icon('lock')} You need the <@&{data['req_role']}> role to enter.", ephemeral=True
            )
        already = self.db.execute(
            "SELECT 1 FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (info["id"], member.id)
        ).fetchone()
        if already:
            return await interaction.response.send_message(
                f"{icon('ok')} You are already in this giveaway.", view=LeaveView(self, info["id"], member.id), ephemeral=True
            )
        self.db.execute("INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?,?)", (info["id"], member.id))
        self.db.commit()
        self.dirty.add(info["id"])
        await interaction.response.send_message(f"{data['join_emoji']} You entered the giveaway. Good luck!", ephemeral=True)

    async def handle_list(self, interaction: discord.Interaction) -> None:
        row = self.db.execute(
            "SELECT id FROM giveaways WHERE message_id=? AND saved=0", (interaction.message.id if interaction.message else 0,)
        ).fetchone()
        if row is None:
            return await interaction.response.send_message("Giveaway not found.", ephemeral=True)
        users = self.entries(row[0])
        shown = " ".join(f"<@{u}>" for u in users[:40]) or "Nobody yet."
        more = f"\n… and {len(users) - 40} more" if len(users) > 40 else ""
        await interaction.response.send_message(f"**Participants ({len(users)})**\n{shown}{more}", ephemeral=True)

    # ------------------------------------------------------------- ending
    def _pick(self, row: dict, count: int, exclude: set[int]) -> list[int]:
        guild = self.bot.get_guild(row["guild_id"])
        data = merged(row["data"])
        pool = []
        for uid in self.entries(row["id"]):
            if uid in exclude:
                continue
            member = guild.get_member(uid) if guild else None
            if member is None or member.bot:
                continue
            if data["req_role"] and not any(r.id == data["req_role"] for r in member.roles):
                continue
            pool.append(uid)
        return random.sample(pool, min(count, len(pool)))

    async def finish(self, giveaway_id: int, *, reroll: bool = False, count: Optional[int] = None) -> Optional[list[int]]:
        row = self.row(giveaway_id)
        if row is None:
            return None
        data = merged(row["data"])
        previous = json.loads(row["winners"] or "[]")
        if reroll:
            winners = self._pick(row, count or int(data["winners"]), set(previous))
            all_winners = previous + winners
        else:
            winners = self._pick(row, int(data["winners"]), set())
            all_winners = winners
        self.db.execute(
            "UPDATE giveaways SET status='ended', winners=? WHERE id=?", (json.dumps(all_winners), giveaway_id)
        )
        self.db.commit()
        self.dirty.discard(giveaway_id)

        guild = self.bot.get_guild(row["guild_id"])
        message = await self._message(row)
        if guild is None or message is None:
            return winners
        embed, files = render(
            self, guild, data, host_id=row["host_id"], end_at=datetime.fromisoformat(row["end_at"]),
            label=f"#{giveaway_id}", ended=True, winners=all_winners,
        )
        try:
            await message.edit(
                embed=embed, attachments=files, view=GiveawayView(data=data, count=len(self.entries(giveaway_id)), ended=True)
            )
        except discord.HTTPException:
            log.exception("Could not edit ended giveaway %s", giveaway_id)
        try:
            if winners:
                await message.reply(
                    f"{data['emoji_winners']} Congratulations " + ", ".join(f"<@{w}>" for w in winners)
                    + f"! You won **{data['prize']}**",
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            else:
                await message.reply(f"{icon('warn')} Not enough valid entries, so there is no winner for **{data['prize']}**.")
        except discord.HTTPException:
            pass
        return winners

    @tasks.loop(seconds=10)
    async def loop(self) -> None:
        try:
            due = self.db.execute(
                "SELECT id FROM giveaways WHERE status='active' AND saved=0 AND end_at<=?", (now().isoformat(),)
            ).fetchall()
            for (gid,) in due:
                await self.finish(gid)
            for gid in list(self.dirty):  # refresh the participant counters (one edit per giveaway per cycle)
                self.dirty.discard(gid)
                row = self.row(gid)
                if row is None or row["status"] != "active":
                    continue
                message = await self._message(row)
                if message is not None:
                    try:
                        await message.edit(view=GiveawayView(data=merged(row["data"]), count=len(self.entries(gid))))
                    except discord.HTTPException:
                        pass
        except Exception:
            log.exception("Giveaway loop failed")

    @loop.before_loop
    async def _wait(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ commands
    @commands.hybrid_group(
        name="giveaway", aliases=["gw"], invoke_without_command=True, fallback="help",
        description="Create and manage giveaways (admins only).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway(self, ctx: commands.Context) -> None:
        p = config.PREFIX
        embed = discord.Embed(
            title=f"{icon('gift')} Giveaway",
            description=(
                f"`{p}giveaway edit` - open the full editor panel\n"
                f"`{p}giveaway edit <name|id>` - edit a saved or running giveaway\n"
                f"`{p}giveaway load` - load a saved / previous giveaway\n"
                f"`{p}giveaway start <time> <winners> <prize>` - quick start\n"
                f"`{p}giveaway end <id>` • `{p}giveaway reroll <id> [count]`\n"
                f"`{p}giveaway list` • `{p}giveaway saved` • `{p}giveaway delete <name>`"
            ),
            color=config.ACCENT,
        )
        await ctx.send(embed=embed)

    async def _open_panel(self, ctx: commands.Context, data: dict, **kwargs) -> None:
        assert isinstance(ctx.author, discord.Member)
        editor = Editor(self, ctx.author, data, **kwargs)
        text, embed, files = editor.panel()
        editor.message = await ctx.send(content=text, embed=embed, files=files, view=editor)

    @giveaway.command(name="edit", description="Open the giveaway editor panel.")
    @app_commands.describe(target="A saved giveaway name, or the ID of a giveaway to edit.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_edit(self, ctx: commands.Context, *, target: Optional[str] = None) -> None:
        assert ctx.guild is not None
        if not target:
            return await self._open_panel(ctx, merged(None))
        saved = self.db.execute(
            "SELECT id, name, data FROM giveaways WHERE guild_id=? AND saved=1 AND lower(name)=lower(?)",
            (ctx.guild.id, target.strip()),
        ).fetchone()
        if saved:
            return await self._open_panel(ctx, merged(saved[2]), saved_id=saved[0], saved_name=saved[1])
        if target.strip().isdigit():
            row = self.find(ctx.guild.id, int(target))
            if row:
                if row["status"] == "active":
                    return await self._open_panel(ctx, merged(row["data"]), live_id=row["id"])
                return await self._open_panel(ctx, merged(row["data"]))  # ended: copy as a new giveaway
        await ctx.send(f"{icon('warn')} No saved giveaway or giveaway ID `{target}`. See `{config.PREFIX}giveaway saved`.", ephemeral=True)

    @giveaway.command(name="load", description="Load a saved or previous giveaway into the editor.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_load(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        view = LoadView(self, ctx.author, ctx.guild.id)
        if not view.has_options:
            return await ctx.send(f"{icon('warn')} Nothing saved yet. Use `{config.PREFIX}giveaway edit`, then **Save**.", ephemeral=True)
        await ctx.send("Pick a saved or previous giveaway:", view=view)

    @giveaway.command(name="start", description="Quick start: !giveaway start <time> <winners> <prize>")
    @app_commands.describe(duration="Like 30m, 2h, 1d.", winners="Number of winners.", prize="The prize.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_start(self, ctx: commands.Context, duration: str, winners: int, *, prize: str) -> None:
        assert ctx.guild is not None and isinstance(ctx.author, discord.Member)
        dur = parse_duration(duration)
        if dur is None or not MIN_SECONDS <= dur.total_seconds() <= MAX_SECONDS or not 1 <= winners <= 50:
            return await ctx.send(f"{icon('warn')} Usage: `{config.PREFIX}giveaway start 1h 1 Nitro` (time 10s-60d, winners 1-50).", ephemeral=True)
        data = merged(None)
        data.update(prize=prize[:200], winners=winners, duration=int(dur.total_seconds()))
        ok, text = await self.launch(ctx.author, data, ctx.channel)
        await ctx.send(f"{icon('ok') if ok else icon('warn')} {text}", ephemeral=not ok)

    @giveaway.command(name="end", description="End a giveaway now and pick the winners.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_end(self, ctx: commands.Context, giveaway_id: int) -> None:
        assert ctx.guild is not None
        row = self.find(ctx.guild.id, giveaway_id)
        if row is None or row["status"] != "active":
            return await ctx.send(f"{icon('warn')} No running giveaway with ID `{giveaway_id}`.", ephemeral=True)
        await self.finish(row["id"])
        await ctx.send(f"{icon('ok')} Giveaway **#{row['id']}** ended.")

    @giveaway.command(name="reroll", description="Pick new winner(s) for an ended giveaway.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_reroll(self, ctx: commands.Context, giveaway_id: int, count: int = 1) -> None:
        assert ctx.guild is not None
        row = self.find(ctx.guild.id, giveaway_id)
        if row is None or row["status"] != "ended":
            return await ctx.send(f"{icon('warn')} No ended giveaway with ID `{giveaway_id}`.", ephemeral=True)
        await self.finish(row["id"], reroll=True, count=max(1, min(count, 50)))
        await ctx.send(f"{icon('ok')} Rerolled giveaway **#{row['id']}**.")

    @giveaway.command(name="list", description="Running giveaways of this server.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_list(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        rows = self.db.execute(
            "SELECT id, channel_id, message_id, end_at, data FROM giveaways WHERE guild_id=? AND status='active' AND saved=0 ORDER BY end_at",
            (ctx.guild.id,),
        ).fetchall()
        if not rows:
            return await ctx.send(f"{icon('warn')} No running giveaways.")
        lines = [
            f"`#{r[0]}` **{merged(r[4])['prize']}** • <#{r[1]}> • ends {discord.utils.format_dt(datetime.fromisoformat(r[3]), 'R')} • {len(self.entries(r[0]))} entries"
            for r in rows
        ]
        await ctx.send(embed=discord.Embed(title=f"{icon('gift')} Running giveaways", description="\n".join(lines)[:4000], color=config.ACCENT))

    @giveaway.command(name="saved", description="Saved giveaways of this server.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_saved(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        rows = self.db.execute("SELECT name, data FROM giveaways WHERE guild_id=? AND saved=1 ORDER BY name", (ctx.guild.id,)).fetchall()
        if not rows:
            return await ctx.send(f"{icon('warn')} Nothing saved yet. Open `{config.PREFIX}giveaway edit` and press **Save**.")
        lines = [f"**{name}** • prize: {merged(data)['prize']}" for name, data in rows]
        await ctx.send(embed=discord.Embed(title=f"{icon('folder')} Saved giveaways", description="\n".join(lines)[:4000], color=config.ACCENT,))

    @giveaway.command(name="delete", description="Delete a saved giveaway.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def giveaway_delete(self, ctx: commands.Context, *, name: str) -> None:
        assert ctx.guild is not None
        cur = self.db.execute(
            "DELETE FROM giveaways WHERE guild_id=? AND saved=1 AND lower(name)=lower(?)", (ctx.guild.id, name.strip())
        )
        self.db.commit()
        if cur.rowcount:
            await ctx.send(f"{icon('trash')} Deleted saved giveaway **{name}**.")
        else:
            await ctx.send(f"{icon('warn')} No saved giveaway named `{name}`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Giveaway(bot))
