import os
import json
import time
import sqlite3
import asyncio
import random
import re
import logging
from collections import deque
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("galaxy_bot")

# Keep the bot token outside this file, in .env as DISCORD_TOKEN=your_token.
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "+")
# For multiple servers, optionally set JSON environment variables keyed by
# Discord server ID. Example:
# INVITES_CHANNEL_IDS={"GUILD_ID_1":111111111111111111,"GUILD_ID_2":222222222222222222}
# MOD_LOG_CHANNEL_IDS={"GUILD_ID_1":333333333333333333}
def load_guild_channel_map(variable_name):
    raw = os.getenv(variable_name, "").strip()
    if not raw:
        return {}
    try:
        return {int(guild_id): int(channel_id)
                for guild_id, channel_id in json.loads(raw).items()}
    except (ValueError, TypeError, json.JSONDecodeError):
        raise SystemExit(f"{variable_name} must be valid JSON mapping server IDs to channel IDs.")

INVITES_CHANNEL_IDS = load_guild_channel_map("INVITES_CHANNEL_IDS")
MOD_LOG_CHANNEL_IDS = load_guild_channel_map("MOD_LOG_CHANNEL_IDS")
# Backward-compatible default for the original server only.
DEFAULT_INVITES_CHANNEL_ID = int(os.getenv("INVITES_CHANNEL_ID", "1549961077473808504"))
DEFAULT_MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "1549961164262215761"))
JOIN_WINDOW_SECONDS = int(os.getenv("JOIN_WINDOW_SECONDS", "20"))
JOIN_BURST_LIMIT = int(os.getenv("JOIN_BURST_LIMIT", "6"))
GALAXY_GAME_CHANNEL_ID = 1549949873468604438
GALAXY_GAME_INTERVAL_SECONDS = 60 * 60  # Start a new trivia round every hour.
GALAXY_GAME_HINT_SECONDS = 90  # One hint every 90 seconds, up to three hints.
GALAXY_QUESTION_REWARD = 100
GALAXY_LEADERBOARD_CHANNEL_ID = 1549950104516165682
GALAXY_LEADERBOARD_INTERVAL_SECONDS = 3 * 60 * 60  # Every 3 hours.

# All 20 staff role IDs supplied in the screenshots.
ROLE_IDS = {
    "founder": 1574537379803431036,
    "co_owner": 1550545604839735416,
    "dev": 1561622770855706705,
    "axelle": 1571483535565389965,
    "drc": 1564021026839334963,
    "senior_drc": 1550583960512241785,
    "trial_director": 1568319372764647424,
    "head_manager": 1561457371878916246,
    "assistant_head_manager": 1562142313105330247,
    "manager": 1564013035478188173,
    "staff_manager": 1549921109137424446,
    "assistant_staff_manager": 1568318227132579942,
    "head_admin": 1568318014955458651,
    "senior_admin": 1568319272554340352,
    "admin": 1549922536115146772,
    "junior_admin": 1568317976141111316,
    "head_moderator": 1549921367913271366,
    "senior_moderator": 1549921724743811183,
    "moderator": 1549922177673994320,
    "trial_moderator": 1558956173397327872,
}

# Six permission levels. Higher levels inherit every general staff command.
PERM_ROLE_IDS = {
    1: {ROLE_IDS["trial_moderator"]},
    2: {ROLE_IDS["moderator"]},
    3: {ROLE_IDS["senior_moderator"], ROLE_IDS["head_moderator"]},
    4: {ROLE_IDS["admin"], ROLE_IDS["senior_admin"], ROLE_IDS["junior_admin"]},
    5: {
        ROLE_IDS["head_admin"], ROLE_IDS["assistant_staff_manager"],
        ROLE_IDS["staff_manager"], ROLE_IDS["manager"],
        ROLE_IDS["assistant_head_manager"], ROLE_IDS["head_manager"],
        ROLE_IDS["trial_director"], ROLE_IDS["senior_drc"],
        ROLE_IDS["drc"], ROLE_IDS["axelle"], ROLE_IDS["dev"],
    },
    6: {ROLE_IDS["founder"], ROLE_IDS["co_owner"]},
}
PERM1_ROLE_IDS = set().union(*PERM_ROLE_IDS.values())
ROLE_RANK = {role_id: level for level, role_ids in PERM_ROLE_IDS.items() for role_id in role_ids}

# Restored from the previous Galaxy Bot word/scam filter. Perm 5+ staff are exempt.
BLACKLISTED_WORDS = [
    # slurs / hate
    "nigger", "nigga", "faggot", "fag", "tranny", "retard", "retarded",
    "nazi", "hitler", "kike", "chink", "spic", "coon", "beaner",
    "fuck bitch", "fuck you bitch",
    # sexual / crude
    "femboy", "d*ck", "dick", "cock", "pussy", "whore", "slut", "hoe",
    "porn", "nudes", "onlyfans",
    # self-harm / threats
    "kys", "kill yourself", "kill urself", "hang yourself", "go die",
    "neck yourself", "end yourself",
    # other (retained from the previous filter)
    "server",
]

SCAM_WORDS = [
    "free nitro", "discord.gift", "steamcommunity.com/gift",
    "free nitro giveaway", "nitro gift", "claim nitro",
]

# Blacklisted-word escalation is counted over a rolling 30-day period.
# 1-3: delete + warn; 4: 30m; 5: 1h; 6: 6h; 7: 12h; 8+: 24h.
WORD_TIMEOUT_MINUTES = {4: 30, 5: 60, 6: 360, 7: 720}
WORD_STRIKE_WINDOW_DAYS = 30

# Restricted commands: ban = Perm 4+, kick = Perm 3+, blacklist = Perm 6.
BAN_ROLE_IDS = set().union(PERM_ROLE_IDS[4], PERM_ROLE_IDS[5], PERM_ROLE_IDS[6])
KICK_ROLE_IDS = set().union(PERM_ROLE_IDS[3], PERM_ROLE_IDS[4], PERM_ROLE_IDS[5], PERM_ROLE_IDS[6])
BLACKLIST_ROLE_IDS = set(PERM_ROLE_IDS[6])
ADMIN_ACTION_ROLE_IDS = set(KICK_ROLE_IDS)
OWNER_ACTION_ROLE_IDS = set(BLACKLIST_ROLE_IDS)


def has_staff_rank(member, minimum_rank):
    return max((ROLE_RANK.get(role.id, 0) for role in getattr(member, "roles", [])), default=0) >= minimum_rank


def require_staff_rank(minimum_rank):
    async def predicate(ctx):
        return has_staff_rank(ctx.author, minimum_rank)
    return commands.check(predicate)


def require_founder_or_coowner():
    async def predicate(ctx):
        return has_any_role(ctx.author, OWNER_ACTION_ROLE_IDS)
    return commands.check(predicate)

# Staff strikes remain available to the higher staff group.
STAFF_STRIKE_ROLE_IDS = set(PERM_ROLE_IDS[5]) | set(PERM_ROLE_IDS[6])

# Special users supplied by the server owner.
# This is a global user-ID registry. It does not automatically grant permissions;
# add a permission check deliberately if these users should receive extra access.
SPECIAL_USERS = {
    1464807318637121618,
    1400805489889120286,
    1566616591267930112,
    1244774065231761458,
    841710745897009212,
    1504581544042434731,
    1532561566271017009,
}
BAN_USER_ALLOWLIST = set()
KICK_USER_ALLOWLIST = set()
BLACKLIST_USER_ALLOWLIST = set()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class GalaxyContext(commands.Context):
    """Send command output as a clean, non-reply Galaxy-themed embed panel."""
    async def send(self, content=None, **kwargs):
        embed = kwargs.get("embed")
        if embed is None and content is not None:
            embed = discord.Embed(
                description=str(content),
                color=discord.Color.from_rgb(111, 66, 193),
            )
            command_name = self.command.qualified_name if self.command else "Galaxy Bot"
            embed.set_author(name=f"✦ GALAXY SYSTEMS  •  {command_name.upper()}")
            embed.set_footer(text="✧ The Multiverse • Galaxy Bot")
            kwargs.pop("content", None)
            kwargs["embed"] = embed
            content = None
        elif embed is not None:
            embed.color = discord.Color.from_rgb(111, 66, 193)
            if not embed.author or not embed.author.name:
                command_name = self.command.qualified_name if self.command else "Galaxy Bot"
                embed.set_author(name=f"✦ GALAXY SYSTEMS  •  {command_name.upper()}")
            if not embed.footer or not embed.footer.text:
                embed.set_footer(text="✧ The Multiverse • Galaxy Bot")
        kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
        kwargs.pop("reference", None)
        kwargs.pop("mention_author", None)
        return await super().send(content, **kwargs)


bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    cls=GalaxyContext,
    allowed_mentions=discord.AllowedMentions.none(),
)
DATABASE_PATH = os.getenv("DATABASE_PATH", "raiders.sqlite3")
database_dir = os.path.dirname(DATABASE_PATH)
if database_dir:
    os.makedirs(database_dir, exist_ok=True)
db = sqlite3.connect(DATABASE_PATH)
db.execute("""CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL, reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
db.execute("""CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER PRIMARY KEY, antiraid INTEGER DEFAULT 1
)""")
db.execute("""CREATE TABLE IF NOT EXISTS blacklist (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL, reason TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (guild_id, user_id)
)""")
db.execute("""CREATE TABLE IF NOT EXISTS sanctions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL, moderator_id INTEGER NOT NULL,
    sanction_type TEXT NOT NULL, reason TEXT NOT NULL,
    details TEXT DEFAULT '', source_warning_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
# Bring older warning and blacklist records into the unified sanctions history.
db.execute("""INSERT INTO sanctions
    (guild_id, user_id, moderator_id, sanction_type, reason, source_warning_id, created_at)
    SELECT w.guild_id, w.user_id, w.moderator_id, 'Warn', COALESCE(w.reason, 'No reason provided'),
           w.id, w.created_at
    FROM warnings w
    WHERE NOT EXISTS (
        SELECT 1 FROM sanctions s
        WHERE s.guild_id=w.guild_id AND s.source_warning_id=w.id AND s.sanction_type='Warn'
    )""")
db.execute("""INSERT INTO sanctions
    (guild_id, user_id, moderator_id, sanction_type, reason, created_at)
    SELECT b.guild_id, b.user_id, b.moderator_id, 'Blacklist', COALESCE(b.reason, 'No reason provided'),
           b.created_at
    FROM blacklist b
    WHERE NOT EXISTS (
        SELECT 1 FROM sanctions s
        WHERE s.guild_id=b.guild_id AND s.user_id=b.user_id AND s.sanction_type='Blacklist'
    )""")
