"""Ticket transcripts as ONE self-contained HTML file that looks like the Discord web client.

Avatars, custom emojis, stickers and image attachments are downloaded and embedded into the file
(base64), so the transcript keeps working after the Discord CDN links expire and without internet.
Every message is rendered inside its own try/except, so one odd message can never break a transcript.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import html
import logging
import mimetypes
import re
import warnings
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import aiohttp

if TYPE_CHECKING:  # only for type hints, the renderer itself is duck-typed
    import discord

log = logging.getLogger("premium-bot.transcript")

MAX_MESSAGES = 5000
MEDIA_BUDGET = 6 * 1024 * 1024   # total size of attachment/embed images embedded in one file
PER_FILE = 2 * 1024 * 1024       # biggest single image that gets embedded
SMALL_FILE = 512 * 1024          # avatars, emojis, stickers, icons
FETCH_TIMEOUT = 15
TOTAL_FETCH_TIMEOUT = 90

SYSTEM_OK = {"default", "reply", "chat_input_command", "context_menu_command"}
BUTTON_COLORS = {1: "#5865f2", 2: "#4e5058", 3: "#248046", 4: "#da373c", 5: "#4e5058", 6: "#5865f2"}

EMOJI_RAW = re.compile(r"<(a?):([A-Za-z0-9_~]{2,32}):(\d{15,25})>")
TOKEN = re.compile(r"\x00(\d+)\x00")
JUMBO_CHARS = re.compile(
    "^[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2190-\u21ff\u2300-\u23ff\u2460-\u24ff\u25a0-\u27bf"
    "\u2900-\u297f\u2b00-\u2bff\u3030\u303d\u3297\u3299\U0001F000-\U0001FAFF\ufe0f\u200d\u20e3]+$"
)


# ------------------------------------------------------------------ small helpers
def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def fmt_size(num: int) -> str:
    size = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} B"


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hour12(dt: datetime, seconds: bool = False) -> str:
    text = dt.strftime("%I:%M:%S %p" if seconds else "%I:%M %p")
    return text[1:] if text.startswith("0") else text


def fmt_full(dt: Optional[datetime]) -> str:
    dt = _utc(dt)
    return "Unknown" if dt is None else f"{dt.strftime('%m/%d/%Y')} {_hour12(dt)}"


def fmt_day(dt: datetime) -> str:
    dt = _utc(dt)
    assert dt is not None
    return f"{dt.strftime('%B')} {dt.day}, {dt.year}"


def emoji_url(emoji_id: Any, animated: bool, size: int = 48) -> str:
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.{'gif' if animated else 'png'}?size={size}&quality=lossless"


def avatar_url(user: Any) -> str:
    try:
        return str(user.display_avatar.replace(size=64, format="png"))
    except Exception:
        try:
            return str(user.display_avatar.url)
        except Exception:
            return ""


def color_css(value: Any) -> str:
    try:
        number = int(getattr(value, "value", value))
    except (TypeError, ValueError):
        return ""
    return f"#{number:06x}" if number else ""


def _is_image(att: Any) -> bool:
    ctype = (getattr(att, "content_type", None) or "").lower()
    name = (getattr(att, "filename", "") or "").lower()
    return ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))


# ------------------------------------------------------------------ asset downloads
class Assets:
    """Downloads images once and keeps them as data: URIs."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.sem = asyncio.Semaphore(10)
        self.data: dict[str, str] = {}
        self.failed: set[str] = set()
        self.used = 0

    async def fetch(self, url: str, *, max_size: int = SMALL_FILE, essential: bool = False) -> None:
        if not url or not url.startswith(("http://", "https://")):
            return
        if url in self.data or url in self.failed:
            return
        if not essential and self.used >= MEDIA_BUDGET:
            return
        async with self.sem:
            try:
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT)) as resp:
                    if resp.status != 200:
                        self.failed.add(url)
                        return
                    declared = resp.content_length or 0
                    if declared and declared > max_size:
                        self.failed.add(url)
                        return
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > max_size:
                            self.failed.add(url)
                            return
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                self.failed.add(url)
                return
        if not mime.startswith("image/"):
            guessed = mimetypes.guess_type(url.split("?")[0])[0]
            mime = guessed if guessed and guessed.startswith("image/") else "image/png"
        if not essential:
            self.used += len(body)
        self.data[url] = f"data:{mime};base64,{base64.b64encode(body).decode('ascii')}"


# ------------------------------------------------------------------ render context
class RenderCtx:
    def __init__(self, bot: Any, guild: Any, channel: Any, assets: Assets, inline: bool, bot_name: str) -> None:
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.assets = assets
        self.inline = inline
        self.bot_name = bot_name
        self.users: dict[int, str] = {}
        self.ids_in_transcript: set[int] = set()

    def src(self, url: str) -> str:
        if self.inline:
            return self.assets.data.get(url, url)
        return url

    # -- lookups used by the markdown renderer
    def user_name(self, user_id: int) -> str:
        if user_id in self.users:
            return self.users[user_id]
        name = None
        try:
            member = self.guild.get_member(user_id) if self.guild else None
            user = member or (self.bot.get_user(user_id) if self.bot else None)
            name = getattr(user, "display_name", None)
        except Exception:
            name = None
        return name or "Unknown User"

    def role_info(self, role_id: int) -> tuple[str, str]:
        try:
            role = self.guild.get_role(role_id) if self.guild else None
        except Exception:
            role = None
        if role is None:
            return "deleted-role", ""
        return role.name, color_css(role.color)

    def channel_name(self, channel_id: int) -> str:
        try:
            channel = self.guild.get_channel_or_thread(channel_id) if self.guild else None
        except Exception:
            channel = None
        return getattr(channel, "name", None) or "deleted-channel"


