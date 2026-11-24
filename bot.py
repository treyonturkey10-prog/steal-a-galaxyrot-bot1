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
# Staff-strike logs use the permanent channel unless test mode is enabled.
STRIKE_LOG_CHANNEL_ID = 1549959193614753852
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
    1: {ROLE_IDS["trial_moderator"], ROLE_IDS["moderator"]},
    2: {ROLE_IDS["senior_moderator"], ROLE_IDS["head_moderator"]},
    3: {ROLE_IDS["admin"]},
    4: {ROLE_IDS["senior_admin"], ROLE_IDS["junior_admin"]},
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

# Members with this role can use Galaxy Coins, +avatar, and +userinfo only.
MEMBER_ROLE_ID = 1549925555736936468
MEMBER_ALLOWED_COMMANDS = {
    "balance", "bal", "coins", "leaderboard", "topcoins", "richest",
    "daily", "gamble", "coinflip", "cf", "slots", "avatar", "userinfo",
}

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
]

# Only discord.gg invite URLs are blocked. Other Discord domains, .gift links,
# GIF links, image links, and ordinary URLs are not matched by this filter.
DISCORD_GG_INVITE_PATTERN = re.compile(
    r"(?i)(?<![\w.-])(?:https?://)?(?:www\.)?discord\.gg/[A-Za-z0-9-]+(?:\?[^\s]*)?"
)

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


def effective_staff_rank(member):
    """Special users receive Perm 5 without a staff role; Perm 6 remains owner-only."""
    rank = max((ROLE_RANK.get(role.id, 0) for role in getattr(member, "roles", [])), default=0)
    if getattr(member, "id", None) in SPECIAL_USERS:
        rank = max(rank, 5)
    return rank


def has_staff_rank(member, minimum_rank):
    return effective_staff_rank(member) >= minimum_rank


def require_staff_rank(minimum_rank):
    async def predicate(ctx):
        # Server owners and Discord administrators are never blocked by the
        # configured role ladder; all other staff must meet the exact rank.
        return (
            ctx.author.id == ctx.guild.owner_id
            or ctx.author.guild_permissions.administrator
            or has_staff_rank(ctx.author, minimum_rank)
        )
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

# Galaxy visual identity: midnight plum with a quiet lavender accent.
GALAXY_COLOR = discord.Color.from_rgb(48, 39, 65)
# Staff records keep the Galaxy base theme, with restrained status colors.
STRIKE_ISSUED_COLOR = discord.Color.from_rgb(224, 145, 65)
STRIKE_REMOVED_COLOR = discord.Color.from_rgb(76, 166, 112)


class GalaxyContext(commands.Context):
    """Consistent, understated response styling for the Galaxy community."""
    async def send(self, content=None, **kwargs):
        embed = kwargs.get("embed")
        if embed is None and content is not None:
            embed = discord.Embed(description=str(content), color=GALAXY_COLOR)
            kwargs.pop("content", None)
            kwargs["embed"] = embed
            content = None
        if embed is not None:
            if embed.color is None:
                embed.color = GALAXY_COLOR
            if not embed.footer or not embed.footer.text:
                embed.set_footer(text="Galaxy Community")
            if embed.timestamp is None:
                embed.timestamp = discord.utils.utcnow()
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
db = sqlite3.connect(DATABASE_PATH, timeout=30)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=NORMAL")
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA busy_timeout=30000")
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
# Most recently deleted messages, kept separately for each text channel.
sniped_messages = {}

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


def spend_coins(guild_id, user_id, amount):
    """Atomically debit a wager; return (success, current_balance)."""
    if amount <= 0:
        return False, get_coin_balance(guild_id, user_id)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,0)",
            (guild_id, user_id),
        )
        debit = db.execute(
            "UPDATE galaxy_coins SET balance=balance-? "
            "WHERE guild_id=? AND user_id=? AND balance>=?",
            (amount, guild_id, user_id, amount),
        )
        if debit.rowcount != 1:
            db.rollback()
            return False, get_coin_balance(guild_id, user_id)
        db.commit()
        return True, get_coin_balance(guild_id, user_id)
    except Exception:
        db.rollback()
        raise


def transfer_coins(guild_id, sender_id, recipient_id, amount):
    """Atomically transfer coins; return (success, current_sender_balance)."""
    if amount <= 0 or sender_id == recipient_id:
        return False, get_coin_balance(guild_id, sender_id)
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,0)",
            (guild_id, sender_id),
        )
        debit = db.execute(
            "UPDATE galaxy_coins SET balance=balance-? "
            "WHERE guild_id=? AND user_id=? AND balance>=?",
            (amount, guild_id, sender_id, amount),
        )
        if debit.rowcount != 1:
            db.rollback()
            return False, get_coin_balance(guild_id, sender_id)
        db.execute(
            "INSERT INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=balance+excluded.balance",
            (guild_id, recipient_id, amount),
        )
        db.commit()
        return True, get_coin_balance(guild_id, sender_id)
    except Exception:
        db.rollback()
        raise

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
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_staff_rank(member, 1)