db.commit()
db.execute("""CREATE TABLE IF NOT EXISTS staff_strikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
db.execute("""CREATE TABLE IF NOT EXISTS blacklisted_word_strikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    matched_word TEXT NOT NULL,
    message_content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
db.execute("CREATE INDEX IF NOT EXISTS idx_word_strikes_lookup ON blacklisted_word_strikes(guild_id, user_id, created_at)")
db.commit()
db.execute("""CREATE TABLE IF NOT EXISTS galaxy_coins (
    guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    balance INTEGER NOT NULL DEFAULT 0,
    last_daily TEXT,
    PRIMARY KEY (guild_id, user_id)
)""")
db.commit()

recent_joins = {}
invite_cache = {}
active_galaxy_questions = {}  # guild_id -> active question dictionary

def record_sanction(guild_id, user_id, moderator_id, sanction_type, reason,
                    details="", source_warning_id=None):
    cursor = db.execute(
        """INSERT INTO sanctions
           (guild_id, user_id, moderator_id, sanction_type, reason, details, source_warning_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (guild_id, user_id, moderator_id, sanction_type, reason, details, source_warning_id),
    )
    db.commit()
    return cursor.lastrowid


def get_coin_balance(guild_id, user_id):
    row = db.execute("SELECT balance FROM galaxy_coins WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    return int(row[0]) if row else 0

def change_coins(guild_id, user_id, amount):
    db.execute(
        "INSERT INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,?) "
        "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=MAX(0, balance + excluded.balance)",
        (guild_id, user_id, amount),
    )
    db.commit()

def set_coin_balance(guild_id, user_id, amount):
    db.execute(
        "INSERT INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,?) "
        "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
        (guild_id, user_id, amount),
    )
    db.commit()

GALAXY_QUESTIONS = [
    {"question": "What has keys but cannot open locks?", "answer": "piano", "hints": ["It makes music.", "It can be grand or upright.", "It usually has black and white keys."]},
    {"question": "What gets wetter the more it dries?", "answer": "towel", "hints": ["You use it after washing.", "It absorbs water.", "You might hang it in a bathroom."]},
    {"question": "What has a face and two hands but no arms or legs?", "answer": "clock", "hints": ["It helps you stay on schedule.", "It can hang on a wall.", "Its hands point to numbers."]},
    {"question": "What can travel around the world while staying in one corner?", "answer": "stamp", "hints": ["It is used with mail.", "It goes on an envelope.", "It shows that postage was paid."]},
    {"question": "What has one eye but cannot see?", "answer": "needle", "hints": ["It is used for sewing.", "Thread passes through it.", "Its eye is a tiny hole."]},
    {"question": "What has many teeth but cannot bite?", "answer": "comb", "hints": ["You might keep it in a bathroom.", "It helps fix your hair.", "Its teeth are in a row."]},
    {"question": "What goes up but never comes down?", "answer": "age", "hints": ["Everyone has one.", "It changes every birthday.", "It measures how long you have lived."]},
    {"question": "What has a neck but no head?", "answer": "bottle", "hints": ["It can hold a drink.", "It may have a cap.", "You pour liquid from it."]},
    {"question": "What can you break without touching it?", "answer": "promise", "hints": ["It involves trust.", "You make one to someone.", "You break it by not keeping your word."]},
    {"question": "What has cities but no houses, forests but no trees, and water but no fish?", "answer": "map", "hints": ["It helps you find places.", "It can be folded or digital.", "It shows roads and locations."]},
]


def member_role_ids(member):
    return {role.id for role in getattr(member, "roles", [])}


def has_any_role(member, role_ids):
    return bool(member_role_ids(member) & role_ids)


def has_perm1(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_any_role(member, PERM1_ROLE_IDS)


def has_ban(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_any_role(member, BAN_ROLE_IDS) or member.id in BAN_USER_ALLOWLIST


def has_admin_actions(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_any_role(member, KICK_ROLE_IDS) or member.id in KICK_USER_ALLOWLIST


def has_owner_actions(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_any_role(member, BLACKLIST_ROLE_IDS) or member.id in BLACKLIST_USER_ALLOWLIST


def antiraid_enabled(guild_id):
    row = db.execute("SELECT antiraid FROM settings WHERE guild_id=?", (guild_id,)).fetchone()
    return True if row is None else bool(row[0])


def set_antiraid(guild_id, enabled):
    db.execute(
        "INSERT INTO settings(guild_id, antiraid) VALUES(?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET antiraid=excluded.antiraid",
        (guild_id, int(enabled)),
    )
    db.commit()


def configured_guild_channel_id(guild, mapping, fallback_id):
    # A configured channel must belong to this guild; never post into another server.
    return mapping.get(guild.id, fallback_id)


async def fetch_guild_text_channel(guild, channel_id):
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        channel = await guild.fetch_channel(channel_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None
    return channel if getattr(channel, "guild", None) and channel.guild.id == guild.id else None


async def send_mod_log(guild, text):
    """Send a staff/audit log to the configured logging channel.

    Failures are written to the host console so a missing channel or permission
    is diagnosable instead of silently hiding the problem.
    """
    if guild is None:
        return False
    channel_id = configured_guild_channel_id(
        guild, MOD_LOG_CHANNEL_IDS, DEFAULT_MOD_LOG_CHANNEL_ID
    )
    channel = await fetch_guild_text_channel(guild, channel_id)
    if channel is None or not hasattr(channel, "send"):
        logger.warning("Logging channel %s was not found, is not messageable, or is not in guild %s (%s)",
                       channel_id, guild.name, guild.id)
        return False
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.Forbidden:
        logger.error("Missing permission to send logs in #%s (%s), guild %s",
                     getattr(channel, "name", "unknown"), channel_id, guild.id)
    except discord.HTTPException as exc:
        logger.exception("Discord rejected a log message in channel %s: %s", channel_id, exc)
    return False


@bot.before_invoke
async def log_command_usage(ctx):
    """Log authorized command usage without deleting the command message."""
    if not ctx.guild:
        return
    command_text = ctx.message.content[:500]
    await send_mod_log(
        ctx.guild,
        "📜 **Command used**\n"
        f"Command: `{ctx.command.qualified_name if ctx.command else 'unknown'}`\n"
        f"User: {ctx.author} (`{ctx.author.id}`)\n"
        f"Channel: #{getattr(ctx.channel, 'name', 'unknown')} (`{ctx.channel.id}`)\n"
        f"Input: `{discord.utils.escape_markdown(command_text)}`"
    )


async def refresh_invites(guild):
    try:
        invite_cache[guild.id] = {inv.code: inv.uses for inv in await guild.invites()}
    except (discord.Forbidden, discord.HTTPException):
        invite_cache[guild.id] = {}


async def get_inviter_text(guild):
    try:
        before = invite_cache.get(guild.id, {})
        current = await guild.invites()
        used = next((inv for inv in current if (inv.uses or 0) > before.get(inv.code, 0)), None)
        invite_cache[guild.id] = {inv.code: inv.uses for inv in current}
        if used and used.inviter:
            return f"Invited by {used.inviter.mention} using `{used.code}`."
        return "Invite could not be determined."
    except (discord.Forbidden, discord.HTTPException):
        return "Invite could not be determined (the bot may need Manage Server to inspect invites)."


@bot.event
async def on_ready():
    # Staff strikes expire automatically 30 days after they were issued.
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    logger.info("Logged in as %s (ID %s) | Prefix: %s | Guilds: %s",
                bot.user, getattr(bot.user, "id", "unknown"), PREFIX, len(bot.guilds))
    if not intents.message_content:
        logger.error("Message Content Intent is disabled in code; prefix commands cannot work.")
    for guild in bot.guilds:
        await refresh_invites(guild)
        log_channel_id = configured_guild_channel_id(
            guild, MOD_LOG_CHANNEL_IDS, DEFAULT_MOD_LOG_CHANNEL_ID
        )
        log_channel = await fetch_guild_text_channel(guild, log_channel_id)
        if log_channel is None:
            logger.error("Logging channel %s is unavailable in guild %s (%s)",
                          log_channel_id, guild.name, guild.id)
        else:
            perms = log_channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages):
                logger.error("Bot cannot view/send messages in logging channel %s (%s)",
                             getattr(log_channel, "name", "unknown"), log_channel_id)
            else:
                logger.info("Logging channel ready: #%s (%s) in %s",
                            getattr(log_channel, "name", "unknown"), log_channel_id, guild.name)
    if not getattr(bot, "galaxy_game_task", None) or bot.galaxy_game_task.done():
        bot.galaxy_game_task = asyncio.create_task(galaxy_game_scheduler())
    if not getattr(bot, "galaxy_leaderboard_task", None) or bot.galaxy_leaderboard_task.done():
        bot.galaxy_leaderboard_task = asyncio.create_task(galaxy_leaderboard_scheduler())


@bot.event
async def on_guild_join(guild):
    await refresh_invites(guild)


@bot.event
async def on_invite_create(invite):
    await refresh_invites(invite.guild)


@bot.event
async def on_invite_delete(invite):
    await refresh_invites(invite.guild)


@bot.event
async def on_member_join(member):
    guild = member.guild

    # Invite tracking only: welcome messages have intentionally been removed.
    inviter_text = await get_inviter_text(guild)
    invite_channel_id = configured_guild_channel_id(
        guild, INVITES_CHANNEL_IDS, DEFAULT_INVITES_CHANNEL_ID
    )
    channel = await fetch_guild_text_channel(guild, invite_channel_id)
    if channel:
        try:
            await channel.send(f"📨 **Member joined:** {member.mention} (`{member.id}`). {inviter_text}")
        except (discord.Forbidden, discord.HTTPException):
            pass
    await send_mod_log(guild, f"📥 **Member joined**\nMember: {member} (`{member.id}`)\nAccount created: {discord.utils.format_dt(member.created_at, style='R')}\n{inviter_text}")

    now = time.monotonic()
    joins = recent_joins.setdefault(guild.id, deque())
    joins.append((now, member.id))
    while joins and now - joins[0][0] > JOIN_WINDOW_SECONDS:
        joins.popleft()

    if antiraid_enabled(guild.id) and len(joins) >= JOIN_BURST_LIMIT:
        alert = f"⚠️ **Possible join raid:** {len(joins)} members joined within {JOIN_WINDOW_SECONDS} seconds. Staff should review recent joins."
        if channel:
            try:
                await channel.send(alert)
            except (discord.Forbidden, discord.HTTPException):
                pass
        await send_mod_log(guild, alert)


@bot.event
async def on_member_remove(member):
    await send_mod_log(member.guild, f"📤 **Member left**\nMember: {member} (`{member.id}`)")


@bot.event
async def on_guild_channel_create(channel):
    await send_mod_log(channel.guild, f"➕ **Channel created**\nChannel: {channel.name} (`{channel.id}`)\nType: {type(channel).__name__}")


@bot.event
async def on_guild_channel_delete(channel):
    await send_mod_log(channel.guild, f"➖ **Channel deleted**\nChannel: {channel.name} (`{channel.id}`)\nType: {type(channel).__name__}")


@bot.event
async def on_guild_channel_update(before, after):
    changes = []
    if before.name != after.name:
        changes.append(f"Name: `{before.name}` → `{after.name}`")
    if getattr(before, "category_id", None) != getattr(after, "category_id", None):
        changes.append("Category changed")
    if getattr(before, "overwrites", None) != getattr(after, "overwrites", None):
        changes.append("Permission overwrites changed")
    if changes:
        await send_mod_log(after.guild, f"✏️ **Channel updated**\nChannel: {after.mention} (`{after.id}`)\n" + "\n".join(changes))


@bot.event
async def on_guild_role_create(role):
    await send_mod_log(role.guild, f"🎭 **Role created**\nRole: {role.name} (`{role.id}`)")


@bot.event
async def on_guild_role_delete(role):
    await send_mod_log(role.guild, f"🗑️ **Role deleted**\nRole: {role.name} (`{role.id}`)")


@bot.event
async def on_guild_role_update(before, after):
    changes = []
    if before.name != after.name:
        changes.append(f"Name: `{before.name}` → `{after.name}`")
    if before.permissions != after.permissions:
        changes.append("Permissions changed")
    if before.position != after.position:
        changes.append(f"Position: `{before.position}` → `{after.position}`")
    if before.color != after.color:
        changes.append("Color changed")
    if changes:
        await send_mod_log(after.guild, f"🎭 **Role updated**\nRole: {after.name} (`{after.id}`)\n" + "\n".join(changes))


@bot.event
async def on_member_update(before, after):
    # Log timeout changes made directly in Discord as well as through bot commands.
    if before.communication_disabled_until != after.communication_disabled_until:
        if after.communication_disabled_until:
            detail = f"Timeout applied/changed for {after.mention} until {after.communication_disabled_until}."
        else:
            detail = f"Timeout removed from {after.mention}."
        await send_mod_log(after.guild, f"📝 **Discord audit update:** {detail}")
    before_roles = {role.id for role in before.roles}
    after_roles = {role.id for role in after.roles}
    added = [role for role in after.roles if role.id not in before_roles and role.name != "@everyone"]
    removed = [role for role in before.roles if role.id not in after_roles and role.name != "@everyone"]
    if added or removed:
        detail = []
        if added:
            detail.append("Added: " + ", ".join(f"`{r.name}`" for r in added))
        if removed:
            detail.append("Removed: " + ", ".join(f"`{r.name}`" for r in removed))
        await send_mod_log(after.guild, f"🎭 **Member roles changed**\nMember: {after} (`{after.id}`)\n" + "\n".join(detail))


async def filter_bad_content(message):
    """Delete blocked content, warn the author, escalate repeat use, and log verbatim."""
    if not message.guild or message.author.bot or not (message.content or "").strip():
        return False

    member = message.guild.get_member(message.author.id)
    # Preserve the previous exemption for Perm 5+ staff.
    if member and has_staff_rank(member, 5):
        return False

    content = message.content
    lowered = content.lower()

    # Scam / Nitro bait filter restored from the previous bot.
    scam_match = next((word for word in SCAM_WORDS if word in lowered), None)
    if scam_match:
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not delete scam message %s", message.id)
        record_sanction(message.guild.id, message.author.id, bot.user.id if bot.user else 0,
                        "Scam Link", "Blocked scam/Nitro bait",
                        details=f"Matched: {scam_match} | Original message: {content}")
        try:
            await message.channel.send(
                f"{message.author.mention} scam or Nitro-bait content is not allowed here.",
                allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False),
            )
        except discord.HTTPException:
            pass
        await send_mod_log(
            message.guild,
            "🚨 **Scam / Link Filter**\n"
            f"User: {message.author} (`{message.author.id}`)\n"
            f"Channel: {message.channel.mention} (`{message.channel.id}`)\n"
            f"Matched text: {scam_match}\n"
            f"Original message (unredacted): {content[:1500]}"
        )
        return True

    matched = next((word for word in sorted(BLACKLISTED_WORDS, key=len, reverse=True) if word in lowered), None)
    if not matched:
        return False

    try:
        await message.delete()
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not delete blacklisted-word message %s", message.id)

    db.execute(
        "INSERT INTO blacklisted_word_strikes(guild_id,user_id,matched_word,message_content) VALUES(?,?,?,?)",
        (message.guild.id, message.author.id, matched, content),
    )
    db.commit()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WORD_STRIKE_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    count_row = db.execute(
        "SELECT COUNT(*) FROM blacklisted_word_strikes WHERE guild_id=? AND user_id=? AND created_at >= ?",
        (message.guild.id, message.author.id, cutoff),
    ).fetchone()
    strike_count = int(count_row[0] or 0)

    timeout_minutes = WORD_TIMEOUT_MINUTES.get(strike_count, 1440 if strike_count >= 8 else 0)
    timeout_applied = False
    if timeout_minutes and member is not None:
        try:
            await member.timeout(timedelta(minutes=timeout_minutes),
                                 reason=f"Blacklisted words: strike {strike_count} in {WORD_STRIKE_WINDOW_DAYS} days")
            timeout_applied = True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not timeout member %s for blacklisted words", message.author.id)

    if timeout_applied:
        action_text = f"Strike **{strike_count}**: timed out for **{timeout_minutes} minutes**."
    elif strike_count >= 4:
        action_text = f"Strike **{strike_count}**: timeout of {timeout_minutes} minutes could not be applied (check bot role and Timeout Members permission)."
    else:
        action_text = f"Warning **{strike_count}/3**. A timeout starts on the 4th violation."

    record_sanction(
        message.guild.id, message.author.id, bot.user.id if bot.user else 0,
        "Blacklisted Word", f"Matched: {matched}",
        details=f"Strike {strike_count}; timeout_minutes={timeout_minutes}; original_message={content}",
    )

    try:
        await message.channel.send(
            f"⚠️ {message.author.mention} you are using blacklisted words. Your message was deleted. {action_text}",
            allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False),
        )
    except discord.HTTPException:
        pass

    await send_mod_log(
        message.guild,
        "🚫 **Blacklisted Word Detected**\n"
        f"User: {message.author} (`{message.author.id}`)\n"
        f"Channel: {message.channel.mention} (`{message.channel.id}`)\n"
        f"Matched word: {matched}\n"
        f"Violation count (rolling {WORD_STRIKE_WINDOW_DAYS} days): **{strike_count}**\n"
        f"Action: {action_text}\n"
        f"Original message (unredacted): {content[:1500]}"
    )
    return True


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    if await filter_bad_content(message):
        return
    if bot.user and message.content.strip() in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        mention_embed = discord.Embed(
            title="✦ GALAXY SYSTEMS",
            description=f"My command prefix is `{PREFIX}`. Type `{PREFIX}help` to view the command panels.",
            color=discord.Color.from_rgb(111, 66, 193),
        )
        mention_embed.set_footer(text="✧ The Multiverse • Galaxy Bot")
        await message.channel.send(embed=mention_embed, allowed_mentions=discord.AllowedMentions.none())
        return
    # Accept trivia answers only in the configured game channel.
    active = active_galaxy_questions.get(message.guild.id)
    if message.channel.id == GALAXY_GAME_CHANNEL_ID and active and not message.content.startswith(PREFIX):
        cleaned = re.sub(r"[^a-z0-9 ]", "", message.content.lower()).strip()
        if cleaned == active["answer"]:
            reward = GALAXY_QUESTION_REWARD + active["hints_used"] * 25
            change_coins(message.guild.id, message.author.id, reward)
            active_galaxy_questions.pop(message.guild.id, None)
            await message.channel.send(f"🌌 Correct, {message.author.mention}! You earned **{reward} Galaxy Coins**. Your balance is **{get_coin_balance(message.guild.id, message.author.id):,}**. A new question will arrive in about an hour.")
            return
    # Replying to a member's message supplies the first member/user argument
    # for target-based commands, so mentions are optional when replying.
    if message.content.startswith(PREFIX) and message.reference:
        try:
            replied = message.reference.resolved
            if replied is None and message.reference.message_id:
                replied = await message.channel.fetch_message(message.reference.message_id)
            if replied and getattr(replied, "author", None) and not replied.author.bot:
                ctx_for_reply = await bot.get_context(message)
                command = ctx_for_reply.command
                if command is not None:
                    import inspect
                    params = list(inspect.signature(command.callback).parameters.values())[1:]
                    target_index = next(
                        (i for i, p in enumerate(params)
                         if p.name in {"member", "user", "target", "user_id"}),
                        None,
                    )
                    if target_index is not None:
                        rest = message.content[len(PREFIX):]
                        command_name, _, tail = rest.partition(" ")
                        tokens = tail.split()
                        # Skip required arguments before the target (e.g. "add" in +role add).
                        has_explicit_target = bool(
                            re.search(r"<@!?\d+>", tail) or
                            re.search(r"(?<!\d)\d{15,22}(?!\d)", tail)
                        )
                        if not has_explicit_target:
                            target_value = str(replied.author.id) if params[target_index].name == "user_id" else f"<@{replied.author.id}>"
                            insert_at = min(target_index, len(tokens))
                            tokens.insert(insert_at, target_value)
                            rebuilt_tail = " ".join(tokens)
                            message.content = f"{PREFIX}{command_name}" + (f" {rebuilt_tail}" if rebuilt_tail else "")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    row = db.execute(
        "SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?",
        (message.guild.id, message.author.id),
    ).fetchone()
    if row and not message.content.startswith(PREFIX + "unbl"):
        return
    await bot.process_commands(message)



async def galaxy_game_scheduler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            channel = guild.get_channel(GALAXY_GAME_CHANNEL_ID)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(GALAXY_GAME_CHANNEL_ID)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None
            if channel is None or not isinstance(channel, discord.TextChannel):
                continue
            question = random.choice(GALAXY_QUESTIONS)
            active_galaxy_questions[guild.id] = {**question, "hints_used": 0}
            try:
                await channel.send(
                    "🌌 **GALAXY RIDDLE!**\n"
                    f"{question['question']}\n\n"
                    f"First correct answer wins **{GALAXY_QUESTION_REWARD} Galaxy Coins**! "
                    "I'll reveal up to **3 hints** if nobody gets it."
                )
                for hint_number, hint in enumerate(question["hints"], start=1):
                    await asyncio.sleep(GALAXY_GAME_HINT_SECONDS)
                    current = active_galaxy_questions.get(guild.id)
                    if current is None:
                        break  # Someone already solved this round.
                    current["hints_used"] = hint_number
                    await channel.send(f"💡 **Hint {hint_number}/3:** {hint}")
                await asyncio.sleep(GALAXY_GAME_HINT_SECONDS)
                if active_galaxy_questions.get(guild.id) is not None:
                    active_galaxy_questions.pop(guild.id, None)
                    await channel.send(f"⌛ Time's up! The answer was **{question['answer']}**. Next riddle in about an hour!")
            except (discord.Forbidden, discord.HTTPException):
                active_galaxy_questions.pop(guild.id, None)
        await asyncio.sleep(GALAXY_GAME_INTERVAL_SECONDS)

def build_coin_leaderboard(guild):
    rows = db.execute(
        "SELECT user_id, balance FROM galaxy_coins "
        "WHERE guild_id=? AND balance > 0 ORDER BY balance DESC, user_id ASC LIMIT 10",
        (guild.id,),
    ).fetchall()
    embed = discord.Embed(title="🏆 Galaxy Coins Leaderboard",
                          color=discord.Color.from_rgb(111, 66, 193))
    if not rows:
        embed.description = "No one has earned Galaxy Coins yet. Be the first!"
    else:
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for index, (user_id, balance) in enumerate(rows, start=1):
            member = guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"
            rank = medals[index - 1] if index <= 3 else f"`#{index}`"
            lines.append(f"{rank} **{discord.utils.escape_markdown(name)}** — **{balance:,}** coins")
        embed.description = "\\n".join(lines)
    embed.set_footer(text="✧ Top 10 Galaxy Coin balances • Updates every 3 hours")
    return embed


async def galaxy_leaderboard_scheduler():
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            channel = await fetch_guild_text_channel(guild, GALAXY_LEADERBOARD_CHANNEL_ID)
            if channel is None or not isinstance(channel, discord.TextChannel):
                continue
            try:
                await channel.send(embed=build_coin_leaderboard(guild),
                                   allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                pass
        await asyncio.sleep(GALAXY_LEADERBOARD_INTERVAL_SECONDS)


@bot.command(name="balance", aliases=["bal", "coins"])
async def galaxy_balance(ctx, member: discord.Member = None):
    member = member or ctx.author
    balance = get_coin_balance(ctx.guild.id, member.id)
    await ctx.send(f"🌌 **{member.display_name}** has **{balance:,} Galaxy Coins**.")

@bot.command(name="givecoins", aliases=["paycoins", "tipcoins"])
async def galaxy_give_coins(ctx, member: discord.Member, amount: int):
    """Any member can transfer coins from their own balance."""
    if member.bot:
        return await ctx.send("You can't send Galaxy Coins to bots.")
    if member.id == ctx.author.id:
        return await ctx.send("You can't send Galaxy Coins to yourself.")
    if amount < 1:
        return await ctx.send("Choose an amount of at least 1 Galaxy Coin.")
    balance = get_coin_balance(ctx.guild.id, ctx.author.id)
    if amount > balance:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")
    change_coins(ctx.guild.id, ctx.author.id, -amount)
    change_coins(ctx.guild.id, member.id, amount)
    await ctx.send(f"💸 {ctx.author.mention} sent **{amount:,} Galaxy Coins** to {member.mention}. "
                   f"Your balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**.")
    await send_mod_log(ctx.guild, f"💸 **Galaxy Coins transfer**\nFrom: {ctx.author} (`{ctx.author.id}`)\n"
                                  f"To: {member} (`{member.id}`)\nAmount: **{amount:,}**")


@bot.command(name="addcoins", aliases=["coinadd"])

def require_perm1():
    async def predicate(ctx):
        return has_perm1(ctx.author)
    return commands.check(predicate)

@require_perm1()
async def galaxy_add_coins(ctx, member: discord.Member, amount: int, *, reason="Staff adjustment"):
    if amount < 1:
        return await ctx.send("Amount must be at least 1.")
    change_coins(ctx.guild.id, member.id, amount)
    await ctx.send(f"➕ Added **{amount:,} Galaxy Coins** to {member.mention}. "
                   f"New balance: **{get_coin_balance(ctx.guild.id, member.id):,}**.")
    await send_mod_log(ctx.guild, f"➕ **Galaxy Coins added**\nUser: {member} (`{member.id}`)\n"
                                  f"Amount: **{amount:,}**\nBy: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="removecoins", aliases=["coinremove"])
@require_perm1()
async def galaxy_remove_coins(ctx, member: discord.Member, amount: int, *, reason="Staff adjustment"):
    if amount < 1:
        return await ctx.send("Amount must be at least 1.")
    current = get_coin_balance(ctx.guild.id, member.id)
    removed = min(amount, current)
    change_coins(ctx.guild.id, member.id, -removed)
    await ctx.send(f"➖ Removed **{removed:,} Galaxy Coins** from {member.mention}. "
                   f"New balance: **{get_coin_balance(ctx.guild.id, member.id):,}**.")
    await send_mod_log(ctx.guild, f"➖ **Galaxy Coins removed**\nUser: {member} (`{member.id}`)\n"
                                  f"Amount: **{removed:,}**\nBy: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="setcoins", aliases=["coinset"])
@require_perm1()
async def galaxy_set_coins(ctx, member: discord.Member, amount: int, *, reason="Staff adjustment"):
    if amount < 0:
        return await ctx.send("A balance cannot be negative.")
    previous = get_coin_balance(ctx.guild.id, member.id)
    set_coin_balance(ctx.guild.id, member.id, amount)
    await ctx.send(f"⚙️ Set {member.mention}'s balance from **{previous:,}** to **{amount:,}**.")
    await send_mod_log(ctx.guild, f"⚙️ **Galaxy Coins balance set**\nUser: {member} (`{member.id}`)\n"
                                  f"Previous: **{previous:,}**\nNew: **{amount:,}**\n"
                                  f"By: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="leaderboard", aliases=["topcoins", "richest"])
async def galaxy_coin_leaderboard(ctx):
    await ctx.send(embed=build_coin_leaderboard(ctx.guild))


@bot.command(name="daily")
async def galaxy_daily(ctx):
    row = db.execute("SELECT last_daily FROM galaxy_coins WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.author.id)).fetchone()
    if row and row[0]:
        from datetime import datetime, timezone
        try:
            last = datetime.fromisoformat(row[0]).replace(tzinfo=timezone.utc)
            remaining = timedelta(hours=24) - (datetime.now(timezone.utc) - last)
            if remaining.total_seconds() > 0:
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                return await ctx.send(f"⏳ Your daily reward is ready in **{hours}h {minutes}m**.")
        except ValueError:
            pass
    reward = 250
    db.execute("INSERT INTO galaxy_coins(guild_id,user_id,balance,last_daily) VALUES(?,?,?,CURRENT_TIMESTAMP) "
               "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=balance+excluded.balance,last_daily=CURRENT_TIMESTAMP",
               (ctx.guild.id, ctx.author.id, reward))
    db.commit()
    await ctx.send(f"🎁 You collected **{reward} Galaxy Coins**! Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**.")

@bot.command(name="gamble", aliases=["coinflip", "cf"])
async def galaxy_gamble(ctx, amount: int):
    if amount < 1:
        return await ctx.send("Bet at least 1 Galaxy Coin.")
    balance = get_coin_balance(ctx.guild.id, ctx.author.id)
    if amount > balance:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")
    if random.random() < 0.5:
        change_coins(ctx.guild.id, ctx.author.id, amount)
        await ctx.send(f"🪙 **You won!** Your bet doubled in profit: **+{amount:,} Galaxy Coins**. Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**.")
    else:
        change_coins(ctx.guild.id, ctx.author.id, -amount)
        await ctx.send(f"🪙 **You lost!** You lost **{amount:,} Galaxy Coins**. Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**.")

@bot.command(name="slots")
async def galaxy_slots(ctx, amount: int):
    if amount < 1:
        return await ctx.send("Bet at least 1 Galaxy Coin.")
    balance = get_coin_balance(ctx.guild.id, ctx.author.id)
    if amount > balance:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")
    symbols = ["🌌", "⭐", "🪐", "💎", "☄️"]
    result = [random.choice(symbols) for _ in range(3)]
    if result[0] == result[1] == result[2]:
        winnings = amount * 5
        change_coins(ctx.guild.id, ctx.author.id, winnings)
        outcome = f"JACKPOT! **+{winnings:,}** coins"
    elif len(set(result)) == 2:
        winnings = amount
        change_coins(ctx.guild.id, ctx.author.id, winnings)
        outcome = f"Two match! **+{winnings:,}** coins"
    else:
        change_coins(ctx.guild.id, ctx.author.id, -amount)
        outcome = f"No match. **-{amount:,}** coins"
    await ctx.send(f"🎰 {' | '.join(result)}\n{outcome}\nBalance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**")


def require_perm1():
    async def predicate(ctx):
        return has_perm1(ctx.author)
    return commands.check(predicate)


def require_ban():
    async def predicate(ctx):
        return has_ban(ctx.author)
    return commands.check(predicate)


def require_admin_actions():
    async def predicate(ctx):
        return has_admin_actions(ctx.author)
    return commands.check(predicate)


def require_owner_actions():
    async def predicate(ctx):
        return has_owner_actions(ctx.author)
    return commands.check(predicate)


def require_staff_strike_access():
    async def predicate(ctx):
        # Must hold an eligible staff role; Discord Administrator alone is not enough.
        return has_any_role(ctx.author, STAFF_STRIKE_ROLE_IDS)
    return commands.check(predicate)


def can_moderate_target(ctx, member):
    if member.id == ctx.author.id:
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True
    actor_rank = max((ROLE_RANK.get(r.id, 0) for r in getattr(ctx.author, "roles", [])), default=0)
    target_rank = max((ROLE_RANK.get(r.id, 0) for r in getattr(member, "roles", [])), default=0)
    if target_rank:
        return actor_rank > target_rank
    # Non-staff targets are governed by Discord's native role order.
    return member.top_role < ctx.author.top_role


@bot.command()
@require_perm1()
async def perms(ctx):
    labels = {
        1: "Trial Moderator",
        2: "Moderator",
        3: "Senior Moderator, Head Moderator",
        4: "Admin, Senior Admin, Junior Admin",
        5: "Head Admin, Assistant Staff Manager, Staff Manager, Manager, Assistant Head Manager, Head Manager, Trial Director, Senior DRC, DRC, Axelle, Dev",
        6: "Founder, Co-Owner",
    }
    embed = discord.Embed(title="✦ GALAXY PERMISSIONS", color=discord.Color.from_rgb(111, 66, 193))
    for level in range(1, 7):
        role_mentions = [ctx.guild.get_role(rid).mention for rid in sorted(PERM_ROLE_IDS[level]) if ctx.guild.get_role(rid)]
        embed.add_field(name=f"Perm {level}", value="\n".join(role_mentions) or labels[level], inline=False)
    embed.add_field(
        name="Command access",
        value=(
            "All normal staff commands: Perm 1+ (higher perms inherit lower perms).\n"
            "+sanctions: Perm 1+ (Trial Moderator+).\n"
            "+del sanction <id>: Perm 3+ (Senior Moderator+).\n"
            "+kick: Perm 3+.  +ban: Perm 4+.  +bl: Perm 6+."
        ), inline=False
    )
    embed.set_footer(text="Higher-ranked staff can moderate lower-ranked staff only. Equal or higher targets are protected.")
    await ctx.send(embed=embed)


@bot.command()
@require_perm1()
async def antiraid(ctx, action: str = "status"):
    action = action.lower()
    if action == "on":
        set_antiraid(ctx.guild.id, True)
        await ctx.send("✅ Anti-raid join monitoring enabled.")
        await send_mod_log(ctx.guild, f"🛡️ Anti-raid monitoring enabled by {ctx.author.mention}.")
    elif action == "off":
        set_antiraid(ctx.guild.id, False)
        await ctx.send("Anti-raid join monitoring disabled.")
        await send_mod_log(ctx.guild, f"🛡️ Anti-raid monitoring disabled by {ctx.author.mention}.")
    elif action == "status":
        await ctx.send(f"Anti-raid join monitoring: **{'ON' if antiraid_enabled(ctx.guild.id) else 'OFF'}**")
    else:
        await ctx.send("Usage: `+antiraid on`, `+antiraid off`, or `+antiraid status`.")


@bot.command()
@require_perm1()
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return
    db.execute(
        "INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, member.id, ctx.author.id, reason),
    )
    db.commit()
    warning_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Warn", reason,
                                  source_warning_id=warning_id)
    await ctx.send(f"⚠️ Warned {member.mention}. Reason: {reason} (Sanction #{sanction_id})")
    await send_mod_log(ctx.guild, f"⚠️ **Warning**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_perm1()
async def warnings(ctx, member: discord.Member):
    rows = db.execute(
        "SELECT id, reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
        (ctx.guild.id, member.id),
    ).fetchall()
    if not rows:
        return await ctx.send(f"{member} has no recorded warnings.")
    lines = [f"`#{row[0]}` {row[1]} — {row[2]}" for row in rows]
    await ctx.send(f"Warnings for {member.mention}:\n" + "\n".join(lines))


@bot.command()
@require_perm1()
async def clearwarns(ctx, member: discord.Member):
    db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    db.commit()
    await ctx.send(f"Cleared warnings for {member.mention}.")
    await send_mod_log(ctx.guild, f"🧹 **Warnings cleared**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="strike")
@require_perm1()
async def staff_strike(ctx, member: discord.Member, *, reason="No reason provided"):
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    if not has_any_role(member, set(ROLE_IDS.values())):
        return await ctx.send("Staff strikes can only be issued to members who hold a configured staff role.")
    if not can_moderate_target(ctx, member):
        return
    cursor = db.execute(
        "INSERT INTO staff_strikes(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, member.id, ctx.author.id, reason),
    )
    db.commit()
    strike_id = cursor.lastrowid
    await ctx.send(f"⚠️ Staff strike **#{strike_id}** issued to {member.mention}. Reason: {reason}")
    await send_mod_log(
        ctx.guild,
        f"⚠️ **Staff strike issued**\nStrike: `#{strike_id}`\nStaff member: {member} (`{member.id}`)\nIssued by: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}",
    )


@bot.command(name="strikes", aliases=["staffstrikes"])
@require_perm1()
async def staff_strikes(ctx, member: discord.Member):
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    if not has_any_role(member, set(ROLE_IDS.values())):
        return await ctx.send("That member does not hold a configured staff role.")
    rows = db.execute(
        "SELECT id, reason, created_at, moderator_id FROM staff_strikes "
        "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
        (ctx.guild.id, member.id),
    ).fetchall()
    if not rows:
        return await ctx.send(f"**{member}** has no recorded staff strikes.")
    lines = [f"`#{row[0]}` {row[1]} — {row[2]} (by <@{row[3]}>)" for row in rows]
    await ctx.send(f"**Staff strikes for {member.mention}:**\n" + "\n".join(lines))


@bot.command(name="removestrike", aliases=["unstrike"])
@require_perm1()
async def remove_staff_strike(ctx, strike_id: int, *, reason="No reason provided"):
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    row = db.execute(
        "SELECT user_id, reason FROM staff_strikes WHERE guild_id=? AND id=?",
        (ctx.guild.id, strike_id),
    ).fetchone()
    if row is None:
        return await ctx.send("I couldn't find that staff strike ID.")
    db.execute("DELETE FROM staff_strikes WHERE guild_id=? AND id=?", (ctx.guild.id, strike_id))
    db.commit()
    await ctx.send(f"✅ Removed staff strike `#{strike_id}` from user `{row[0]}`.")
    await send_mod_log(
        ctx.guild,
        f"🧹 **Staff strike removed**\nStrike: `#{strike_id}`\nStaff member ID: `{row[0]}`\nOriginal reason: {row[1]}\nRemoved by: {ctx.author} (`{ctx.author.id}`)\nRemoval reason: {reason}",
    )


@bot.command(name="tempmute", aliases=["timeout"])
@require_perm1()
async def tempmute(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    if minutes < 1 or minutes > 40320:
        return await ctx.send("Choose a timeout from 1 to 40,320 minutes.")
    if not can_moderate_target(ctx, member):
        return
    await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Timeout", reason,
                                  details=f"Duration: {minutes} minutes")
    await ctx.send(f"⏳ Timed out {member.mention} for {minutes} minutes. (Sanction #{sanction_id})")
    await send_mod_log(ctx.guild, f"⏳ **Timeout**\nUser: {member} (`{member.id}`)\nDuration: {minutes} minutes\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_perm1()
async def unmute(ctx, member: discord.Member):
    if not can_moderate_target(ctx, member):
        return
    await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
    await ctx.send(f"Removed timeout for {member.mention}.")
    await send_mod_log(ctx.guild, f"🔊 **Timeout removed**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_ban()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return
    await member.ban(reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Ban", reason)
    await ctx.send(f"🔨 Banned {member}. Reason: {reason} (Sanction #{sanction_id})")
    await send_mod_log(ctx.guild, f"🔨 **Ban**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_ban()
async def unban(ctx, user_id: int, *, reason="No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.send("That user is not banned or the ID is incorrect.")
    await ctx.send(f"Unbanned **{user}**.")
    await send_mod_log(ctx.guild, f"♻️ **Unban**\nUser: {user} (`{user.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_admin_actions()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return
    await member.kick(reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Kick", reason)
    await ctx.send(f"👢 Kicked {member}. Reason: {reason} (Sanction #{sanction_id})")
    await send_mod_log(ctx.guild, f"👢 **Kick**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="bl", aliases=["blacklist"])
@require_owner_actions()
async def blacklist_user(ctx, user: discord.User, *, reason="No reason provided"):
    db.execute(
        "INSERT OR REPLACE INTO blacklist(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, user.id, ctx.author.id, reason),
    )
    db.commit()
    sanction_id = record_sanction(ctx.guild.id, user.id, ctx.author.id, "Blacklist", reason)
    await ctx.send(f"⛔ Added **{user}** to the bot blacklist. (Sanction #{sanction_id})")
    await send_mod_log(ctx.guild, f"⛔ **Bot blacklist add**\nUser: {user} (`{user.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="unbl", aliases=["unblacklist"])
@require_owner_actions()
async def unblacklist_user(ctx, user_id: int):
    cursor = db.execute(
        "DELETE FROM blacklist WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, user_id),
    )
    db.commit()
    if cursor.rowcount == 0:
        return await ctx.send("That user is not on the bot blacklist.")
    await ctx.send(f"✅ Removed user `{user_id}` from the bot blacklist.")
    await send_mod_log(ctx.guild, f"✅ **Bot blacklist removal**\nUser ID: `{user_id}`\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="sanctions", aliases=["cases", "history"])
@require_staff_rank(1)  # Trial Moderator+
async def sanctions(ctx, user: discord.User):
    rows = db.execute(
        """SELECT id, sanction_type, reason, details, moderator_id, created_at
           FROM sanctions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 25""",
        (ctx.guild.id, user.id),
    ).fetchall()
    if not rows:
        return await ctx.send(f"📋 No recorded sanctions for **{user}** (`{user.id}`).")
    lines = []
    for case_id, kind, reason, details, moderator_id, created_at in rows:
        detail_text = f" • {details}" if details else ""
        lines.append(f"`#{case_id}` **{kind}** — {reason}{detail_text}\\n  By <@{moderator_id}> • {created_at}")
    embed = discord.Embed(title=f"📋 Sanctions — {user}", description="\\n".join(lines),
                          color=discord.Color.from_rgb(111, 66, 193))
    embed.set_footer(text=f"User ID: {user.id} • Showing up to 25 records")
    await ctx.send(embed=embed)


@bot.command(name="del", aliases=["delsanction", "deletesanction"])
@require_staff_rank(3)  # Senior Moderator+
async def delete_sanction(ctx, subcommand: str, sanction_id: int, *, reason="No reason provided"):
    if subcommand.lower() != "sanction":
        return await ctx.send(f"Usage: `{PREFIX}del sanction <sanction_id> [reason]`.")
    row = db.execute(
        """SELECT user_id, sanction_type, reason, source_warning_id
           FROM sanctions WHERE guild_id=? AND id=?""",
        (ctx.guild.id, sanction_id),
    ).fetchone()
    if row is None:
        return await ctx.send("I couldn't find that sanction ID.")
    user_id, sanction_type, original_reason, warning_id = row
    db.execute("DELETE FROM sanctions WHERE guild_id=? AND id=?", (ctx.guild.id, sanction_id))
    if sanction_type.lower() == "warn" and warning_id is not None:
        db.execute("DELETE FROM warnings WHERE guild_id=? AND id=?", (ctx.guild.id, warning_id))
    db.commit()
    await ctx.send(f"✅ Deleted sanction `#{sanction_id}` ({sanction_type}) for user `{user_id}`.")
    await send_mod_log(
        ctx.guild,
        f"🧹 **Sanction deleted**\\nSanction: `#{sanction_id}` ({sanction_type})"
        f"\\nUser ID: `{user_id}`\\nOriginal reason: {original_reason}"
        f"\\nDeleted by: {ctx.author} (`{ctx.author.id}`)\\nDeletion reason: {reason}",
    )


@bot.command(name="purge", aliases=["clear"])
@require_staff_rank(3)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send("Choose a number from 1 to 100.")

    deleted = await ctx.channel.purge(limit=amount)
    # Keep the channel clean: do not send a public success message.
    # The action and exact deletion count are recorded in the configured mod-log channel.
    await send_mod_log(
        ctx.guild,
        "🧹 **Messages cleared**\n"
        f"Channel: {ctx.channel.mention} (`{ctx.channel.id}`)\n"
        f"Messages deleted: **{len(deleted)}**\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)"
    )


@bot.command()
@require_perm1()
async def lock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {ctx.author}")
    await ctx.send("🔒 This channel is now locked.")
    await send_mod_log(ctx.guild, f"🔒 **Channel locked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def unlock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {ctx.author}")
    await ctx.send("🔓 This channel is now unlocked.")
    await send_mod_log(ctx.guild, f"🔓 **Channel unlocked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def slowmode(ctx, seconds: int):
    if not 0 <= seconds <= 21600:
        return await ctx.send("Slowmode must be between 0 and 21,600 seconds.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {ctx.author}")
    await ctx.send(f"⏱️ Slowmode set to **{seconds} seconds**.")
    await send_mod_log(ctx.guild, f"⏱️ **Slowmode changed**\nChannel: {ctx.channel.mention}\nSeconds: {seconds}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def nick(ctx, member: discord.Member, *, nickname: str = None):
    if not can_moderate_target(ctx, member):
        return
    await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
    await ctx.send(f"✏️ Updated {member.mention}'s nickname.")
    await send_mod_log(ctx.guild, f"✏️ **Nickname changed**\nMember: {member} (`{member.id}`)\nNew nickname: {nickname or 'Reset to username'}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="role")
@require_perm1()
async def role_command(ctx, action: str, member: discord.Member, role: discord.Role):
    action = action.lower()
    if action not in ("add", "remove"):
        return await ctx.send(f"Usage: `{PREFIX}role add @member @role` or `{PREFIX}role remove @member @role`.")
    if role.is_default() or role.managed or role >= ctx.author.top_role or role >= ctx.guild.me.top_role:
        return await ctx.send("I can't edit that role because it is managed or not below your and the bot's top roles.")
    if not can_moderate_target(ctx, member):
        return
    if action == "add":
        await member.add_roles(role, reason=f"Role added by {ctx.author}")
        result = "added"
    else:
        await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
        result = "removed"
    await ctx.send(f"✅ {role.mention} {result} {member.mention}.")
    await send_mod_log(ctx.guild, f"🎭 **Role {result}**\nRole: {role.name} (`{role.id}`)\nMember: {member} (`{member.id}`)\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="unwarn")
@require_perm1()
async def unwarn(ctx, warning_id: int):
    row = db.execute("SELECT user_id, reason FROM warnings WHERE guild_id=? AND id=?", (ctx.guild.id, warning_id)).fetchone()
    if row is None:
        return await ctx.send("I couldn't find that warning ID.")
    db.execute("DELETE FROM warnings WHERE guild_id=? AND id=?", (ctx.guild.id, warning_id))
    db.execute(
        "DELETE FROM sanctions WHERE guild_id=? AND source_warning_id=? AND sanction_type='Warn'",
        (ctx.guild.id, warning_id),
    )
    db.commit()
    await ctx.send(f"✅ Removed warning `#{warning_id}` from user `{row[0]}` and its sanctions history.")
    await send_mod_log(ctx.guild, f"🧹 **Warning removed**\nWarning: `#{warning_id}`\nUser ID: `{row[0]}`\nOriginal reason: {row[1]}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"User info — {member}", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="User ID", value=str(member.id), inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(member.created_at, style="R"), inline=True)
    embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Unknown", inline=True)
    embed.add_field(name="Top role", value=member.top_role.mention, inline=True)
    await ctx.send(embed=embed)


@bot.command()
@require_perm1()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(title=f"Server info — {guild.name}", color=discord.Color.blurple())
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Server ID", value=str(guild.id), inline=True)
    embed.add_field(name="Members", value=str(guild.member_count), inline=True)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="R"), inline=True)
    embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
    embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def avatar(ctx, member: discord.Member = None):
    member = member or ctx.author
    embed = discord.Embed(title=f"{member.display_name}'s avatar", color=discord.Color.blurple())
    embed.set_image(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command()
@require_perm1()
async def membercount(ctx):
    await ctx.send(f"👥 **{ctx.guild.name}** has **{ctx.guild.member_count:,}** members.")


@bot.command()
@require_perm1()
async def roleinfo(ctx, role: discord.Role):
    embed = discord.Embed(title=f"Role info — {role.name}", color=role.color if role.color.value else discord.Color.blurple())
    embed.add_field(name="Role ID", value=str(role.id), inline=True)
    embed.add_field(name="Members", value=str(len(role.members)), inline=True)
    embed.add_field(name="Position", value=str(role.position), inline=True)
    embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, style="R"), inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def ping(ctx):
    await ctx.send(f"Pong! `{round(bot.latency * 1000)} ms`")


@bot.command()
async def help(ctx):
    """Compact command directory, displayed as one Galaxy panel."""
    embed = discord.Embed(
        title="✦ THE MULTIVERSE • COMMANDS",
        description=f"Use `{PREFIX}` before each command. Reply to a member's message to use target-based commands without typing their mention.",
        color=discord.Color.from_rgb(111, 66, 193),
    )
    embed.add_field(
        name="✧ General",
        value=(
            f"`{PREFIX}help`  `{PREFIX}ping`  `{PREFIX}perms`\n"
            f"`{PREFIX}userinfo [member]`  `{PREFIX}avatar [member]`\n"
            f"`{PREFIX}membercount`  `{PREFIX}serverinfo`  `{PREFIX}roleinfo`"
        ),
        inline=False,
    )
    embed.add_field(
        name="✧ Moderation",
        value=(
            f"`{PREFIX}warn`  `{PREFIX}warnings`  `{PREFIX}clearwarns`  `{PREFIX}unwarn` (Senior Moderator+)\n"
            f"`{PREFIX}sanctions @user` (Trial Moderator+)  `{PREFIX}del sanction <id>` (Senior Moderator+)\n"
            f"`{PREFIX}tempmute` / `{PREFIX}timeout`  `{PREFIX}unmute`\n"
            f"`{PREFIX}ban`  `{PREFIX}unban`  `{PREFIX}kick`  `{PREFIX}purge` / `{PREFIX}clear`"
        ),
        inline=False,
    )
    embed.add_field(
        name="✧ Staff tools",
        value=(
            f"`{PREFIX}lock`  `{PREFIX}unlock`  `{PREFIX}slowmode`\n"
            f"`{PREFIX}nick`  `{PREFIX}role add/remove`  `{PREFIX}antiraid on/off/status`\n"
            f"`{PREFIX}bl` / `{PREFIX}blacklist`  `{PREFIX}unbl` / `{PREFIX}unblacklist`"
        ),
        inline=False,
    )
    embed.add_field(
        name="✧ Staff strikes",
        value=(
            f"`{PREFIX}strike`  `{PREFIX}strikes`  `{PREFIX}removestrike`\n"
            "Available to Perm 5+ roles. Strikes expire after 30 days."
        ),
        inline=False,
    )
    embed.add_field(
        name="✧ Galaxy Coins",
        value=(
            f"`{PREFIX}balance` / `{PREFIX}bal`  `{PREFIX}daily`  `{PREFIX}givecoins`\n"
            f"`{PREFIX}gamble` / `{PREFIX}coinflip`  `{PREFIX}slots`  `{PREFIX}leaderboard`\n"
            f"`{PREFIX}addcoins`  `{PREFIX}removecoins`  `{PREFIX}setcoins` (Senior Moderator+)\n"
            "Anyone can transfer their own coins to another member. Senior Moderator+ can "
            "adjust balances. The top 10 leaderboard posts every 3 hours."
        ),
        inline=False,
    )
    embed.set_footer(text="✧ The Multiverse • Galaxy Bot")
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    # Unwrap command callback errors so the host console shows the real cause.
    original = getattr(error, "original", error)
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing an argument. Try `{PREFIX}help`.")
        if ctx.guild:
            await send_mod_log(ctx.guild, f"⚠️ **Command argument missing**\nCommand: `{ctx.command}`\nUser: {ctx.author} (`{ctx.author.id}`)\nChannel: {ctx.channel.mention}")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"I couldn't understand that member or number. Try `{PREFIX}help`.")
        if ctx.guild:
            await send_mod_log(ctx.guild, f"⚠️ **Invalid command argument**\nCommand: `{ctx.command}`\nUser: {ctx.author} (`{ctx.author.id}`)\nChannel: {ctx.channel.mention}")
    elif isinstance(error, commands.CheckFailure):
        # Unauthorized command attempts are intentionally silent.
        return
    elif isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(original, discord.Forbidden):
        logger.warning("Discord permission failure for %s: %s", ctx.command, original)
        await ctx.send("I don't have the Discord permissions needed for that action.")
        if ctx.guild:
            await send_mod_log(ctx.guild, f"🚫 **Command blocked by Discord permissions**\nCommand: `{ctx.command}`\nUser: {ctx.author} (`{ctx.author.id}`)\nDetails: `{discord.utils.escape_markdown(str(original)[:500])}`")
    elif isinstance(original, discord.HTTPException):
        logger.error("Discord HTTP error while running %s: %s", ctx.command, original,
                     exc_info=(type(original), original, original.__traceback__) if original.__traceback__ else None)
        await ctx.send("Discord could not complete that action. Check the bot's permissions and role position.")
        if ctx.guild:
            await send_mod_log(ctx.guild, f"⚠️ **Discord API error**\nCommand: `{ctx.command}`\nUser: {ctx.author} (`{ctx.author.id}`)\nDetails: `{discord.utils.escape_markdown(str(original)[:500])}`")
    else:
        logger.error("Unexpected command error in %s: %s", ctx.command, original,
                     exc_info=(type(original), original, original.__traceback__) if original.__traceback__ else None)
        await ctx.send("Something went wrong. Check the bot console and permissions.")
        if ctx.guild:
            await send_mod_log(ctx.guild, f"❗ **Unexpected bot error**\nCommand: `{ctx.command}`\nUser: {ctx.author} (`{ctx.author.id}`)\nError: `{discord.utils.escape_markdown(str(original)[:700])}`")


if not TOKEN:
    raise SystemExit("Galaxy Bot cannot start: DISCORD_TOKEN is missing. Add DISCORD_TOKEN=your_bot_token to the host environment or .env file, then restart.")

bot.run(TOKEN)