# ------------------------------------------------------------------ markdown
class Markdown:
    """Discord markdown -> HTML. Code, links, mentions and emojis are parked in a stash first, so
    the emphasis rules can never damage them (for example underscores inside a link)."""

    CODEBLOCK = re.compile(r"```(?:([A-Za-z0-9_+#.\-]*)\n)?(.*?)```", re.S)
    INLINE_CODE = re.compile(r"``(.+?)``|`([^`\n]+?)`", re.S)
    MASKED = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
    NOEMBED = re.compile(r"&lt;(https?://[^\s&]+?)&gt;")
    BARE_URL = re.compile(r"(?<![\w\"=/>])https?://[^\s<>\"\x00]+")
    USER = re.compile(r"&lt;@!?(\d+)&gt;")
    ROLE = re.compile(r"&lt;@&amp;(\d+)&gt;")
    CHANNEL = re.compile(r"&lt;#(\d+)&gt;")
    EMOJI = re.compile(r"&lt;(a?):([A-Za-z0-9_~]{2,32}):(\d{15,25})&gt;")
    TIMESTAMP = re.compile(r"&lt;t:(-?\d{1,13})(?::([tTdDfFR]))?&gt;")
    SLASH = re.compile(r"&lt;/([\w\- ]{1,64}):(\d{15,25})&gt;")
    EVERYONE = re.compile(r"(?<![\w@])@(everyone|here)\b")

    def __init__(self, ctx: RenderCtx) -> None:
        self.ctx = ctx
        self.stash: list[str] = []
        self.blocks: set[int] = set()

    # -- stash handling
    def _hold(self, value: str, block: bool = False) -> str:
        self.stash.append(value)
        if block:
            self.blocks.add(len(self.stash) - 1)
        return f"\x00{len(self.stash) - 1}\x00"

    def _release(self, text: str) -> str:
        for _ in range(6):
            if "\x00" not in text:
                break
            text = TOKEN.sub(lambda m: self.stash[int(m.group(1))], text)
        return text

    # -- public
    def render(self, text: Optional[str], *, jumbo: bool = False) -> str:
        self.stash = []
        self.blocks = set()
        text = (text or "").replace("\x00", "")
        if not text.strip():
            return ""
        text = self.CODEBLOCK.sub(self._codeblock, text)
        text = self.INLINE_CODE.sub(self._inline_code, text)
        text = html.escape(text, quote=False)
        text = self._specials(text, jumbo)
        text = self._emphasis(text)
        return self._release(self._blocks(text))

    # -- code
    def _codeblock(self, match: "re.Match[str]") -> str:
        body = match.group(2).strip("\n")
        return self._hold(f'<pre class="codeblock"><code>{esc(body)}</code></pre>', block=True)

    def _inline_code(self, match: "re.Match[str]") -> str:
        body = match.group(1) if match.group(1) is not None else match.group(2)
        return self._hold(f"<code class=\"inline\">{esc(body)}</code>")

    # -- links, mentions, emojis
    def _specials(self, text: str, jumbo: bool) -> str:
        text = self.MASKED.sub(self._masked, text)
        text = self.NOEMBED.sub(lambda m: self._hold(self._link(m.group(1), m.group(1))), text)
        text = self.BARE_URL.sub(self._bare, text)
        text = self.USER.sub(
            lambda m: self._hold(f'<span class="mention">@{esc(self.ctx.user_name(int(m.group(1))))}</span>'), text
        )
        text = self.ROLE.sub(self._role, text)
        text = self.CHANNEL.sub(
            lambda m: self._hold(f'<span class="mention">#{esc(self.ctx.channel_name(int(m.group(1))))}</span>'), text
        )
        text = self.EMOJI.sub(lambda m: self._hold(self._emoji(m, jumbo)), text)
        text = self.TIMESTAMP.sub(self._timestamp, text)
        text = self.SLASH.sub(lambda m: self._hold(f'<span class="mention">/{esc(m.group(1))}</span>'), text)
        text = self.EVERYONE.sub(lambda m: self._hold(f'<span class="mention">@{m.group(1)}</span>'), text)
        return text

    @staticmethod
    def _link(url: str, label: str) -> str:
        safe = url.replace('"', "%22")
        return f'<a href="{safe}" target="_blank" rel="noopener noreferrer">{label}</a>'

    def _masked(self, match: "re.Match[str]") -> str:
        return self._hold(self._link(match.group(2), self._emphasis(match.group(1))))

    def _bare(self, match: "re.Match[str]") -> str:
        url = match.group(0)
        trail = ""
        while url and url[-1] in ".,;:!?'\")":
            if url[-1] == ")" and url.count("(") >= url.count(")"):
                break
            trail = url[-1] + trail
            url = url[:-1]
        return self._hold(self._link(url, url)) + trail

    def _role(self, match: "re.Match[str]") -> str:
        name, color = self.ctx.role_info(int(match.group(1)))
        style = ""
        if color:
            red, green, blue = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            style = f' style="color:{color};background:rgba({red},{green},{blue},.1)"'
        return self._hold(f'<span class="mention"{style}>@{esc(name)}</span>')

    def _emoji(self, match: "re.Match[str]", jumbo: bool) -> str:
        animated, name, eid = bool(match.group(1)), match.group(2), match.group(3)
        url = emoji_url(eid, animated)
        cls = "emoji jumbo" if jumbo else "emoji"
        return f'<img class="{cls}" src="{esc(self.ctx.src(url))}" alt=":{esc(name)}:" title=":{esc(name)}:">'

    def _timestamp(self, match: "re.Match[str]") -> str:
        try:
            dt = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return match.group(0)
        style = match.group(2) or "f"
        day = f"{dt.strftime('%B')} {dt.day}, {dt.year}"
        shown = {
            "t": _hour12(dt),
            "T": _hour12(dt, True),
            "d": dt.strftime("%m/%d/%Y"),
            "D": day,
            "f": f"{day} {_hour12(dt)}",
            "F": f"{dt.strftime('%A')}, {day} {_hour12(dt)}",
            "R": f"{day} {_hour12(dt)}",
        }[style]
        return self._hold(f'<span class="timestamp" title="{esc(day)} {esc(_hour12(dt))} UTC">{esc(shown)}</span>')

    # -- emphasis
    @staticmethod
    def _emphasis(text: str) -> str:
        text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text, flags=re.S)
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"__(.+?)__", r"<u>\1</u>", text, flags=re.S)
        text = re.sub(r"(?<!\*)\*(?![\*\s])(.+?)(?<![\*\s])\*(?!\*)", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"(?<![\w_])_(?![\s_])(.+?)(?<![\s_])_(?![\w_])", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
        text = re.sub(
            r"\|\|(.+?)\|\|",
            r"""<span class="spoiler" onclick="this.classList.toggle('open')">\1</span>""",
            text,
            flags=re.S,
        )
        return text

    # -- block level: headers, lists, quotes, line breaks
    def _blocks(self, text: str) -> str:
        lines = text.split("\n")
        out: list[tuple[bool, str]] = []  # (is_block_element, html)
        para: list[str] = []

        def flush() -> None:
            while para and para[-1] == "":
                para.pop()
            if para and para[0] == "" and out and out[-1][0]:
                para.pop(0)  # the blank line right after a heading/list/quote adds no extra gap
            if para:
                out.append((False, "<br>".join(para)))
                para.clear()

        index = 0
        while index < len(lines):
            line = lines[index]
            if line.startswith("&gt;&gt;&gt; ") or line == "&gt;&gt;&gt;":
                flush()
                rest = [line[len("&gt;&gt;&gt; "):]] + lines[index + 1:]
                out.append((True, f'<div class="quote">{"<br>".join(rest)}</div>'))
                break
            if line.startswith("&gt; ") or line == "&gt;":
                flush()
                quoted = []
                while index < len(lines) and (lines[index].startswith("&gt; ") or lines[index] == "&gt;"):
                    quoted.append(lines[index][5:] if lines[index].startswith("&gt; ") else "")
                    index += 1
                out.append((True, f'<div class="quote">{"<br>".join(quoted)}</div>'))
                continue
            head = re.match(r"^(#{1,3}) (.+)$", line)
            if head:
                flush()
                out.append((True, f'<div class="h{len(head.group(1))}">{head.group(2)}</div>'))
                index += 1
                continue
            sub = re.match(r"^-# (.+)$", line)
            if sub:
                flush()
                out.append((True, f'<div class="sub">{sub.group(1)}</div>'))
                index += 1
                continue
            bullet = re.match(r"^\s*[-*] (.+)$", line)
            numbered = re.match(r"^\s*(\d{1,9})\. (.+)$", line)
            if bullet or numbered:
                flush()
                ordered = bool(numbered)
                items = []
                start = numbered.group(1) if numbered else "1"
                while index < len(lines):
                    b = re.match(r"^\s*[-*] (.+)$", lines[index])
                    n = re.match(r"^\s*(\d{1,9})\. (.+)$", lines[index])
                    if ordered and n:
                        items.append(n.group(2))
                    elif not ordered and b:
                        items.append(b.group(1))
                    else:
                        break
                    index += 1
                tag = f'ol start="{esc(start)}"' if ordered else "ul"
                out.append((True, f"<{tag}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{tag.split()[0]}>"))
                continue
            token = TOKEN.fullmatch(line)
            if token and int(token.group(1)) in self.blocks:
                flush()
                out.append((True, line))
                index += 1
                continue
            para.append(line)
            index += 1
        flush()
        return "".join(part if block else f"<span>{part}</span>" for block, part in out)


# ------------------------------------------------------------------ pieces of a message
def _is_jumbo(content: str) -> bool:
    if not content or not content.strip():
        return False
    customs = EMOJI_RAW.findall(content)
    rest = EMOJI_RAW.sub("", content)
    rest = re.sub(r"\s+", "", rest)
    if rest and not JUMBO_CHARS.match(rest):
        return False
    total = len(customs) + len(rest)
    return 0 < total <= 27


def _plain_snippet(content: str, limit: int = 90) -> str:
    text = EMOJI_RAW.sub(lambda m: f":{m.group(2)}:", content or "")
    text = re.sub(r"```.*?```", "[code]", text, flags=re.S)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def render_embed(ctx: RenderCtx, md: Markdown, embed: Any) -> str:
    data = embed.to_dict()
    kind = data.get("type", "rich")
    thumb = (data.get("thumbnail") or {}).get("url")
    image = (data.get("image") or {}).get("url")
    if kind in ("image", "gifv") and not data.get("title") and not data.get("description"):
        shown = thumb or image or (data.get("video") or {}).get("url")
        if shown:
            return f'<div class="embed-plain"><img src="{esc(ctx.src(shown))}" alt="image"></div>'
    color = data.get("color")
    bar = f"#{int(color):06x}" if color is not None else "#1e1f22"
    parts: list[str] = []
    provider = (data.get("provider") or {}).get("name")
    if provider and kind in ("link", "video", "article"):
        parts.append(f'<div class="e-provider">{esc(provider)}</div>')
    author = data.get("author")
    if author and author.get("name"):
        icon = (
            f'<img class="e-author-icon" src="{esc(ctx.src(author["icon_url"]))}" alt="">'
            if author.get("icon_url")
            else ""
        )
        name = esc(author["name"])
        if author.get("url"):
            name = f'<a href="{esc(author["url"])}" target="_blank" rel="noopener noreferrer">{name}</a>'
        parts.append(f'<div class="e-author">{icon}<span>{name}</span></div>')
    if data.get("title"):
        title = md.render(data["title"])
        if data.get("url"):
            title = f'<a href="{esc(data["url"])}" target="_blank" rel="noopener noreferrer">{title}</a>'
        parts.append(f'<div class="e-title">{title}</div>')
    if data.get("description"):
        parts.append(f'<div class="e-desc">{md.render(data["description"])}</div>')
    fields = data.get("fields") or []
    if fields:
        cells: list[str] = []
        pos = 0
        while pos < len(fields):
            if fields[pos].get("inline"):
                group = []
                while pos < len(fields) and fields[pos].get("inline") and len(group) < 3:
                    group.append(fields[pos])
                    pos += 1
                span = 12 // len(group)
            else:
                group, span = [fields[pos]], 12
                pos += 1
            for field in group:
                cells.append(
                    f'<div class="e-field" style="grid-column:span {span}">'
                    f'<div class="e-fname">{md.render(field.get("name", ""))}</div>'
                    f'<div class="e-fvalue">{md.render(field.get("value", ""))}</div></div>'
                )
        parts.append(f'<div class="e-fields">{"".join(cells)}</div>')
    if image:
        parts.append(f'<div class="e-image"><img src="{esc(ctx.src(image))}" alt="embed image"></div>')
    footer = data.get("footer")
    stamp = data.get("timestamp")
    if footer or stamp:
        bits = []
        if footer and footer.get("icon_url"):
            bits.append(f'<img class="e-footer-icon" src="{esc(ctx.src(footer["icon_url"]))}" alt="">')
        text = esc(footer["text"]) if footer and footer.get("text") else ""
        if stamp:
            try:
                when = fmt_full(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
            except ValueError:
                when = esc(stamp)
            text = f"{text} • {when}" if text else when
        bits.append(f"<span>{text}</span>")
        parts.append(f'<div class="e-footer">{"".join(bits)}</div>')
    if not parts and not thumb:
        return ""
    thumb_html = ""
    if thumb and kind not in ("image", "gifv"):
        thumb_html = f'<div class="e-thumb"><img src="{esc(ctx.src(thumb))}" alt="thumbnail"></div>'
    elif thumb and not image:
        parts.append(f'<div class="e-image"><img src="{esc(ctx.src(thumb))}" alt="embed image"></div>')
    return f'<div class="embed" style="--bar:{bar}"><div class="e-main">{"".join(parts)}</div>{thumb_html}</div>'


def _button_emoji(ctx: RenderCtx, emoji: Any) -> str:
    if emoji is None:
        return ""
    emoji_id = getattr(emoji, "id", None)
    if emoji_id:
        url = emoji_url(emoji_id, bool(getattr(emoji, "animated", False)), 32)
        return f'<img class="emoji small" src="{esc(ctx.src(url))}" alt="">'
    return f'<span class="uemoji">{esc(getattr(emoji, "name", emoji))}</span>'


def _component_type(component: Any) -> str:
    kind = getattr(component, "type", None)
    return str(getattr(kind, "name", kind) or "").lower()


def render_components(ctx: RenderCtx, md: Markdown, components: Any, depth: int = 0) -> str:
    out: list[str] = []
    for comp in components or []:
        try:
            kind = _component_type(comp)
            if kind == "action_row":
                inner = []
                for child in getattr(comp, "children", []):
                    ckind = _component_type(child)
                    if ckind == "button":
                        style = int(getattr(getattr(child, "style", 2), "value", 2))
                        label = esc(getattr(child, "label", "") or "")
                        link = " ↗" if getattr(child, "url", None) else ""
                        disabled = " disabled" if getattr(child, "disabled", False) else ""
                        inner.append(
                            f'<span class="btn{disabled}" style="background:{BUTTON_COLORS.get(style, "#4e5058")}">'
                            f'{_button_emoji(ctx, getattr(child, "emoji", None))}{label}{link}</span>'
                        )
                    else:  # select menus of every kind
                        holder = esc(getattr(child, "placeholder", None) or "Make a selection")
                        inner.append(f'<span class="select">{holder}<span class="chev">⌄</span></span>')
                out.append(f'<div class="row">{"".join(inner)}</div>')
            elif kind == "text_display":
                out.append(f'<div class="c-text">{md.render(getattr(comp, "content", ""))}</div>')
            elif kind == "separator":
                out.append("<hr class=\"c-sep\">")
            elif kind == "media_gallery":
                for item in getattr(comp, "items", []):
                    media = getattr(getattr(item, "media", None), "url", None)
                    if media:
                        out.append(f'<div class="embed-plain"><img src="{esc(ctx.src(media))}" alt=""></div>')
            elif kind == "section":
                out.append(render_components(ctx, md, getattr(comp, "children", []), depth + 1))
            elif kind == "container" and depth < 4:
                bar = color_css(getattr(comp, "accent_colour", None) or getattr(comp, "accent_color", None)) or "#1e1f22"
                inner_html = render_components(ctx, md, getattr(comp, "children", []), depth + 1)
                out.append(f'<div class="embed container" style="--bar:{bar}"><div class="e-main">{inner_html}</div></div>')
        except Exception:
            log.debug("component skipped", exc_info=True)
    return "".join(out)


def render_reply(ctx: RenderCtx, md: Markdown, message: Any) -> str:
    ref = getattr(message, "reference", None)
    if ref is None or message.type.name != "reply":
        return ""
    resolved = getattr(ref, "resolved", None)
    if resolved is None or not hasattr(resolved, "author"):
        return '<div class="reply"><span class="r-missing">Original message was deleted or could not be loaded.</span></div>'
    author = resolved.author
    name = esc(getattr(author, "display_name", None) or getattr(author, "name", "Unknown"))
    color = color_css(getattr(author, "color", None))
    style = f' style="color:{color}"' if color else ""
    snippet = esc(_plain_snippet(resolved.content or ""))
    if not snippet:
        snippet = "<i>Click to see attachment</i>" if getattr(resolved, "attachments", None) else "<i>Click to see embed</i>"
    avatar = esc(ctx.src(avatar_url(author)))
    target = f"m-{resolved.id}" if resolved.id in ctx.ids_in_transcript else ""
    anchor = f' onclick="document.getElementById(\'{target}\')&&document.getElementById(\'{target}\').scrollIntoView({{block:\'center\'}})"' if target else ""
    return (
        f'<div class="reply"{anchor}><img class="r-avatar" src="{avatar}" alt="">'
        f'<span class="r-name"{style}>@{name}</span><span class="r-text">{snippet}</span></div>'
    )


def render_attachments(ctx: RenderCtx, message: Any) -> str:
    out: list[str] = []
    for att in getattr(message, "attachments", []) or []:
        name = esc(att.filename)
        url = att.url
        ctype = (getattr(att, "content_type", None) or "").lower()
        if _is_image(att):
            spoiler = " spoiler-img" if getattr(att, "is_spoiler", lambda: False)() else ""
            out.append(
                f'<div class="att{spoiler}"><a href="{esc(url)}" target="_blank" rel="noopener noreferrer">'
                f'<img src="{esc(ctx.src(url))}" alt="{name}"></a></div>'
            )
            continue
        if getattr(att, "duration", None) is not None or ctype.startswith("audio/"):
            icon, note = "🎵", "Audio"
        elif ctype.startswith("video/"):
            icon, note = "🎞️", "Video"
        else:
            icon, note = "📄", "File"
        out.append(
            f'<div class="file"><span class="f-icon">{icon}</span><div class="f-info">'
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{name}</a>'
            f'<div class="f-size">{note} • {fmt_size(getattr(att, "size", 0))}</div></div></div>'
        )
    return "".join(out)


def render_stickers(ctx: RenderCtx, message: Any) -> str:
    out = []
    for sticker in getattr(message, "stickers", []) or []:
        fmt = getattr(getattr(sticker, "format", None), "name", "")
        if fmt == "lottie":
            out.append(f'<div class="file"><span class="f-icon">🏷️</span><div class="f-info">Sticker: {esc(sticker.name)}</div></div>')
        else:
            out.append(f'<div class="sticker"><img src="{esc(ctx.src(str(sticker.url)))}" alt="{esc(sticker.name)}" title="{esc(sticker.name)}"></div>')
    return "".join(out)


def render_reactions(ctx: RenderCtx, message: Any) -> str:
    out = []
    for reaction in getattr(message, "reactions", []) or []:
        emoji = reaction.emoji
        emoji_id = getattr(emoji, "id", None)
        if emoji_id:
            url = emoji_url(emoji_id, bool(getattr(emoji, "animated", False)), 32)
            shown = f'<img class="emoji small" src="{esc(ctx.src(url))}" alt=":{esc(emoji.name)}:">'
        else:
            shown = f'<span class="uemoji">{esc(emoji)}</span>'
        out.append(f'<span class="reaction">{shown}<span class="count">{reaction.count}</span></span>')
    return f'<div class="reactions">{"".join(out)}</div>' if out else ""


def render_message(ctx: RenderCtx, md: Markdown, message: Any, compact: bool) -> str:
    author = message.author
    kind = message.type.name
    stamp = _utc(message.created_at)
    full_time = fmt_full(stamp)
    mid = f"m-{message.id}"

    if kind not in SYSTEM_OK:
        text = getattr(message, "system_content", None) or ""
        if not text:
            return ""
        name = esc(getattr(author, "display_name", None) or getattr(author, "name", "Unknown"))
        return (
            f'<div class="sys" id="{mid}"><span class="sys-arrow">➜</span>'
            f'<span class="sys-text">{esc(text)}</span><span class="time">{esc(full_time)}</span>'
            f'<span class="sys-user" hidden>{name}</span></div>'
        )

    content = message.content or ""
    body: list[str] = []
    if content.strip():
        edited = ' <span class="edited">(edited)</span>' if getattr(message, "edited_at", None) else ""
        body.append(f'<div class="content">{md.render(content, jumbo=_is_jumbo(content))}{edited}</div>')
    body.append(render_attachments(ctx, message))
    body.append(render_stickers(ctx, message))
    for embed in getattr(message, "embeds", []) or []:
        try:
            body.append(render_embed(ctx, md, embed))
        except Exception:
            log.debug("embed skipped", exc_info=True)
    body.append(render_components(ctx, md, getattr(message, "components", None)))
    body.append(render_reactions(ctx, message))
    inner = "".join(part for part in body if part)
    if not inner:
        inner = '<div class="content muted"><i>Message without text content</i></div>'

    if compact:
        return (
            f'<div class="msg compact" id="{mid}"><span class="gutter" title="{esc(full_time)}">{esc(_hour12(stamp))}</span>'
            f'<div class="body">{inner}</div></div>'
        )

    name = esc(getattr(author, "display_name", None) or getattr(author, "name", "Unknown"))
    color = color_css(getattr(author, "color", None))
    style = f' style="color:{color}"' if color else ""
    tag = ""
    if getattr(author, "bot", False) or getattr(message, "webhook_id", None):
        verified = False
        try:
            verified = bool(author.public_flags.verified_bot)
        except Exception:
            pass
        tag = f'<span class="tag">{"✓ " if verified else ""}APP</span>'
    used = ""
    if kind == "chat_input_command":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                interaction = getattr(message, "interaction", None)
            if interaction is not None:
                used = (
                    f'<div class="used">{esc(getattr(interaction.user, "display_name", interaction.user))} '
                    f'used <span class="mention">/{esc(interaction.name)}</span></div>'
                )
        except Exception:
            used = ""
    return (
        f'<div class="msg first" id="{mid}">{render_reply(ctx, md, message)}{used}'
        f'<img class="avatar" src="{esc(ctx.src(avatar_url(author)))}" alt="">'
        f'<div class="body"><div class="header"><span class="name"{style}>{name}</span>{tag}'
        f'<span class="time" title="{esc(full_time)} UTC">{esc(full_time)}</span></div>{inner}</div></div>'
    )


# ------------------------------------------------------------------ whole document
CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
html,body{margin:0;background:#313338;color:#dbdee1}
body{font-family:"gg sans","Noto Sans","Helvetica Neue",Helvetica,Arial,sans-serif;font-size:16px;line-height:1.375;-webkit-font-smoothing:antialiased}
a{color:#00a8fc;text-decoration:none}a:hover{text-decoration:underline}
img{max-width:100%}
.chan-header{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;padding:12px 16px;background:#313338;border-bottom:1px solid #1f2023;box-shadow:0 1px 0 rgba(0,0,0,.2)}
.guild-icon{width:36px;height:36px;border-radius:50%;background:#5865f2;object-fit:cover;flex:none}
.guild-fallback{width:36px;height:36px;border-radius:50%;background:#5865f2;color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;flex:none}
.guild-name{font-size:12px;color:#949ba4;font-weight:600;text-transform:uppercase;letter-spacing:.02em}
.chan-title{font-weight:600;color:#f2f3f5;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.chan-title .hash{color:#80848e;font-size:20px}
.chan-title .topic{font-weight:400;font-size:14px;color:#b5bac1}
.summary{margin:16px;padding:14px 16px;background:#2b2d31;border:1px solid #232428;border-left:4px solid #5865f2;border-radius:6px;display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px 18px}
.summary .k{font-size:12px;font-weight:700;color:#b5bac1;text-transform:uppercase;letter-spacing:.02em}
.summary .v{font-size:14px;color:#f2f3f5;word-break:break-word}
.messages{padding-bottom:24px}
.divider{display:flex;align-items:center;margin:24px 16px 8px;color:#949ba4;font-size:12px;font-weight:600}
.divider::before,.divider::after{content:"";flex:1;border-top:1px solid #3f4147}
.divider span{margin:0 8px}
.msg{position:relative;padding:2px 48px 2px 72px;min-height:22px}
.msg.first{margin-top:17px;padding-top:2px}
.msg:hover{background:rgba(2,2,2,.06)}
.msg .avatar{position:absolute;left:16px;top:4px;width:40px;height:40px;border-radius:50%;object-fit:cover;background:#1e1f22}
.msg .gutter{position:absolute;left:0;top:3px;width:56px;text-align:right;font-size:11px;color:#949ba4;visibility:hidden}
.msg.compact:hover .gutter{visibility:visible}
.header{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.name{font-weight:500;color:#f2f3f5}
.tag{background:#5865f2;color:#fff;font-size:10px;font-weight:600;border-radius:4px;padding:0 5px;line-height:15px;align-self:center;text-transform:uppercase}
.time{font-size:12px;color:#949ba4}
.content{overflow-wrap:anywhere;word-break:break-word}
.content.muted{color:#949ba4}
.edited{font-size:10px;color:#949ba4}
.reply{display:flex;align-items:center;gap:4px;font-size:14px;color:#b5bac1;margin:0 0 2px 0;position:relative;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.reply::before{content:"";position:absolute;left:-56px;top:50%;width:36px;height:10px;border-left:2px solid #4e5058;border-top:2px solid #4e5058;border-top-left-radius:6px}
.msg .reply{margin-left:0}
.r-avatar{width:16px;height:16px;border-radius:50%}
.r-name{font-weight:500;margin-right:2px;opacity:.9}
.r-text{overflow:hidden;text-overflow:ellipsis}
.used{font-size:14px;color:#b5bac1;margin-bottom:2px}
.mention{background:rgba(88,101,242,.3);color:#c9cdfb;border-radius:3px;padding:0 2px;font-weight:500}
.mention:hover{background:#5865f2;color:#fff}
.timestamp{background:rgba(78,80,88,.6);border-radius:3px;padding:0 2px}
.emoji{width:1.375em;height:1.375em;vertical-align:bottom;object-fit:contain}
.emoji.jumbo{width:3rem;height:3rem}
.emoji.small{width:18px;height:18px}
.uemoji{font-family:"Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif}
code.inline{background:#2b2d31;border:1px solid #1e1f22;border-radius:4px;padding:.15em .3em;font-family:Consolas,"Andale Mono WT","Andale Mono","Lucida Console",Monaco,monospace;font-size:.875em}
pre.codeblock{background:#2b2d31;border:1px solid #1e1f22;border-radius:4px;padding:7px 9px;margin:6px 0 0;max-width:90%;overflow:auto;font-family:Consolas,"Andale Mono WT","Andale Mono","Lucida Console",Monaco,monospace;font-size:.875rem;line-height:1.125rem;white-space:pre-wrap}
.quote{border-left:4px solid #4e5058;border-radius:4px;padding:0 8px 0 12px;margin:2px 0}
.h1{font-size:1.5em;font-weight:700;margin:8px 0 4px}.h2{font-size:1.25em;font-weight:700;margin:8px 0 4px}.h3{font-size:1.1em;font-weight:700;margin:6px 0 2px}
.sub{font-size:.75rem;color:#949ba4}
ul,ol{margin:4px 0;padding-left:28px}
.spoiler{background:#1e1f22;color:transparent;border-radius:3px;cursor:pointer;padding:0 2px}
.spoiler.open{background:rgba(78,80,88,.5);color:inherit}
.att img{max-width:min(550px,100%);max-height:350px;border-radius:8px;margin-top:4px;display:block}
.sticker img{width:160px;height:160px;object-fit:contain;margin-top:4px}
.file{display:flex;gap:10px;align-items:center;background:#2b2d31;border:1px solid #232428;border-radius:8px;padding:10px 12px;max-width:432px;margin-top:4px}
.f-icon{font-size:30px}.f-size{font-size:12px;color:#949ba4}
.embed{display:flex;gap:16px;max-width:520px;margin-top:8px;background:#2b2d31;border:1px solid #232428;border-left:4px solid var(--bar,#1e1f22);border-radius:4px;padding:8px 16px 16px 12px}
.e-main{min-width:0;flex:1}
.e-provider{font-size:12px;color:#b5bac1;margin-top:8px}
.e-author{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:14px;font-weight:600;color:#f2f3f5}
.e-author a{color:inherit}.e-author-icon,.e-footer-icon{width:24px;height:24px;border-radius:50%}
.e-title{font-weight:600;margin-top:8px;color:#f2f3f5;overflow-wrap:anywhere}
.e-title a{color:#00a8fc}
.e-desc{font-size:14px;margin-top:8px;overflow-wrap:anywhere}
.e-fields{display:grid;grid-template-columns:repeat(12,1fr);gap:8px;margin-top:8px}
.e-fname{font-size:14px;font-weight:600;color:#f2f3f5}.e-fvalue{font-size:14px;overflow-wrap:anywhere}
.e-image{margin-top:16px}.e-image img{max-width:100%;max-height:300px;border-radius:4px;display:block}
.e-thumb img{width:80px;height:80px;object-fit:cover;border-radius:4px;margin-top:8px}
.e-footer{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:#b5bac1}
.embed-plain img{max-width:min(400px,100%);border-radius:8px;margin-top:4px;display:block}
.c-text{font-size:14px;margin-top:4px}.c-sep{border:0;border-top:1px solid #3f4147;margin:8px 0}
.row{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.btn{display:inline-flex;align-items:center;gap:6px;padding:2px 16px;min-height:32px;border-radius:3px;font-size:14px;font-weight:500;color:#fff}
.btn.disabled{opacity:.5}
.select{display:inline-flex;align-items:center;justify-content:space-between;gap:24px;min-width:240px;padding:8px 12px;background:#1e1f22;border:1px solid #1e1f22;border-radius:4px;color:#949ba4;font-size:14px}
.reactions{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.reaction{display:inline-flex;align-items:center;gap:6px;background:#2b2d31;border:1px solid #2b2d31;border-radius:8px;padding:2px 6px;font-size:14px}
.reaction .count{color:#b5bac1;font-weight:600}
.sys{display:flex;align-items:baseline;gap:8px;padding:6px 16px 2px;color:#949ba4;font-size:15px}
.sys .sys-arrow{color:#3ba55d}
footer{padding:16px;text-align:center;color:#949ba4;font-size:12px;border-top:1px solid #1f2023}
@media (max-width:600px){.msg{padding-right:12px}.embed{flex-direction:column}}
"""


def _summary(info: dict, count: int) -> str:
    rows = [
        ("Ticket", f"#{int(info.get('number', 0)):04d}" if info.get("number") is not None else "—"),
        ("Category", info.get("category") or "—"),
        ("Opened by", info.get("opener") or "—"),
        ("Opened", fmt_full(info.get("opened_at")) + " UTC" if info.get("opened_at") else "—"),
        ("Claimed by", info.get("claimed_by") or "Nobody"),
        ("Closed by", info.get("closed_by") or "—"),
        ("Closed", fmt_full(info.get("closed_at")) + " UTC" if info.get("closed_at") else "—"),
        ("Reason", info.get("reason") or "No reason provided"),
        ("Messages", str(count)),
    ]
    return "".join(f'<div><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>' for k, v in rows)


def render_document(ctx: RenderCtx, messages: list, info: dict, truncated: bool) -> str:
    md = Markdown(ctx)
    ctx.ids_in_transcript = {m.id for m in messages}
    body: list[str] = []
    prev = None
    prev_day = None
    for message in messages:
        try:
            created = _utc(message.created_at)
            day = created.date() if created else None
            if day != prev_day:
                body.append(f'<div class="divider"><span>{esc(fmt_day(created)) if created else ""}</span></div>')
            kind = message.type.name
            compact = bool(
                prev is not None
                and day == prev_day
                and kind in ("default",)
                and prev.type.name in ("default",)
                and prev.author.id == message.author.id
                and getattr(prev, "webhook_id", None) == getattr(message, "webhook_id", None)
                and (message.created_at - prev.created_at).total_seconds() < 420
            )
            rendered = render_message(ctx, md, message, compact)
        except Exception:
            log.exception("Could not render message %s", getattr(message, "id", "?"))
            rendered = (
                f'<div class="msg first"><div class="body"><div class="content muted">'
                f"<i>Message {esc(getattr(message, 'id', '?'))} could not be displayed.</i></div></div></div>"
            )
        body.append(rendered)
        prev, prev_day = message, (_utc(message.created_at).date() if message.created_at else None)

    guild = ctx.guild
    guild_name = esc(getattr(guild, "name", "Server"))
    icon_url = ""
    try:
        icon_url = str(guild.icon.replace(size=64, format="png")) if guild.icon else ""
    except Exception:
        icon_url = ""
    icon_html = (
        f'<img class="guild-icon" src="{esc(ctx.src(icon_url))}" alt="">'
        if icon_url
        else f'<div class="guild-fallback">{esc((getattr(guild, "name", "S") or "S")[:1].upper())}</div>'
    )
    topic = getattr(ctx.channel, "topic", None)
    note = f"Only the first {MAX_MESSAGES} messages are shown. " if truncated else ""
    generated = datetime.now(timezone.utc)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>Transcript - #{esc(ctx.channel.name)}</title><style>{CSS}</style></head><body>"
        f'<header class="chan-header">{icon_html}<div><div class="guild-name">{guild_name}</div>'
        f'<div class="chan-title"><span class="hash">#</span><span>{esc(ctx.channel.name)}</span>'
        f'{f"<span class=topic>{esc(topic)}</span>" if topic else ""}</div></div></header>'
        f'<section class="summary">{_summary(info, len(messages))}</section>'
        f'<main class="messages">{"".join(body)}</main>'
        f"<footer>{note}Transcript generated by {esc(ctx.bot_name)} on {esc(fmt_full(generated))} UTC. "
        "All times are UTC.</footer></body></html>"
    )


# ------------------------------------------------------------------ asset collection
async def _collect(ctx: RenderCtx, messages: list) -> None:
    queued: set[str] = set()
    jobs: list = []

    def add(url: Optional[str], *, max_size: int = SMALL_FILE, essential: bool = False) -> None:
        if url and url not in queued:
            queued.add(url)
            jobs.append(ctx.assets.fetch(url, max_size=max_size, essential=essential))

    def scan_text(text: Optional[str]) -> None:
        for match in EMOJI_RAW.finditer(text or ""):
            add(emoji_url(match.group(3), bool(match.group(1))), essential=True)

    guild = ctx.guild
    try:
        if guild is not None and guild.icon:
            add(str(guild.icon.replace(size=64, format="png")), essential=True)
    except Exception:
        pass

    for message in messages:
        try:
            author = message.author
            ctx.users[author.id] = getattr(author, "display_name", None) or getattr(author, "name", "Unknown")
            for user in getattr(message, "mentions", []) or []:
                ctx.users.setdefault(user.id, getattr(user, "display_name", None) or user.name)
            add(avatar_url(author), essential=True)
            ref = getattr(getattr(message, "reference", None), "resolved", None)
            if ref is not None and hasattr(ref, "author"):
                add(avatar_url(ref.author), essential=True)
            scan_text(message.content)
            for att in getattr(message, "attachments", []) or []:
                if _is_image(att) and getattr(att, "size", 0) <= PER_FILE:
                    add(att.url, max_size=PER_FILE)
            for sticker in getattr(message, "stickers", []) or []:
                if getattr(getattr(sticker, "format", None), "name", "") != "lottie":
                    add(str(sticker.url), essential=True)
            for reaction in getattr(message, "reactions", []) or []:
                if getattr(reaction.emoji, "id", None):
                    add(emoji_url(reaction.emoji.id, bool(getattr(reaction.emoji, "animated", False)), 32), essential=True)
            for embed in getattr(message, "embeds", []) or []:
                data = embed.to_dict()
                scan_text(data.get("title"))
                scan_text(data.get("description"))
                for field in data.get("fields", []) or []:
                    scan_text(field.get("name"))
                    scan_text(field.get("value"))
                for key in ("thumbnail", "image"):
                    add((data.get(key) or {}).get("url"), max_size=PER_FILE)
                add((data.get("author") or {}).get("icon_url"))
                add((data.get("footer") or {}).get("icon_url"))
            stack = list(getattr(message, "components", []) or [])
            while stack:
                comp = stack.pop()
                emoji = getattr(comp, "emoji", None)
                if emoji is not None and getattr(emoji, "id", None):
                    add(emoji_url(emoji.id, bool(getattr(emoji, "animated", False)), 32), essential=True)
                stack.extend(getattr(comp, "children", []) or [])
        except Exception:
            log.debug("asset scan skipped a message", exc_info=True)

    if jobs:
        try:
            await asyncio.wait_for(asyncio.gather(*jobs, return_exceptions=True), timeout=TOTAL_FETCH_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("Transcript media download timed out; continuing with what was downloaded")


def _plain_fallback(channel_name: str, messages: list, info: dict) -> str:
    """Last resort if the HTML renderer itself fails: still a readable file."""
    lines = [f"Transcript of #{channel_name}", ""]
    for message in messages:
        try:
            lines.append(
                f"[{fmt_full(message.created_at)}] {getattr(message.author, 'display_name', '?')}: "
                f"{message.content or ''}"
            )
            for att in getattr(message, "attachments", []) or []:
                lines.append(f"    attachment: {att.filename} {att.url}")
        except Exception:
            continue
    return "<pre style=\"white-space:pre-wrap;font-family:monospace\">" + esc("\n".join(lines)) + "</pre>"


async def build(
    bot: Any, channel: "discord.TextChannel", info: dict, *, limit_bytes: int, bot_name: str = "WailQueen"
) -> tuple[str, bytes]:
    """Returns (filename, bytes). The bytes are .html, or .html.gz when the HTML is bigger than limit_bytes."""
    messages: list = []
    async for message in channel.history(limit=MAX_MESSAGES, oldest_first=True):
        messages.append(message)
    truncated = len(messages) >= MAX_MESSAGES

    async with aiohttp.ClientSession(headers={"User-Agent": "WailQueen-Transcript/1.0"}) as session:
        assets = Assets(session)
        ctx = RenderCtx(bot, channel.guild, channel, assets, True, bot_name)
        try:
            await _collect(ctx, messages)
        except Exception:
            log.exception("Transcript asset collection failed")

    def make(inline: bool) -> bytes:
        ctx.inline = inline
        try:
            doc = render_document(ctx, messages, info, truncated)
        except Exception:
            log.exception("Transcript rendering failed, using the plain fallback")
            doc = f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>{_plain_fallback(channel.name, messages, info)}</body></html>"
        return doc.encode("utf-8")

    data = make(True)
    name = f"transcript-{channel.name}.html"
    if len(data) > limit_bytes:
        data = make(False)  # same page with normal links instead of embedded images
    if len(data) > limit_bytes:
        data = gzip.compress(data, compresslevel=9)
        name += ".gz"
    return name, data