def has_ban(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_staff_rank(member, 4) or member.id in BAN_USER_ALLOWLIST


def has_admin_actions(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_staff_rank(member, 3) or member.id in KICK_USER_ALLOWLIST


def has_owner_actions(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_any_role(member, BLACKLIST_ROLE_IDS) or member.id in BLACKLIST_USER_ALLOWLIST


def require_perm1():
    async def predicate(ctx):
        return has_perm1(ctx.author)
    return commands.check(predicate)


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
        if isinstance(text, discord.Embed):
            await channel.send(embed=text, allowed_mentions=discord.AllowedMentions.none())
        else:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.Forbidden:
        logger.error("Missing permission to send logs in #%s (%s), guild %s",
                     getattr(channel, "name", "unknown"), channel_id, guild.id)
    except discord.HTTPException as exc:
        logger.exception("Discord rejected a log message in channel %s: %s", channel_id, exc)
    return False


async def send_strike_log(guild, text):
    """Send staff-strike audit entries to the configured strike-log channel."""
    if guild is None:
        return False
    channel_id = STRIKE_LOG_CHANNEL_ID
    channel = await fetch_guild_text_channel(guild, channel_id)
    if channel is None or not hasattr(channel, "send"):
        logger.warning("Strike logging channel %s is unavailable in guild %s (%s)", channel_id, guild.name, guild.id)
        return False
    try:
        if isinstance(text, discord.Embed):
            await channel.send(embed=text, allowed_mentions=discord.AllowedMentions.none())
        else:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.Forbidden:
        logger.error("Missing permission to send strike logs in channel %s", channel_id)
    except discord.HTTPException as exc:
        logger.exception("Discord rejected strike log in channel %s: %s", channel_id, exc)
    return False


@bot.before_invoke
async def log_command_usage(ctx):
    """Show a compact command receipt in-channel and mirror it to staff logs."""
    if not ctx.guild:
        return
    command_text = discord.utils.escape_markdown((ctx.message.content or "")[:900])
    panel = discord.Embed(
        title="GALAXY  /  COMMAND",
        description=(
            f"**{discord.utils.escape_markdown(ctx.command.qualified_name if ctx.command else 'command')}** "
            "was run.\n\n"
            f"```{command_text[:900]}```"
        ),
        color=GALAXY_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    panel.add_field(name="Used by", value=f"{ctx.author.mention}  ·  `{ctx.author.id}`", inline=True)
    panel.add_field(name="Channel", value=f"{ctx.channel.mention}", inline=True)
    if ctx.message.reference and ctx.message.reference.message_id:
        panel.add_field(name="Reply target", value=f"`{ctx.message.reference.message_id}`", inline=True)
    panel.set_footer(text="GALAXY  ·  COMMAND RECORD")
    try:
        await ctx.channel.send(embed=panel, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        logger.warning("Could not show command receipt in channel %s", ctx.channel.id)
    await send_mod_log(ctx.guild, panel)


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
    logger.info("Prefix commands require Message Content Intent enabled in the Discord Developer Portal.")
    logger.info("Startup diagnostics: discord.py=%s | database=%s | prefix=%r",
                getattr(discord, "__version__", "unknown"), DATABASE_PATH, PREFIX)
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


def contains_blocked_phrase(content, phrase):
    """Match a complete word/phrase, not an accidental substring."""
    pattern = r"(?<!\w)" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?!\w)"
    return re.search(pattern, content, flags=re.IGNORECASE) is not None


def remember_deleted_message(message):
    """Save a deleted message immediately so +snipe also catches bot-filter deletions."""
    if not message.guild or message.author.bot:
        return
    sniped_messages[message.channel.id] = {
        "content": message.content or "",
        "author_name": str(message.author),
        "author_id": message.author.id,
        "avatar_url": message.author.display_avatar.url,
        "created_at": message.created_at,
        "deleted_at": discord.utils.utcnow(),
        "attachments": [attachment.url for attachment in message.attachments[:4]],
        "channel_id": message.channel.id,
    }


async def filter_bad_content(message):
    """Delete blocked content, warn the author, escalate repeat use, and log verbatim."""
    if not message.guild or message.author.bot or not (message.content or "").strip():
        return False

    member = message.guild.get_member(message.author.id)
    if member is None:
        try:
            member = await message.guild.fetch_member(message.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    # Preserve the previous exemption for Perm 5+ staff.
    if member and has_staff_rank(member, 5):
        return False

    content = message.content
    lowered = content.lower()

    # Only discord.gg invite links are blocked; .gift and unrelated links are allowed.
    link_match = DISCORD_GG_INVITE_PATTERN.search(content)
    if link_match:
        remember_deleted_message(message)
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not delete discord.gg invite message %s", message.id)
        record_sanction(
            message.guild.id, message.author.id, bot.user.id if bot.user else 0,
            "Blacklisted Link", "Discord invite links are not allowed",
            details=f"Matched: {link_match.group(0)} | Original message: {content}",
        )
        try:
            notice = discord.Embed(
                title="GALAXY  /  LINK FILTER",
                description=f"{message.author.mention} Discord invite links (`discord.gg`) are not allowed here.",
                color=GALAXY_COLOR,
                timestamp=discord.utils.utcnow(),
            )
            notice.set_footer(text="GALAXY  ·  COMMUNITY RULES")
            await message.channel.send(
                embed=notice,
                allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False),
            )
        except discord.HTTPException:
            pass
        await send_mod_log(
            message.guild,
            "🔗 **Blacklisted Discord Invite Link**\n"
            f"User: {message.author} (`{message.author.id}`)\n"
            f"Channel: {message.channel.mention} (`{message.channel.id}`)\n"
            f"Matched link: `{discord.utils.escape_markdown(link_match.group(0))}`\n"
            f"Original message: {content[:1500]}"
        )
        return True

    matched = next((word for word in sorted(BLACKLISTED_WORDS, key=len, reverse=True)
                    if contains_blocked_phrase(lowered, word)), None)
    if not matched:
        return False

    remember_deleted_message(message)
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
        notice = discord.Embed(
            title="GALAXY  /  WORD FILTER",
            description=(
                f"{message.author.mention} your message was removed for a blacklisted word.\n"
                f"{action_text}"
            ),
            color=STRIKE_ISSUED_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        notice.set_footer(text="GALAXY  ·  COMMUNITY RULES")
        await message.channel.send(
            embed=notice,
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


def is_staff_member(member):
    """Return True for configured staff, the server owner, or administrators."""
    return bool(
        member
        and (
            getattr(member, "guild_permissions", None)
            and member.guild_permissions.administrator
            or getattr(member, "id", None) == getattr(getattr(member, "guild", None), "owner_id", None)
            or has_any_role(member, PERM1_ROLE_IDS)
        )
    )


def member_command_is_allowed(member, command):
    """Apply the member-role command allowlist without restricting staff."""
    if not has_any_role(member, {MEMBER_ROLE_ID}) or is_staff_member(member):
        return True
    if command is None:
        return True
    names = {command.name, *(command.aliases or [])}
    return bool(names & MEMBER_ALLOWED_COMMANDS)


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return
    if await filter_bad_content(message):
        return
    if bot.user and message.content.strip() in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
        mention_embed = discord.Embed(
            title="GALAXY",
            description=f"Need a hand? My prefix is `{PREFIX}` — use `{PREFIX}help` and I’ll show you what I can do.",
            color=discord.Color.from_rgb(111, 66, 193),
        )
        mention_embed.set_footer(text="GALAXY  •  COMMUNITY SYSTEMS")
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
            result_panel = discord.Embed(
                title="GALAXY  /  CORRECT ANSWER",
                description=(
                    f"{message.author.mention} got it right and earned **{reward:,} Galaxy Coins**.\n"
                    f"New balance: **{get_coin_balance(message.guild.id, message.author.id):,}**."
                ),
                color=GALAXY_COLOR,
                timestamp=discord.utils.utcnow(),
            )
            result_panel.set_footer(text="GALAXY  ·  COMMUNITY GAME")
            await message.channel.send(embed=result_panel, allowed_mentions=discord.AllowedMentions.none())
            return
    # Replying to a human message can supply the target for any command whose
    # callback accepts a member/user/target/user_id parameter. Explicit targets win.
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
                    parameters = list(inspect.signature(command.callback).parameters.values())
                    parameters = [p for p in parameters if p.name not in {"ctx", "context"}]
                    target_index = next(
                        (i for i, param in enumerate(parameters)
                         if param.name.lower() in {"member", "user", "target", "user_id"}),
                        None,
                    )
                    if target_index is not None:
                        prefix_and_command, _, tail = message.content[len(PREFIX):].partition(" ")
                        tokens = tail.split()
                        # If a mention or snowflake is already present, never override it.
                        target_text = re.sub(r"<@&\d+>|<#\d+>", "", tail)
                        has_explicit_target = bool(
                            re.search(r"<@!?\d+>", target_text)
                            or re.search(r"(?<![\w])\d{15,22}(?![\w])", target_text)
                        )
                        if not has_explicit_target:
                            target_param = parameters[target_index]
                            target_value = (
                                str(replied.author.id)
                                if target_param.name == "user_id"
                                else f"<@{replied.author.id}>"
                            )
                            tokens.insert(min(target_index, len(tokens)), target_value)
                            message.content = f"{PREFIX}{prefix_and_command}" + (" " + " ".join(tokens) if tokens else "")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, TypeError, ValueError):
            logger.debug("Could not resolve reply target for command message %s", message.id, exc_info=True)

    # The Member role is limited to the coin economy and profile lookups.
    # Configured staff are exempt from this role-based restriction.
    if message.content.startswith(PREFIX):
        member = message.guild.get_member(message.author.id)
        if member is None:
            try:
                member = await message.guild.fetch_member(message.author.id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is not None:
            command_context = await bot.get_context(message)
            if not member_command_is_allowed(member, command_context.command):
                return

    row = db.execute(
        "SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?",
        (message.guild.id, message.author.id),
    ).fetchone()
    if row and not message.content.startswith(PREFIX + "unbl"):
        return
    await bot.process_commands(message)


@bot.event
async def on_message_delete(message):
    """Keep the latest deleted human message per channel, including filtered messages."""
    remember_deleted_message(message)



@bot.command(name="snipe")
@commands.guild_only()
async def snipe(ctx):
    """Show the most recently deleted message in this channel."""
    deleted = sniped_messages.get(ctx.channel.id)
    if not deleted:
        return await ctx.send("There isn’t a recently deleted message saved for this channel.")

    content = deleted["content"].strip() or "*No text content (the message may have contained an attachment or embed).*"
    # Keep the message readable without allowing mentions or markdown to ping/imitate staff.
    safe_content = discord.utils.escape_mentions(content)
    if len(safe_content) > 3500:
        safe_content = safe_content[:3497] + "..."
    panel = discord.Embed(
        title="RECENTLY DELETED MESSAGE",
        description=safe_content,
        color=GALAXY_COLOR,
        timestamp=deleted["deleted_at"],
    )
    panel.set_author(name=deleted["author_name"], icon_url=deleted["avatar_url"])
    panel.add_field(name="Channel", value=f"<#{deleted['channel_id']}>", inline=True)
    panel.add_field(name="Author ID", value=f"`{deleted['author_id']}`", inline=True)
    if deleted["created_at"]:
        panel.add_field(name="Originally sent", value=discord.utils.format_dt(deleted["created_at"], style="R"), inline=True)
    if deleted["attachments"]:
        panel.add_field(name="Attachments", value="\n".join(deleted["attachments"]), inline=False)
    panel.set_footer(text="GALAXY  ·  SNIPE  ·  LATEST DELETION IN THIS CHANNEL")
    await ctx.send(embed=panel, allowed_mentions=discord.AllowedMentions.none())



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
    embed = discord.Embed(title="Galaxy Coins  /  Leaderboard",
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
        embed.description = "\n".join(lines)
    embed.set_footer(text="GALAXY  •  TOP 10  •  REFRESHES EVERY 3 HOURS")
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
@commands.cooldown(1, 3, commands.BucketType.user)
async def galaxy_give_coins(ctx, member: discord.Member, amount: int):
    """Any member can transfer coins from their own balance."""
    if member.bot:
        return await ctx.send("Bots can’t spend coins, so that transfer won’t go through.")
    if member.id == ctx.author.id:
        return await ctx.send("Keeping your coins for yourself? Fair — but you can’t transfer them to your own account.")
    if amount < 1:
        return await ctx.send("Pick an amount of at least **1 coin**.")
    success, balance = transfer_coins(ctx.guild.id, ctx.author.id, member.id, amount)
    if not success:
        return await ctx.send(f"The transfer could not be completed. Your balance is **{balance:,} Galaxy Coins**.")
    await ctx.send(f"💸 {ctx.author.mention} sent **{amount:,} Galaxy Coins** to {member.mention}. "
                   f"Your balance: **{balance:,}**.")
    await send_mod_log(ctx.guild, f"💸 **Galaxy Coins transfer**\nFrom: {ctx.author} (`{ctx.author.id}`)\n"
                                  f"To: {member} (`{member.id}`)\nAmount: **{amount:,}**")


@bot.command(name="addcoins", aliases=["coinadd"])
@require_staff_rank(2)
async def galaxy_add_coins(ctx, member: discord.Member, amount: int, *, reason="Staff adjustment"):
    if amount < 1:
        return await ctx.send("The amount needs to be at least **1**.")
    change_coins(ctx.guild.id, member.id, amount)
    await ctx.send(f"➕ Added **{amount:,} Galaxy Coins** to {member.mention}. "
                   f"New balance: **{get_coin_balance(ctx.guild.id, member.id):,}**.")
    await send_mod_log(ctx.guild, f"➕ **Galaxy Coins added**\nUser: {member} (`{member.id}`)\n"
                                  f"Amount: **{amount:,}**\nBy: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="removecoins", aliases=["coinremove"])
@require_staff_rank(2)
async def galaxy_remove_coins(ctx, member: discord.Member, amount: int, *, reason="Staff adjustment"):
    if amount < 1:
        return await ctx.send("The amount needs to be at least **1**.")
    current = get_coin_balance(ctx.guild.id, member.id)
    removed = min(amount, current)
    change_coins(ctx.guild.id, member.id, -removed)
    await ctx.send(f"➖ Removed **{removed:,} Galaxy Coins** from {member.mention}. "
                   f"New balance: **{get_coin_balance(ctx.guild.id, member.id):,}**.")
    await send_mod_log(ctx.guild, f"➖ **Galaxy Coins removed**\nUser: {member} (`{member.id}`)\n"
                                  f"Amount: **{removed:,}**\nBy: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="setcoins", aliases=["coinset"])
@require_staff_rank(2)
async def galaxy_set_coins(ctx, member: discord.Member, amount: int, *, reason="Staff adjustment"):
    if amount < 0:
        return await ctx.send("I can’t set someone’s balance below zero.")
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
@commands.cooldown(1, 5, commands.BucketType.user)
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
                return await ctx.send(f"You’ve already claimed today’s reward. Come back in **{hours}h {minutes}m**.")
        except ValueError:
            pass
    reward = 250
    db.execute("INSERT INTO galaxy_coins(guild_id,user_id,balance,last_daily) VALUES(?,?,?,CURRENT_TIMESTAMP) "
               "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=balance+excluded.balance,last_daily=CURRENT_TIMESTAMP",
               (ctx.guild.id, ctx.author.id, reward))
    db.commit()
    await ctx.send(f"🎁 Daily coins claimed — **+{reward:,}**. You now have **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**.")

@bot.command(name="gamble", aliases=["coinflip", "cf"])
@commands.cooldown(1, 3, commands.BucketType.user)
async def galaxy_gamble(ctx, amount: int):
    if amount < 1:
        return await ctx.send("You’ll need to bet at least **1 Galaxy Coin**.")
    success, balance = spend_coins(ctx.guild.id, ctx.author.id, amount)
    if not success:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")
    if random.random() < 0.5:
        change_coins(ctx.guild.id, ctx.author.id, amount * 2)
        await ctx.send(f"🪙 **You won!** Net profit: **+{amount:,} Galaxy Coins**. Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**.")
    else:
        await ctx.send(f"🪙 **You lost!** You lost **{amount:,} Galaxy Coins**. Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**.")

@bot.command(name="slots")
@commands.cooldown(1, 3, commands.BucketType.user)
async def galaxy_slots(ctx, amount: int):
    if amount < 1:
        return await ctx.send("You’ll need to bet at least **1 Galaxy Coin**.")
    success, balance = spend_coins(ctx.guild.id, ctx.author.id, amount)
    if not success:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")
    symbols = ["🌌", "⭐", "🪐", "💎", "☄️"]
    result = [random.choice(symbols) for _ in range(3)]
    if result[0] == result[1] == result[2]:
        payout = amount * 6  # Stake returned plus 5x net winnings.
        change_coins(ctx.guild.id, ctx.author.id, payout)
        outcome = f"JACKPOT! **+{amount * 5:,} net coins**"
    elif len(set(result)) == 2:
        payout = amount * 2  # Stake returned plus 1x net winnings.
        change_coins(ctx.guild.id, ctx.author.id, payout)
        outcome = f"Two match! **+{amount:,} net coins**"
    else:
        outcome = f"No match. **-{amount:,} coins**"
    await ctx.send(f"🎰 {' | '.join(result)}\n{outcome}\nBalance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**")


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
        return ctx.author.id == ctx.guild.owner_id or has_staff_rank(ctx.author, 5)
    return commands.check(predicate)


def can_moderate_target(ctx, member):
    if member.id == ctx.author.id:
        return False
    if ctx.author.id == ctx.guild.owner_id:
        return True
    actor_rank = effective_staff_rank(ctx.author)
    target_rank = effective_staff_rank(member)
    if target_rank:
        return actor_rank > target_rank
    # Non-staff targets are governed by Discord's native role order.
    return member.top_role < ctx.author.top_role


@bot.command()
@require_perm1()
async def perms(ctx):
    """Show the staff ladder and command access."""
    role_order = {
        1: ["trial_moderator", "moderator"],
        2: ["senior_moderator", "head_moderator"],
        3: ["admin"],
        4: ["senior_admin", "junior_admin"],
        5: ["head_admin", "assistant_staff_manager", "staff_manager", "manager",
            "assistant_head_manager", "head_manager", "trial_director", "senior_drc",
            "drc", "axelle", "dev"],
        6: ["founder", "co_owner"],
    }
    labels = {1: "MODERATION", 2: "SENIOR MODERATION", 3: "ADMINISTRATION",
              4: "SENIOR ADMINISTRATION", 5: "LEADERSHIP", 6: "OWNERSHIP"}
    sections = []
    for level in range(1, 7):
        roles = "  ·  ".join(f"<@&{ROLE_IDS[key]}>" for key in role_order[level])
        sections.append(f"**PERMISSION {level}  /  {labels[level]}**\n{roles}")
    embed = discord.Embed(title="Staff permissions", description="\n\n".join(sections), color=GALAXY_COLOR)
    embed.add_field(name="COMMAND ACCESS", value=(
        "`+sanctions`  Perm 1+\n"
        "`+del sanction`  Perm 2+\n`+kick`  Perm 3+  ·  `+ban`  Perm 4+\n"
        "`+bl` / `+blacklist`  Perm 6+"), inline=False)
    embed.add_field(name="STAFF HIERARCHY", value=(
        "Higher ranks inherit lower-rank access. Staff may moderate lower-ranked members only; "
        "equal- and higher-ranked staff are protected."), inline=False)
    embed.set_footer(text="Galaxy Community  ·  Staff reference")
    await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


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
    await ctx.send(f"⚠️ {member.mention} has received a warning.\n**Reason:** {reason}\n**Case:** `#{sanction_id}`")
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
    await ctx.send(f"🧹 Warnings cleared for {member.mention}.")
    await send_mod_log(ctx.guild, f"🧹 **Warnings cleared**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="strike")
@commands.guild_only()
@require_staff_strike_access()
async def staff_strike(ctx, member: discord.Member, *, reason="No reason provided"):
    """Issue a staff strike and send a compact branded audit panel."""
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    if not has_any_role(member, set(ROLE_IDS.values())):
        return await ctx.send("That member isn’t on the staff team, so a staff strike doesn’t apply to them.")
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can only issue a strike to a staff member below your permission level.")

    reason = reason.strip()[:900] or "No reason provided"
    cursor = db.execute(
        "INSERT INTO staff_strikes(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, member.id, ctx.author.id, reason),
    )
    db.commit()
    strike_id = cursor.lastrowid
    strike_count = db.execute(
        "SELECT COUNT(*) FROM staff_strikes WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, member.id),
    ).fetchone()[0]
    issued_at = discord.utils.utcnow()

    response = discord.Embed(
        title="STAFF RECORD  /  STRIKE ISSUED",
        description=f"A staff strike has been recorded for {member.mention}.",
        color=STRIKE_ISSUED_COLOR,
        timestamp=issued_at,
    )
    response.set_author(name="STEAL A GALACTICROT  ·  STAFF MANAGEMENT")
    response.add_field(name="Strike ID", value=f"`#{strike_id}`", inline=True)
    response.add_field(name="Active strikes", value=f"`{strike_count}`", inline=True)
    response.add_field(name="Issued by", value=ctx.author.mention, inline=True)
    response.add_field(name="Reason", value=discord.utils.escape_markdown(discord.utils.escape_mentions(reason)), inline=False)
    response.add_field(name="Recorded", value=f"<t:{int(issued_at.timestamp())}:F>  ·  <t:{int(issued_at.timestamp())}:R>", inline=False)
    response.set_footer(text="GALAXY  ·  STAFF RECORDS")
    await ctx.send(embed=response, allowed_mentions=discord.AllowedMentions.none())

    audit = discord.Embed(
        title="STAFF AUDIT  /  STRIKE ISSUED",
        description=f"A staff record has been updated for {member.mention}.",
        color=STRIKE_ISSUED_COLOR,
        timestamp=issued_at,
    )
    audit.set_author(name="STEAL A GALACTICROT  ·  OWNER AUDIT")
    audit.add_field(name="Staff member", value=f"{member.mention}\n`{member.id}`", inline=True)
    audit.add_field(name="Issued by", value=f"{ctx.author.mention}\n`{ctx.author.id}`", inline=True)
    audit.add_field(name="Strike record", value=f"`#{strike_id}`  ·  `#{strike_count}` active", inline=False)
    audit.add_field(name="Reason", value=discord.utils.escape_markdown(discord.utils.escape_mentions(reason)), inline=False)
    audit.set_footer(text="GALAXY  ·  PRIVATE STAFF AUDIT")
    if not await send_strike_log(ctx.guild, audit):
        await ctx.send("The strike was saved, but I couldn’t deliver its audit entry. Check the bot’s access to the configured strike-log channel.")


@bot.command(name="strikes", aliases=["staffstrikes"])
@commands.guild_only()
@require_staff_strike_access()
async def staff_strikes(ctx, member: discord.Member):
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    if not has_any_role(member, set(ROLE_IDS.values())):
        return await ctx.send("I can’t find a staff role on that member. This command only works for configured staff.")
    rows = db.execute(
        "SELECT id, reason, created_at, moderator_id FROM staff_strikes "
        "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
        (ctx.guild.id, member.id),
    ).fetchall()
    active_count = db.execute(
        "SELECT COUNT(*) FROM staff_strikes WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, member.id),
    ).fetchone()[0]
    history = discord.Embed(
        title="Staff strike history",
        description=f"Records for {member.mention}",
        color=STRIKE_ISSUED_COLOR if active_count else GALAXY_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    history.set_author(name="STEAL A GALACTICROT  ·  STAFF MANAGEMENT")
    history.add_field(name="Active strikes", value=f"`{active_count}`", inline=True)
    history.add_field(name="Retention", value="`30 days`", inline=True)
    if rows:
        lines = []
        for strike_id, reason, created_at, moderator_id in rows:
            safe_reason = discord.utils.escape_mentions(reason or "No reason provided")
            try:
                recorded = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                recorded_ts = int(recorded.timestamp())
                date_label = f"<t:{recorded_ts}:d> · <t:{recorded_ts}:R>"
            except (TypeError, ValueError):
                date_label = discord.utils.escape_markdown(str(created_at or "Date unavailable"))
            lines.append(f"**`#{strike_id}`** · {safe_reason[:180]}\n{date_label} · by <@{moderator_id}>")
        history.add_field(name="Recent records", value="\n\n".join(lines)[:3900], inline=False)
    else:
        history.description += "\n\nNo active strike records were found."
    history.set_footer(text="GALAXY  ·  STAFF RECORDS  ·  30-DAY RETENTION")
    await ctx.send(embed=history, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="removestrike", aliases=["unstrike"])
@commands.guild_only()
@require_staff_strike_access()
async def remove_staff_strike(ctx, strike_id: int, *, reason="No reason provided"):
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    row = db.execute(
        "SELECT user_id, reason, moderator_id, created_at FROM staff_strikes WHERE guild_id=? AND id=?",
        (ctx.guild.id, strike_id),
    ).fetchone()
    if row is None:
        return await ctx.send("I couldn’t find that strike. Check the ID and try again.")
    db.execute("DELETE FROM staff_strikes WHERE guild_id=? AND id=?", (ctx.guild.id, strike_id))
    db.commit()
    removed_at = discord.utils.utcnow()
    reason = (reason or "No reason provided").strip()[:900] or "No reason provided"
    remaining_count = db.execute(
        "SELECT COUNT(*) FROM staff_strikes WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, row[0]),
    ).fetchone()[0]
    safe_removal_reason = discord.utils.escape_markdown(discord.utils.escape_mentions(reason))
    response = discord.Embed(
        title="STAFF RECORD  /  STRIKE REMOVED",
        description=f"Strike `#{strike_id}` has been removed from <@{row[0]}>.",
        color=STRIKE_REMOVED_COLOR,
        timestamp=removed_at,
    )
    response.set_author(name="STEAL A GALACTICROT  ·  STAFF MANAGEMENT")
    response.add_field(name="Strike ID", value=f"`#{strike_id}`", inline=True)
    response.add_field(name="Active strikes left", value=f"`{remaining_count}`", inline=True)
    response.add_field(name="Removed by", value=ctx.author.mention, inline=True)
    response.add_field(name="Removal reason", value=safe_removal_reason, inline=False)
    response.set_footer(text="GALAXY  ·  STAFF RECORDS")
    await ctx.send(embed=response, allowed_mentions=discord.AllowedMentions.none())

    audit = discord.Embed(
        title="STAFF AUDIT  /  STRIKE REMOVED",
        description=f"Strike `#{strike_id}` was removed from <@{row[0]}>.",
        color=STRIKE_REMOVED_COLOR,
        timestamp=removed_at,
    )
    audit.set_author(name="STEAL A GALACTICROT  ·  OWNER AUDIT")
    audit.add_field(name="Staff member ID", value=f"`{row[0]}`", inline=True)
    audit.add_field(name="Removed by", value=f"{ctx.author.mention}\n`{ctx.author.id}`", inline=True)
    audit.add_field(name="Active strikes left", value=f"`{remaining_count}`", inline=True)
    audit.add_field(name="Original reason", value=discord.utils.escape_markdown(discord.utils.escape_mentions(row[1] or "No reason provided")), inline=False)
    audit.add_field(name="Originally issued", value=discord.utils.escape_markdown(str(row[3] or "Date unavailable")), inline=True)
    audit.add_field(name="Originally issued by", value=f"<@{row[2]}>  ·  `{row[2]}`", inline=True)
    audit.add_field(name="Removal reason", value=safe_removal_reason, inline=False)
    audit.set_footer(text="GALAXY  ·  PRIVATE STAFF AUDIT")
    if not await send_strike_log(ctx.guild, audit):
        await ctx.send("The strike was removed, but I couldn’t deliver its audit entry. Check the bot’s access to the configured strike-log channel.")


@bot.command(name="tempmute", aliases=["timeout"])
@require_perm1()
async def tempmute(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    if minutes < 1 or minutes > 40320:
        return await ctx.send("Timeout length must be between **1 minute and 28 days**.")
    if not can_moderate_target(ctx, member):
        return
    await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Timeout", reason,
                                  details=f"Duration: {minutes} minutes")
    await ctx.send(f"⏳ {member.mention} has been timed out for **{minutes} minutes**.\n**Case:** `#{sanction_id}`")
    await send_mod_log(ctx.guild, f"⏳ **Timeout**\nUser: {member} (`{member.id}`)\nDuration: {minutes} minutes\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_perm1()
async def unmute(ctx, member: discord.Member):
    if not can_moderate_target(ctx, member):
        return
    await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
    await ctx.send(f"Timeout lifted for {member.mention}. They can speak again.")
    await send_mod_log(ctx.guild, f"🔊 **Timeout removed**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_ban()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return
    await member.ban(reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Ban", reason)
    await ctx.send(f"🔨 **{member}** has been banned.\n**Reason:** {reason}\n**Case:** `#{sanction_id}`")
    await send_mod_log(ctx.guild, f"🔨 **Ban**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_ban()
async def unban(ctx, user_id: int, *, reason="No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.send("I couldn’t find an active ban for that ID. Check the ID and try again.")
    await ctx.send(f"Unbanned **{user}**.")
    await send_mod_log(ctx.guild, f"♻️ **Unban**\nUser: {user} (`{user.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_admin_actions()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return
    await member.kick(reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Kick", reason)
    await ctx.send(f"👢 **{member}** has been kicked.\n**Reason:** {reason}\n**Case:** `#{sanction_id}`")
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
        return await ctx.send("That user isn’t on the bot blacklist.")
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
    embed = discord.Embed(title=f"Sanctions  /  {user}", description="\\n".join(lines),
                          color=discord.Color.from_rgb(111, 66, 193))
    embed.set_footer(text=f"User ID: {user.id} • Showing up to 25 records")
    await ctx.send(embed=embed)


@bot.command(name="del", aliases=["delsanction", "deletesanction"])
@require_staff_rank(2)  # Senior Moderator+
async def delete_sanction(ctx, subcommand: str, sanction_id: int, *, reason="No reason provided"):
    if subcommand.lower() != "sanction":
        return await ctx.send(f"Usage: `{PREFIX}del sanction <sanction_id> [reason]`.")
    row = db.execute(
        """SELECT user_id, sanction_type, reason, source_warning_id
           FROM sanctions WHERE guild_id=? AND id=?""",
        (ctx.guild.id, sanction_id),
    ).fetchone()
    if row is None:
        return await ctx.send("That sanction ID doesn’t match anything in the records. Double-check the number.")
    user_id, sanction_type, original_reason, warning_id = row
    db.execute("DELETE FROM sanctions WHERE guild_id=? AND id=?", (ctx.guild.id, sanction_id))
    if sanction_type.lower() == "warn" and warning_id is not None:
        db.execute("DELETE FROM warnings WHERE guild_id=? AND id=?", (ctx.guild.id, warning_id))
    db.commit()
    await ctx.send(f"✅ Deleted sanction `#{sanction_id}` ({sanction_type}) for user `{user_id}`.")
    await send_mod_log(
        ctx.guild,
        f"🧹 **Sanction deleted**\nSanction: `#{sanction_id}` ({sanction_type})"
        f"\nUser ID: `{user_id}`\nOriginal reason: {original_reason}"
        f"\nDeleted by: {ctx.author} (`{ctx.author.id}`)\nDeletion reason: {reason}",
    )



@bot.command()
@require_perm1()
async def lock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {ctx.author}")
    await ctx.send("🔒 Channel locked. Members can no longer send messages here.")
    await send_mod_log(ctx.guild, f"🔒 **Channel locked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def unlock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {ctx.author}")
    await ctx.send("🔓 Channel unlocked. Members can chat here again.")
    await send_mod_log(ctx.guild, f"🔓 **Channel unlocked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def slowmode(ctx, seconds: int):
    if not 0 <= seconds <= 21600:
        return await ctx.send("Slowmode can be anywhere from **0 to 21,600 seconds**.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {ctx.author}")
    await ctx.send(f"⏱️ Done — slowmode is now **{seconds} seconds**.")
    await send_mod_log(ctx.guild, f"⏱️ **Slowmode changed**\nChannel: {ctx.channel.mention}\nSeconds: {seconds}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def nick(ctx, member: discord.Member, *, nickname: str = None):
    if not can_moderate_target(ctx, member):
        return
    await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
    await ctx.send(f"✏️ Changed {member.mention}’s nickname.")
    await send_mod_log(ctx.guild, f"✏️ **Nickname changed**\nMember: {member} (`{member.id}`)\nNew nickname: {nickname or 'Reset to username'}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="role")
@require_perm1()
async def role_command(ctx, action: str, member: discord.Member, role: discord.Role):
    action = action.lower()
    if action not in ("add", "remove"):
        return await ctx.send(f"Usage: `{PREFIX}role add @member @role` or `{PREFIX}role remove @member @role`.")
    if role.is_default() or role.managed or role >= ctx.author.top_role or role >= ctx.guild.me.top_role:
        return await ctx.send("I can’t move that role. It may be managed by an integration, or it sits above your role or mine.")
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
        return await ctx.send("I couldn’t find that warning. Make sure you copied the right ID.")
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
    embed = discord.Embed(title=f"User info — {member}", color=discord.Color.from_rgb(111, 66, 193))
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
    embed = discord.Embed(title=f"Server info — {guild.name}", color=discord.Color.from_rgb(111, 66, 193))
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
    embed = discord.Embed(title=f"{member.display_name}'s avatar", color=discord.Color.from_rgb(111, 66, 193))
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
    await ctx.send(f"Right here. `{round(bot.latency * 1000)} ms`")


@bot.command()
async def help(ctx):
    """Compact command directory, displayed as one Galaxy panel."""
    embed = discord.Embed(
        title="Galaxy  ·  Command guide",
        description=f"Commands use the `{PREFIX}` prefix. Reply to a member’s message to use them as the target for supported moderation commands. `{PREFIX}snipe` shows the latest deleted message in the current channel.",
        color=GALAXY_COLOR,
    )
    embed.add_field(
        name="START HERE",
        value=(
            f"`{PREFIX}help`  `{PREFIX}ping`  `{PREFIX}perms`\n"
            f"`{PREFIX}userinfo [member]`  `{PREFIX}avatar [member]`\n"
            f"`{PREFIX}membercount`  `{PREFIX}serverinfo`  `{PREFIX}roleinfo`  `{PREFIX}snipe`"
        ),
        inline=False,
    )
    embed.add_field(
        name="MODERATION",
        value=(
            f"`{PREFIX}warn`  `{PREFIX}warnings`  `{PREFIX}clearwarns`  `{PREFIX}unwarn` (Perm 1+)\n"
            f"`{PREFIX}sanctions @user` (Perm 1+)  `{PREFIX}del sanction <id>` (Perm 2+)\n"
            f"`{PREFIX}tempmute` / `{PREFIX}timeout`  `{PREFIX}unmute`\n"
            f"`{PREFIX}ban` (Perm 4+)  `{PREFIX}unban`  `{PREFIX}kick` (Perm 3+)"
        ),
        inline=False,
    )
    embed.add_field(
        name="SERVER & STAFF TOOLS",
        value=(
            f"`{PREFIX}lock`  `{PREFIX}unlock`  `{PREFIX}slowmode`\n"
            f"`{PREFIX}nick`  `{PREFIX}role add/remove`  `{PREFIX}antiraid on/off/status`\n"
            f"`{PREFIX}bl` / `{PREFIX}blacklist`  `{PREFIX}unbl` / `{PREFIX}unblacklist`"
        ),
        inline=False,
    )
    embed.add_field(
        name="STAFF STRIKES",
        value=(
            f"`{PREFIX}strike`  `{PREFIX}strikes`  `{PREFIX}removestrike`\n"
            "Available to Perm 5+ roles. Strikes expire after 30 days."
        ),
        inline=False,
    )
    embed.add_field(
        name="GALAXY COINS",
        value=(
            f"`{PREFIX}balance` / `{PREFIX}bal`  `{PREFIX}daily`  `{PREFIX}givecoins`\n"
            f"`{PREFIX}gamble` / `{PREFIX}coinflip`  `{PREFIX}slots`  `{PREFIX}leaderboard`\n"
            f"`{PREFIX}addcoins`  `{PREFIX}removecoins`  `{PREFIX}setcoins` (Senior Moderator+)\n"
            "Anyone can transfer their own coins to another member. Senior Moderator+ can "
            "adjust balances. Bets are deducted before payouts; daily and game commands have cooldowns. "
            "The top 10 leaderboard posts every 3 hours."
        ),
        inline=False,
    )
    embed.add_field(
        name="STAFF LEVELS & ACCESS",
        value=(
            "**Perm 1 — Trial Moderator / Moderator:** General staff commands and `+sanctions`.\n"
            "**Perm 2 — Senior Moderator / Head Moderator:** Inherits Perm 1; `+del sanction` and coin balance adjustments.\n"
            "**Perm 3 — Admin:** Inherits Perm 1–2 and gets `+kick`.\n"
            "**Perm 4 — Senior Admin / Junior Admin:** Inherits Perm 1–3 and gets `+ban`.\n"
            "**Perm 5 — Head Admin / Management / DRC / Dev / Axelle:** Inherits Perm 1–4; staff-strike tools.\n"
            "**Perm 6 — Founder / Co-Owner:** All permission levels, including `+bl` / `+blacklist`.\n\n"
            "Higher permission levels inherit lower-level access. Staff can moderate only "
            "members below their rank; equal- or higher-ranked staff are protected."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Galaxy Community  ·  Prefix: {PREFIX}")
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors; permission denials remain completely silent."""
    original = getattr(error, "original", error)
    command_name = ctx.command.qualified_name if ctx.command else "that command"
    usage = f"`{PREFIX}{command_name}"
    if ctx.command and ctx.command.signature:
        usage += f" {ctx.command.signature}"
    usage += "`"

    if isinstance(error, commands.MissingRequiredArgument):
        missing = error.param.name.replace("_", " ")
        await ctx.send(
            f"Hold on — I still need **{missing}** to run that.\n"
            f"**Try:** {usage}\n"
            f"If you’re not sure what goes there, use `{PREFIX}help`."
        )
        if ctx.guild:
            await send_mod_log(
                ctx.guild,
                f"⚠️ **Command missing an argument**\\nCommand: `{command_name}`"
                f"\\nMissing: `{error.param.name}`\\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\\nChannel: {ctx.channel.mention}"
            )

    elif isinstance(error, commands.BadArgument):
        await ctx.send(
            f"I couldn’t match one of those details. Make sure the member is mentioned "
            f"or their ID is correct, and check that any numbers are valid.\n"
            f"**Try:** {usage}"
        )
        if ctx.guild:
            await send_mod_log(
                ctx.guild,
                f"⚠️ **Command argument couldn’t be read**\\nCommand: `{command_name}`"
                f"\\nUser: {ctx.author} (`{ctx.author.id}`)\\nChannel: {ctx.channel.mention}"
            )

    elif isinstance(error, commands.CheckFailure):
        return

    elif isinstance(error, commands.CommandNotFound):
        return

    elif isinstance(error, commands.CommandOnCooldown):
        wait = max(1, round(error.retry_after))
        await ctx.send(f"Give it a second — you can use that again in **{wait}s**.")

    elif isinstance(error, commands.NoPrivateMessage):
        return

    elif isinstance(original, discord.Forbidden):
        logger.warning("Discord permission failure for %s: %s", command_name, original)
        await ctx.send(
            "I can’t carry that out with the permissions I have right now. "
            "The server may need to give me the required permission and move my role "
            "above the member or role I’m trying to manage. If everything looks right, "
            "ask an admin to check my role settings."
        )
        if ctx.guild:
            await send_mod_log(
                ctx.guild,
                f"🚫 **Discord blocked a command**\\nCommand: `{command_name}`"
                f"\\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\\nDetails: `{discord.utils.escape_markdown(str(original)[:500])}`"
            )

    elif isinstance(original, discord.HTTPException):
        logger.error(
            "Discord HTTP error while running %s: %s", command_name, original,
            exc_info=(type(original), original, original.__traceback__)
            if original.__traceback__ else None
        )
        await ctx.send(
            "Discord didn’t accept that action. Nothing else to try on your end for now — "
            "an admin can check the bot’s permissions and role position."
        )
        if ctx.guild:
            await send_mod_log(
                ctx.guild,
                f"⚠️ **Discord API error**\\nCommand: `{command_name}`"
                f"\\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\\nDetails: `{discord.utils.escape_markdown(str(original)[:500])}`"
            )

    else:
        logger.error(
            "Unexpected command error in %s: %s", command_name, original,
            exc_info=(type(original), original, original.__traceback__)
            if original.__traceback__ else None
        )
        await ctx.send(
            "That didn’t work. I’ve logged the error so the staff team can check it — "
            "try again in a moment."
        )
        if ctx.guild:
            await send_mod_log(
                ctx.guild,
                f"❗ **Unexpected bot error**\\nCommand: `{command_name}`"
                f"\\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\\nError: `{discord.utils.escape_markdown(str(original)[:700])}`"
            )


if not TOKEN:
    raise SystemExit("Galaxy Bot cannot start: DISCORD_TOKEN is missing. Add DISCORD_TOKEN=your_bot_token to the host environment or .env file, then restart.")

bot.run(TOKEN)
