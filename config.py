"""Central configuration. Edit values here or override them in the .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _path(name: str, default: str) -> str:
    """Relative paths are relative to the bot folder (not to wherever the bot was started from),
    so the data folder is found no matter which directory the host starts the bot in."""
    p = Path(os.getenv(name, default))
    return str(p if p.is_absolute() else BASE_DIR / p)

TOKEN = os.getenv("DISCORD_TOKEN", "")
PREFIX = os.getenv("BOT_PREFIX", "!")
BOT_NAME = os.getenv("BOT_NAME", "WailQueen")
AUTO_SYNC = os.getenv("AUTO_SYNC", "1") == "1"  # sync slash commands on startup

# Where all server data (welcome settings, warnings) is saved. Keep this folder between restarts.
DB_PATH = _path("DB_PATH", "data/bot.db")

# Rate limit: a user may run RATE_LIMIT_COMMANDS commands every RATE_LIMIT_PERIOD seconds.
RATE_LIMIT_COMMANDS = int(os.getenv("RATE_LIMIT_COMMANDS", "5"))
RATE_LIMIT_PERIOD = float(os.getenv("RATE_LIMIT_PERIOD", "10"))

# Longest timeout Discord allows is 28 days.
MAX_TIMEOUT_DAYS = 28

# Embed colors (hex integers)
ACCENT = 0x5865F2

# Optional support link shown in /botinfo. Leave empty to hide it.
SUPPORT_URL = os.getenv("SUPPORT_URL", "")

# Channels referenced inside the default welcome message
PROTOCOL_CHANNEL_ID = int(os.getenv("PROTOCOL_CHANNEL_ID", "0"))
INFO_BOARD_CHANNEL_ID = int(os.getenv("INFO_BOARD_CHANNEL_ID", "0"))

# ---------------- Welcome message defaults (easy to edit) ----------------
# Placeholders: {user} {username} {server} {count}
WELCOME_CONTENT = "Warm greetings, {user}! 🕸️"
WELCOME_TITLE = "Welcome to the Spider's Web community."
WELCOME_DESCRIPTION = (
    "To ensure a productive environment for all members, please review our "
    "**Community Guidelines** in {protocol}. "
    "For a comprehensive overview of our resources and objectives, "
    "additional documentation is available in {info_board}."
)
WELCOME_COLOR = "random"  # "random" or a hex value such as "#9B59B6"
RANDOM_COLORS = [0x9B59B6, 0x9EFF4A, 0xA3204F, 0x3498DB, 0xE67E22, 0x1ABC9C]

# ---------------- Emojis used in /userinfo ----------------
EMOJI = {
    "badges": "🏅",
    "none": "❌",
    "roles": "🎭",
    "permissions": "🔐",
    "activity": "🎮",
    "online": "🟢",
    "idle": "🟡",
    "dnd": "🔴",
    "offline": "⚫",
}

# ---------------- Webhook backup (cogs/backup.py) ----------------
# Webhook that receives a copy of ALL bot data every BACKUP_WEBHOOK_INTERVAL seconds. Keep it secret!
BACKUP_WEBHOOK_URL = os.getenv("BACKUP_WEBHOOK_URL", "").strip()
# "send" = a new message with the files each time, "edit" = keep one message and update it.
BACKUP_WEBHOOK_MODE = os.getenv("BACKUP_WEBHOOK_MODE", "edit").strip().lower()
BACKUP_WEBHOOK_INTERVAL = max(int(os.getenv("BACKUP_WEBHOOK_INTERVAL", "30")), 10)
# 1 = if the bot starts with EMPTY data (wiped disk / new host), restore the newest backup from the backup channel.
AUTO_RECOVER = os.getenv("AUTO_RECOVER", "1") == "1"

# ---------------- Ticket system defaults (cogs/tickets.py) ----------------
# Everything here is only the DEFAULT. Admins change it per server with !ticketedit / !ticketcategory.
TICKET_COLOR = 0x2ECC71  # green bar of the panel and the ticket messages

# Panel message (Components V2). Discord markdown works. {server} = server name.
TICKET_PANEL_TEXT = """### __Contact Support Team__

📩 **Create A Ticket**- if you Need support or __Any important queries__

🎫 **Do Not Create a Ticket For Fun!**

📜 __**Ticket For Support**__ :-
- Create Ticket For General Support
[for e.g. Reporting Someone , Bugs, Special Category Role claiming,etc]
- Creating a ticket to be staff is strictly prohibited.
- Tickets for fun would result in 2days mute / ban
- If you are found spamming or wasting time by making tickets for irrelevant issues might lead to a strike or a serious action which will be taken by our Support Team.
- Opening a ticket to talk to Senpai or ask for roles is strictly prohibited. So, please don't create a ticket regarding this topic.

🤝 __**Ticket for Partnership/Sponsors**__ :-
- Create a Ticket If You Got Any Brand Deals Or Sponsors For SenpaiSpider
- Kindly Mention The Basic Details And Have Patience.
- The Rates For Brand Deals Would Be Discussed In Ticket
- Leaking The Information To Anyone Would Lead To Ban.
- You Could Contact Our Business Mail Too senpaispidersponsership@gmail.com

__select category from the dropdown menu to contact us__"""

TICKET_PANEL_PLACEHOLDER = "Select a topic..."

# First message inside a new ticket. Placeholders: {user} {server} {category}
TICKET_WELCOME_TITLE = "Help/support"
TICKET_WELCOME_DESCRIPTION = (
    "🧑‍💼 **Welcome to the Ticket Support**\n"
    "> __Kindly describe your issue/reason of opening this ticket a staff member would reach to support soon__"
)

# Categories created automatically the first time a server uses !ticketset.
#   name = used inside the ticket (topic, embed, logs) | label = text in the dropdown
#   question / placeholder = the form that opens when the category is picked
TICKET_DEFAULT_CATEGORIES = [
    {
        "name": "General Support",
        "label": "General Support",
        "emoji": "📩",
        "question": "General Support",
        "placeholder": "Kindly Mention your issue!",
    },
    {
        "name": "Partnership",
        "label": "Open a ticket For Partnership",
        "emoji": "📩",
        "question": "Partnership",
        "placeholder": "Kindly mention the basic details!",
    },
]

# ---------------- Anti-nuke (cogs/antinuke.py) ----------------
AN_DB_PATH = _path("AN_DB_PATH", "data/antinuke.db")
AN_TICK_SECONDS = 1                                               # structure backup + webhook mirror interval
AN_CHANNEL_DELETE_COOLDOWN = 2 * 3600                             # after deleting 1 channel, the next delete within 2h is punished
AN_SNAPSHOT_HISTORY = 100                                         # structure versions kept per server
# action -> (how many, within how many seconds) before it counts as a nuke attempt
AN_LIMITS = {
    "channel_create": (3, 10), "role_create": (3, 10), "role_delete": (2, 10), "ban": (3, 10),
    "kick": (3, 10), "webhook_create": (3, 10), "message_wipe": (15, 10),
}
AN_WIPE_PUNISH_AT = int(os.getenv("AN_WIPE_PUNISH_AT", "50"))  # message wipe this big = punish + auto restore (smaller = log only)
