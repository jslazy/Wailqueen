"""Small embed helper used across the bot."""
from __future__ import annotations

from typing import Optional

import discord

import config
from utils.icons import icon


def make_embed(
    title: Optional[str] = None,
    description: Optional[str] = None,
    *,
    color: Optional[int] = None,
    thumbnail: Optional[str] = None,
    image: Optional[str] = None,
    footer: Optional[str] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=config.ACCENT if color is None else color,
    )
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if image:
        embed.set_image(url=image)
    if footer:
        embed.set_footer(text=footer)
    return embed


def build_pages(
    title: str,
    lines: list[str],
    *,
    per_page: int = 8,
    empty: str = "Nothing to show.",
    footer: Optional[str] = None,
) -> list[discord.Embed]:
    """Split lines into embed pages."""
    if not lines:
        return [make_embed(title, empty)]
    chunks = [lines[i : i + per_page] for i in range(0, len(lines), per_page)]
    pages = []
    for number, chunk in enumerate(chunks, start=1):
        text = f"{footer} • " if footer else ""
        pages.append(
            make_embed(title, "\n\n".join(chunk), footer=f"{text}Page {number}/{len(chunks)} • {len(lines)} total")
        )
    return pages


class Paginator(discord.ui.View):
    """Previous / Next buttons. Only the person who ran the command can use them."""

    def __init__(self, author_id: int, pages: list[discord.Embed]) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.pages = pages
        self.index = 0
        self.message: Optional[discord.Message] = None
        self.previous.emoji = icon("left")
        self.forward.emoji = icon("right")
        self._sync()

    def _sync(self) -> None:
        self.previous.disabled = self.index == 0
        self.forward.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                f"{icon('lock')} Only the person who ran the command can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def forward(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.index += 1
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_pages(ctx, pages: list[discord.Embed]) -> None:
    if len(pages) == 1:
        await ctx.send(embed=pages[0])
        return
    view = Paginator(ctx.author.id, pages)
    view.message = await ctx.send(embed=pages[0], view=view)
