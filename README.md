# Steal a Galacticrot Discord moderation bot — Railway

## Files
- `bot.py` — bot source
- `requirements.txt` — Python dependencies

## Railway settings
- Start command: `python bot.py`
- Add service variables:
  - `DISCORD_TOKEN` = your Discord bot token (keep it secret)
  - `PREFIX` = `+` (optional)
  - `INVITES_CHANNEL_ID` = `1549961077473808504` (optional; already the default)
  - `MOD_LOG_CHANNEL_ID` = `1549961164262215761` (optional; already the default)
  - `DATABASE_PATH` = `/data/raiders.sqlite3`

Attach a Railway Volume to this service and set its mount path to `/data`. This keeps warnings, anti-raid settings, and blacklist records across redeployments.

Do not upload `.env` or put your bot token in GitHub. Set the token only in Railway's Variables tab.
