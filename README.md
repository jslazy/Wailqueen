# WailQueen

Information and welcome bot for discord.py 2.6+. Prefix and slash commands.

## Commands

### Everyone
| Command | Aliases | Description |
|---|---|---|
| help [command] | h, commands | Shows only the commands *you* are allowed to use |
| botinfo | bi, about, stats | Bot information and statistics |
| userinfo [member] | ui, whois, profile | Badges (with icons), roles, permissions, status, activity |
| serverinfo | si, guildinfo | Server overview |
| avatar [user] | av, pfp | Full-size avatar |
| banner [user] | bn | Profile banner |
| ping | | Latency and uptime |

### Webhook backup (automatic, no command)
Set `BACKUP_WEBHOOK_URL` in `.env` and every minute (`BACKUP_WEBHOOK_INTERVAL`) the webhook gets ONE zip with the whole bot
(all source files + a fresh `data/bot.db` snapshot and `data/bot-data.json`) and a detailed report embed (status, uptime,
latency, servers, users, database rows, zip size and hash, versions). One more backup is sent right before the bot goes offline.
`.env` is never included. `BACKUP_WEBHOOK_MODE=send` posts a new message each time, `edit` keeps updating one message.
The database is only read, never changed. The webhook receives ALL bot data, so keep it private.

### Custom aliases (Administrator)
`alias add <command> <name>` gives a command a short name in your server, e.g. `alias add ping p`,
then `!p` works. `alias remove <name>`, `alias list`. Permissions of the real
command still apply.

### No prefix (bot owner only)
`noprefix add <user>` lets that user run commands without the prefix (`ping` instead of `!ping`). `noprefix remove <user>`, `noprefix list` (alias `np`). Slash: `/noprefix add|remove|list`. Saved in `data/bot.db`.

### Welcome setup (slash only, needs Manage Server, hidden from normal members)
`/welcome-setup`, `/welcome-edit` (text and color, with an Upload banner button), `/welcome-test`, `/welcome-toggle`, `/welcome-reset`

Normal members never see these in the slash menu or in `help`. Administrators see everything.

## Tickets

All setup commands need **Manage Server**. The bot needs **Manage Channels** (and View/Send/Embed/Attach in the log channels).

