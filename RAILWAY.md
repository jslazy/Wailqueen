# Deploy on Railway

1. Put this folder in a PRIVATE GitHub repo (it contains your server data in `seed/`) and create a Railway project from it
   (New Project > Deploy from GitHub repo). It is a worker: no port, no domain needed.
2. Service > **Volumes** > New Volume, mount path `/data`  (without it every redeploy erases the database).
3. Service > **Variables**, add:

```
DISCORD_TOKEN=your_bot_token
BOT_PREFIX=!
BOT_NAME=WailQueen
AUTO_SYNC=1
DB_PATH=/data/bot.db
AN_DB_PATH=/data/antinuke.db
AN_FILES_DIR=/data/antinuke_files
BACKUP_WEBHOOK_URL=your_private_webhook_url
BACKUP_WEBHOOK_MODE=edit
BACKUP_WEBHOOK_INTERVAL=60
RATE_LIMIT_COMMANDS=5
RATE_LIMIT_PERIOD=10
SUPPORT_URL=
PROTOCOL_CHANNEL_ID=0
INFO_BOARD_CHANNEL_ID=0
```
4. Deploy. To bring in more data later (for example from another data folder), upload the `-data.zip` with `!backupload`, or put the file in the `import/` folder before deploying. On the first start the bundled `seed/bot.db` is copied into the empty volume (an existing database is never overwritten).
5. Developer Portal > Bot: turn on Server Members, Presence and Message Content intents.
