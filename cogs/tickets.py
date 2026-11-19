"""Ticket system.

Admin commands (Manage Server):
  ticketset [#channel]          send the ticket panel (Components V2) and remember its channel
  ticketedit                    edit panel text/banner, ticket message, support + admin roles, ticket limit
  ticketcategory [create|edit|delete] <name>   manage categories (each has its own channel category)
  ticketlogs [#channel]         set the opened / closed / transcript log channels

Panel, ticket buttons and dropdown are handled in on_interaction by custom_id, so they keep working
after a restart without re-sending anything. Only the panel is Components V2, everything else is v1.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import images, imagehost
from utils import transcript as tscript
from utils.icons import icon
from utils.ui import make_embed

log = logging.getLogger("premium-bot.tickets")

PANEL_SELECT_ID = "wq_tk:panel"
CLOSE_ID = "wq_tk:close"
CLOSE_REASON_ID = "wq_tk:close_reason"
CLAIM_ID = "wq_tk:claim"

MAX_CATEGORIES = 25
DM_FILE_LIMIT = 10 * 1024 * 1024
CLOSE_DELAY = 5

CFG_FIELDS = (
    "panel_channel_id", "panel_message_id", "panel_text", "panel_banner", "panel_color",
    "panel_placeholder", "welcome_title", "welcome_description", "welcome_color",
    "support_roles", "admin_roles", "log_open", "log_close", "log_transcript", "max_tickets",
)
CAT_FIELDS = ("name", "label", "emoji", "question", "placeholder", "channel_category_id")
CAT_COLS = ("id", "guild_id") + CAT_FIELDS
TICKET_COLS = (
    "id", "guild_id", "number", "channel_id", "user_id", "category_id", "category_name", "answer",
    "claimed_by", "status", "created_at", "closed_at", "closed_by", "close_reason",
)

CUSTOM_EMOJI = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")
HEX_COLOR = re.compile(r"^#?[0-9a-fA-F]{6}$")


# ------------------------------------------------------------------ helpers
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_dt(value: Optional[str]) -> datetime:
    try:
        parsed = datetime.fromisoformat(value or "")
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def parse_ids(value) -> list[int]:
    return [int(x) for x in str(value or "").split(",") if x.strip().isdigit()]


def parse_color(value, default: int) -> int:
    try:
        return int(str(value).strip().lstrip("#"), 16) & 0xFFFFFF
    except (TypeError, ValueError):
        return default


def clean_emoji(value: str) -> Optional[str]:
    """None for empty input, the emoji for valid input, ValueError otherwise."""
    value = (value or "").strip()
    if not value:
        return None
    if CUSTOM_EMOJI.match(value):
        return value
    if len(value) <= 16 and not any(ch.isascii() and ch.isalnum() for ch in value):
        return value
    raise ValueError("emoji")


def channel_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_\-]", "", name.lower().replace(" ", "-"))
    return (slug or "user")[:80]


def powered(embed: discord.Embed) -> discord.Embed:
    embed.set_footer(text=f"Powered by {config.BOT_NAME}")
    return embed


def number_str(number: int) -> str:
    return f"#{int(number):04d}"


async def reply(interaction: discord.Interaction, content: Optional[str] = None, *, embed=None, ephemeral=True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, embed=embed, ephemeral=ephemeral)


# ------------------------------------------------------------------ generic view bits
class OwnedView(discord.ui.View):
    """A view only the admin who opened it can use."""

    def __init__(self, author_id: int, *, timeout: float = 600) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"{icon('lock')} Only the person who ran the command can use this.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ConfirmView(OwnedView):
    def __init__(self, author_id: int) -> None:
        super().__init__(author_id, timeout=60)
        self.confirmed: Optional[bool] = None

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.confirmed = False
        self.stop()
        await interaction.response.defer()


# ------------------------------------------------------------------ modals
class TicketModal(discord.ui.Modal):
    """The form a member fills in after picking a category."""

    def __init__(self, cog: "Tickets", category: dict) -> None:
        super().__init__(title=category["name"][:45], timeout=600)
        self.cog = cog
        self.category = category
        self.answer = discord.ui.TextInput(
            label=(category["question"] or category["name"])[:45],
            placeholder=(category["placeholder"] or "Kindly mention your issue!")[:100],
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1000,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.create_ticket(interaction, self.category, self.answer.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Ticket form failed", exc_info=error)
        await self.cog.respond_error(interaction)


class CloseReasonModal(discord.ui.Modal, title="Close ticket with reason"):
    reason = discord.ui.TextInput(
        label="Reason", style=discord.TextStyle.paragraph, placeholder="Why is this ticket being closed?",
        required=True, max_length=500,
    )

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.begin_close(interaction, self.reason.value.strip())

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Close reason form failed", exc_info=error)
        await self.cog.respond_error(interaction)


class PanelEditModal(discord.ui.Modal, title="Edit ticket panel"):
    def __init__(self, cog: "Tickets", guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=600)
        self.cog, self.guild, self.author_id = cog, guild, author_id
        cfg = cog.get_cfg(guild.id)
        self.text_in = discord.ui.TextInput(
            label="Panel text (markdown works)", style=discord.TextStyle.paragraph,
            default=(cfg["panel_text"] or config.TICKET_PANEL_TEXT)[:3500], max_length=3500,
        )
        self.color_in = discord.ui.TextInput(
            label="Color (#HEX)", placeholder="#2ECC71",
            default=cfg["panel_color"] or "", required=False, max_length=7,
        )
        self.placeholder_in = discord.ui.TextInput(
            label="Dropdown placeholder", default=cfg["panel_placeholder"] or config.TICKET_PANEL_PLACEHOLDER,
            required=False, max_length=100,
        )
        for item in (self.text_in, self.color_in, self.placeholder_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color = (self.color_in.value or "").strip()
        if color and not HEX_COLOR.match(color):
            return await interaction.response.send_message(
                f"{icon('warn')} Color must look like `#2ECC71`. Nothing was saved.", ephemeral=True
            )
        await interaction.response.defer()
        self.cog.save_cfg(
            self.guild.id,
            panel_text=self.text_in.value.strip() or None,
            panel_color=color or None,
            panel_placeholder=(self.placeholder_in.value or "").strip() or None,
        )
        await self.cog.refresh_panel(self.guild)
        await show_edit(interaction, self.cog, self.guild, self.author_id)


class WelcomeEditModal(discord.ui.Modal, title="Edit ticket message"):
    def __init__(self, cog: "Tickets", guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=600)
        self.cog, self.guild, self.author_id = cog, guild, author_id
        cfg = cog.get_cfg(guild.id)
        self.title_in = discord.ui.TextInput(
            label="Title", default=cfg["welcome_title"] or config.TICKET_WELCOME_TITLE, max_length=256
        )
        self.desc_in = discord.ui.TextInput(
            label="Description ({user} {server} {category})", style=discord.TextStyle.paragraph,
            default=(cfg["welcome_description"] or config.TICKET_WELCOME_DESCRIPTION)[:2000], max_length=2000,
        )
        self.color_in = discord.ui.TextInput(
            label="Color (#HEX)", placeholder="#2ECC71", default=cfg["welcome_color"] or "", required=False, max_length=7
        )
        for item in (self.title_in, self.desc_in, self.color_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color = (self.color_in.value or "").strip()
        if color and not HEX_COLOR.match(color):
            return await interaction.response.send_message(
                f"{icon('warn')} Color must look like `#2ECC71`. Nothing was saved.", ephemeral=True
            )
        await interaction.response.defer()
        self.cog.save_cfg(
            self.guild.id,
            welcome_title=self.title_in.value.strip() or None,
            welcome_description=self.desc_in.value.strip() or None,
            welcome_color=color or None,
        )
        await show_edit(interaction, self.cog, self.guild, self.author_id)


class SettingsModal(discord.ui.Modal, title="Ticket settings"):
    def __init__(self, cog: "Tickets", guild: discord.Guild, author_id: int) -> None:
        super().__init__(timeout=600)
        self.cog, self.guild, self.author_id = cog, guild, author_id
        cfg = cog.get_cfg(guild.id)
        self.limit_in = discord.ui.TextInput(
            label="Open tickets allowed per member (1-10)", default=str(cfg["max_tickets"]), max_length=2
        )
        self.add_item(self.limit_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = (self.limit_in.value or "").strip()
        if not raw.isdigit() or not 1 <= int(raw) <= 10:
            return await interaction.response.send_message(
                f"{icon('warn')} Enter a number from 1 to 10. Nothing was saved.", ephemeral=True
            )
        await interaction.response.defer()
        self.cog.save_cfg(self.guild.id, max_tickets=int(raw))
        await show_edit(interaction, self.cog, self.guild, self.author_id)


class CategoryModal(discord.ui.Modal, title="Edit ticket category"):
    def __init__(self, cog: "Tickets", guild: discord.Guild, author_id: int, category: dict) -> None:
        super().__init__(timeout=600)
        self.cog, self.guild, self.author_id, self.category = cog, guild, author_id, category
        self.name_in = discord.ui.TextInput(label="Category name (used inside the ticket)", default=category["name"], max_length=50)
        self.label_in = discord.ui.TextInput(
            label="Dropdown text", default=category["label"] or category["name"], max_length=100
        )
        self.emoji_in = discord.ui.TextInput(
            label="Dropdown emoji", placeholder="📩", default=category["emoji"] or "", required=False, max_length=80
        )
        self.question_in = discord.ui.TextInput(
            label="Form field title", default=category["question"] or category["name"], max_length=45
        )
        self.holder_in = discord.ui.TextInput(
            label="Form placeholder", default=category["placeholder"] or "", required=False, max_length=100
        )
        for item in (self.name_in, self.label_in, self.emoji_in, self.question_in, self.holder_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.name_in.value.strip()
        other = self.cog.find_category(self.guild.id, name)
        if other is not None and other["id"] != self.category["id"]:
            return await interaction.response.send_message(
                f"{icon('warn')} Another category is already called **{name}**. Nothing was saved.", ephemeral=True
            )
        try:
            emoji = clean_emoji(self.emoji_in.value)
        except ValueError:
            return await interaction.response.send_message(
                f"{icon('warn')} That is not a valid emoji. Use a normal emoji or a server emoji like `<:name:123>`. "
                "Nothing was saved.", ephemeral=True,
            )
        await interaction.response.defer()
        self.cog.update_category(
            self.category["id"],
            name=name,
            label=self.label_in.value.strip() or name,
            emoji=emoji,
            question=self.question_in.value.strip() or name,
            placeholder=(self.holder_in.value or "").strip() or None,
        )
        await self.cog.refresh_panel(self.guild)
        view = CategoryEditView(self.cog, self.author_id, self.guild, self.category["id"])
        view.message = interaction.message
        await interaction.edit_original_response(embed=self.cog.category_embed(self.guild, self.category["id"]), view=view)


# ------------------------------------------------------------------ admin views
async def show_edit(interaction: discord.Interaction, cog: "Tickets", guild: discord.Guild, author_id: int) -> None:
    """Redraw the !ticketedit panel after a modal (the modal response was already deferred)."""
    view = TicketEditView(cog, author_id, guild)
    view.message = interaction.message
    await interaction.edit_original_response(embed=cog.edit_embed(guild), view=view)


class TicketEditView(OwnedView):
    def __init__(self, cog: "Tickets", author_id: int, guild: discord.Guild) -> None:
        super().__init__(author_id)
        self.cog, self.guild = cog, guild
        cfg = cog.get_cfg(guild.id)
        support = discord.ui.RoleSelect(
            placeholder="Support roles (can see, claim & close tickets)", min_values=0, max_values=10,
            default_values=cog.roles_from_ids(guild, cfg["support_ids"]), row=1,
        )
        admin = discord.ui.RoleSelect(
            placeholder="Admin roles (can see, claim & close tickets)", min_values=0, max_values=10,
            default_values=cog.roles_from_ids(guild, cfg["admin_ids"]), row=2,
        )
        support.callback = self._role_callback("support_roles", support)
        admin.callback = self._role_callback("admin_roles", admin)
        self.add_item(support)
        self.add_item(admin)

    def _role_callback(self, field: str, select: discord.ui.RoleSelect):
        async def callback(interaction: discord.Interaction) -> None:
            ids = [role.id for role in select.values if role.id != self.guild.id]
            self.cog.save_cfg(self.guild.id, **{field: ",".join(str(i) for i in ids)})
            new = TicketEditView(self.cog, self.author_id, self.guild)
            await interaction.response.edit_message(embed=self.cog.edit_embed(self.guild), view=new)
            new.message = interaction.message
            self.stop()

        return callback

    @discord.ui.button(label="Panel message", emoji=icon("clipboard"), style=discord.ButtonStyle.primary, row=0)
    async def edit_panel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PanelEditModal(self.cog, self.guild, self.author_id))

    @discord.ui.button(label="Upload banner", emoji=icon("banner"), style=discord.ButtonStyle.primary, row=0)
    async def upload_banner(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        marker = await imagehost.ask_image(interaction, self.cog.bot, author_id=self.author_id, what="panel banner")
        if marker is None:
            return
        self.cog.save_cfg(self.guild.id, panel_banner=marker or None)
        await self.cog.refresh_panel(self.guild)
        await interaction.followup.send(
            f"{icon('ok')} Panel banner {'saved' if marker else 'removed'} and the panel was updated.", ephemeral=True
        )

    @discord.ui.button(label="Ticket message", emoji=icon("ticket"), style=discord.ButtonStyle.primary, row=0)
    async def edit_welcome(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(WelcomeEditModal(self.cog, self.guild, self.author_id))

    @discord.ui.button(label="Settings", emoji=icon("gear"), style=discord.ButtonStyle.secondary, row=0)
    async def edit_settings(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(SettingsModal(self.cog, self.guild, self.author_id))

    @discord.ui.button(label="Resend panel", emoji=icon("recycle"), style=discord.ButtonStyle.secondary, row=0)
    async def resend(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = self.cog.get_cfg(self.guild.id)
        channel = self.guild.get_channel(cfg["panel_channel_id"]) if cfg["panel_channel_id"] else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                f"{icon('warn')} No panel channel yet. Use `{config.PREFIX}ticketset #channel` first.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cog.send_panel(channel)
        except discord.HTTPException as exc:
            return await interaction.followup.send(f"{icon('error')} Could not send the panel: {exc.text}", ephemeral=True)
        await interaction.followup.send(f"{icon('ok')} Panel sent again in {channel.mention}.", ephemeral=True)


class TicketLogsView(OwnedView):
    SLOTS = (
        ("log_open", "Ticket opened logs"),
        ("log_close", "Ticket closed logs"),
        ("log_transcript", "Ticket transcript logs"),
    )

    def __init__(self, cog: "Tickets", author_id: int, guild: discord.Guild) -> None:
        super().__init__(author_id)
        self.cog, self.guild = cog, guild
        cfg = cog.get_cfg(guild.id)
        for row, (key, label) in enumerate(self.SLOTS):
            current = guild.get_channel(cfg[key]) if cfg[key] else None
            select = discord.ui.ChannelSelect(
                placeholder=label, channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                min_values=0, max_values=1, default_values=[current] if current else [], row=row,
            )
            select.callback = self._callback(key, select)
            self.add_item(select)

    def _callback(self, key: str, select: discord.ui.ChannelSelect):
        async def callback(interaction: discord.Interaction) -> None:
            values = select.values
            self.cog.save_cfg(self.guild.id, **{key: values[0].id if values else None})
            new = TicketLogsView(self.cog, self.author_id, self.guild)
            await interaction.response.edit_message(embed=self.cog.logs_embed(self.guild), view=new)
            new.message = interaction.message
            self.stop()

        return callback

    @discord.ui.button(label="Reset all", style=discord.ButtonStyle.danger, row=3)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.cog.save_cfg(self.guild.id, log_open=None, log_close=None, log_transcript=None)
        new = TicketLogsView(self.cog, self.author_id, self.guild)
        await interaction.response.edit_message(embed=self.cog.logs_embed(self.guild), view=new)
        new.message = interaction.message
        self.stop()


class CategoryEditView(OwnedView):
    def __init__(self, cog: "Tickets", author_id: int, guild: discord.Guild, category_id: int) -> None:
        super().__init__(author_id)
        self.cog, self.guild, self.category_id = cog, guild, category_id
        category = cog.get_category(category_id)
        current = guild.get_channel(category["channel_category_id"]) if category and category["channel_category_id"] else None
        self.select = discord.ui.ChannelSelect(
            placeholder="Channel category where these tickets are created",
            channel_types=[discord.ChannelType.category], min_values=0, max_values=1,
            default_values=[current] if current else [], row=1,
        )
        self.select.callback = self._picked
        self.add_item(self.select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = self.select.values
        self.cog.update_category(self.category_id, channel_category_id=values[0].id if values else None)
        new = CategoryEditView(self.cog, self.author_id, self.guild, self.category_id)
        await interaction.response.edit_message(embed=self.cog.category_embed(self.guild, self.category_id), view=new)
        new.message = interaction.message
        self.stop()

    @discord.ui.button(label="Edit details", emoji=icon("clipboard"), style=discord.ButtonStyle.primary, row=0)
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        category = self.cog.get_category(self.category_id)
        if category is None:
            return await interaction.response.send_message(f"{icon('warn')} This category was deleted.", ephemeral=True)
        await interaction.response.send_modal(CategoryModal(self.cog, self.guild, self.author_id, category))


# ------------------------------------------------------------------ the cog
class Tickets(commands.Cog):
    """Ticket panel, categories, logs and HTML transcripts. Setup commands need Manage Server."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._closing: set[int] = set()
        self._opening: set[tuple[int, int]] = set()

    @property
    def db(self):
        return self.bot.db.conn  # type: ignore[attr-defined]

    # ------------------------------------------------------------- storage: config
    def get_cfg(self, guild_id: int) -> dict:
        row = self.db.execute(
            f"SELECT {', '.join(CFG_FIELDS)} FROM ticket_config WHERE guild_id=?", (guild_id,)
        ).fetchone()
        cfg = dict(zip(CFG_FIELDS, row)) if row else dict.fromkeys(CFG_FIELDS)
        cfg["support_ids"] = parse_ids(cfg["support_roles"])
        cfg["admin_ids"] = parse_ids(cfg["admin_roles"])
        cfg["max_tickets"] = cfg["max_tickets"] or 1
        return cfg

    def save_cfg(self, guild_id: int, **changes) -> None:
        unknown = set(changes) - set(CFG_FIELDS)
        if unknown:
            raise ValueError(f"unknown ticket setting(s): {unknown}")
        self.db.execute("INSERT OR IGNORE INTO ticket_config (guild_id) VALUES (?)", (guild_id,))
        if changes:
            columns = ", ".join(f"{key}=?" for key in changes)
            self.db.execute(f"UPDATE ticket_config SET {columns} WHERE guild_id=?", (*changes.values(), guild_id))
        self.db.commit()

    # ------------------------------------------------------------- storage: categories
    def categories(self, guild_id: int) -> list[dict]:
        rows = self.db.execute(
            f"SELECT {', '.join(CAT_COLS)} FROM ticket_categories WHERE guild_id=? ORDER BY id", (guild_id,)
        ).fetchall()
        return [dict(zip(CAT_COLS, row)) for row in rows]

    def get_category(self, category_id: int) -> Optional[dict]:
        row = self.db.execute(
            f"SELECT {', '.join(CAT_COLS)} FROM ticket_categories WHERE id=?", (category_id,)
        ).fetchone()
        return dict(zip(CAT_COLS, row)) if row else None

    def find_category(self, guild_id: int, key: str) -> Optional[dict]:
        key = (key or "").strip().lower()
        if not key:
            return None
        for category in self.categories(guild_id):
            if str(category["id"]) == key or category["name"].lower() == key or (category["label"] or "").lower() == key:
                return category
        return None

    def ensure_defaults(self, guild_id: int) -> None:
        if self.db.execute("SELECT 1 FROM ticket_categories WHERE guild_id=? LIMIT 1", (guild_id,)).fetchone():
            return
        for item in config.TICKET_DEFAULT_CATEGORIES:
            self.add_category(guild_id, item["name"], label=item.get("label"), emoji=item.get("emoji"),
                              question=item.get("question"), placeholder=item.get("placeholder"))

    def add_category(self, guild_id: int, name: str, *, label=None, emoji="📩", question=None, placeholder=None) -> int:
        cursor = self.db.execute(
            "INSERT INTO ticket_categories (guild_id, name, label, emoji, question, placeholder) VALUES (?,?,?,?,?,?)",
            (guild_id, name, label or name, emoji, (question or name)[:45], placeholder or "Kindly mention your issue!"),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def update_category(self, category_id: int, **changes) -> None:
        unknown = set(changes) - set(CAT_FIELDS)
        if unknown:
            raise ValueError(f"unknown category field(s): {unknown}")
        columns = ", ".join(f"{key}=?" for key in changes)
        self.db.execute(f"UPDATE ticket_categories SET {columns} WHERE id=?", (*changes.values(), category_id))
        self.db.commit()

    def delete_category(self, category_id: int) -> None:
        self.db.execute("DELETE FROM ticket_categories WHERE id=?", (category_id,))
        self.db.commit()

    # ------------------------------------------------------------- storage: tickets
    def get_ticket(self, channel_id: int) -> Optional[dict]:
        row = self.db.execute(
            f"SELECT {', '.join(TICKET_COLS)} FROM tickets WHERE channel_id=? ORDER BY id DESC LIMIT 1", (channel_id,)
        ).fetchone()
        return dict(zip(TICKET_COLS, row)) if row else None

    def open_channels_of(self, guild: discord.Guild, user_id: int) -> list[int]:
        """Channels of the member's open tickets. Tickets whose channel was deleted by hand are closed here."""
        rows = self.db.execute(
            "SELECT channel_id FROM tickets WHERE guild_id=? AND user_id=? AND status='open'", (guild.id, user_id)
        ).fetchall()
        alive = []
        for (channel_id,) in rows:
            if guild.get_channel(channel_id) is None:
                self.db.execute(
                    "UPDATE tickets SET status='closed', closed_at=?, close_reason='Channel deleted' "
                    "WHERE channel_id=? AND status='open'", (now_iso(), channel_id),
                )
            else:
                alive.append(channel_id)
        self.db.commit()
        return alive

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self.db.execute(
            "UPDATE tickets SET status='closed', closed_at=?, close_reason='Channel deleted' "
            "WHERE channel_id=? AND status='open'", (now_iso(), channel.id),
        )
        self.db.commit()

    # ------------------------------------------------------------- roles / permissions
    @staticmethod
    def roles_from_ids(guild: discord.Guild, ids: list[int]) -> list[discord.Role]:
        roles = (guild.get_role(i) for i in ids)
        return [r for r in roles if r is not None and not r.is_default()]

    @staticmethod
    def staff_ids(cfg: dict) -> list[int]:
        return list(dict.fromkeys(cfg["support_ids"] + cfg["admin_ids"]))

    def is_staff(self, member: discord.abc.User, cfg: dict) -> bool:
        """Only the configured support/admin roles may claim and close. With no role set yet, members
        with Manage Server may (otherwise nobody could ever close a ticket)."""
        if not isinstance(member, discord.Member):
            return False
        ids = self.staff_ids(cfg)
        if not ids:
            return member.guild_permissions.administrator or member.guild_permissions.manage_guild
        return any(role.id in ids for role in member.roles)

    # ------------------------------------------------------------- embeds for admins
    def edit_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.get_cfg(guild.id)

        def roles(ids: list[int]) -> str:
            return " ".join(f"<@&{i}>" for i in ids) or "Not set"

        panel = f"<#{cfg['panel_channel_id']}>" if cfg["panel_channel_id"] else f"Not set (`{config.PREFIX}ticketset`)"
        names = ", ".join(c["name"] for c in self.categories(guild.id)) or "None yet"
        embed = make_embed(
            f"{icon('gear')} Ticket settings",
            "Use the buttons to edit messages and the menus to pick roles.\n"
            f"Categories: `{config.PREFIX}ticketcategory` • Logs: `{config.PREFIX}ticketlogs`",
            color=config.TICKET_COLOR,
        )
        embed.add_field(name="Panel channel", value=panel)
        embed.add_field(name="Open tickets per member", value=str(cfg["max_tickets"]))
        embed.add_field(name="Categories", value=names[:1000], inline=False)
        embed.add_field(name="Support roles", value=roles(cfg["support_ids"]), inline=False)
        embed.add_field(name="Admin roles", value=roles(cfg["admin_ids"]), inline=False)
        if not self.staff_ids(cfg):
            embed.set_footer(text="No role set yet: members with Manage Server can claim and close tickets.")
        else:
            embed.set_footer(text="Only these roles can claim and close tickets.")
        return embed

    def logs_embed(self, guild: discord.Guild) -> discord.Embed:
        cfg = self.get_cfg(guild.id)

        def show(key: str) -> str:
            return f"<#{cfg[key]}>" if cfg[key] else "Not set"

        embed = make_embed(
            f"{icon('clipboard')} Ticket logs",
            "Pick a channel for each log. Clear a menu to turn that log off.",
            color=config.TICKET_COLOR,
        )
        embed.add_field(name="Ticket opened", value=show("log_open"))
        embed.add_field(name="Ticket closed", value=show("log_close"))
        embed.add_field(name="Ticket transcript", value=show("log_transcript"))
        return embed

    def category_embed(self, guild: discord.Guild, category_id: int) -> discord.Embed:
        category = self.get_category(category_id)
        if category is None:
            return make_embed("Category not found", "It was deleted.")
        parent = f"<#{category['channel_category_id']}>" if category["channel_category_id"] else "Default (same as the panel channel)"
        embed = make_embed(
            f"{category['emoji'] or ''} {category['name']}".strip(),
            "Edit the texts with the button and choose the channel category with the menu.",
            color=config.TICKET_COLOR,
        )
        embed.add_field(name="Dropdown text", value=category["label"] or category["name"])
        embed.add_field(name="Form field title", value=category["question"] or category["name"])
        embed.add_field(name="Form placeholder", value=category["placeholder"] or "—", inline=False)
        embed.add_field(name="Tickets are created in", value=parent, inline=False)
        return embed

    # ------------------------------------------------------------- panel (Components V2)
    def build_panel(self, guild: discord.Guild) -> discord.ui.LayoutView:
        cfg = self.get_cfg(guild.id)
        text = (cfg["panel_text"] or config.TICKET_PANEL_TEXT).replace("{server}", guild.name)[:3500]
        color = parse_color(cfg["panel_color"], config.TICKET_COLOR)
        parts: list[discord.ui.Item] = [discord.ui.TextDisplay(text)]
        banner_url = images.resolve(self.db, cfg["panel_banner"])
        if banner_url:
            parts.append(discord.ui.MediaGallery(discord.MediaGalleryItem(banner_url)))
        parts.append(discord.ui.TextDisplay(f"-# Powered by {config.BOT_NAME}"))
        options = [
            discord.SelectOption(label=(c["label"] or c["name"])[:100], value=str(c["id"]), emoji=c["emoji"] or None)
            for c in self.categories(guild.id)[:MAX_CATEGORIES]
        ]
        select = discord.ui.Select(
            custom_id=PANEL_SELECT_ID,
            placeholder=(cfg["panel_placeholder"] or config.TICKET_PANEL_PLACEHOLDER)[:150],
            min_values=1, max_values=1, options=options,
        )
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*parts, accent_colour=color))
        view.add_item(discord.ui.ActionRow(select))
        return view

    async def send_panel(self, channel: discord.TextChannel) -> discord.Message:
        guild = channel.guild
        self.ensure_defaults(guild.id)
        cfg = self.get_cfg(guild.id)
        if cfg["panel_channel_id"] and cfg["panel_message_id"]:
            old_channel = guild.get_channel(cfg["panel_channel_id"])
            if old_channel is not None:
                try:
                    await old_channel.get_partial_message(cfg["panel_message_id"]).delete()  # type: ignore[union-attr]
                except discord.HTTPException:
                    pass
        message = await channel.send(view=self.build_panel(guild))
        self.save_cfg(guild.id, panel_channel_id=channel.id, panel_message_id=message.id)
        return message

    async def refresh_panel(self, guild: discord.Guild) -> bool:
        """Update the existing panel message (categories or texts changed). False if there is no panel."""
        cfg = self.get_cfg(guild.id)
        channel = guild.get_channel(cfg["panel_channel_id"]) if cfg["panel_channel_id"] else None
        if not isinstance(channel, discord.TextChannel) or not cfg["panel_message_id"]:
            return False
        try:
            await channel.get_partial_message(cfg["panel_message_id"]).edit(view=self.build_panel(guild))
            return True
        except discord.NotFound:
            try:
                await self.send_panel(channel)
                return True
            except discord.HTTPException:
                return False
        except discord.HTTPException:
            log.exception("Could not refresh the ticket panel in guild %s", guild.id)
            return False

    # ------------------------------------------------------------- ticket messages
    @staticmethod
    def control_view(claimed: bool = False) -> discord.ui.View:
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(style=discord.ButtonStyle.danger, label="Close", emoji=icon("lock"), custom_id=CLOSE_ID))
        view.add_item(
            discord.ui.Button(style=discord.ButtonStyle.danger, label="Close With Reason", emoji=icon("lock"), custom_id=CLOSE_REASON_ID)
        )
        view.add_item(
            discord.ui.Button(
                style=discord.ButtonStyle.secondary if claimed else discord.ButtonStyle.success,
                label="Claimed" if claimed else "Claim", emoji=icon("hand"), custom_id=CLAIM_ID, disabled=claimed,
            )
        )
        return view

    def welcome_embeds(self, guild: discord.Guild, user: discord.Member, category: dict, answer: str) -> list[discord.Embed]:
        cfg = self.get_cfg(guild.id)

        def fill(text: str) -> str:
            return text.replace("{user}", user.mention).replace("{server}", guild.name).replace("{category}", category["name"])

        color = parse_color(cfg["welcome_color"], config.TICKET_COLOR)
        first = discord.Embed(
            title=fill(cfg["welcome_title"] or config.TICKET_WELCOME_TITLE),
            description=fill(cfg["welcome_description"] or config.TICKET_WELCOME_DESCRIPTION),
            color=color,
        )
        first.set_author(name=f"{guild.name} Support", icon_url=guild.icon.url if guild.icon else None)
        second = discord.Embed(title=category["name"], description=answer[:4000], color=color)
        return [powered(first), powered(second)]

    async def log_channel(self, guild: discord.Guild, key: str) -> Optional[discord.TextChannel]:
        channel_id = self.get_cfg(guild.id)[key]
        channel = guild.get_channel(channel_id) if channel_id else None
        if isinstance(channel, discord.TextChannel) and channel.permissions_for(guild.me).send_messages:
            return channel
        return None

    def info_embed(
        self, title: str, guild: discord.Guild, ticket: dict, *, opener, closer=None, claimed=None,
        reason: Optional[str] = None, closed_at: Optional[datetime] = None, channel_name: str = "",
        color: int = config.TICKET_COLOR,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Ticket", value=f"{number_str(ticket['number'])} • `{channel_name}`")
        embed.add_field(name="Category", value=ticket["category_name"] or "—")
        opener_ref = getattr(opener, "mention", None) or "<@%s>" % ticket["user_id"]
        embed.add_field(name="Opened by", value=f"{opener_ref} (`{ticket['user_id']}`)")
        if closer is not None:
            embed.add_field(name="Closed by", value=f"{closer.mention} (`{closer.id}`)")
        embed.add_field(name="Claimed by", value=claimed.mention if claimed else "Nobody")
        embed.add_field(name="Opened", value=discord.utils.format_dt(parse_dt(ticket["created_at"]), "f"))
        if closed_at is not None:
            embed.add_field(name="Closed", value=discord.utils.format_dt(closed_at, "f"))
        if closer is not None:
            embed.add_field(name="Reason", value=(reason or "No reason provided")[:1000], inline=False)
        return powered(embed)

    # ------------------------------------------------------------- errors
    async def respond_error(self, interaction: discord.Interaction) -> None:
        try:
            await reply(interaction, f"{icon('warn')} Something went wrong. Please try again in a moment.")
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------- interaction router
    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.type is not discord.InteractionType.component or interaction.guild is None:
            return
        custom_id = (interaction.data or {}).get("custom_id") or ""
        if not str(custom_id).startswith("wq_tk:"):
            return
        try:
            if custom_id == PANEL_SELECT_ID:
                await self.on_panel_select(interaction)
            elif custom_id == CLOSE_ID:
                await self.begin_close(interaction, None)
            elif custom_id == CLOSE_REASON_ID:
                await self.ask_close_reason(interaction)
            elif custom_id == CLAIM_ID:
                await self.on_claim(interaction)
        except Exception:
            log.exception("Ticket interaction %s failed", custom_id)
            await self.respond_error(interaction)

    # ------------------------------------------------------------- open a ticket
    async def on_panel_select(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        values = (interaction.data or {}).get("values") or []
        category = self.get_category(int(values[0])) if values and str(values[0]).isdigit() else None
        if category is None or category["guild_id"] != guild.id:
            await reply(interaction, f"{icon('warn')} This category does not exist anymore. Ask an admin to refresh the panel.")
            await self.refresh_panel(guild)
            return
        cfg = self.get_cfg(guild.id)
        open_now = self.open_channels_of(guild, interaction.user.id)
        if len(open_now) >= cfg["max_tickets"]:
            mentions = ", ".join(f"<#{c}>" for c in open_now)
            await reply(interaction, f"{icon('warn')} You already have {len(open_now)} open ticket(s): {mentions}")
        else:
            await interaction.response.send_modal(TicketModal(self, category))
        # Put the dropdown back to "Select a topic..." so the same category can be picked again.
        try:
            if interaction.message is not None:
                await interaction.message.edit(view=self.build_panel(guild))
        except discord.HTTPException:
            pass

    def build_overwrites(self, guild: discord.Guild, opener: discord.Member, cfg: dict) -> dict:
        mine = guild.me.guild_permissions

        def allow(**wanted: bool) -> discord.PermissionOverwrite:
            # Discord refuses overwrites that grant permissions the bot itself does not have.
            if mine.administrator:
                return discord.PermissionOverwrite(**wanted)
            return discord.PermissionOverwrite(**{k: v for k, v in wanted.items() if v is False or getattr(mine, k, False)})

        base = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: allow(**base, manage_channels=True, manage_messages=True),
            opener: allow(**base, add_reactions=True),
        }
        for role in self.roles_from_ids(guild, self.staff_ids(cfg)):
            overwrites[role] = allow(**base, add_reactions=True)
        return overwrites

    async def create_ticket(self, interaction: discord.Interaction, category: dict, answer: str) -> None:
        guild = interaction.guild
        user = interaction.user
        assert guild is not None and isinstance(user, discord.Member)
        key = (guild.id, user.id)
        if key in self._opening:
            return await reply(interaction, f"{icon('hourglass')} Your ticket is already being created.")
        self._opening.add(key)
        try:
            cfg = self.get_cfg(guild.id)
            open_now = self.open_channels_of(guild, user.id)
            if len(open_now) >= cfg["max_tickets"]:
                mentions = ", ".join(f"<#{c}>" for c in open_now)
                return await reply(interaction, f"{icon('warn')} You already have {len(open_now)} open ticket(s): {mentions}")
            if not guild.me.guild_permissions.manage_channels:
                return await reply(interaction, f"{icon('error')} I need the **Manage Channels** permission to create tickets. Please tell an admin.")

            parent = guild.get_channel(category["channel_category_id"]) if category["channel_category_id"] else None
            if not isinstance(parent, discord.CategoryChannel):
                panel = guild.get_channel(cfg["panel_channel_id"]) if cfg["panel_channel_id"] else None
                parent = getattr(panel, "category", None)
            try:
                channel = await guild.create_text_channel(
                    name=f"ticket-{channel_slug(user.name)}",
                    category=parent,
                    topic=f"Ticket For {category['name']}",
                    overwrites=self.build_overwrites(guild, user, cfg),
                    reason=f"Ticket opened by {user} ({user.id})",
                )
            except discord.Forbidden:
                return await reply(interaction, f"{icon('error')} I am missing permissions to create the ticket channel. Please tell an admin.")
            except discord.HTTPException as exc:
                return await reply(interaction, f"{icon('error')} Could not create the ticket channel ({exc.text}). The channel category may be full.")

            number = self.db.execute("SELECT COALESCE(MAX(number),0)+1 FROM tickets WHERE guild_id=?", (guild.id,)).fetchone()[0]
            self.db.execute(
                "INSERT INTO tickets (guild_id, number, channel_id, user_id, category_id, category_name, answer, status, created_at) "
                "VALUES (?,?,?,?,?,?,?, 'open', ?)",
                (guild.id, number, channel.id, user.id, category["id"], category["name"], answer, now_iso()),
            )
            self.db.commit()
            ticket = self.get_ticket(channel.id)

            staff_roles = self.roles_from_ids(guild, self.staff_ids(cfg))
            pings = " ".join(role.mention for role in staff_roles)
            try:
                await channel.send(
                    content=f"{user.mention} {pings}".strip(),
                    embeds=self.welcome_embeds(guild, user, category, answer),
                    view=self.control_view(),
                    allowed_mentions=discord.AllowedMentions(users=[user], roles=staff_roles),
                )
            except discord.HTTPException:
                log.exception("Could not send the welcome message in %s", channel.id)

            opened = await self.log_channel(guild, "log_open")
            if opened is not None and ticket is not None:
                embed = self.info_embed("Ticket Opened", guild, ticket, opener=user, channel_name=channel.name, color=0x57F287)
                embed.add_field(name="Channel", value=channel.mention)
                embed.add_field(name=category["question"] or "Answer", value=answer[:1000], inline=False)
                try:
                    await opened.send(embed=embed)
                except discord.HTTPException:
                    log.warning("Could not send the ticket-opened log")

            done = powered(make_embed("Ticket", f"Opened a new ticket: {channel.mention}", color=config.TICKET_COLOR))
            await reply(interaction, embed=done)
        finally:
            self._opening.discard(key)

    # ------------------------------------------------------------- claim
    async def on_claim(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        ticket = self.get_ticket(channel.id) if channel else None
        if ticket is None or ticket["status"] != "open":
            return await reply(interaction, f"{icon('warn')} This channel is not an active ticket.")
        if not self.is_staff(interaction.user, self.get_cfg(interaction.guild.id)):  # type: ignore[union-attr]
            return await reply(interaction, f"{icon('lock')} Only the support/admin roles can claim tickets.")
        if ticket["claimed_by"]:
            return await reply(interaction, f"{icon('warn')} This ticket is already claimed by <@{ticket['claimed_by']}>.")
        self.db.execute("UPDATE tickets SET claimed_by=? WHERE channel_id=? AND status='open'", (interaction.user.id, channel.id))
        self.db.commit()
        await interaction.response.edit_message(view=self.control_view(claimed=True))
        await channel.send(  # type: ignore[union-attr]
            embed=powered(make_embed("Ticket claimed", f"{icon('ok')} {interaction.user.mention} has claimed this ticket.", color=config.TICKET_COLOR)),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # ------------------------------------------------------------- close
    async def ask_close_reason(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        ticket = self.get_ticket(channel.id) if channel else None
        if ticket is None or ticket["status"] != "open":
            return await reply(interaction, f"{icon('warn')} This channel is not an active ticket.")
        if not self.is_staff(interaction.user, self.get_cfg(interaction.guild.id)):  # type: ignore[union-attr]
            return await reply(interaction, f"{icon('lock')} Only the support/admin roles can close tickets.")
        await interaction.response.send_modal(CloseReasonModal(self))

    async def begin_close(self, interaction: discord.Interaction, reason: Optional[str]) -> None:
        channel = interaction.channel
        guild = interaction.guild
        ticket = self.get_ticket(channel.id) if channel else None
        if ticket is None or ticket["status"] != "open" or guild is None:
            return await reply(interaction, f"{icon('warn')} This channel is not an active ticket.")
        if not self.is_staff(interaction.user, self.get_cfg(guild.id)):
            return await reply(interaction, f"{icon('lock')} Only the support/admin roles can close tickets.")
        if channel.id in self._closing:  # type: ignore[union-attr]
            return await reply(interaction, f"{icon('hourglass')} This ticket is already being closed.")
        self._closing.add(channel.id)  # type: ignore[union-attr]
        try:
            await interaction.response.send_message(
                embed=powered(make_embed("Closing ticket", f"{icon('lock')} This ticket will be closed in {CLOSE_DELAY} seconds...", color=0xED4245))
            )
            await asyncio.sleep(CLOSE_DELAY)
            await self.finish_close(channel, interaction.user, reason, ticket)  # type: ignore[arg-type]
        finally:
            self._closing.discard(channel.id)  # type: ignore[union-attr]

    async def finish_close(self, channel: discord.TextChannel, closer: discord.Member, reason: Optional[str], ticket: dict) -> None:
        guild = channel.guild
        opener: Optional[discord.abc.User] = guild.get_member(ticket["user_id"])
        if opener is None:
            try:
                opener = await self.bot.fetch_user(ticket["user_id"])
            except discord.HTTPException:
                opener = None
        claimed = guild.get_member(ticket["claimed_by"]) if ticket["claimed_by"] else None
        closed_at = datetime.now(timezone.utc)
        channel_name = channel.name

        info = {
            "number": ticket["number"], "category": ticket["category_name"],
            "opener": f"{opener} ({ticket['user_id']})" if opener else str(ticket["user_id"]),
            "claimed_by": str(claimed) if claimed else None, "closed_by": f"{closer} ({closer.id})",
            "reason": reason, "opened_at": parse_dt(ticket["created_at"]), "closed_at": closed_at,
        }
        limit = min(getattr(guild, "filesize_limit", DM_FILE_LIMIT), DM_FILE_LIMIT) - 256 * 1024
        filename: Optional[str] = None
        data: Optional[bytes] = None
        try:
            filename, data = await tscript.build(self.bot, channel, info, limit_bytes=limit, bot_name=config.BOT_NAME)
        except Exception:
            log.exception("Transcript failed for ticket %s", ticket["id"])
        too_big = data is not None and len(data) > limit

        def make_file() -> Optional[discord.File]:
            if data is None or filename is None or too_big:
                return None
            return discord.File(io.BytesIO(data), filename=filename)

        shared = dict(opener=opener, closer=closer, claimed=claimed, reason=reason, closed_at=closed_at, channel_name=channel_name)

        transcript_channel = await self.log_channel(guild, "log_transcript")
        if transcript_channel is not None:
            embed = self.info_embed("Ticket Transcript", guild, ticket, color=0x5865F2, **shared)
            if too_big or data is None:
                embed.add_field(name="Transcript", value="The transcript could not be created or is too large to upload.", inline=False)
            try:
                await transcript_channel.send(embed=embed, file=make_file())
            except discord.HTTPException:
                log.warning("Could not send the transcript to the transcript channel")

        close_channel = await self.log_channel(guild, "log_close")
        if close_channel is not None:
            try:
                await close_channel.send(embed=self.info_embed("Ticket Closed", guild, ticket, color=0xED4245, **shared))
            except discord.HTTPException:
                log.warning("Could not send the ticket-closed log")

        if opener is not None:
            dm = self.info_embed("Your ticket was closed", guild, ticket, color=0xED4245, **shared)
            dm.description = f"Your ticket in **{guild.name}** has been closed."
            if guild.icon:
                dm.set_thumbnail(url=guild.icon.url)
            try:
                await opener.send(embed=dm, file=make_file())
            except discord.HTTPException:
                log.info("Could not DM %s about ticket %s (DMs closed)", ticket["user_id"], ticket["id"])

        self.db.execute(
            "UPDATE tickets SET status='closed', closed_at=?, closed_by=?, close_reason=? WHERE id=?",
            (closed_at.isoformat(), closer.id, reason, ticket["id"]),
        )
        self.db.commit()
        try:
            await channel.delete(reason=f"Ticket closed by {closer} ({closer.id})")
        except discord.HTTPException as exc:
            self.db.execute("UPDATE tickets SET status='open', closed_at=NULL, closed_by=NULL, close_reason=NULL WHERE id=?", (ticket["id"],))
            self.db.commit()
            try:
                await channel.send(f"{icon('error')} I could not delete this channel ({exc.text}). I need **Manage Channels**.")
            except discord.HTTPException:
                pass

    # =============================================================== commands
    async def _fail(self, ctx: commands.Context, message: str) -> None:
        await ctx.send(f"{icon('warn')} {message}", ephemeral=True)

    @commands.hybrid_command(name="ticketset", description="Set the ticket channel and send the ticket panel there.")
    @app_commands.describe(channel="Channel for the ticket panel (default: this channel).")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketset(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        assert ctx.guild is not None
        target = channel or ctx.channel
        if not isinstance(target, discord.TextChannel):
            return await self._fail(ctx, "Pick a normal text channel.")
        perms = target.permissions_for(ctx.guild.me)
        if not (perms.view_channel and perms.send_messages):
            return await self._fail(ctx, f"I need View Channel and Send Messages in {target.mention}.")
        await ctx.defer()
        try:
            await self.send_panel(target)
        except discord.HTTPException as exc:
            return await self._fail(ctx, f"Could not send the panel: {exc.text}")
        lines = [f"{icon('ok')} Ticket panel sent in {target.mention}."]
        cfg = self.get_cfg(ctx.guild.id)
        if not self.staff_ids(cfg):
            lines.append(f"{icon('warn')} No support/admin role is set yet. Use `{config.PREFIX}ticketedit`. Until then members with Manage Server can close tickets.")
        if not ctx.guild.me.guild_permissions.manage_channels:
            lines.append(f"{icon('warn')} I still need the **Manage Channels** permission to create tickets.")
        await ctx.send("\n".join(lines))

    @commands.hybrid_command(name="ticketedit", description="Edit the ticket panel, ticket message, support/admin roles and settings.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketedit(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        self.ensure_defaults(ctx.guild.id)
        view = TicketEditView(self, ctx.author.id, ctx.guild)
        view.message = await ctx.send(embed=self.edit_embed(ctx.guild), view=view)

    @commands.hybrid_command(name="ticketlogs", description="Set the ticket log channels (opened, closed, transcript).")
    @app_commands.describe(channel="Optional: use this one channel for all three logs.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketlogs(self, ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
        assert ctx.guild is not None
        if channel is not None:
            perms = channel.permissions_for(ctx.guild.me)
            if not (perms.send_messages and perms.embed_links and perms.attach_files):
                return await self._fail(ctx, f"I need Send Messages, Embed Links and Attach Files in {channel.mention}.")
            self.save_cfg(ctx.guild.id, log_open=channel.id, log_close=channel.id, log_transcript=channel.id)
            return await ctx.send(f"{icon('ok')} Ticket opened, closed and transcript logs will all go to {channel.mention}.")
        view = TicketLogsView(self, ctx.author.id, ctx.guild)
        view.message = await ctx.send(embed=self.logs_embed(ctx.guild), view=view)

    # ------------------------------------------------------------- categories
    async def _category_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if interaction.guild is None:
            return []
        current = current.lower()
        return [
            app_commands.Choice(name=c["name"][:100], value=c["name"])
            for c in self.categories(interaction.guild.id) if current in c["name"].lower()
        ][:25]

    @commands.hybrid_group(
        name="ticketcategory", description="Manage ticket categories.", fallback="list", invoke_without_command=True
    )
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketcategory(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        self.ensure_defaults(ctx.guild.id)
        lines = []
        for c in self.categories(ctx.guild.id):
            parent = f"<#{c['channel_category_id']}>" if c["channel_category_id"] else "default"
            lines.append(f"{c['emoji'] or ''} **{c['name']}**\nDropdown: {c['label'] or c['name']} • Created in: {parent}")
        embed = make_embed(
            f"{icon('clipboard')} Ticket categories",
            "\n\n".join(lines) or "No categories.",
            color=config.TICKET_COLOR,
            footer=f"{config.PREFIX}ticketcategory create <name> • edit <name> • delete <name>",
        )
        await ctx.send(embed=embed)

    @ticketcategory.command(name="create", description="Create a new ticket category.")
    @app_commands.describe(name="Name of the new category.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketcategory_create(self, ctx: commands.Context, *, name: str) -> None:
        assert ctx.guild is not None
        self.ensure_defaults(ctx.guild.id)
        name = name.strip()[:50]
        if not name:
            return await self._fail(ctx, "Give the category a name.")
        if self.find_category(ctx.guild.id, name) is not None:
            return await self._fail(ctx, f"A category called **{name}** already exists.")
        if len(self.categories(ctx.guild.id)) >= MAX_CATEGORIES:
            return await self._fail(ctx, f"A dropdown can hold {MAX_CATEGORIES} categories at most.")
        category_id = self.add_category(ctx.guild.id, name)
        await self.refresh_panel(ctx.guild)
        view = CategoryEditView(self, ctx.author.id, ctx.guild, category_id)
        view.message = await ctx.send(
            f"{icon('ok')} Category **{name}** created. Customize it here:",
            embed=self.category_embed(ctx.guild, category_id), view=view,
        )

    @ticketcategory.command(name="edit", description="Edit a ticket category.")
    @app_commands.describe(name="Name of the category to edit.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketcategory_edit(self, ctx: commands.Context, *, name: str) -> None:
        assert ctx.guild is not None
        self.ensure_defaults(ctx.guild.id)
        category = self.find_category(ctx.guild.id, name)
        if category is None:
            return await self._fail(ctx, f"No category called **{name}**. See `{config.PREFIX}ticketcategory`.")
        view = CategoryEditView(self, ctx.author.id, ctx.guild, category["id"])
        view.message = await ctx.send(embed=self.category_embed(ctx.guild, category["id"]), view=view)

    @ticketcategory.command(name="delete", description="Delete a ticket category.")
    @app_commands.describe(name="Name of the category to delete.")
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    async def ticketcategory_delete(self, ctx: commands.Context, *, name: str) -> None:
        assert ctx.guild is not None
        self.ensure_defaults(ctx.guild.id)
        category = self.find_category(ctx.guild.id, name)
        if category is None:
            return await self._fail(ctx, f"No category called **{name}**. See `{config.PREFIX}ticketcategory`.")
        if len(self.categories(ctx.guild.id)) <= 1:
            return await self._fail(ctx, "You need at least one category. Create another one first.")
        view = ConfirmView(ctx.author.id)
        prompt = await ctx.send(
            f"{icon('warn')} Delete the category **{category['name']}**? Tickets that are already open stay open.", view=view
        )
        await view.wait()
        if not view.confirmed:
            return await prompt.edit(content=f"{icon('ok')} Nothing was deleted.", view=None)
        self.delete_category(category["id"])
        await self.refresh_panel(ctx.guild)
        await prompt.edit(content=f"{icon('ok')} Category **{category['name']}** deleted.", view=None)

    @ticketcategory_edit.autocomplete("name")
    async def _edit_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._category_autocomplete(interaction, current)

    @ticketcategory_delete.autocomplete("name")
    async def _delete_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._category_autocomplete(interaction, current)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