| Command | Description |
|---|---|
| ticketset [#channel] | Sends the ticket panel (Components V2, default text from `config.py`) and remembers the channel |
| ticketedit | Edit panel text/color, **Upload banner** button, ticket message, **support roles**, **admin roles**, tickets per member, resend panel |
| ticketcategory | List categories. `create <name>`, `edit <name>`, `delete <name>` (edit lets you pick the Discord channel category tickets are created in) |
| ticketlogs [#channel] | Pick the 3 log channels: ticket opened, ticket closed, ticket transcript (or one channel for all) |

- Defaults: categories *General Support* and *Partnership* are created on first use.
- Buttons in every ticket: **Close**, **Close With Reason**, **Claim**. Only members with a support/admin role can close/claim (if no role is set yet, Manage Server members can).
- On close the opener gets a DM (ticket, category, closed by, reason, times) with the transcript file, the logs are posted, then the channel is deleted.
- Transcript = one `.html` file that looks like Discord: avatars, role colors, APP tags, replies, embeds, buttons, reactions, stickers, mentions, server emojis and image attachments are embedded in the file, so it works offline.
- Panel/buttons keep working after restarts.

## Automod and help

| Command | Description |
|---|---|
| automod | Panel: on/off, mode (Clean / Timeout / Both), timeout time, ignored roles |
| automod add/remove/list <word> | Your own blocked words (fu#k, a$$, sh!t, f**k style spellings are caught too) |
| adminhelp | Small admin help menu with a category dropdown |
| help | Same small dropdown style for everyone |

- Clean mode deletes the message and re-sends it with nice words instead (same name + avatar via webhook). Needs Manage Messages + Manage Webhooks. Timeout needs Moderate Members.
- Backup now includes `.env` (token + webhook) and edits ONE message instead of sending a new one. Keep the backup channel private.

## Rate limit
Every user can run 5 commands per 10 seconds (prefix + slash). Change it with
`RATE_LIMIT_COMMANDS` and `RATE_LIMIT_PERIOD` in `.env`. The bot owner is exempt.

## Saved data
Everything (welcome settings, warnings) is stored per server in `data/bot.db`.
Keep the `data/` folder between restarts/updates and nothing is lost. An old `welcome.db`
in the project folder is imported automatically on first start.

## Setup
1. Python 3.11 or newer.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
4. Developer Portal, Bot: enable Presence, Server Members and Message Content intents.
5. Invite with scopes `bot` + `applications.commands`. Permissions: Send Messages, Embed Links,
   Ban Members, Kick Members, Moderate Members, View Audit Log. The bot's role must sit above the members it moderates.
6. `python main.py` (slash commands sync automatically on start, `AUTO_SYNC=1`).

## Badges
`userinfo` shows profile badges with icons from https://github.com/mezotv/discord-badges. Only one account-age
badge is shown (10+ years = Primordial only) and only one boost badge. On first start the bot downloads the icons,
converts them to PNG (cached in `assets/badges/`) and uploads them as application emojis; until then (or if that
fails) unicode emojis are shown. You can also drop your own `assets/badges/<key>.png`.

### Streaming / hours played / game variety / Nitro tier / legacy badges (bot owner only)
Discord does not give these to bots, so they cannot be detected. Assign them yourself and `userinfo` shows them:
`badge add <user> <name>` (e.g. `badge add @user phenomenon`, `opal`, `orb`, `quest`, `username`), `badge remove`, `badge list [user]`, `badge keys`.
One badge per family (a new streaming badge replaces the old one). Saved in the `custom_badges` table of `data/bot.db`.

## Icons
The icons in `assets/icons/` are uploaded automatically on first start as application emojis of the bot
(they work in every server and in DMs). Nothing to set up. If an upload fails the bot falls back to plain
unicode emojis. To change an icon, replace the PNG (128x128, same file name) and delete the matching
`wl_<name>` emoji in the Developer Portal (Bot > Emojis). Icon names are in `utils/icons.py`.

## Anti-nuke (`!antinuke`, alias `!an`)
Only the server owner, the bot owner and trusted users can open it. Everybody else gets no answer at all (their message is removed).

Panel buttons: **Enable/Disable protection**, **Set webhook** (paste the webhook link), **Backup now**, **Backup details & restore**, **Settings** (punishment, auto restore, trusted users), **Refresh**.

* Backup: every second the server structure (roles, categories, channels, permissions) is saved; every message (users, bots, webhooks) and its links is saved the moment it is sent. Old history is back-filled in the background. Only messages + channels (and the roles their permissions need) are saved, no files are downloaded. Everything lives inside the bot in `data/antinuke.db` (no zip). The settings (webhook link, trusted users, punishment, on/off) are part of every backup and come back automatically after a restart / wiped disk.
* Webhook: live status message (edited every second when something changed) + `structure.json.gz`, and alerts for deleted channels / nukes. Optional. Keep that channel private.
* Channel delete cooldown: deleting one channel (voice channels excluded) starts a 2 hour cooldown for that person. A second delete in that time = punishment + the channel and its messages are restored automatically.
* Also watched: mass ban / kick, role + channel + webhook spam, bots added by non-trusted people, channels "nuked" by delete + clone, messages wiped from a channel.
* Restore: panel > Backup details & restore > pick a deleted channel / role / wipe, or restore everything. Channels are rebuilt with permissions, messages are re-posted with the original name, avatar and text (through a temporary webhook).
* Needs the permissions: Administrator (or Manage Channels, Manage Roles, Manage Webhooks, Ban/Kick Members, View Audit Log) and a bot role placed ABOVE the roles it must punish.

## Badge emojis
`!badge emoji <badge> <emoji>` changes the emoji of a badge (normal or custom server emoji, a lost `>` is repaired). `!badge emoji <badge> reset` restores the default.

## Loading / merging backup data
* `!backupload` (owner only, alias `!loadbackup`): attach the `-data.zip` from your backup channel (or a `bot.db` / `bot-data.json`, several files at once, or reply to a message that has them). Everything that is new is ADDED to the running bot (warnings, mod cases, tickets, giveaways and their entries, settings, badges...), rows that already exist are skipped, nothing is deleted. Loading the same file twice changes nothing. `!backupload replace` also lets uploaded settings overwrite existing ones. A safety copy of the old data is saved in `data/backups/` first.
* No command needed: put backup files (`.zip`, `.db`, `.json`) into the `import/` folder, or replace `seed/bot.db`, and they are merged at the next start (once per file).
* bot.db no longer uses `-wal` files, so replacing `data/bot.db` can no longer corrupt or reset it. A leftover `-wal` file is moved to `data/stale/` and its rows are merged back automatically.
* **Backup now holds everything**: `-data.zip` = `bot.db` + `bot-data.json` + `antinuke-config.json` (anti-nuke webhook link, trusted users, punishment...), `-antinuke.zip` = the full anti-nuke database, `-code.zip` = the code. `!backupload` merges all of it (bot data AND anti-nuke data); the same applies to files in `import/` and `seed/`.
* **Automatic recovery** (`AUTO_RECOVER=1`, default): if the bot starts with completely EMPTY data (host wiped the disk), it takes the newest backup from the backup webhook's channel and merges it back. Needs Read Message History for the bot in that channel (or `data/backup_state.json` surviving). It only adds data and runs before the first new backup is sent.
* **Images go through the owner's image host.** The bot owner sets one channel with `!imgchannel #channel` (any server). Every banner / thumbnail uploaded with the **Upload** buttons (ticket panel, giveaway, welcome) is re-posted there; pasting links is no longer possible. Only a reference (`host:<channel>:<message>`) and the link are saved in `bot.db`, so backups hold links, never images. Discord image links expire, so the bot reads the links again from the host messages every 2 hours. Never delete messages in the host channel.
* **Owner commands:** `!imgchannel [#channel|off]`, `!tolink` (attach an image or reply to one: uploads it and gives the link), `!linklist` (all saved links, with pages). Hidden from everyone except the bot owner.
* Relative paths (`data/...`) are now relative to the bot folder, not to the folder the host starts the bot from.


## Anti-nuke restore (how messages come back)
When a deleted channel is restored, every saved message is posted back through a temporary channel webhook, so it shows the
**same name + same profile picture + same text** as the original (original embeds are sent along, attachments come back as links).
The webhook is deleted again after the channel is done. Needs the **Manage Webhooks** permission; without it the bot falls back to
embeds that show the author's name + avatar.
Messages that fail stay saved; open `!antinuke` > Backup Details > "Retry messages" to try again.

> The old `antinuke.zip` (Upload / Refresh buttons) is gone: the backup stays in the bot's own database (`AN_DB_PATH`).
> On Railway keep the volume mounted so `antinuke.db` survives redeploys. You can delete the old `data/antinuke_files` folder.
