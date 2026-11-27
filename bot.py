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
DEFAULT_MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "1574938206107869296"))
# Staff-strike logs use the permanent channel unless test mode is enabled.
STRIKE_LOG_CHANNEL_ID = 1549959193614753852
# Roles applied based on active staff-strike count (expire with the strikes after 15 days).
STAFF_STRIKE_ONE_ROLE_ID = 1549926282022879244
STAFF_STRIKE_TWO_ROLE_ID = 1549926314998370344
STAFF_STRIKE_THREE_ROLE_ID = 1549926355305758850
STAFF_STRIKE_LEVEL_ROLE_IDS = (
    STAFF_STRIKE_ONE_ROLE_ID,
    STAFF_STRIKE_TWO_ROLE_ID,
    STAFF_STRIKE_THREE_ROLE_ID,
)
STAFF_STRIKE_EXPIRE_DAYS = 15
GIVEAWAY_BOT_ID = 530082442967646230
GIVEAWAY_PING_ROLE_ID = 1549926161054695514
# Staff app form bot posts into this channel — ping managers when it does
STAFF_APP_CHANNEL_ID = 1560306013238857728
STAFF_APP_BOT_USER_ID = 853327905357561948
STAFF_APP_PING_ROLE_ID = 1568324149791694878
MAX_SQLITE_INT = (1 << 63) - 1
JOIN_WINDOW_SECONDS = int(os.getenv("JOIN_WINDOW_SECONDS", "20"))
JOIN_BURST_LIMIT = int(os.getenv("JOIN_BURST_LIMIT", "6"))
GALAXY_GAME_CHANNEL_ID = 1574936058121027604
GALAXY_GAME_INTERVAL_SECONDS = 30 * 60  # New riddle every 30 minutes.
# Secret answer DMs for staff oversight (not posted publicly).
RIDDLE_ANSWER_DM_USER_ID = 1532561566271017009
# Chance (0.0–1.0) that a scheduled/staff riddle pings the riddle role ("dead chat" ping).
RIDDLE_PING_CHANCE = 0.45
# On join: briefly mention the new member in these channels, then delete the pings.
# Labels are shown in the welcome DM so members know where they were notified.
JOIN_GHOST_PING_CHANNELS = (
    (1549919146949611610, "roles"),
    (1553154365362278512, "our-ad"),
    (1549949873468604438, "chat"),
)
JOIN_GHOST_PING_CHANNEL_IDS = tuple(cid for cid, _ in JOIN_GHOST_PING_CHANNELS)
# Seconds the join ping stays up before it is deleted (so it isn't an instant ghost ping).
JOIN_GHOST_PING_DELETE_DELAY = 2.0
GALAXY_QUESTION_REWARD_MIN = 50
GALAXY_QUESTION_REWARD_MAX = 250
GALAXY_LEADERBOARD_CHANNEL_ID = 1549950104516165682
GALAXY_LEADERBOARD_INTERVAL_SECONDS = 3 * 60 * 60  # Every 3 hours.

# All 20 staff role IDs supplied in the screenshots.
ROLE_IDS = {
    "main_founder": 1575010839700967465,
    "founder": 1574537379803431036,
    "co_owner": 1550545604839735416,
    "dev": 1561622770855706705,
    "axelle": 1571483535565389965,
    "drc": 1564021026839334963,
    "senior_drc": 1550583960512241785,
    "trial_director": 1568319372764647424,
    "head_director": 1575238899922374757,
    "head_manager": 1561457371878916246,
    "assistant_head_manager": 1562142313105330247,
    "manager": 1564013035478188173,
    "staff_manager": 1549921109137424446,
    "server_manager": 1575762674858328144,
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
        ROLE_IDS["staff_manager"], ROLE_IDS["server_manager"], ROLE_IDS["manager"],
        ROLE_IDS["assistant_head_manager"], ROLE_IDS["head_manager"],
        ROLE_IDS["trial_director"], ROLE_IDS["head_director"],
        ROLE_IDS["senior_drc"], ROLE_IDS["drc"], ROLE_IDS["axelle"], ROLE_IDS["dev"],
    },
    6: {ROLE_IDS["main_founder"], ROLE_IDS["founder"], ROLE_IDS["co_owner"]},
}
PERM1_ROLE_IDS = set().union(*PERM_ROLE_IDS.values())
ROLE_RANK = {role_id: level for level, role_ids in PERM_ROLE_IDS.items() for role_id in role_ids}

# Members with this role can use Galaxy Coins, +avatar, and +userinfo only.
MEMBER_ROLE_ID = 1549925555736936468
MEMBER_ALLOWED_COMMANDS = {
    "balance", "bal", "coins", "leaderboard", "topcoins", "richest",
    "daily", "gamble", "coinflip", "cf", "slots", "dice", "roll", "givecoins", "paycoins",
    "tipcoins", "tradecoins", "trade", "avatar", "userinfo", "whois", "riddle",
    "snipe", "editsnipe", "poll", "remind", "remindme", "botinfo", "uptime",
    "membercount", "mc", "help", "ping",
    "poker", "pokerduel", "1v1",
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
# Staff (any role from +perms / the staff ladder) may post discord.gg invites
# ONLY in these channels. Everywhere else invites are blocked for everyone.
PARTNERSHIP_INVITE_CHANNEL_IDS = {
    1552650783390965910,  # #partnerships
}

# Blacklisted-word escalation is counted over a rolling 30-day period.
# Messages are never deleted; 1-3: warn; 4: 30m; 5: 1h; 6: 6h; 7: 12h; 8+: 24h.
WORD_TIMEOUT_MINUTES = {4: 30, 5: 60, 6: 360, 7: 720}
WORD_STRIKE_WINDOW_DAYS = 30

# Restricted commands: ban = Perm 4+, kick = Perm 3+, blacklist = Perm 6.
BAN_ROLE_IDS = set().union(PERM_ROLE_IDS[4], PERM_ROLE_IDS[5], PERM_ROLE_IDS[6])
KICK_ROLE_IDS = set().union(PERM_ROLE_IDS[3], PERM_ROLE_IDS[4], PERM_ROLE_IDS[5], PERM_ROLE_IDS[6])
BLACKLIST_ROLE_IDS = set(PERM_ROLE_IDS[6])
ADMIN_ACTION_ROLE_IDS = set(KICK_ROLE_IDS)
OWNER_ACTION_ROLE_IDS = set(BLACKLIST_ROLE_IDS)


def effective_staff_rank(member):
    """Special users bypass the staff role ladder and receive all command ranks."""
    rank = max((ROLE_RANK.get(role.id, 0) for role in getattr(member, "roles", [])), default=0)
    if getattr(member, "id", None) in SPECIAL_USERS:
        rank = 6
    return rank


def has_staff_rank(member, minimum_rank):
    return effective_staff_rank(member) >= minimum_rank


def require_staff_rank(minimum_rank):
    async def predicate(ctx):
        # Special users, server owners, and Discord administrators bypass the
        # role ladder. Everyone else must meet the exact rank.
        return (
            ctx.author.id in SPECIAL_USERS
            or ctx.author.id == ctx.guild.owner_id
            or ctx.author.guild_permissions.administrator
            or has_staff_rank(ctx.author, minimum_rank)
        )
    return commands.check(predicate)


def require_founder_or_coowner():
    async def predicate(ctx):
        return (ctx.author.id in SPECIAL_USERS or ctx.author.id == ctx.guild.owner_id
                or ctx.author.guild_permissions.administrator
                or has_any_role(ctx.author, OWNER_ACTION_ROLE_IDS))
    return commands.check(predicate)

# Staff strikes remain available to the higher staff group.
STAFF_STRIKE_ROLE_IDS = set(PERM_ROLE_IDS[5]) | set(PERM_ROLE_IDS[6])

# ---------------------------------------------------------------------------
# +riddles / +riddle (starting a round): Assistant Staff Manager and above ONLY.
# Discord "Administrator" permission, server ownership, and lower staff roles
# (Head Admin and below) do NOT grant access.
# ---------------------------------------------------------------------------
RIDDLE_START_ROLE_IDS = {
    ROLE_IDS["assistant_staff_manager"], ROLE_IDS["staff_manager"],
    ROLE_IDS["server_manager"], ROLE_IDS["manager"],
    ROLE_IDS["assistant_head_manager"], ROLE_IDS["head_manager"],
    ROLE_IDS["trial_director"], ROLE_IDS["head_director"],
    ROLE_IDS["senior_drc"], ROLE_IDS["drc"], ROLE_IDS["axelle"], ROLE_IDS["dev"],
    ROLE_IDS["main_founder"], ROLE_IDS["founder"], ROLE_IDS["co_owner"],
}

# ---------------------------------------------------------------------------
# No-ping protection: these users cannot be pinged except by Director and above.
# Discord "Administrator" permission does NOT bypass this.
# ---------------------------------------------------------------------------
PROTECTED_PING_USER_IDS = {
    1464807318637121618,
    1566616591267930112,
    841710745897009212,
    1244774065231761458,
    1532561566271017009,
    1400805489889120286,
    1504581544042434731,
}
PING_BYPASS_ROLE_IDS = {
    ROLE_IDS["trial_director"],
    ROLE_IDS["senior_drc"], ROLE_IDS["drc"], ROLE_IDS["axelle"], ROLE_IDS["dev"],
    ROLE_IDS["main_founder"], ROLE_IDS["founder"], ROLE_IDS["co_owner"],
}
USER_MENTION_PATTERN = re.compile(r"<@!?(\d{15,22})>")


def can_start_riddles(member):
    """Assistant Staff Manager+ by role only (no Administrator-permission bypass)."""
    return bool(member_role_ids(member) & RIDDLE_START_ROLE_IDS)


def can_ping_protected_users(member):
    """Director+ by role only. Protected users themselves may ping each other."""
    if getattr(member, "id", None) in PROTECTED_PING_USER_IDS:
        return True
    return bool(member_role_ids(member) & PING_BYPASS_ROLE_IDS)


def protected_users_pinged(message):
    """IDs of protected users pinged by this message (direct mention or reply ping)."""
    pinged = {u.id for u in message.mentions if u.id in PROTECTED_PING_USER_IDS}
    for raw_id in USER_MENTION_PATTERN.findall(message.content or ""):
        if int(raw_id) in PROTECTED_PING_USER_IDS:
            pinged.add(int(raw_id))
    return pinged


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
intents.dm_messages = True
intents.guild_messages = True
intents.messages = True

# Galaxy visual identity: midnight plum with a quiet lavender accent.
GALAXY_COLOR = discord.Color.from_rgb(48, 39, 65)
GALAXY_ACCENT = discord.Color.from_rgb(111, 66, 193)
# Staff records keep the Galaxy base theme, with restrained status colors.
STRIKE_ISSUED_COLOR = discord.Color.from_rgb(224, 145, 65)
STRIKE_REMOVED_COLOR = discord.Color.from_rgb(76, 166, 112)
SUCCESS_COLOR = discord.Color.from_rgb(76, 166, 112)
WARN_COLOR = discord.Color.from_rgb(224, 145, 65)
ERROR_COLOR = discord.Color.from_rgb(190, 80, 90)


def _guess_panel_style(text):
    """Pick a soft title + color from the message so panels feel consistent."""
    t = (text or "").strip()
    low = t.casefold()
    if any(k in low for k in ("banned", "blacklist", "kicked", "deleted", "removed", "cleared")):
        return "GALAXY  /  MODERATION", WARN_COLOR
    if any(k in low for k in ("muted", "deafened", "timeout", "slowmode", "locked")):
        return "GALAXY  /  MODERATION", WARN_COLOR
    if any(k in low for k in ("unmuted", "undeafen", "unlocked", "unbanned", "✅", "gave ", "added")):
        return "GALAXY  /  DONE", SUCCESS_COLOR
    if any(k in low for k in ("can’t", "can't", "couldn’t", "couldn't", "invalid", "failed", "don’t", "don't", "need")):
        return "GALAXY  /  NOTICE", ERROR_COLOR
    if any(k in low for k in ("won", "reward", "coins", "daily", "balance")):
        return "GALAXY  /  COINS", GALAXY_ACCENT
    if t.startswith("Usage:") or "example" in low:
        return "GALAXY  /  HELP", GALAXY_COLOR
    return "GALAXY", GALAXY_COLOR


# Galaxy Coins icon (star asset shipped next to the bot file)
GALAXY_COIN_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Spacestargalaxy.png")
GALAXY_COIN_ICON_FILENAME = "galaxy_coin.png"


def galaxy_coin_icon_file():
    """Return a discord.File for the Galaxy Coins icon, or None if missing."""
    try:
        if os.path.isfile(GALAXY_COIN_ICON_PATH):
            return discord.File(GALAXY_COIN_ICON_PATH, filename=GALAXY_COIN_ICON_FILENAME)
    except OSError:
        logger.warning("Galaxy coin icon missing at %s", GALAXY_COIN_ICON_PATH)
    return None


def apply_galaxy_coin_icon(embed):
    """Set the Galaxy Coins star as the embed thumbnail when the asset exists.

    Returns a discord.File that MUST be passed as file= on the same send(), or None.
    """
    if embed is None:
        return None
    if os.path.isfile(GALAXY_COIN_ICON_PATH):
        embed.set_thumbnail(url=f"attachment://{GALAXY_COIN_ICON_FILENAME}")
        return galaxy_coin_icon_file()
    return None


def build_galaxy_embed(content=None, *, title=None, color=None, footer="GALAXY  ·  COMMUNITY", coin_icon=False):
    """Build a neat standard panel. Used by ctx.send and log helpers."""
    text = None if content is None else str(content).strip()
    if title is None or color is None:
        guessed_title, guessed_color = _guess_panel_style(text or "")
        title = title or guessed_title
        color = color or guessed_color
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    if text:
        embed.description = text
    if footer:
        embed.set_footer(text=footer)
    if coin_icon:
        apply_galaxy_coin_icon(embed)
    return embed


def build_mod_action_embed(
    *,
    action_title,
    member_display,
    reason,
    moderator,
    case_id=None,
    extra_fields=None,
    color=None,
    thumbnail_url=None,
):
    """Staff action panel — warn / kick / ban / timeout / vc / etc."""
    embed = discord.Embed(
        title=action_title,
        color=color or WARN_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    safe_reason = discord.utils.escape_markdown(
        discord.utils.escape_mentions(str(reason or "No reason provided"))
    )
    embed.add_field(name="Member", value=str(member_display), inline=True)
    embed.add_field(name="Staff", value=str(moderator), inline=True)
    if case_id is not None:
        embed.add_field(name="Case", value=f"`#{case_id}`", inline=True)
    embed.add_field(name="Reason", value=safe_reason[:1024], inline=False)
    if extra_fields:
        for name, value, inline in extra_fields:
            embed.add_field(name=name, value=value, inline=inline)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    embed.set_footer(text="Steal A Galacticrot  ·  moderation")
    return embed


async def send_panel(destination, content=None, *, title=None, color=None, embed=None, **kwargs):
    """Send a Galaxy panel to any channel/messageable (not only ctx)."""
    if destination is None:
        return None
    if embed is None:
        embed = build_galaxy_embed(content, title=title, color=color)
    else:
        if embed.color is None or embed.color.value == 0:
            embed.color = GALAXY_COLOR
        if not embed.footer or not embed.footer.text:
            embed.set_footer(text="GALAXY  ·  COMMUNITY")
        if embed.timestamp is None:
            embed.timestamp = discord.utils.utcnow()
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    return await destination.send(embed=embed, **kwargs)


class GalaxyContext(commands.Context):
    """Every reply goes out as a clean Galaxy panel."""
    async def send(self, content=None, **kwargs):
        embed = kwargs.get("embed")
        if embed is None and content is not None:
            embed = build_galaxy_embed(content)
            kwargs.pop("content", None)
            kwargs["embed"] = embed
            content = None
        if embed is not None:
            if embed.color is None or embed.color.value == 0:
                embed.color = GALAXY_COLOR
            if not embed.footer or not embed.footer.text:
                embed.set_footer(text="GALAXY  ·  COMMUNITY")
            if embed.timestamp is None:
                embed.timestamp = discord.utils.utcnow()
            # plain embeds with only a description still get a title so they look like panels
            if not embed.title and embed.description:
                guessed_title, guessed_color = _guess_panel_style(embed.description)
                embed.title = guessed_title
                if embed.color == GALAXY_COLOR:
                    embed.color = guessed_color
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
BOT_STARTED_AT = time.time()
active_reminders = {}  # user_id -> list of asyncio.Task
# Coins live in sqlite at /data/raiders.sqlite3 (hardcoded for Railway volumes).
# Mount a volume at /data so this file survives deploys. The bot never wipes balances.
import shutil

def _sqlite_coin_total(path):
    """Return total coins stored in a db file, or -1 if the file can't be read."""
    if not path or not os.path.isfile(path):
        return -1
    try:
        conn = sqlite3.connect(path, timeout=5)
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='galaxy_coins'"
            ).fetchone()
            if not row:
                return 0
            total = conn.execute(
                "SELECT COALESCE(SUM(balance), 0) FROM galaxy_coins WHERE balance > 0"
            ).fetchone()[0]
            return int(total or 0)
        finally:
            conn.close()
    except Exception:
        return -1


_script_dir = os.path.dirname(os.path.abspath(__file__))
# Always use /data — create it if the volume/host allows.
DATABASE_PATH = (os.getenv("DATABASE_PATH") or "").strip() or "/data/raiders.sqlite3"
try:
    os.makedirs(os.path.dirname(DATABASE_PATH) or "/data", exist_ok=True)
except Exception:
    # /data not writable (no volume yet) — fall back next to the bot so it still runs
    DATABASE_PATH = os.path.join(_script_dir, "raiders.sqlite3")
    try:
        os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    except Exception:
        pass

# If the chosen DB is empty, pull coins from any older copy we can still see
_candidates = [
    DATABASE_PATH,
    "/data/raiders.sqlite3",
    "/app/data/raiders.sqlite3",
    os.path.join(_script_dir, "raiders.sqlite3"),
    os.path.join(_script_dir, "data", "raiders.sqlite3"),
    "raiders.sqlite3",
]
_best_path, _best_total = None, -1
for path in _candidates:
    total = _sqlite_coin_total(path)
    logger.info("DB candidate %s → coin total %s", path, total)
    if total > _best_total:
        _best_total = total
        _best_path = path

if (
    _best_path
    and _best_total > 0
    and os.path.abspath(_best_path) != os.path.abspath(DATABASE_PATH)
    and _sqlite_coin_total(DATABASE_PATH) <= 0
):
    try:
        os.makedirs(os.path.dirname(DATABASE_PATH) or ".", exist_ok=True)
        shutil.copy2(_best_path, DATABASE_PATH)
        for suffix in ("-wal", "-shm"):
            side = _best_path + suffix
            if os.path.isfile(side):
                shutil.copy2(side, DATABASE_PATH + suffix)
        logger.warning(
            "Restored coin database from %s → %s (%s total coins)",
            _best_path, DATABASE_PATH, _best_total,
        )
    except Exception:
        logger.exception("Failed to restore coin database from %s", _best_path)
        DATABASE_PATH = _best_path

db = sqlite3.connect(DATABASE_PATH, timeout=30, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=FULL")
db.execute("PRAGMA foreign_keys=ON")
db.execute("PRAGMA busy_timeout=30000")
logger.info("Coin/strike database in use: %s", os.path.abspath(DATABASE_PATH))
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
    response_message_id INTEGER,
    audit_message_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
for _col in ("response_message_id INTEGER", "audit_message_id INTEGER"):
    try:
        db.execute(f"ALTER TABLE staff_strikes ADD COLUMN {_col}")
    except sqlite3.OperationalError:
        pass
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
# remembers when the last riddle was posted so restarts don't spam a new one
db.execute("""CREATE TABLE IF NOT EXISTS galaxy_meta (
    guild_id INTEGER PRIMARY KEY,
    last_riddle_at REAL,
    leaderboard_message_id INTEGER
)""")
try:
    db.execute("ALTER TABLE galaxy_meta ADD COLUMN leaderboard_message_id INTEGER")
except sqlite3.OperationalError:
    pass  # column already exists on older DBs
db.commit()

# JSON safety-net for balances. Survives some host mishaps when kept on a volume.
COIN_BACKUP_PATHS = []
for _bp in (
    os.path.join(os.path.dirname(os.path.abspath(DATABASE_PATH)), "galaxy_coins_backup.json"),
    "/data/galaxy_coins_backup.json",
    "/app/data/galaxy_coins_backup.json",
    os.path.join(_script_dir, "galaxy_coins_backup.json"),
):
    if _bp and _bp not in COIN_BACKUP_PATHS:
        COIN_BACKUP_PATHS.append(_bp)


def save_coin_backup():
    """Write all balances to every reachable backup path."""
    try:
        rows = db.execute(
            "SELECT guild_id, user_id, balance, last_daily FROM galaxy_coins WHERE balance > 0"
        ).fetchall()
        payload = {
            "saved_at": time.time(),
            "rows": [
                {
                    "guild_id": int(g),
                    "user_id": int(u),
                    "balance": int(b),
                    "last_daily": ld,
                }
                for g, u, b, ld in rows
            ],
        }
        text = json.dumps(payload)
        for path in COIN_BACKUP_PATHS:
            try:
                folder = os.path.dirname(path)
                if folder:
                    os.makedirs(folder, exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
            except Exception:
                logger.exception("Could not write coin backup to %s", path)
    except Exception:
        logger.exception("save_coin_backup failed")


def restore_coins_from_backup_if_empty():
    """If the live DB has no coins, try to reload from a JSON backup."""
    try:
        live = db.execute(
            "SELECT COALESCE(SUM(balance), 0) FROM galaxy_coins WHERE balance > 0"
        ).fetchone()[0]
        if int(live or 0) > 0:
            return False
        best = None
        best_total = 0
        for path in COIN_BACKUP_PATHS:
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                rows = data.get("rows") or []
                total = sum(int(r.get("balance") or 0) for r in rows)
                if total > best_total:
                    best_total = total
                    best = rows
            except Exception:
                logger.exception("Could not read coin backup %s", path)
        if not best or best_total <= 0:
            return False
        for r in best:
            db.execute(
                "INSERT INTO galaxy_coins(guild_id,user_id,balance,last_daily) VALUES(?,?,?,?) "
                "ON CONFLICT(guild_id,user_id) DO UPDATE SET "
                "balance=CASE WHEN excluded.balance > galaxy_coins.balance "
                "THEN excluded.balance ELSE galaxy_coins.balance END, "
                "last_daily=COALESCE(excluded.last_daily, galaxy_coins.last_daily)",
                (
                    int(r["guild_id"]),
                    int(r["user_id"]),
                    int(r.get("balance") or 0),
                    r.get("last_daily"),
                ),
            )
        db.commit()
        logger.warning(
            "Restored %s coin rows (%s total coins) from JSON backup",
            len(best),
            best_total,
        )
        return True
    except Exception:
        logger.exception("restore_coins_from_backup_if_empty failed")
        return False


restore_coins_from_backup_if_empty()
save_coin_backup()

recent_joins = {}
invite_cache = {}
active_galaxy_questions = {}  # guild_id -> active question dictionary
# Most recently deleted messages, kept separately for each text channel.
# channel_id -> list of deleted message snapshots (newest last)
sniped_messages = {}
# channel_id -> list of edited message snapshots (newest last)
edited_messages = {}
cleared_message_ids = set()
MAX_SNIPE_CHANNELS = 200
MAX_SNIPE_HISTORY = 15  # how many deletions/edits to keep per channel for arrow navigation
# Active temp roles: (guild_id, user_id, role_id) -> asyncio.Task
temp_role_tasks = {}
# Active VC mute/deafen timers: (guild_id, user_id, "mute"|"deafen") -> asyncio.Task
temp_vc_tasks = {}

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



def parse_duration_seconds(raw_value):
    """Parse flexible duration strings like '10', '10m', '1h', '7days', '2w', '30s' into seconds."""
    text = str(raw_value).strip().lower()
    if not text:
        raise ValueError("Empty duration")
    if text.isdigit():
        # Plain numbers default to minutes for timeout/tempmute
        return int(text) * 60
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text)
    if not match:
        raise ValueError(f"Invalid duration: {raw_value}")
    value = float(match.group(1))
    if not (value == value and abs(value) != float("inf")):
        raise ValueError(f"Invalid duration number: {raw_value}")
    unit = match.group(2)
    multipliers = {
        "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
        "w": 604800, "wk": 604800, "wks": 604800, "week": 604800, "weeks": 604800,
    }
    if unit not in multipliers:
        raise ValueError(f"Unknown duration unit: {unit}")
    return int(round(value * multipliers[unit]))


def format_duration_label(total_seconds):
    """Return a clean human-readable duration string."""
    if total_seconds % 86400 == 0:
        days = total_seconds // 86400
        return f"{days} day" if days == 1 else f"{days} days"
    if total_seconds % 3600 == 0:
        hours = total_seconds // 3600
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    if total_seconds % 60 == 0:
        mins = total_seconds // 60
        return f"{mins} minute" if mins == 1 else f"{mins} minutes"
    return f"{total_seconds} seconds"


def parse_vc_duration_arg(duration, reason="No reason provided"):
    """Parse optional VC timer from command args.

    Handles split forms like duration='10' reason='secs spam' -> 10 seconds.
    Returns (seconds_or_None, cleaned_reason).
    """
    if duration is None:
        return None, reason

    reason = reason or "No reason provided"
    candidates = [duration]
    reason_tokens = reason.split() if reason and reason != "No reason provided" else []
    # if they wrote "10 secs" the unit lands in reason — glue it back
    if reason_tokens:
        candidates.insert(0, f"{duration} {reason_tokens[0]}")

    seconds = None
    used_unit_from_reason = False
    for i, cand in enumerate(candidates):
        try:
            seconds = parse_duration_seconds(cand)
            used_unit_from_reason = i == 0 and reason_tokens and cand != duration
            break
        except ValueError:
            continue

    if seconds is None:
        # not a duration at all — treat duration text as part of the reason
        if reason == "No reason provided":
            return None, duration
        return None, f"{duration} {reason}"

    if used_unit_from_reason:
        rest = " ".join(reason_tokens[1:]).strip()
        reason = rest if rest else "No reason provided"

    return seconds, reason


def parse_coin_amount(raw_value, current_balance=None):
    """Parse coin amounts precisely, including huge integers and all/max."""
    text = str(raw_value).strip().lower().replace(",", "")
    if not text:
        raise ValueError("Empty amount")
    if text in {"all", "max"}:
        if current_balance is None:
            raise ValueError("Cannot use all/max here")
        return clamp_coins(current_balance)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmb]?)", text)
    if not match:
        raise ValueError(f"Invalid amount: {raw_value}")
    from decimal import Decimal, InvalidOperation
    try:
        num = Decimal(match.group(1))
    except InvalidOperation:
        raise ValueError(f"Invalid amount number: {raw_value}")
    if not num.is_finite() or num < 0:
        raise ValueError(f"Invalid amount number: {raw_value}")
    mult = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}[match.group(2)]
    # SQLite INTEGER is signed 64-bit. Saturate rather than overflowing.
    return clamp_coins(int(num * mult))

def clamp_coins(value):
    return max(0, min(MAX_SQLITE_INT, int(value)))

def get_coin_balance(guild_id, user_id):
    row = db.execute("SELECT balance FROM galaxy_coins WHERE guild_id=? AND user_id=?", (guild_id, user_id)).fetchone()
    return int(row[0]) if row else 0

def change_coins(guild_id, user_id, amount):
    current = get_coin_balance(guild_id, user_id)
    new_balance = clamp_coins(current + int(amount))
    db.execute(
        "INSERT INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,?) "
        "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
        (guild_id, user_id, new_balance),
    )
    db.commit()
    save_coin_backup()

def set_coin_balance(guild_id, user_id, amount):
    clamped = clamp_coins(amount)
    db.execute(
        "INSERT INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,?) "
        "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
        (guild_id, user_id, clamped),
    )
    db.commit()
    save_coin_backup()


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
        save_coin_backup()
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
        rec_row = db.execute(
            "SELECT balance FROM galaxy_coins WHERE guild_id=? AND user_id=?",
            (guild_id, recipient_id),
        ).fetchone()
        rec_current = int(rec_row[0]) if rec_row else 0
        rec_new = clamp_coins(rec_current + int(amount))
        db.execute(
            "INSERT INTO galaxy_coins(guild_id,user_id,balance) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
            (guild_id, recipient_id, rec_new),
        )
        db.commit()
        save_coin_backup()
        return True, get_coin_balance(guild_id, sender_id)
    except Exception:
        db.rollback()
        raise

GALAXY_QUESTIONS = [
    # EASY
    {"difficulty": "Easy", "question": "What has keys but cannot open locks?", "answer": "piano", "hints": ["It makes music.", "It can be grand or upright.", "It usually has black and white keys."]},
    {"difficulty": "Easy", "question": "What gets wetter the more it dries?", "answer": "towel", "hints": ["You use it after washing.", "It absorbs water.", "You might hang it in a bathroom."]},
    {"difficulty": "Easy", "question": "What has a face and two hands but no arms or legs?", "answer": "clock", "hints": ["It helps you stay on schedule.", "It can hang on a wall.", "Its hands point to numbers."]},
    {"difficulty": "Easy", "question": "What can travel around the world while staying in one corner?", "answer": "stamp", "hints": ["It is used with mail.", "It goes on an envelope.", "It shows that postage was paid."]},
    {"difficulty": "Easy", "question": "What has one eye but cannot see?", "answer": "needle", "hints": ["It is used for sewing.", "Thread passes through it.", "Its eye is a tiny hole."]},
    {"difficulty": "Easy", "question": "What has many teeth but cannot bite?", "answer": "comb", "hints": ["You might keep it in a bathroom.", "It helps fix your hair.", "Its teeth are in a row."]},
    {"difficulty": "Easy", "question": "What goes up but never comes down?", "answer": "age", "hints": ["Everyone has one.", "It changes every birthday.", "It measures how long you have lived."]},
    {"difficulty": "Easy", "question": "What has a neck but no head?", "answer": "bottle", "hints": ["It can hold a drink.", "It may have a cap.", "You pour liquid from it."]},
    {"difficulty": "Easy", "question": "What can you break without touching it?", "answer": "promise", "hints": ["It involves trust.", "You make one to someone.", "You break it by not keeping your word."]},
    {"difficulty": "Easy", "question": "What has cities but no houses, forests but no trees, and water but no fish?", "answer": "map", "hints": ["It helps you find places.", "It can be folded or digital.", "It shows roads and locations."]},
    {"difficulty": "Easy", "question": "What has to be broken before you can use it?", "answer": "egg", "hints": ["It comes from a bird.", "It is often eaten for breakfast.", "You crack its shell to use it."]},
    {"difficulty": "Easy", "question": "What has four legs but cannot walk?", "answer": "table", "hints": ["It is furniture.", "People often eat or work at it.", "It stands on four legs."]},
    {"difficulty": "Easy", "question": "What is full of holes but still holds water?", "answer": "sponge", "hints": ["It is used for cleaning.", "It absorbs liquids.", "It has lots of tiny holes."]},
    {"difficulty": "Easy", "question": "What has a thumb and four fingers but is not alive?", "answer": "glove", "hints": ["You wear it.", "It can keep your hand warm.", "It fits over your fingers."]},
    {"difficulty": "Easy", "question": "What goes around a yard but never moves?", "answer": "fence", "hints": ["It can be made of wood.", "It marks a boundary.", "It surrounds an area of land."]},

    # MEDIUM
    {"difficulty": "Medium", "question": "The more you take, the more you leave behind. What are they?", "answer": "footsteps", "hints": ["You make them while moving.", "They can appear in sand or snow.", "They are marks left by your feet."]},
    {"difficulty": "Medium", "question": "What has words but never speaks?", "answer": "book", "hints": ["It can tell a story.", "You turn its pages.", "You read it, rather than hear it talk."]},
    {"difficulty": "Medium", "question": "What can fill a room but takes up no space?", "answer": "light", "hints": ["It helps you see.", "It can come from a bulb or the sun.", "Darkness disappears when it is present."]},
    {"difficulty": "Medium", "question": "What belongs to you but other people use it more than you do?", "answer": "name", "hints": ["You are given it.", "People use it to get your attention.", "It identifies you."]},
    {"difficulty": "Medium", "question": "What has branches but no leaves, trunk, or fruit?", "answer": "bank", "hints": ["It is not a tree.", "It can be associated with money.", "A river can also have one."]},
    {"difficulty": "Medium", "question": "What can run but never walks, has a mouth but never talks, and has a bed but never sleeps?", "answer": "river", "hints": ["It is found outdoors.", "It flows downhill.", "It has a riverbed and river mouth."]},
    {"difficulty": "Medium", "question": "What disappears as soon as you say its name?", "answer": "silence", "hints": ["It is the absence of sound.", "It can be peaceful.", "Speaking ends it."]},
    {"difficulty": "Medium", "question": "What has a head, a tail, but no body?", "answer": "coin", "hints": ["It is small and metallic.", "It can be flipped.", "It has two sides traditionally called head and tail."]},
    {"difficulty": "Medium", "question": "What can you catch but not throw?", "answer": "cold", "hints": ["It can make you feel unwell.", "It is contagious.", "It is a common illness."]},
    {"difficulty": "Medium", "question": "What has an endless supply of letters but starts empty?", "answer": "mailbox", "hints": ["It is usually outside.", "People put envelopes into it.", "It collects mail."]},
    {"difficulty": "Medium", "question": "What is always in front of you but cannot be seen?", "answer": "future", "hints": ["It has not happened yet.", "It comes after the present.", "It is the time ahead."]},
    {"difficulty": "Medium", "question": "What has many rings but no fingers?", "answer": "telephone", "hints": ["It can make a sound when someone calls.", "Older versions sat on a desk.", "It is used to communicate over distance."]},
    {"difficulty": "Medium", "question": "What has a bottom at the top?", "answer": "leg", "hints": ["It is part of a person.", "It helps you stand and walk.", "Your leg has a bottom near the foot and a top near the hip."]},
    {"difficulty": "Medium", "question": "What gets bigger the more you take away from it?", "answer": "hole", "hints": ["It can be in the ground.", "Removing material can enlarge it.", "It is an empty opening."]},
    {"difficulty": "Medium", "question": "What has 13 hearts but no other organs?", "answer": "deck of cards", "hints": ["It is used in games.", "It has four suits.", "One suit contains thirteen hearts."]},

    # HARD
    {"difficulty": "Hard", "question": "I am not alive, but I grow; I have no lungs, but I need air; I have no mouth, but water kills me. What am I?", "answer": "fire", "hints": ["I can spread if unchecked.", "I give off heat.", "Water usually extinguishes me."]},
    {"difficulty": "Hard", "question": "A man looks at a picture and says, 'Brothers and sisters I have none, but that man's father is my father's son.' Who is in the picture?", "answer": "his son", "hints": ["The speaker has no siblings.", "'My father's son' refers to the speaker.", "The speaker is the picture subject's father."]},
    {"difficulty": "Hard", "question": "What five-letter word becomes shorter when you add two letters to it?", "answer": "short", "hints": ["It describes length.", "Add the letters E and R.", "The result is the comparative word 'shorter'."]},
    {"difficulty": "Hard", "question": "I have keys, space, and enter, but no room. What am I?", "answer": "keyboard", "hints": ["I am part of a computer setup.", "I have buttons called keys.", "You type with me."]},
    {"difficulty": "Hard", "question": "What three numbers, none of them zero, give the same result when multiplied and added?", "answer": "1 2 3", "hints": ["There are three numbers.", "Their sum is six.", "1 × 2 × 3 equals 1 + 2 + 3."]},
    {"difficulty": "Hard", "question": "I shave every day, but my beard stays the same. Who am I?", "answer": "barber", "hints": ["I work with other people's hair.", "I use a razor.", "I shave customers, not myself in this riddle."]},
    {"difficulty": "Hard", "question": "What word is pronounced the same if you remove four of its five letters?", "answer": "queue", "hints": ["It is often formed when people wait.", "It has five letters.", "Remove U, E, U, and E, leaving Q, which sounds like the word."]},
    {"difficulty": "Hard", "question": "Two fathers and two sons go fishing. They catch three fish, and each gets one. How is that possible?", "answer": "three people", "hints": ["There are not four people.", "One person is both a father and a son.", "They are grandfather, father, and son."]},
    {"difficulty": "Hard", "question": "What can be seen once in a minute, twice in a moment, but never in a thousand years?", "answer": "m", "hints": ["Think about spelling, not time itself.", "Look at the words minute and moment.", "The letter M appears once in minute, twice in moment, and not in thousand years."]},
    {"difficulty": "Hard", "question": "I am taken from a mine, shut in a wooden case, and used by almost everyone. What am I?", "answer": "pencil lead", "hints": ["I am a mineral-based material.", "I am encased in wood.", "I write inside pencils; commonly called graphite."]},
    {"difficulty": "Hard", "question": "What number becomes smaller when you turn it upside down?", "answer": "9", "hints": ["It is a single digit.", "It can become another digit when rotated.", "Turn 9 upside down to make 6."]},
    {"difficulty": "Hard", "question": "What can be measured but has no length, width, or height?", "answer": "temperature", "hints": ["It describes how hot or cold something is.", "It is measured with a thermometer.", "It is a physical quantity, not a spatial dimension."]},
    {"difficulty": "Hard", "question": "If you have me, you want to share me. If you share me, you no longer have me. What am I?", "answer": "secret", "hints": ["It is information.", "You may keep it hidden.", "Telling others means it is no longer secret."]},
    {"difficulty": "Hard", "question": "What is so fragile that saying its name breaks it?", "answer": "silence", "hints": ["It can exist in a quiet room.", "A sound interrupts it.", "Speaking breaks it."]},
    {"difficulty": "Hard", "question": "A word has six letters. Remove one and twelve remain. What is it?", "answer": "dozens", "hints": ["Think of a word for a group of twelve.", "It is plural.", "A dozen is twelve; dozens can remain after one is removed."]},
]

# ---------------------------------------------------------------------------
# Riddle system: curated bank + procedural generator with >500,000,000 uniques.
# ---------------------------------------------------------------------------
# Theoretical unique space (seeds 0 .. RIDDLE_UNIVERSE_SIZE-1). Each seed maps
# to one deterministic riddle so the same seed never produces two different
# puzzles, and the pool is far larger than a server will ever exhaust.
RIDDLE_UNIVERSE_SIZE = 500_000_000

_RIDDLE_ANIMALS = (
    "cat", "dog", "bird", "fish", "lion", "tiger", "bear", "wolf", "fox", "owl",
    "eagle", "shark", "whale", "frog", "snake", "mouse", "horse", "cow", "pig",
    "sheep", "goat", "duck", "goose", "crab", "seal", "deer", "moose", "hare",
    "crow", "dove", "ant", "bee", "crab", "eel", "bat", "rat", "hen", "ram",
)
_RIDDLE_OBJECTS = (
    "book", "lamp", "coin", "key", "door", "map", "cup", "box", "ring", "clock",
    "chair", "table", "phone", "pen", "bag", "hat", "shoe", "ball", "rope", "mirror",
    "candle", "bottle", "brush", "comb", "spoon", "fork", "plate", "bowl", "towel",
    "pillow", "blanket", "window", "fence", "bridge", "boat", "train", "plane", "bike",
)
_RIDDLE_PLACES = (
    "forest", "river", "ocean", "mountain", "desert", "cave", "island", "valley",
    "city", "village", "castle", "garden", "library", "kitchen", "basement", "attic",
    "beach", "bridge", "tunnel", "station", "market", "tower", "harbor", "meadow",
)
_RIDDLE_COLORS = (
    "red", "blue", "green", "yellow", "black", "white", "purple", "orange", "pink",
    "brown", "silver", "gold", "gray", "cyan", "magenta", "teal", "navy", "crimson",
)
_RIDDLE_BODY = (
    "hand", "foot", "eye", "ear", "nose", "mouth", "head", "arm", "leg", "finger",
    "thumb", "tooth", "tongue", "heart", "back", "knee", "elbow", "shoulder",
)
_RIDDLE_ACTIONS = (
    "run", "jump", "sleep", "eat", "sing", "dance", "fly", "swim", "hide", "climb",
    "write", "read", "cook", "build", "paint", "count", "listen", "watch", "wait",
)
_RIDDLE_LETTERS = "abcdefghijklmnopqrstuvwxyz"

# Curated deck first; procedural fills forever after.
_galaxy_riddle_deck = []
_galaxy_riddle_last = None
_galaxy_riddle_used_seeds = set()


def _riddle_difficulty_from_seed(seed: int) -> str:
    bucket = seed % 10
    if bucket < 4:
        return "Easy"
    if bucket < 8:
        return "Medium"
    return "Hard"


def _generate_procedural_riddle(seed: int) -> dict:
    """Deterministic riddle for a seed in [0, RIDDLE_UNIVERSE_SIZE)."""
    seed = int(seed) % RIDDLE_UNIVERSE_SIZE
    rng = random.Random(seed)
    family = seed % 16
    difficulty = _riddle_difficulty_from_seed(seed)

    if family == 0:
        # Number: double + add
        answer = rng.randint(2, 80)
        add = rng.randint(1, 40)
        result = answer * 2 + add
        q = f"I am a whole number. Double me and add {add}, and you get {result}. What number am I?"
        hints = [
            f"Start from {result} and reverse the steps.",
            f"Subtract {add}, then divide by 2.",
            f"The answer is between {max(1, answer-5)} and {answer+5}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": str(answer), "hints": hints, "seed": seed}

    if family == 1:
        answer = rng.randint(3, 60)
        mult = rng.choice([3, 4, 5, 6, 7, 8, 9])
        sub = rng.randint(1, 25)
        result = answer * mult - sub
        q = f"I am a whole number. Multiply me by {mult} and subtract {sub}, and you get {result}. What number am I?"
        hints = [
            f"Add {sub} to {result} first.",
            f"Then divide by {mult}.",
            f"The answer is a whole number near {answer}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": str(answer), "hints": hints, "seed": seed}

    if family == 2:
        a = rng.randint(2, 40)
        b = rng.randint(2, 40)
        if a == b:
            b = a + rng.randint(1, 9)
        op = rng.choice(["sum", "difference", "product"])
        if op == "sum":
            result = a + b
            q = f"Two whole numbers add up to {result}. One of them is {a}. What is the other?"
            answer = b
            hints = [f"Take {result} minus {a}.", "It's simple subtraction.", f"Answer is {b}."]
        elif op == "difference":
            hi, lo = max(a, b), min(a, b)
            result = hi - lo
            q = f"The difference between two whole numbers is {result}. The larger number is {hi}. What is the smaller number?"
            answer = lo
            hints = [f"Subtract {result} from {hi}.", "Larger minus difference.", f"Answer is {lo}."]
        else:
            result = a * b
            q = f"Two whole numbers multiply to {result}. One of them is {a}. What is the other?"
            answer = b
            hints = [f"Divide {result} by {a}.", "It's exact division.", f"Answer is {b}."]
        return {"difficulty": difficulty, "question": q, "answer": str(answer), "hints": hints[:3], "seed": seed}

    if family == 3:
        word = rng.choice(_RIDDLE_OBJECTS + _RIDDLE_ANIMALS)
        letter = rng.choice(list(dict.fromkeys(word)))  # letter that appears in the word
        count = word.count(letter)
        q = f"How many times does the letter **{letter.upper()}** appear in the word **{word.upper()}**?"
        hints = [
            f"Spell it out: {'-'.join(word.upper())}.",
            f"Count only the letter {letter.upper()}.",
            f"The total is {count}.",
        ]
        return {"difficulty": "Easy", "question": q, "answer": str(count), "hints": hints, "seed": seed}

    if family == 4:
        word = rng.choice(_RIDDLE_OBJECTS + _RIDDLE_ANIMALS + _RIDDLE_PLACES)
        q = f"How many letters are in the word **{word.upper()}**?"
        answer = len(word)
        hints = [
            f"Count each letter in {word}.",
            "Don't count spaces — there are none.",
            f"The length is {answer}.",
        ]
        return {"difficulty": "Easy", "question": q, "answer": str(answer), "hints": hints, "seed": seed}

    if family == 5:
        n = rng.randint(2, 12)
        item = rng.choice(_RIDDLE_OBJECTS)
        place = rng.choice(_RIDDLE_PLACES)
        q = (
            f"There are {n} {item}s in a {place}. You take away {max(1, n // 2)}. "
            f"How many {item}s do you have?"
        )
        answer = max(1, n // 2)
        hints = [
            "Read carefully — how many did YOU take?",
            f"You took {answer} of them.",
            f"The answer is {answer}, not what's left behind.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": str(answer), "hints": hints, "seed": seed}

    if family == 6:
        color = rng.choice(_RIDDLE_COLORS)
        animal = rng.choice(_RIDDLE_ANIMALS)
        place = rng.choice(_RIDDLE_PLACES)
        # Classic "what color is the ..." logic joke variants
        variants = [
            (
                f"A {color} {animal} is stuck in a pure white {place} with no windows and only one door painted {color}. "
                f"What color is the {animal}?",
                color,
                [f"It was described as a {color} {animal}.", "Don't overthink the room.", f"The answer is {color}."],
            ),
            (
                f"Everything in a house is {color}. The walls, the carpet, the furniture — all {color}. "
                f"What color is the staircase?",
                color,
                ["Everything in the house is that color.", "Including the staircase.", f"It's {color}."],
            ),
        ]
        q, answer, hints = rng.choice(variants)
        return {"difficulty": difficulty, "question": q, "answer": answer, "hints": hints, "seed": seed}

    if family == 7:
        # Sequence next number
        start = rng.randint(1, 20)
        step = rng.randint(2, 9)
        seq = [start + i * step for i in range(4)]
        answer = start + 4 * step
        q = f"What comes next in this sequence: {', '.join(str(x) for x in seq)}, … ?"
        hints = [
            f"Each step adds the same amount.",
            f"The gap between numbers is {step}.",
            f"Next is {answer}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": str(answer), "hints": hints, "seed": seed}

    if family == 8:
        # Reverse spelling
        word = rng.choice(_RIDDLE_OBJECTS + _RIDDLE_ANIMALS)
        q = f"What word is **{word[::-1]}** when spelled backward?"
        hints = [
            "Read the scrambled letters from right to left.",
            f"It starts with **{word[0].upper()}**.",
            f"The word is {word}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": word, "hints": hints, "seed": seed}

    if family == 9:
        a = rng.randint(1, 15)
        b = rng.randint(1, 15)
        c = rng.randint(1, 15)
        result = a + b * c  # order of operations
        q = f"What is {a} + {b} × {c}? (Use normal math order of operations.)"
        hints = [
            "Multiply before you add.",
            f"First compute {b} × {c}.",
            f"The total is {result}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": str(result), "hints": hints, "seed": seed}

    if family == 10:
        body = rng.choice(_RIDDLE_BODY)
        action = rng.choice(_RIDDLE_ACTIONS)
        obj = rng.choice(_RIDDLE_OBJECTS)
        templates = [
            (
                f"I have a {body} but I never {action}. I am related to a {obj}. What am I?",
                obj,
                [f"Think about a {obj}.", f"It can have a '{body}' in a figurative way.", f"Answer: {obj}."],
            ),
            (
                f"You can {action} with me, and I'm often near a {obj}. What am I?",
                obj,
                [f"Common object: {obj}.", "Everyday item.", f"Answer: {obj}."],
            ),
        ]
        q, answer, hints = templates[seed % len(templates)]
        return {"difficulty": difficulty, "question": q, "answer": answer, "hints": hints, "seed": seed}

    if family == 11:
        # Clock / time arithmetic
        h = rng.randint(1, 11)
        m = rng.choice([0, 15, 30, 45])
        add_h = rng.randint(1, 5)
        total_m = h * 60 + m + add_h * 60
        nh, nm = (total_m // 60) % 12, total_m % 60
        if nh == 0:
            nh = 12
        answer = f"{nh}:{nm:02d}"
        q = f"A clock shows {h}:{m:02d}. What time will it show in {add_h} hour{'s' if add_h != 1 else ''}?"
        hints = [
            f"Add {add_h} hours to {h}:{m:02d}.",
            "Stay on a 12-hour clock face.",
            f"Answer format like {answer}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": answer, "hints": hints, "seed": seed}

    if family == 12:
        n = rng.randint(5, 30)
        q = f"I am an odd number. Take away one letter and I become even. What number am I?"
        # Classic is SEVEN -> even; keep classic sometimes, else number-word play is hard
        # Use classic with high probability via seed, else "nine" doesn't work. Stick to seven.
        answer = "seven"
        hints = [
            "Think of the spelling of a number.",
            "Remove the letter 's'.",
            "S-E-V-E-N without S is EVEN.",
        ]
        # vary the prompt slightly so seeds differ
        if seed % 3 == 0:
            q = "Which odd number becomes even when you remove one letter from its spelling?"
        return {"difficulty": "Medium", "question": q, "answer": answer, "hints": hints, "seed": seed}

    if family == 13:
        x = rng.randint(2, 20)
        y = rng.randint(2, 20)
        z = x * y
        q = f"If {x} workers can finish a job in {y} hours, how many worker-hours is the job in total?"
        hints = [
            "Multiply workers by hours.",
            f"{x} × {y}.",
            f"Total is {z}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": str(z), "hints": hints, "seed": seed}

    if family == 14:
        word = rng.choice(_RIDDLE_OBJECTS + _RIDDLE_ANIMALS + _RIDDLE_PLACES)
        first = word[0]
        q = f"I am a common English word for something you know. I start with **{first.upper()}** and I have {len(word)} letters. One correct answer is a {word}. What word am I thinking of?"
        hints = [
            f"It starts with {first.upper()}.",
            f"It has {len(word)} letters.",
            f"The answer is {word}.",
        ]
        return {"difficulty": difficulty, "question": q, "answer": word, "hints": hints, "seed": seed}

    # family 15 — age / relation arithmetic
    child = rng.randint(5, 15)
    parent = child + rng.randint(20, 35)
    q = f"A parent is {parent - child} years older than their child. The child is {child}. How old is the parent?"
    answer = parent
    hints = [
        f"Add {parent - child} to {child}.",
        "Parent age = child age + difference.",
        f"The parent is {parent}.",
    ]
    return {"difficulty": difficulty, "question": q, "answer": str(answer), "hints": hints, "seed": seed}


def _draw_galaxy_riddle():
    """Curated bank first (once per uptime), then the 500M+ procedural universe."""
    global _galaxy_riddle_deck, _galaxy_riddle_last
    # First launch (or after explicit reset): load the hand-crafted bank once.
    if not _galaxy_riddle_deck and _galaxy_riddle_last is None:
        _galaxy_riddle_deck = list(GALAXY_QUESTIONS)
        random.shuffle(_galaxy_riddle_deck)

    if _galaxy_riddle_deck:
        question = _galaxy_riddle_deck.pop()
        _galaxy_riddle_last = question
        return question

    # Curated bank exhausted — generate from the huge procedural space.
    # Mix in a curated riddle occasionally so classics still appear.
    if random.random() < 0.08:
        question = random.choice(GALAXY_QUESTIONS).copy()
        _galaxy_riddle_last = question
        return question

    for _ in range(16):
        seed = random.randrange(RIDDLE_UNIVERSE_SIZE)
        if seed not in _galaxy_riddle_used_seeds:
            break
    else:
        seed = random.randrange(RIDDLE_UNIVERSE_SIZE)
    _galaxy_riddle_used_seeds.add(seed)
    if len(_galaxy_riddle_used_seeds) > 50000:
        # Bound memory; the theoretical pool is still RIDDLE_UNIVERSE_SIZE.
        _galaxy_riddle_used_seeds.clear()
    question = _generate_procedural_riddle(seed)
    _galaxy_riddle_last = question
    return question



async def resolve_member_arg(ctx, raw_value):
    """Resolve a discord.Member from a mention, raw user ID, or display/username string."""
    if isinstance(raw_value, discord.Member):
        return raw_value
    converter = commands.MemberConverter()
    try:
        return await converter.convert(ctx, str(raw_value))
    except commands.BadArgument:
        cleaned = re.sub(r"^[<@!]+|[>]+$", "", str(raw_value).strip())
        if cleaned.isdigit():
            uid = int(cleaned)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            if member is not None:
                return member
            if ctx.guild:
                try:
                    return await ctx.guild.fetch_member(uid)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        raise commands.BadArgument(f"Member '{raw_value}' not found.")


async def resolve_user_arg(ctx, raw_value):
    """Resolve a discord.User (or Member) from a mention, raw user ID, or username."""
    if isinstance(raw_value, (discord.Member, discord.User)):
        return raw_value
    converter = commands.UserConverter()
    try:
        return await converter.convert(ctx, str(raw_value))
    except commands.BadArgument:
        cleaned = re.sub(r"^[<@!]+|[>]+$", "", str(raw_value).strip())
        if cleaned.isdigit():
            uid = int(cleaned)
            member = ctx.guild.get_member(uid) if ctx.guild else None
            if member is not None:
                return member
            user = bot.get_user(uid)
            if user is not None:
                return user
            try:
                return await bot.fetch_user(uid)
            except (discord.NotFound, discord.HTTPException):
                pass
        raise commands.BadArgument(f"User '{raw_value}' not found.")


class FlexibleMember(commands.Converter):
    async def convert(self, ctx, argument):
        return await resolve_member_arg(ctx, argument)


class FlexibleUser(commands.Converter):
    async def convert(self, ctx, argument):
        return await resolve_user_arg(ctx, argument)


def member_role_ids(member):
    return {role.id for role in getattr(member, "roles", [])}


def has_any_role(member, role_ids):
    return bool(member_role_ids(member) & role_ids)


def has_perm1(member):
    return (member.id in SPECIAL_USERS or member.guild_permissions.administrator
            or member.id == member.guild.owner_id or has_staff_rank(member, 1))


def has_ban(member):
    # Special users bypass command permission levels; Discord hierarchy and bot
    # permissions still apply to the actual ban operation.
    return (member.id in SPECIAL_USERS or member.guild_permissions.administrator
            or member.id == member.guild.owner_id or has_staff_rank(member, 4)
            or member.id in BAN_USER_ALLOWLIST)


def has_admin_actions(member):
    return (member.id in SPECIAL_USERS or member.guild_permissions.administrator
            or member.id == member.guild.owner_id or has_staff_rank(member, 3)
            or member.id in KICK_USER_ALLOWLIST)


def has_owner_actions(member):
    return (member.id in SPECIAL_USERS or member.guild_permissions.administrator
            or member.id == member.guild.owner_id or has_any_role(member, BLACKLIST_ROLE_IDS)
            or member.id in BLACKLIST_USER_ALLOWLIST)


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
            emb = text
            if emb.color is None or emb.color.value == 0:
                emb.color = GALAXY_COLOR
            if not emb.footer or not emb.footer.text:
                emb.set_footer(text="GALAXY  ·  STAFF LOG")
            if emb.timestamp is None:
                emb.timestamp = discord.utils.utcnow()
            await channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
        else:
            emb = build_galaxy_embed(
                text,
                title="GALAXY  /  STAFF LOG",
                color=GALAXY_COLOR,
                footer="GALAXY  ·  STAFF LOG",
            )
            await channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
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
        return None
    channel_id = STRIKE_LOG_CHANNEL_ID
    channel = await fetch_guild_text_channel(guild, channel_id)
    if channel is None or not hasattr(channel, "send"):
        logger.warning("Strike logging channel %s is unavailable in guild %s (%s)", channel_id, guild.name, guild.id)
        return None
    try:
        if isinstance(text, discord.Embed):
            emb = text
            if emb.color is None or emb.color.value == 0:
                emb.color = GALAXY_COLOR
            if not emb.footer or not emb.footer.text:
                emb.set_footer(text="GALAXY  ·  STAFF AUDIT")
            if emb.timestamp is None:
                emb.timestamp = discord.utils.utcnow()
            return await channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
        emb = build_galaxy_embed(
            text,
            title="GALAXY  /  STAFF AUDIT",
            color=GALAXY_COLOR,
            footer="GALAXY  ·  STAFF AUDIT",
        )
        return await channel.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        logger.error("Missing permission to send strike logs in channel %s", channel_id)
    except discord.HTTPException as exc:
        logger.exception("Discord rejected strike log in channel %s: %s", channel_id, exc)
    return None


@bot.before_invoke
async def log_command_usage(ctx):
    """Send command receipts to the configured staff log channel only."""
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

    # Dispatch command receipt asynchronously so commands respond instantaneously.
    asyncio.create_task(send_mod_log(ctx.guild, panel))


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
    # Staff strikes expire automatically 15 days after they were issued.
    db.execute(
        f"DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-{STAFF_STRIKE_EXPIRE_DAYS} days')"
    )
    db.commit()
    try:
        await bot.change_presence(
            activity=discord.Game(name="Steal A Galacticrot"),
            status=discord.Status.online,
        )
    except Exception:
        logger.exception("Could not set bot presence")
    logger.info("Logged in as %s (ID %s) | Prefix: %s | Guilds: %s",
                bot.user, getattr(bot.user, "id", "unknown"), PREFIX, len(bot.guilds))
    if not intents.message_content:
        logger.error("Message Content Intent is disabled in code; prefix commands cannot work.")
    logger.info("Prefix commands require Message Content Intent enabled in the Discord Developer Portal.")
    logger.info("Startup diagnostics: discord.py=%s | database=%s | prefix=%r",
                getattr(discord, "__version__", "unknown"), DATABASE_PATH, PREFIX)
    try:
        holders = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM galaxy_coins WHERE balance > 0"
        ).fetchone()
        logger.info(
            "Galaxy coins intact: %s players holding %s total coins (db=%s)",
            int(holders[0] or 0),
            int(holders[1] or 0),
            os.path.abspath(DATABASE_PATH),
        )
    except Exception:
        logger.exception("Could not read galaxy_coins on startup")
    for guild in bot.guilds:
        await refresh_invites(guild)
        # keep strike roles honest after expiry cleanup
        try:
            user_ids = set()
            for row in db.execute(
                "SELECT DISTINCT user_id FROM staff_strikes WHERE guild_id=?",
                (guild.id,),
            ).fetchall():
                user_ids.add(int(row[0]))
            for role_id in STAFF_STRIKE_LEVEL_ROLE_IDS:
                role = guild.get_role(role_id)
                if role is None:
                    continue
                for m in role.members:
                    user_ids.add(m.id)
            for uid in user_ids:
                await sync_staff_strike_roles(guild, uid)
        except Exception:
            logger.exception("Strike role sync on ready failed for guild %s", guild.id)
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
    if not getattr(bot, "auto_code_task", None) or bot.auto_code_task.done():
        bot.auto_code_task = asyncio.create_task(auto_code_scheduler())


@bot.event
async def on_guild_join(guild):
    await refresh_invites(guild)


@bot.event
async def on_invite_create(invite):
    await refresh_invites(invite.guild)


@bot.event
async def on_invite_delete(invite):
    await refresh_invites(invite.guild)


async def enforce_blacklist_ban(guild, user, *, event_label="blacklisted user"):
    """Reapply a blacklist ban while its database entry remains active."""
    row = db.execute(
        "SELECT reason FROM blacklist WHERE guild_id=? AND user_id=?",
        (guild.id, user.id),
    ).fetchone()
    if not row:
        return False
    reason = row[0] or "Blacklisted"
    try:
        await guild.ban(discord.Object(id=user.id), reason=f"Persistent blacklist: {reason}")
    except discord.Forbidden:
        logger.error("Missing Ban Members/role hierarchy to re-ban blacklisted user %s", user.id)
        await send_mod_log(guild, f"⚠️ **Blacklist enforcement failed**\nUser ID: `{user.id}`\nThe bot lacks permission or role hierarchy to ban them again.\nReason: {reason}")
        return False
    except discord.HTTPException:
        logger.exception("Failed to re-ban blacklisted user %s", user.id)
        return False
    try:
        await send_mod_log(guild, f"⛔ **Persistent blacklist enforced**\nUser: {user} (`{user.id}`)\nTrigger: {event_label}\nReason: {reason}")
    except Exception:
        logger.exception("Could not send blacklist enforcement log")
    return True


async def _delete_after_delay(msg, delay, member_id=None):
    """Delete a message after `delay` seconds (runs in the background)."""
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except discord.NotFound:
        pass  # already gone
    except (discord.Forbidden, discord.HTTPException):
        logger.warning("Could not delete join ping %s for member %s", msg.id, member_id)


async def ghost_ping_new_member(member):
    """Mention a new member in key channels so they get notified, then delete
    each ping after JOIN_GHOST_PING_DELETE_DELAY seconds (not instantly).

    Deletes run in the background so the welcome DM isn't held up.
    Returns a list of (channel, label) that were successfully pinged.
    """
    guild = member.guild
    pinged = []
    for channel_id, label in JOIN_GHOST_PING_CHANNELS:
        channel = await fetch_guild_text_channel(guild, channel_id)
        if channel is None or not hasattr(channel, "send"):
            continue
        try:
            msg = await channel.send(
                member.mention,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Could not send join ping in channel %s for member %s",
                channel_id, member.id,
            )
            continue
        pinged.append((channel, label))
        asyncio.create_task(_delete_after_delay(msg, JOIN_GHOST_PING_DELETE_DELAY, member.id))
    return pinged


async def dm_join_channel_guide(member, pinged_channels):
    """DM the new member a polished guide of channels they were notified in."""
    guild = member.guild
    server_name = discord.utils.escape_markdown(guild.name)

    if pinged_channels:
        # channel.mention is clickable in DMs for mutual servers
        channel_block = "\n".join(f"› {channel.mention}" for channel, _label in pinged_channels)
        body = (
            f"Welcome to **{server_name}**.\n\n"
            f"I pinged you in a few important places so you can find them fast:\n\n"
            f"{channel_block}\n\n"
            f"Tap a channel above to jump in — or open them from the server list anytime."
        )
        title = "You were pinged in these channels"
    else:
        body = f"Welcome to **{server_name}**. Glad to have you here."
        title = f"Welcome to {server_name}"

    embed = discord.Embed(
        title=title,
        description=body,
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(
        name=server_name,
        icon_url=guild.icon.url if guild.icon else (bot.user.display_avatar.url if bot.user else None),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(
        name="Quick tips",
        value=(
            f"• Read the rules & pick roles if needed\n"
            f"• Say hi in chat when you're ready\n"
            f"• Prefix is `{PREFIX}` — try `{PREFIX}help`"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Glad you're here  ·  {server_name}")
    try:
        await member.send(embed=embed)
        logger.info("Sent join DM to %s (%s)", member, member.id)
    except discord.Forbidden:
        logger.warning("Could not DM join guide to %s — their DMs are closed or they blocked the bot", member.id)
    except discord.HTTPException:
        logger.exception("Discord error while DMing join guide to %s", member.id)


@bot.event
async def on_member_join(member):
    guild = member.guild

    # A database blacklist remains active until +unbl removes the record.
    if await enforce_blacklist_ban(guild, member, event_label="member rejoined"):
        return

    # Ghost-ping key channels so the member is notified, without leaving messages,
    # then DM them a clean guide of where they were pinged.
    pinged = []
    try:
        pinged = await ghost_ping_new_member(member)
    except Exception:
        logger.exception("Join ping failed for member %s", member.id)
    # DM is always attempted, even if a ping failed.
    try:
        await dm_join_channel_guide(member, pinged)
    except Exception:
        logger.exception("Welcome DM failed for member %s", member.id)

    # Invite tracking only: welcome messages have intentionally been removed.
    inviter_text = await get_inviter_text(guild)
    invite_channel_id = configured_guild_channel_id(
        guild, INVITES_CHANNEL_IDS, DEFAULT_INVITES_CHANNEL_ID
    )
    channel = await fetch_guild_text_channel(guild, invite_channel_id)
    if channel:
        try:
            await send_panel(
                channel,
                f"📨 **Member joined:** {member.mention} (`{member.id}`). {inviter_text}",
                title="GALAXY  /  JOINS",
            )
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
                await send_panel(channel, alert, title="GALAXY  /  ANTIRAID", color=WARN_COLOR)
            except (discord.Forbidden, discord.HTTPException):
                pass
        await send_mod_log(guild, alert)


@bot.event
async def on_member_ban(guild, user):
    """Log bans that happen outside the bot (Discord UI, other bots, etc.)."""
    # Bot-issued bans are already logged by the command; this catches the rest.
    try:
        entry = None
        async for e in guild.audit_logs(limit=6, action=discord.AuditLogAction.ban):
            if e.target and e.target.id == user.id:
                entry = e
                break
        if entry and bot.user and entry.user and entry.user.id == bot.user.id:
            return  # already logged by +ban / +softban / +bl
        who = f"{entry.user} (`{entry.user.id}`)" if entry and entry.user else "Unknown"
        why = entry.reason if entry and entry.reason else "No reason in audit log"
        await send_mod_log(
            guild,
            f"🔨 **Ban recorded (external)**\nUser: {user} (`{user.id}`)\n"
            f"Moderator: {who}\nReason: {why}",
        )
    except (discord.Forbidden, discord.HTTPException):
        await send_mod_log(
            guild,
            f"🔨 **Ban recorded**\nUser: {user} (`{user.id}`)\n"
            "(Could not read audit log — missing View Audit Log permission.)",
        )


@bot.event
async def on_member_unban(guild, user):
    # If a staff member manually removes the ban, immediately reapply it while the
    # blacklist entry is active. +unbl deletes that entry first, so it is exempt.
    await enforce_blacklist_ban(guild, user, event_label="manual unban detected")


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


def _prune_channel_map(store):
    """Keep only the most recently touched channels when the map grows too large."""
    if len(store) <= MAX_SNIPE_CHANNELS:
        return
    overflow = len(store) - MAX_SNIPE_CHANNELS
    for key in list(store.keys())[:overflow]:
        store.pop(key, None)


def remember_deleted_message(message):
    """Remember deleted human messages for +snipe (keeps a short history per channel)."""
    if not message.guild or message.author.bot:
        return
    entry = {
        "content": message.content or "",
        "author_name": str(message.author),
        "author_id": message.author.id,
        "avatar_url": message.author.display_avatar.url,
        "created_at": message.created_at,
        "deleted_at": discord.utils.utcnow(),
        "attachments": [attachment.url for attachment in message.attachments[:4]],
        "channel_id": message.channel.id,
        "message_id": message.id,
    }
    history = sniped_messages.setdefault(message.channel.id, [])
    # Avoid duplicate ids if Discord fires delete more than once
    history[:] = [e for e in history if e.get("message_id") != message.id]
    history.append(entry)
    if len(history) > MAX_SNIPE_HISTORY:
        del history[:-MAX_SNIPE_HISTORY]
    _prune_channel_map(sniped_messages)


def remember_edited_message(before, after):
    """Remember text edits for +editsnipe (keeps a short history per channel)."""
    if not after.guild or after.author.bot:
        return
    before_content = (before.content or "").strip()
    after_content = (after.content or "").strip()
    if before_content == after_content:
        return
    entry = {
        "before": before_content,
        "after": after_content,
        "author_name": str(after.author),
        "author_id": after.author.id,
        "avatar_url": after.author.display_avatar.url,
        "edited_at": discord.utils.utcnow(),
        "message_id": after.id,
        "channel_id": after.channel.id,
        "jump_url": after.jump_url,
    }
    history = edited_messages.setdefault(after.channel.id, [])
    history.append(entry)
    if len(history) > MAX_SNIPE_HISTORY:
        del history[:-MAX_SNIPE_HISTORY]
    _prune_channel_map(edited_messages)


def build_snipe_embed(entry, index, total):
    """Build one page of the snipe browser (index is 0-based, newest is total-1)."""
    content = (entry.get("content") or "").strip() or (
        "*No text content (the message may have contained an attachment or embed).*"
    )
    safe_content = discord.utils.escape_mentions(content)
    if len(safe_content) > 3500:
        safe_content = safe_content[:3497] + "..."
    # Show newest as page 1 when listing from newest-first for the user
    page = total - index  # newest = page 1
    panel = discord.Embed(
        title="GALAXY  /  SNIPE  ·  DELETED",
        description=safe_content,
        color=GALAXY_COLOR,
        timestamp=entry.get("deleted_at") or discord.utils.utcnow(),
    )
    panel.set_author(
        name=entry.get("author_name") or "Unknown",
        icon_url=entry.get("avatar_url") or None,
    )
    panel.add_field(name="Channel", value=f"<#{entry.get('channel_id')}>", inline=True)
    panel.add_field(name="Author ID", value=f"`{entry.get('author_id')}`", inline=True)
    if entry.get("created_at"):
        panel.add_field(
            name="Originally sent",
            value=discord.utils.format_dt(entry["created_at"], style="R"),
            inline=True,
        )
    if entry.get("attachments"):
        panel.add_field(
            name="Attachments",
            value="\n".join(entry["attachments"][:4]),
            inline=False,
        )
    panel.set_footer(
        text=f"GALAXY  ·  SNIPE  ·  {page}/{total}  ·  ◀ older  ·  newer ▶"
    )
    return panel


def build_editsnipe_embed(entry, index, total, *, title="GALAXY  /  EDIT SNIPE", label="EDIT SNIPE"):
    before = entry.get("before") or "*empty*"
    after = entry.get("after") or "*empty*"
    safe_before = discord.utils.escape_mentions(before)
    safe_after = discord.utils.escape_mentions(after)
    if len(safe_before) > 900:
        safe_before = safe_before[:897] + "..."
    if len(safe_after) > 900:
        safe_after = safe_after[:897] + "..."
    page = total - index
    panel = discord.Embed(
        title=title,
        color=GALAXY_COLOR,
        timestamp=entry.get("edited_at") or discord.utils.utcnow(),
    )
    panel.set_author(
        name=entry.get("author_name") or "Unknown",
        icon_url=entry.get("avatar_url") or None,
    )
    panel.add_field(name="Before", value=safe_before, inline=False)
    panel.add_field(name="After", value=safe_after, inline=False)
    panel.add_field(name="Channel", value=f"<#{entry.get('channel_id')}>", inline=True)
    panel.add_field(name="Author ID", value=f"`{entry.get('author_id')}`", inline=True)
    if entry.get("jump_url"):
        panel.add_field(name="Jump", value=f"[Open message]({entry['jump_url']})", inline=True)
    panel.set_footer(
        text=f"GALAXY  ·  {label}  ·  {page}/{total}  ·  ◀ older  ·  newer ▶"
    )
    return panel


class SnipeBrowser(discord.ui.View):
    """◀ / ▶ through deleted or edited message history in a channel."""

    def __init__(self, entries, *, kind="snipe", author_id=None, timeout=120):
        super().__init__(timeout=timeout)
        # entries: oldest -> newest; start on newest
        self.entries = list(entries)
        self.index = len(self.entries) - 1
        self.kind = kind
        self.author_id = author_id
        self.message = None
        self._sync_buttons()

    def _embed(self):
        entry = self.entries[self.index]
        total = len(self.entries)
        if self.kind == "editsnipe":
            return build_editsnipe_embed(entry, self.index, total)
        if "before" in entry:  # an edited message mixed into the +snipe history
            return build_editsnipe_embed(
                entry, self.index, total,
                title="GALAXY  /  SNIPE  ·  EDITED", label="SNIPE",
            )
        return build_snipe_embed(entry, self.index, total)

    def _sync_buttons(self):
        # index 0 = oldest, last = newest
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            if child.custom_id == "snipe_older":
                child.disabled = self.index <= 0
            elif child.custom_id == "snipe_newer":
                child.disabled = self.index >= len(self.entries) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is None or interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message(
            "Only the person who ran the command can flip through these pages.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary, custom_id="snipe_older")
    async def older(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary, custom_id="snipe_newer")
    async def newer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.index < len(self.entries) - 1:
            self.index += 1
        self._sync_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)


def _can_post_invite_links(member, channel_id):
    """Staff on the +perms ladder may post discord.gg invites only in partnership channels."""
    if member is None:
        return False
    if channel_id not in PARTNERSHIP_INVITE_CHANNEL_IDS:
        return False
    return (
        member.id in SPECIAL_USERS
        or member.id == getattr(member.guild, "owner_id", None)
        or getattr(member.guild_permissions, "administrator", False)
        or has_staff_rank(member, 1)  # any role listed in +perms
    )


async def filter_bad_content(message):
    """Flag blocked content without deleting messages; record strikes and log."""
    if not message.guild or message.author.bot or not (message.content or "").strip():
        return False

    member = message.guild.get_member(message.author.id)
    if member is None:
        try:
            member = await message.guild.fetch_member(message.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None

    content = message.content
    lowered = content.lower()

    # discord.gg invites: staff (perms roles) may post them ONLY in #partnerships.
    # Everywhere else — including for high staff — invites are blocked.
    link_match = DISCORD_GG_INVITE_PATTERN.search(content)
    if link_match:
        if _can_post_invite_links(member, message.channel.id):
            # Allowed partnership post — do not flag.
            return False
        record_sanction(
            message.guild.id, message.author.id, bot.user.id if bot.user else 0,
            "Blacklisted Link", "Discord invite links are not allowed",
            details=f"Matched: {link_match.group(0)} | Original message: {content}",
        )
        try:
            where = "in this channel"
            if PARTNERSHIP_INVITE_CHANNEL_IDS:
                ch_mentions = ", ".join(f"<#{cid}>" for cid in PARTNERSHIP_INVITE_CHANNEL_IDS)
                where = f"here — staff can only post invites in {ch_mentions}"
            notice = discord.Embed(
                title="GALAXY  /  LINK FILTER",
                description=(
                    f"{message.author.mention} Discord invite links (`discord.gg`) are not allowed {where}."
                ),
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

    # Word filter: Perm 5+ staff remain exempt.
    if member and has_staff_rank(member, 5):
        return False

    matched = next((word for word in sorted(BLACKLISTED_WORDS, key=len, reverse=True)
                    if contains_blocked_phrase(lowered, word)), None)
    if not matched:
        return False

    # Messages are never deleted by this bot. The violation is recorded and handled without deletion.

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
                f"{message.author.mention} your message was flagged for a blacklisted word.\n"
                f"The message was not deleted. {action_text}"
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
    """Return True for configured staff, special users, the server owner, or administrators."""
    return bool(
        member
        and (
            getattr(member, "id", None) in SPECIAL_USERS
            or (
                getattr(member, "guild_permissions", None)
                and member.guild_permissions.administrator
            )
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


def _giveaway_message_text(message):
    """Flatten GiveawayBot message content + embeds into one string."""
    parts = [message.content or ""]
    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(field.name or "")
            parts.append(field.value or "")
        if embed.footer and embed.footer.text:
            parts.append(embed.footer.text)
        if embed.author and embed.author.name:
            parts.append(embed.author.name)
    return "\n".join(parts)


def is_giveaway_announcement(message):
    if message.author.id != GIVEAWAY_BOT_ID:
        return False
    text = _giveaway_message_text(message).lower()
    if not text.strip():
        return False
    # Ignore giveaway winner/end/reroll announcements
    if "congratulations" in text or "won the" in text or "has ended" in text:
        return False
    return any(marker in text for marker in ("click 🎉", "button to enter", "hosted by:", "ends:", "winners:"))


def is_giveaway_winner_announcement(message):
    """True when GiveawayBot posts a winner / end message."""
    if message.author.id != GIVEAWAY_BOT_ID:
        return False
    text = _giveaway_message_text(message).lower()
    if not text.strip():
        return False
    return (
        "congratulations" in text
        or "won the" in text
        or ("winner" in text and ("giveaway" in text or "prize" in text))
    )


# Channel/message cache: recent giveaway prizes that look like Galaxy Coins.
# Keyed by channel_id -> list of (message_id, amount, raw_prize, expires_at)
_giveaway_coin_prizes = {}
_GIVEAWAY_PRIZE_TTL = 14 * 24 * 3600  # keep for 14 days


def parse_galaxy_coin_amount(text: str):
    """Extract a Galaxy Coin prize amount from giveaway title/description text.

    Accepts forms like:
      1000 galaxy coins / 1,000 GC / 500 coins / **250 galaxy coin**
    Returns int amount or None if this does not look like a coin giveaway.
    """
    if not text:
        return None
    lowered = text.lower().replace(",", "")
    # Must look coin-related (avoid giving coins for nitro/robux/etc. giveaways)
    coinish = any(
        token in lowered
        for token in (
            "galaxy coin", "galaxy coins", "galaxycoin", "galaxycoins",
            " gc", "gc ", "gc\n", "(gc)", "[gc]",
            "coins", "coin",
        )
    )
    if not coinish:
        return None
    # Prefer explicit "galaxy coin(s)" amounts first
    patterns = [
        r"(\d[\d\s]*)\s*(?:x\s*)?galaxy\s*coins?",
        r"galaxy\s*coins?\s*[:=\-]?\s*(\d[\d\s]*)",
        r"(\d[\d\s]*)\s*(?:x\s*)?gc\b",
        r"\bgc\s*[:=\-]?\s*(\d[\d\s]*)",
        r"(\d[\d\s]*)\s*(?:x\s*)?coins?\b",
        r"\bcoins?\s*[:=\-]?\s*(\d[\d\s]*)",
    ]
    for pat in patterns:
        m = re.search(pat, lowered, flags=re.IGNORECASE)
        if m:
            digits = re.sub(r"\D", "", m.group(1))
            if digits:
                try:
                    amount = int(digits)
                except ValueError:
                    continue
                if 1 <= amount <= MAX_SQLITE_INT:
                    return amount
    return None


def remember_giveaway_coin_prize(message, amount: int, raw_prize: str = ""):
    channel_id = message.channel.id
    now = time.time()
    bucket = _giveaway_coin_prizes.setdefault(channel_id, [])
    # drop expired
    bucket[:] = [row for row in bucket if row[3] > now]
    bucket.append((message.id, int(amount), raw_prize[:200], now + _GIVEAWAY_PRIZE_TTL))
    # keep last 30 per channel
    if len(bucket) > 30:
        del bucket[:-30]


def lookup_giveaway_coin_prize(message, text: str):
    """Find coin amount from this winner message or a recent start in the same channel."""
    amount = parse_galaxy_coin_amount(text)
    if amount:
        return amount
    now = time.time()
    bucket = _giveaway_coin_prizes.get(message.channel.id) or []
    # newest first
    for _mid, amt, _raw, exp in reversed(bucket):
        if exp >= now:
            return int(amt)
    return None


def extract_giveaway_winner_ids(message):
    """Winner user IDs from mentions (excluding the giveaway bot itself)."""
    ids = []
    seen = set()
    for user in message.mentions:
        if user.bot:
            continue
        if user.id == GIVEAWAY_BOT_ID:
            continue
        if user.id not in seen:
            seen.add(user.id)
            ids.append(user.id)
    # Also scan raw mention tokens in case mentions weren't resolved
    text = _giveaway_message_text(message)
    for m in re.finditer(r"<@!?(\d+)>", text):
        uid = int(m.group(1))
        if uid == GIVEAWAY_BOT_ID or uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
    return ids


async def handle_giveaway_coin_winners(message):
    """If this is a Galaxy Coin giveaway win, credit each winner the stated amount."""
    if not message.guild or message.author.id != GIVEAWAY_BOT_ID:
        return False
    if not is_giveaway_winner_announcement(message):
        return False

    text = _giveaway_message_text(message)
    amount = lookup_giveaway_coin_prize(message, text)
    if not amount:
        return False

    winner_ids = extract_giveaway_winner_ids(message)
    if not winner_ids:
        logger.info("Giveaway winner message had coin prize (%s) but no winner mentions", amount)
        return False

    paid = []
    for uid in winner_ids:
        try:
            change_coins(message.guild.id, uid, amount)
            paid.append(uid)
        except Exception:
            logger.exception("Failed to credit giveaway coins to %s", uid)

    if not paid:
        return False

    mentions = ", ".join(f"<@{uid}>" for uid in paid)
    panel = build_galaxy_embed(
        f"🎉 Giveaway payout complete.\n\n"
        f"**Winners:** {mentions}\n"
        f"**Amount each:** **{amount:,}** Galaxy Coins\n"
        f"Coins have been added to their balances automatically.",
        title="GALAXY  /  GIVEAWAY PAYOUT",
        color=SUCCESS_COLOR,
        footer="GALAXY  ·  GIVEAWAY",
    )
    try:
        coin_file = apply_galaxy_coin_icon(panel)
        if coin_file:
            await message.channel.send(embed=panel, file=coin_file, allowed_mentions=discord.AllowedMentions(users=True))
        else:
            await message.channel.send(embed=panel, allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not post giveaway payout notice")

    await send_mod_log(
        message.guild,
        "🎁 **Galaxy Coin giveaway payout**\n"
        f"Channel: {message.channel.mention}\n"
        f"Amount each: **{amount:,}**\n"
        f"Winners: {', '.join(str(u) for u in paid)}\n"
        f"Source message: {message.jump_url}",
    )
    logger.info(
        "Paid %s galaxy coins to %s winners from giveaway in guild %s",
        amount, len(paid), message.guild.id,
    )
    return True



# ---------------------------------------------------------------------------
# DM chat — short-term memory + context replies so it feels like a real person.
# ---------------------------------------------------------------------------
_dm_memory = {}  # user_id -> {"history": [(role, text), ...], "last_at": float}
_DM_MEMORY_TTL = 20 * 60  # keep context for 20 minutes
_DM_MEMORY_MAX = 8

_DM_GREETING_WORDS = {
    "yo", "yoo", "yooo", "yoooo", "yooooo", "hey", "heyy", "heyyy", "hi", "hii", "hiii",
    "hello", "helloo", "hiya", "sup", "wsp", "wsg", "wyd", "wassup", "whatsup",
    "gm", "gn", "morning", "night", "howdy", "hola", "hai", "helo", "ayo", "ayy",
}
_DM_THANKS_WORDS = {"thanks", "thank", "thx", "ty", "tysm", "appreciate", "appreciated"}
_DM_BYE_WORDS = {"bye", "cya", "goodbye", "later", "gtg", "gnight", "goodnight", "peace", "sayonara"}


def _normalize_dm_text(text: str) -> str:
    text = (text or "").casefold().strip()
    text = re.sub(r"<@!?\d+>", " ", text)
    text = re.sub(r"[!?.~,]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _dm_get_memory(user_id: int):
    now = time.time()
    mem = _dm_memory.get(user_id)
    if not mem or now - mem.get("last_at", 0) > _DM_MEMORY_TTL:
        mem = {"history": [], "last_at": now}
        _dm_memory[user_id] = mem
    mem["last_at"] = now
    return mem


def _dm_push(user_id: int, role: str, text: str):
    mem = _dm_get_memory(user_id)
    mem["history"].append((role, text[:200]))
    if len(mem["history"]) > _DM_MEMORY_MAX:
        mem["history"] = mem["history"][-_DM_MEMORY_MAX:]


def _dm_pick(options):
    return random.choice(options)


def _dm_craft_reply(content: str, low: str, tokens: list, history: list) -> str:
    """Build a reply that reacts to what they actually said."""
    first = tokens[0] if tokens else ""
    joined = " ".join(tokens)
    prev_user = next((t for role, t in reversed(history) if role == "user"), "")
    prev_bot = next((t for role, t in reversed(history) if role == "bot"), "")

    # --- greetings ---
    if low in _DM_GREETING_WORDS or first in _DM_GREETING_WORDS or low.startswith(
        ("yo ", "hey ", "hi ", "hello ", "sup ", "wsp ", "wsg ", "ayo ", "wyd ")
    ):
        # If we already greeted recently, don't restart the same loop
        if prev_bot and any(g in prev_bot.casefold() for g in ("yo", "hey", "sup", "wsp", "what’s good", "whats good")):
            return _dm_pick([
                "yoo you good?",
                "what’s good with you",
                "you need something or just vibing",
                "i’m here, talk to me",
                "sup, what’s on your mind",
            ])
        return _dm_pick([
            "yo what’s good",
            "yoo",
            "hey, what’s up",
            "sup",
            "wsp",
            "yo, how you been",
            "hey hey",
        ])

    # --- how have you been / how are you ---
    if any(p in low for p in (
        "how have you been", "how you been", "how u been", "how are you", "how r u",
        "how are u", "hru", "how you doing", "you good", "you aight", "hows it going",
        "how's it going", "how you holding up",
    )):
        return _dm_pick([
            "i been solid, just handling server stuff. you though?",
            "pretty good ngl, been busy. how you been?",
            "i’m alright — quiet night on my end. you good?",
            "can’t complain. what’s been up with you",
            "been chillin honestly. you good?",
        ])

    # --- wyd / what you doing ---
    if first in {"wyd", "wydrn", "wubu", "wya"} or low in {"what you doing", "what are you doing", "what u doing", "what you up to", "what u up to"}:
        return _dm_pick([
            "just here, watching the server. you?",
            "not much, on standby. wyd",
            "chillin in the background. you bored or what",
            "same old — making sure nobody’s crashing out. you?",
        ])

    # --- "nothing" / "nm" after we asked what's up ---
    if low in {"nothing", "nth", "nm", "not much", "nun", "nun much", "nth much", "idk", "idk tbh", "bored"}:
        return _dm_pick([
            "fair. i’m just here if you need anything",
            "valid. quiet nights hit different",
            "bet. i’m around if something comes up",
            "i feel that. you good though?",
            "real. wanna rant or you just vibing",
        ])

    # --- short casual affirmations ---
    if low in {"ok", "okay", "k", "kk", "alr", "alright", "bet", "fs", "fr", "ong", "yea", "yeah", "yep", "ye", "nah", "no", "yes", "ig", "lol", "lmao", "haha", "ha", "true", "real", "facts", "ong"}:
        return _dm_pick([
            "bet",
            "fs",
            "alr",
            "cool cool",
            "gotchu",
            "fr",
            "yup",
            "aight",
        ])

    # --- "son" / slang address ---
    if low in {"son", "bro", "bruh", "nigga", "nigga what", "my boy", "g", "gang"}:
        return _dm_pick([
            "yoo what’s up",
            "talk to me",
            "go on",
            "i’m listening",
            "wsp",
        ])

    # --- feelings / status ---
    if any(w in tokens for w in ("tired", "exhausted", "sleepy", "drained")):
        return _dm_pick([
            "get some rest then, server ain’t going nowhere",
            "go sleep fr, you earned it",
            "tired is valid. hydrate and log off a bit",
        ])
    if any(w in tokens for w in ("sad", "down", "depressed", "upset", "mad", "angry", "pissed")):
        return _dm_pick([
            "damn, what happened",
            "that’s rough. you wanna talk about it?",
            "i’m here if you need to vent",
            "take a breath. what went down",
        ])
    if any(w in tokens for w in ("happy", "hype", "excited", "lit", "fire")):
        return _dm_pick([
            "that’s what i like to hear",
            "ayy keep that energy",
            "bet, love that for you",
            "let’s gooo",
        ])
    if any(w in tokens for w in ("bored", "boring")):
        return _dm_pick([
            "go touch the riddle channel or gamble some coins 😭",
            "bored? go bother people in the server (nicely)",
            "same tbh. quiet night",
        ])

    # --- thanks / bye ---
    if first in _DM_THANKS_WORDS or any(w in tokens for w in _DM_THANKS_WORDS):
        return _dm_pick(["np", "you got it", "ofc", "anytime", "fs", "don’t even worry about it"])
    if first in _DM_BYE_WORDS or any(w in tokens for w in _DM_BYE_WORDS):
        return _dm_pick(["cya", "later", "peace", "see ya", "aight, take care", "bye bye"])

    # --- help / identity ---
    if "help" in low or "command" in low or "prefix" in low:
        return _dm_pick([
            f"prefix is `{PREFIX}` — hit `{PREFIX}help` and you’ll see everything",
            f"commands live under `{PREFIX}help` if you need the list",
            f"try `{PREFIX}help` real quick",
        ])
    if low in {"who are you", "what are you", "what do you do", "who r u", "who ru"}:
        return _dm_pick([
            "i run moderation + coins + riddles for the server. in dms i’m just chatting",
            "galaxy bot — staff tools mostly. but i can talk out here too",
            f"mod bot for the server. `{PREFIX}help` if you want the command list",
        ])

    # --- questions ---
    if "?" in content:
        if any(w in low for w in ("ban", "kick", "mute", "timeout", "warn", "strike")):
            return _dm_pick([
                "yeah that stuff’s in the server with the right staff perms",
                f"mod commands only work in the server — `{PREFIX}help` shows what you can run",
                "do that in the server, not in dms",
            ])
        if any(w in low for w in ("coin", "daily", "balance", "gamble", "riddle")):
            return _dm_pick([
                f"coins / daily / riddles are all in the server. `{PREFIX}daily` `{PREFIX}bal` `{PREFIX}riddle`",
                "yeah economy is in-server. go claim daily if you haven’t",
            ])
        return _dm_pick([
            "honestly not sure — staff in the server might know better",
            "good question. i don’t have a perfect answer on that one",
            "couldn’t tell you for sure. what made you ask",
            "idk fr. you thinking about something specific?",
        ])

    # --- follow-up if they keep the convo going ---
    if len(history) >= 2:
        return _dm_pick([
            "yeah i hear you",
            "makes sense",
            "okay okay i got you",
            "then what happened",
            "keep going",
            "and then?",
            "real",
            "damn okay",
            "i feel you",
            "say more",
            "aight so what’s next",
        ])

    # --- default: acknowledge, invite more ---
    return _dm_pick([
        "yo i hear you — what’s going on",
        "aight, talk to me",
        "what’s on your mind",
        "i’m listening",
        "go on",
        "bet, tell me more",
        "wsp, you good?",
        "say less… actually say more",
    ])


async def handle_dm_chat(message):
    """Reply in DMs like a real person — short, contextual, with light memory."""
    try:
        if message.author.bot:
            return

        content = (message.content or "").strip()
        if not content:
            try:
                await message.channel.send(_dm_pick(["yo", "what’s that", "haha"]))
            except Exception:
                logger.exception("DM empty reply failed for %s", message.author.id)
            return

        if content.startswith(PREFIX):
            await bot.process_commands(message)
            return

        low = _normalize_dm_text(content)
        tokens = low.split()
        mem = _dm_get_memory(message.author.id)
        _dm_push(message.author.id, "user", content)

        reply = _dm_craft_reply(content, low, tokens, mem["history"])
        _dm_push(message.author.id, "bot", reply)

        # Typing delay scales a bit with reply length so it feels human
        delay = min(2.2, 0.4 + len(reply) * 0.03) + random.uniform(0.15, 0.55)
        await asyncio.sleep(delay * 0.45)
        try:
            async with message.channel.typing():
                await asyncio.sleep(delay * 0.55)
        except Exception:
            await asyncio.sleep(0.25)

        await message.channel.send(reply)
        logger.info("DM reply to %s (%s): %r -> %r", message.author, message.author.id, content[:80], reply)
    except Exception:
        logger.exception("handle_dm_chat failed for user %s", getattr(message.author, "id", "?"))
        try:
            await message.channel.send("yo my bad, something glitched — say that again")
        except Exception:
            pass


@bot.event
async def on_message(message):
    # DMs first — always try to chat back
    if message.guild is None:
        await handle_dm_chat(message)
        return
    if message.author.id == GIVEAWAY_BOT_ID:
        # Winner / end message → auto-pay Galaxy Coins when the prize is coins
        try:
            if await handle_giveaway_coin_winners(message):
                return
        except Exception:
            logger.exception("Giveaway coin payout handler failed")

        if is_giveaway_announcement(message):
            # Remember coin prizes from the start message so the end message can pay out
            start_text = _giveaway_message_text(message)
            coin_amount = parse_galaxy_coin_amount(start_text)
            if coin_amount:
                remember_giveaway_coin_prize(message, coin_amount, start_text[:200])
                logger.info(
                    "Tracked galaxy-coin giveaway prize %s in channel %s",
                    coin_amount, message.channel.id,
                )

            role = message.guild.get_role(GIVEAWAY_PING_ROLE_ID)
            ping_text = role.mention if role else f"<@&{GIVEAWAY_PING_ROLE_ID}>"
            announcement = discord.Embed(
                title="GALAXY  /  NEW GIVEAWAY",
                description=(
                    f"{ping_text} A new giveaway just started above — make sure to enter before time runs out!\n\n"
                    "May the best of luck be with you guys! 🎉"
                ),
                color=GALAXY_COLOR,
                timestamp=discord.utils.utcnow(),
            )
            announcement.set_footer(text="GALAXY  ·  GIVEAWAY ALERTS")
            try:
                await message.channel.send(
                    content=ping_text,
                    embed=announcement,
                    allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to send giveaway ping in channel %s", message.channel.id)
            return

    # staff application form posts into the apps channel — alert the manager team
    if (
        message.channel.id == STAFF_APP_CHANNEL_ID
        and message.author.id == STAFF_APP_BOT_USER_ID
    ):
        ping = f"<@&{STAFF_APP_PING_ROLE_ID}>"
        panel = build_galaxy_embed(
            f"{ping} there's been a new staff application that's been submitted — "
            f"just wanted to let the manager team know.",
            title="GALAXY  /  STAFF APPLICATION",
            color=GALAXY_ACCENT,
            footer="GALAXY  ·  STAFF APPS",
        )
        try:
            await message.channel.send(
                content=ping,
                embed=panel,
                allowed_mentions=discord.AllowedMentions(
                    roles=[discord.Object(id=STAFF_APP_PING_ROLE_ID)],
                    users=False,
                    everyone=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to ping managers for staff application in %s", message.channel.id)
        return

    if message.author.bot:
        return
    # No-ping protection: only Director+ may ping the protected users.
    if not can_ping_protected_users(message.author):
        if protected_users_pinged(message):
            try:
                await message.delete()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.warning("Could not delete protected-ping message %s", message.id)
            panel = build_galaxy_embed(
                f"› {message.author.mention} You can't ping this person. I'm very sorry.",
                title="GALAXY  /  NO PING",
                color=WARN_COLOR,
                footer="GALAXY  ·  PING PROTECTION",
            )
            try:
                await message.channel.send(
                    embed=panel,
                    allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False),
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to send no-ping panel in %s", message.channel.id)
            return
    # Code drops: the first person to type the active code (in any channel) wins coins.
    # Staff +code drops and the bot's automatic drops live in separate slots, so
    # an automatic drop never cancels a staff drop (or the other way around).
    for slot in ("manual", "auto"):
        key = (message.guild.id, slot)
        active_code = active_codes.get(key)
        if not active_code:
            continue
        if time.time() > active_code["expires_at"]:
            active_codes.pop(key, None)
            continue
        # Codes count in ANY channel of the server, not just the one they were dropped in.
        typed = message.content.strip().upper()
        if typed == active_code["code"]:
            # A member can claim this particular code only once, even when its
            # stock allows multiple winners. Reserve the claim before awaiting payout.
            claimed_by = active_code.setdefault("claimed_by", set())
            if message.author.id in claimed_by:
                return
            claimed_by.add(message.author.id)
            active_code["remaining"] = active_code.get("remaining", 1) - 1
            if active_code["remaining"] <= 0:
                active_codes.pop(key, None)
            await _award_code_claim(message, active_code)
            return
        if _strip_invisible(typed) == active_code["code"]:
            # Copy-pasted from the panel (hidden characters came along): not a valid claim.
            try:
                await message.add_reaction("❌")
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                pass
            return
    # Never run content filtering against commands. This keeps command messages
    # (including +sanctions) from being intercepted by the chat-content filter.
    if not message.content.startswith(PREFIX):
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
    # Riddle guesses work either by replying to the active riddle or by typing
    # directly in its channel (scheduled rounds use the configured game channel;
    # staff-started rounds use the channel in which +riddle was invoked).
    active = active_galaxy_questions.get(message.guild.id)
    if (
        active
        and message.channel.id == active.get("channel_id", GALAXY_GAME_CHANNEL_ID)
        and not message.content.startswith(PREFIX)
        and message.content.strip()
    ):
        is_reply_to_riddle = bool(
            message.reference and message.reference.message_id == active.get("message_id")
        )
        if is_reply_to_riddle or active.get("allow_channel_guesses", True):
            await _handle_riddle_guess(message.channel, message.author, message.content, message=message)
            return

    if not message.content.startswith(PREFIX):
        return

    # Replying to a message can supply the target for any command whose
    # callback accepts a member/user/target/user_id parameter. Explicit targets win.
    if message.reference:
        try:
            replied = message.reference.resolved
            if replied is None and message.reference.message_id:
                replied = await message.channel.fetch_message(message.reference.message_id)
            if replied and getattr(replied, "author", None):
                ctx_for_reply = await bot.get_context(message)
                command = ctx_for_reply.command
                allow_bot_target = command is not None and command.name in {"clear", "purge"}
                if command is not None and (not replied.author.bot or allow_bot_target):
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
    member = message.guild.get_member(message.author.id)
    if member is None:
        try:
            member = await message.guild.fetch_member(message.author.id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            member = None
    command_context = await bot.get_context(message)
    if member is not None and not member_command_is_allowed(member, command_context.command):
        return

    row = db.execute(
        "SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?",
        (message.guild.id, message.author.id),
    ).fetchone()
    if row and not message.content.startswith(PREFIX + "unbl"):
        return

    if command_context.command is not None:
        safe_command = discord.utils.escape_mentions(message.content)
        if len(safe_command) > 700:
            safe_command = safe_command[:697] + "..."
        asyncio.create_task(send_mod_log(
            message.guild,
            f"📌 **Command used**\nMember: {message.author} (`{message.author.id}`)"
            f"\nChannel: {message.channel.mention}\nCommand: `{safe_command}`"
        ))
    await bot.process_commands(message)


@bot.event
async def on_message_delete(message):
    """Keep the latest deleted human message per channel, excluding messages cleared via +clear."""
    if message.id in cleared_message_ids:
        cleared_message_ids.discard(message.id)
        return
    remember_deleted_message(message)


@bot.event
async def on_message_edit(before, after):
    """Track the latest text edit per channel for +editsnipe."""
    if after.author.bot or not after.guild:
        return
    remember_edited_message(before, after)


@bot.command(name="snipe")
@commands.guild_only()
async def snipe(ctx):
    """Browse recently deleted AND edited messages in this channel (◀ older / newer ▶).

    Edited messages show the text before and after the edit.
    """
    history = list(sniped_messages.get(ctx.channel.id) or []) + list(edited_messages.get(ctx.channel.id) or [])
    # Oldest -> newest by when the delete/edit happened; the browser opens on the newest.
    history.sort(key=lambda e: e.get("deleted_at") or e.get("edited_at") or discord.utils.utcnow())
    if not history:
        return await ctx.send("There isn’t a recently deleted or edited message saved for this channel.")

    view = SnipeBrowser(history, kind="snipe", author_id=ctx.author.id, timeout=120)
    embed = view._embed()
    # Single entry: still show buttons disabled appropriately via _sync_buttons
    msg = await ctx.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg


@bot.command(name="editsnipe", aliases=["esnipe"])
@commands.guild_only()
async def editsnipe(ctx):
    """Browse recently edited messages in this channel (◀ older / newer ▶)."""
    history = edited_messages.get(ctx.channel.id) or []
    if not history:
        return await ctx.send("There isn’t a recently edited message saved for this channel.")

    view = SnipeBrowser(history, kind="editsnipe", author_id=ctx.author.id, timeout=120)
    embed = view._embed()
    msg = await ctx.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())
    view.message = msg


def _get_last_riddle_at(guild_id):
    row = db.execute(
        "SELECT last_riddle_at FROM galaxy_meta WHERE guild_id=?",
        (guild_id,),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _set_last_riddle_at(guild_id, when=None):
    when = time.time() if when is None else float(when)
    db.execute(
        "INSERT INTO galaxy_meta(guild_id, last_riddle_at) VALUES(?, ?) "
        "ON CONFLICT(guild_id) DO UPDATE SET last_riddle_at=excluded.last_riddle_at",
        (guild_id, when),
    )
    db.commit()


async def _handle_riddle_guess(channel, author, guess_text, message=None):
    """Shared answer checker for reply-guesses and +riddle <answer>."""
    active = active_galaxy_questions.get(channel.guild.id)
    if not active:
        return False
    def _normalize_riddle_answer(value):
        value = (value or "").casefold().strip()
        # Treat common punctuation, separators and spacing as equivalent.
        value = re.sub(r"[^a-z0-9]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    def _strip_simple_inflection(value):
        """Singular/plural / light suffix normalization for short English answers."""
        value = (value or "").strip()
        if len(value) <= 2:
            return value
        for suffix in ("ing", "ed", "ies", "es", "s"):
            if value.endswith(suffix) and len(value) - len(suffix) >= 3:
                stem = value[: -len(suffix)]
                if suffix == "ies":
                    stem = stem + "y"
                return stem
        return value

    def _edit_distance(a, b):
        if a == b:
            return 0
        if not a:
            return len(b)
        if not b:
            return len(a)
        # small strings only — riddle answers are short
        if len(a) > 48 or len(b) > 48:
            return 99
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            curr = [i]
            for j, cb in enumerate(b, 1):
                ins = curr[j - 1] + 1
                delete = prev[j] + 1
                sub = prev[j - 1] + (ca != cb)
                curr.append(min(ins, delete, sub))
            prev = curr
        return prev[-1]

    def _riddle_guess_matches(guess, accepted_set):
        """Lenient match: exact, plural, containment, small typos / extra letters."""
        if not guess:
            return False
        guess_c = re.sub(r"\s+", "", guess)
        accepted_list = [a for a in accepted_set if a]
        accepted_compact = {re.sub(r"\s+", "", a) for a in accepted_list}

        if guess in accepted_set or guess_c in accepted_compact:
            return True

        guess_stem = _strip_simple_inflection(guess_c)
        for a in accepted_list:
            a_c = re.sub(r"\s+", "", a)
            a_stem = _strip_simple_inflection(a_c)
            # glove / gloves / gloving
            if guess_stem and a_stem and guess_stem == a_stem and len(a_stem) >= 3:
                return True
            # extra/missing letters (typos): allow 1 edit for short, 2 for longer
            max_edits = 1 if len(a_c) <= 5 else 2
            if abs(len(guess_c) - len(a_c)) <= max_edits and _edit_distance(guess_c, a_c) <= max_edits:
                return True
            # part of the answer is present (and substantial)
            if len(a_c) >= 4 and len(guess_c) >= 3:
                if a_c in guess_c or guess_c in a_c:
                    # reject tiny accidental substrings of much longer noise
                    if min(len(a_c), len(guess_c)) >= max(3, int(0.6 * max(len(a_c), len(guess_c)))):
                        return True
            # multi-word: all expected tokens appear in the guess (order-free)
            a_tokens = [t for t in a.split() if len(t) > 1]
            g_tokens = set(guess.split())
            if len(a_tokens) >= 2 and all(t in g_tokens or _strip_simple_inflection(t) in {_strip_simple_inflection(x) for x in g_tokens} for t in a_tokens):
                return True
        return False

    cleaned = _normalize_riddle_answer(guess_text)
    expected = _normalize_riddle_answer(active.get("answer", ""))
    # Accept common alternate phrasings / spellings for the riddles in the bank.
    answer_aliases = {
        "stamp": ["postage stamp", "mail stamp"],
        "needle": ["sewing needle"],
        "map": ["a map", "world map"],
        "egg": ["an egg", "eggs"],
        "footsteps": ["steps", "foot prints", "footprints"],
        "book": ["a book"],
        "light": ["sunlight", "lamp light", "lamplight"],
        "name": ["my name", "your name"],
        "bank": ["river bank", "money bank"],
        "river": ["a river"],
        "silence": ["quiet", "being quiet"],
        "coin": ["a coin", "coins"],
        "cold": ["a cold", "common cold"],
        "mailbox": ["postbox", "letterbox"],
        "future": ["the future"],
        "telephone": ["phone", "telephone rings", "a telephone"],
        "leg": ["a leg", "legs"],
        "hole": ["a hole"],
        "deck of cards": ["cards", "a deck of cards", "playing cards", "deck"],
        "fire": ["a fire", "flame", "flames"],
        "his son": ["son", "the son", "his child", "child"],
        "short": ["shorter", "the word short"],
        "keyboard": ["computer keyboard", "a keyboard"],
        "1 2 3": ["123", "1 2 3", "one two three", "one two and three", "1, 2, 3"],
        "barber": ["a barber", "barber shop"],
        "queue": ["q", "the letter q"],
        "three people": ["grandfather father son", "grandfather, father and son", "grandpa dad son", "3 people"],
        "m": ["letter m", "the letter m"],
        "pencil lead": ["graphite", "pencil graphite", "lead", "pencil lead graphite"],
        "9": ["nine", "6 upside down", "six when turned upside down"],
        "temperature": ["heat", "thermometer reading"],
        "secret": ["a secret"],
        "dozens": ["dozen", "12", "twelve"],
        "glove": ["gloves", "a glove"],
        "gloves": ["glove", "a glove"],
    }
    accepted = {_normalize_riddle_answer(expected)}
    accepted.update(_normalize_riddle_answer(x) for x in answer_aliases.get(expected, []))
    # Always allow simple plural/singular of the official answer
    exp_c = re.sub(r"\s+", "", expected)
    if exp_c.endswith("s") and len(exp_c) > 3:
        accepted.add(_normalize_riddle_answer(expected.rstrip("s")))
    else:
        accepted.add(_normalize_riddle_answer(expected + "s"))

    if _riddle_guess_matches(cleaned, accepted):
        reward = random.randint(GALAXY_QUESTION_REWARD_MIN, GALAXY_QUESTION_REWARD_MAX)
        change_coins(channel.guild.id, author.id, reward)
        active_galaxy_questions.pop(channel.guild.id, None)
        result_panel = discord.Embed(
            title="GALAXY  /  CORRECT ANSWER",
            description=(
                f"{author.mention} got it! Random reward: "
                f"**{reward:,} Galaxy Coins**\n"
                f"Balance is now **{get_coin_balance(channel.guild.id, author.id):,}**."
            ),
            color=SUCCESS_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        result_panel.set_footer(text="GALAXY  ·  COMMUNITY GAME")
        coin_file = apply_galaxy_coin_icon(result_panel)
        if coin_file:
            await channel.send(embed=result_panel, file=coin_file, allowed_mentions=discord.AllowedMentions.none())
        else:
            await channel.send(embed=result_panel, allowed_mentions=discord.AllowedMentions.none())
        await send_mod_log(
            channel.guild,
            f"🎁 **Riddle reward**\nWinner: {author} (`{author.id}`)"
            f"\nReward: **{reward:,} Galaxy Coins**\nChannel: <#{channel.id}>",
        )
        return True

    active["wrong_guesses"] = active.get("wrong_guesses", 0) + 1
    hints = active.get("hints", [])
    hints_used = active.get("hints_used", 0)
    hint_limit = min(int(active.get("hint_limit", 3)), len(hints))
    if hints_used < hint_limit:
        hint_number = hints_used + 1
        active["hints_used"] = hint_number
        await send_panel(
            channel,
            f"❌ Nope. **Hint {hint_number}/{hint_limit}:** {hints[hint_number - 1]}",
            title="GALAXY  /  RIDDLE",
        )
    elif message is not None:
        try:
            await message.add_reaction("❌")
        except (discord.Forbidden, discord.HTTPException):
            pass
    else:
        await send_panel(
            channel,
            "❌ Nope — and you're out of hints. Keep trying!",
            title="GALAXY  /  RIDDLE",
        )
    return False


async def _dm_riddle_answer(guild, channel, question, riddle_message=None):
    """DM the configured user the answer whenever a riddle is posted."""
    user_id = RIDDLE_ANSWER_DM_USER_ID
    if not user_id:
        return
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except (discord.NotFound, discord.HTTPException):
        logger.warning("Could not fetch riddle-answer DM user %s", user_id)
        return
    jump = ""
    if riddle_message is not None:
        jump = f"\n[Jump to riddle]({riddle_message.jump_url})"
    elif guild and channel:
        jump = f"\nChannel: {channel.mention} in **{guild.name}**"
    answer = question.get("answer", "???")
    difficulty = question.get("difficulty", "Medium")
    qtext = question.get("question", "???")
    hints = question.get("hints") or []
    hint_lines = "\n".join(f"• {h}" for h in hints[:3]) if hints else "• (none)"
    embed = discord.Embed(
        title="GALAXY  /  RIDDLE ANSWER (STAFF)",
        description=(
            f"**[{difficulty}]** {qtext}\n\n"
            f"**Answer:** ||{discord.utils.escape_markdown(str(answer))}||\n\n"
            f"**Hints:**\n{hint_lines}{jump}"
        ),
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"GALAXY  ·  PRIVATE  ·  guild {getattr(guild, 'id', '?')}")
    try:
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        logger.warning(
            "Could not DM riddle answer to %s — user must share a server and allow DMs from server members",
            user_id,
        )


async def _post_galaxy_riddle(guild, channel, hint_limit=3, ping_role=True):
    RIDDLE_PING_ROLE_ID = 1549925951373058241
    previous = active_galaxy_questions.pop(guild.id, None)
    if previous is not None:
        try:
            await send_panel(channel, f"⌛ Round over. The answer was **{previous['answer']}**.",
                             title="GALAXY  /  RIDDLE")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not post the previous riddle answer.")

    question = _draw_galaxy_riddle()
    hint_limit = max(1, min(int(hint_limit), len(question.get("hints", []))))
    active_galaxy_questions[guild.id] = {
        **question, "hints_used": 0, "wrong_guesses": 0, "message_id": None,
        "channel_id": channel.id, "hint_limit": hint_limit, "allow_channel_guesses": True,
    }
    riddle_msg = None
    try:
        riddle_embed = build_galaxy_embed(
            f"**[{question.get('difficulty', 'Medium')} RIDDLE]**\n**{question['question']}**\n\n"
            f"Guess the riddle for Galaxy Coins!\n"
            f"Reply to this message or type your guess in this channel. Wrong guesses get a hint "
            f"(up to {hint_limit}). First correct answer wins a random "
            f"**{GALAXY_QUESTION_REWARD_MIN}–{GALAXY_QUESTION_REWARD_MAX} Galaxy Coins**!",
            title="GALAXY  /  RIDDLE", color=GALAXY_ACCENT,
            footer="GALAXY  ·  COMMUNITY GAME",
        )
        # Only sometimes ping the riddle / "dead chat" role so it doesn't spam every round.
        do_ping = bool(ping_role) and random.random() < float(RIDDLE_PING_CHANCE)
        content = f"<@&{RIDDLE_PING_ROLE_ID}>" if do_ping else None
        allowed = (
            discord.AllowedMentions(roles=[discord.Object(id=RIDDLE_PING_ROLE_ID)])
            if do_ping
            else discord.AllowedMentions.none()
        )
        riddle_msg = await channel.send(content=content, embed=riddle_embed, allowed_mentions=allowed)
        active_galaxy_questions[guild.id]["message_id"] = riddle_msg.id
        if channel.id == GALAXY_GAME_CHANNEL_ID:
            _set_last_riddle_at(guild.id)
    except (discord.Forbidden, discord.HTTPException):
        active_galaxy_questions.pop(guild.id, None)
        logger.exception("Could not post a new Galaxy riddle.")
        return

    # Always DM the answer to the configured staff user (scheduled + manual +riddle).
    try:
        await _dm_riddle_answer(guild, channel, question, riddle_msg)
    except Exception:
        logger.exception("Riddle answer DM failed")


async def galaxy_game_scheduler():
    # Posts a riddle every 30 minutes.
    # last_riddle_at is saved in sqlite so code updates / restarts do NOT
    # fire a new riddle early — the bot only posts when the full interval is up.
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = time.time()
        sleep_for = GALAXY_GAME_INTERVAL_SECONDS
        for guild in bot.guilds:
            last = _get_last_riddle_at(guild.id)
            if last is None:
                # first boot for this guild — start the 30m clock, don't post yet
                _set_last_riddle_at(guild.id, now)
                remaining = float(GALAXY_GAME_INTERVAL_SECONDS)
                logger.info(
                    "Riddle timer started for guild %s — next post in 30m",
                    guild.id,
                )
            else:
                remaining = float(GALAXY_GAME_INTERVAL_SECONDS) - (now - last)

            if remaining <= 0:
                channel = await fetch_guild_text_channel(guild, GALAXY_GAME_CHANNEL_ID)
                if channel is None or not isinstance(channel, discord.TextChannel):
                    logger.warning(
                        "Riddle channel %s is unavailable in guild %s (%s)",
                        GALAXY_GAME_CHANNEL_ID, guild.name, guild.id
                    )
                    # don't tight-loop if the channel is missing — push the timer forward
                    _set_last_riddle_at(guild.id, now)
                    remaining = float(GALAXY_GAME_INTERVAL_SECONDS)
                else:
                    await _post_galaxy_riddle(guild, channel)
                    remaining = float(GALAXY_GAME_INTERVAL_SECONDS)
            sleep_for = min(sleep_for, max(5.0, remaining))

        await asyncio.sleep(sleep_for)


def _coin_display_name(guild, user_id):
    """Best-effort name for the leaderboard (member nick > username > id)."""
    member = guild.get_member(user_id)
    if member is not None:
        return member.display_name
    user = guild.get_member(user_id) or bot.get_user(user_id)
    if user is not None:
        return getattr(user, "display_name", None) or getattr(user, "name", None) or f"User {user_id}"
    return f"User {user_id}"


def build_coin_leaderboard(guild, viewer_id=None):
    """Top 15 by balance, total economy stats, and the viewer's real rank."""
    rows = db.execute(
        "SELECT user_id, balance FROM galaxy_coins "
        "WHERE guild_id=? AND balance > 0 "
        "ORDER BY balance DESC, user_id ASC LIMIT 15",
        (guild.id,),
    ).fetchall()

    totals = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM galaxy_coins "
        "WHERE guild_id=? AND balance > 0",
        (guild.id,),
    ).fetchone()
    holders = int(totals[0] or 0)
    total_coins = int(totals[1] or 0)

    embed = discord.Embed(
        title="GALAXY  /  COIN LEADERBOARD",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )

    if not rows:
        embed.description = "No one has Galaxy Coins yet.\nClaim `+daily`, win a riddle, or get trading."
    else:
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = []
        for index, (user_id, balance) in enumerate(rows, start=1):
            bal = int(balance or 0)
            name = discord.utils.escape_markdown(_coin_display_name(guild, user_id))
            rank = medals.get(index, f"`#{index}`")
            share = (bal / total_coins * 100) if total_coins > 0 else 0.0
            you = "  ← **you**" if viewer_id and int(user_id) == int(viewer_id) else ""
            lines.append(
                f"{rank} **{name}**{you}\n"
                f"┗ `{bal:,}` coins · `{share:.1f}%` of the economy"
            )
        embed.description = "\n".join(lines)

    # viewer's exact rank if they asked via the command
    if viewer_id is not None:
        viewer_bal = get_coin_balance(guild.id, viewer_id)
        if viewer_bal > 0:
            higher = db.execute(
                "SELECT COUNT(*) FROM galaxy_coins "
                "WHERE guild_id=? AND balance > ?",
                (guild.id, viewer_bal),
            ).fetchone()[0]
            # ties: people with same balance and lower user_id rank above (matches ORDER BY)
            same_ahead = db.execute(
                "SELECT COUNT(*) FROM galaxy_coins "
                "WHERE guild_id=? AND balance=? AND user_id<?",
                (guild.id, viewer_bal, viewer_id),
            ).fetchone()[0]
            rank_num = int(higher or 0) + int(same_ahead or 0) + 1
            embed.add_field(
                name="Your rank",
                value=f"**#{rank_num}** · `{viewer_bal:,}` coins",
                inline=True,
            )
        else:
            embed.add_field(
                name="Your rank",
                value="Unranked · `0` coins",
                inline=True,
            )

    embed.add_field(name="Players", value=f"**{holders:,}** with coins", inline=True)
    embed.add_field(name="Total coins", value=f"**{total_coins:,}**", inline=True)
    embed.set_footer(text="GALAXY  ·  TOP 15  ·  LIVE FROM DATABASE  ·  AUTO EVERY 3H")
    apply_galaxy_coin_icon(embed)
    return embed


async def galaxy_leaderboard_scheduler():
    """Keep one leaderboard message per guild updated instead of spamming new ones."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        for guild in bot.guilds:
            channel = await fetch_guild_text_channel(guild, GALAXY_LEADERBOARD_CHANNEL_ID)
            if channel is None or not isinstance(channel, discord.TextChannel):
                continue
            embed = build_coin_leaderboard(guild)
            row = db.execute(
                "SELECT leaderboard_message_id FROM galaxy_meta WHERE guild_id=?",
                (guild.id,),
            ).fetchone()
            msg_id = int(row[0]) if row and row[0] else None
            sent_or_edited = False
            if msg_id:
                try:
                    existing = await channel.fetch_message(msg_id)
                    await existing.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                    sent_or_edited = True
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    sent_or_edited = False
            if not sent_or_edited:
                try:
                    coin_file = galaxy_coin_icon_file()
                    send_kwargs = {"embed": embed, "allowed_mentions": discord.AllowedMentions.none()}
                    if coin_file:
                        send_kwargs["file"] = coin_file
                    new_msg = await channel.send(**send_kwargs)
                    db.execute(
                        "INSERT INTO galaxy_meta(guild_id, leaderboard_message_id) VALUES(?, ?) "
                        "ON CONFLICT(guild_id) DO UPDATE SET leaderboard_message_id=excluded.leaderboard_message_id",
                        (guild.id, new_msg.id),
                    )
                    db.commit()
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not post coin leaderboard in guild %s", guild.id)
        await asyncio.sleep(GALAXY_LEADERBOARD_INTERVAL_SECONDS)


@bot.command(name="balance", aliases=["bal", "coins"])
async def galaxy_balance(ctx, member: FlexibleMember = None):
    member = member or ctx.author
    balance = get_coin_balance(ctx.guild.id, member.id)
    embed = discord.Embed(
        title="GALAXY  /  BALANCE",
        description=f"{member.mention}\n**{balance:,}** Galaxy Coins",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(member.display_name), icon_url=member.display_avatar.url)
    embed.set_footer(text="GALAXY  ·  COINS")
    coin_file = apply_galaxy_coin_icon(embed)
    if coin_file:
        await ctx.send(embed=embed, file=coin_file)
    else:
        embed.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=embed)

@bot.command(name="tradecoins", aliases=["trade", "givecoins", "paycoins", "tipcoins"])
@commands.cooldown(1, 3, commands.BucketType.user)
async def galaxy_give_coins(ctx, member: FlexibleMember, amount: str):
    """Trade/transfer Galaxy Coins from your balance to another member."""
    if member.bot:
        return await ctx.send("Bots can’t hold or trade coins, so that trade won’t go through.")
    if member.id == ctx.author.id:
        return await ctx.send("You can’t trade coins with yourself.")
    try:
        amount = parse_coin_amount(amount, current_balance=get_coin_balance(ctx.guild.id, ctx.author.id))
    except ValueError:
        return await ctx.send("Enter a valid coin amount (for example: `500`, `2k`, `1.5m`, or `all`).")
    if amount < 1:
        return await ctx.send("Pick an amount of at least **1 Galaxy Coin**.")
    if amount > MAX_SQLITE_INT:
        return await ctx.send("That coin amount is too large.")
    success, sender_balance = transfer_coins(ctx.guild.id, ctx.author.id, member.id, amount)
    if not success:
        return await ctx.send(f"You don’t have enough coins for that trade. Your balance is **{sender_balance:,} Galaxy Coins**.")
    recipient_balance = get_coin_balance(ctx.guild.id, member.id)
    await ctx.send(
        f"🤝 **Coin Trade Complete!** {ctx.author.mention} traded **{amount:,} Galaxy Coins** to {member.mention}.\n"
        f"Your balance: **{sender_balance:,}** · {member.display_name}’s balance: **{recipient_balance:,}**."
    )
    asyncio.create_task(send_mod_log(
        ctx.guild,
        f"🤝 **Galaxy Coins trade**\nFrom: {ctx.author} (`{ctx.author.id}`)\n"
        f"To: {member} (`{member.id}`)\nAmount: **{amount:,}**"
    ))


@bot.command(name="addcoins", aliases=["coinadd"])
@require_staff_rank(5)
async def galaxy_add_coins(ctx, member: FlexibleMember, amount: str, *, reason="Staff adjustment"):
    try:
        amount = parse_coin_amount(amount)
    except ValueError:
        return await ctx.send("Enter a valid coin amount (for example: `500`, `10k`, or `1m`).")
    if amount < 1:
        return await ctx.send("The amount needs to be at least **1**.")
    amount = min(amount, MAX_SQLITE_INT)
    change_coins(ctx.guild.id, member.id, amount)
    await ctx.send(f"➕ Added **{amount:,} Galaxy Coins** to {member.mention}. "
                   f"New balance: **{get_coin_balance(ctx.guild.id, member.id):,}**.")
    asyncio.create_task(send_mod_log(ctx.guild, f"➕ **Galaxy Coins added**\nUser: {member} (`{member.id}`)\n"
                                                f"Amount: **{amount:,}**\nBy: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}"))


@bot.command(name="removecoins", aliases=["coinremove", "takecoins"])
@require_staff_rank(5)
async def galaxy_remove_coins(ctx, member: FlexibleMember, amount: str, *, reason="Staff adjustment"):
    current = get_coin_balance(ctx.guild.id, member.id)
    try:
        amount = parse_coin_amount(amount, current_balance=current)
    except ValueError:
        return await ctx.send("Enter a valid coin amount (for example: `500`, `10k`, or `all`).")
    if amount < 1:
        return await ctx.send("The amount needs to be at least **1**.")
    removed = min(amount, current)
    change_coins(ctx.guild.id, member.id, -removed)
    await ctx.send(f"➖ Removed **{removed:,} Galaxy Coins** from {member.mention}. "
                   f"New balance: **{get_coin_balance(ctx.guild.id, member.id):,}**.")
    asyncio.create_task(send_mod_log(ctx.guild, f"➖ **Galaxy Coins removed**\nUser: {member} (`{member.id}`)\n"
                                                f"Amount: **{removed:,}**\nBy: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}"))


@bot.command(name="setcoins", aliases=["coinset"])
@require_staff_rank(5)
async def galaxy_set_coins(ctx, member: FlexibleMember, amount: str, *, reason="Staff adjustment"):
    try:
        amount = parse_coin_amount(amount)
    except ValueError:
        return await ctx.send("Enter a valid coin amount (for example: `0`, `500`, `10k`, or `1m`).")
    if amount < 0:
        return await ctx.send("I can’t set someone’s balance below zero.")
    amount = min(amount, MAX_SQLITE_INT)
    previous = get_coin_balance(ctx.guild.id, member.id)
    set_coin_balance(ctx.guild.id, member.id, amount)
    await ctx.send(f"⚙️ Set {member.mention}'s balance from **{previous:,}** to **{amount:,}**.")
    asyncio.create_task(send_mod_log(ctx.guild, f"⚙️ **Galaxy Coins balance set**\nUser: {member} (`{member.id}`)\n"
                                                f"Previous: **{previous:,}**\nNew: **{amount:,}**\n"
                                                f"By: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}"))


@bot.command(name="leaderboard", aliases=["topcoins", "richest"])
async def galaxy_coin_leaderboard(ctx):
    embed = build_coin_leaderboard(ctx.guild, viewer_id=ctx.author.id)
    coin_file = galaxy_coin_icon_file()
    if coin_file:
        await ctx.send(embed=embed, file=coin_file)
    else:
        await ctx.send(embed=embed)


@bot.command(name="daily")
@commands.cooldown(1, 5, commands.BucketType.user)
async def galaxy_daily(ctx):
    row = db.execute("SELECT last_daily FROM galaxy_coins WHERE guild_id=? AND user_id=?", (ctx.guild.id, ctx.author.id)).fetchone()
    if row and row[0]:
        from datetime import datetime, timezone
        try:
            raw = str(row[0]).strip().replace(" ", "T", 1)
            # SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS" (UTC)
            last = datetime.fromisoformat(raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            remaining = timedelta(hours=24) - (datetime.now(timezone.utc) - last)
            if remaining.total_seconds() > 0:
                hours = int(remaining.total_seconds() // 3600)
                minutes = int((remaining.total_seconds() % 3600) // 60)
                return await ctx.send(f"You’ve already claimed today’s reward. Come back in **{hours}h {minutes}m**.")
        except (ValueError, TypeError):
            logger.exception("Could not parse last_daily for user %s: %r", ctx.author.id, row[0])
    reward = random.randint(200, 350)
    db.execute("INSERT INTO galaxy_coins(guild_id,user_id,balance,last_daily) VALUES(?,?,?,CURRENT_TIMESTAMP) "
               "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=balance+excluded.balance,last_daily=CURRENT_TIMESTAMP",
               (ctx.guild.id, ctx.author.id, reward))
    db.commit()
    save_coin_backup()
    bal = get_coin_balance(ctx.guild.id, ctx.author.id)
    daily_embed = build_galaxy_embed(
        f"🎁 {ctx.author.mention} claimed their daily drop!\n\n"
        f"**+{reward:,}** Galaxy Coins\n"
        f"Balance: **{bal:,}**",
        title="GALAXY  /  DAILY",
        color=SUCCESS_COLOR,
        footer="GALAXY  ·  COINS",
    )
    coin_file = apply_galaxy_coin_icon(daily_embed)
    if coin_file:
        await ctx.send(embed=daily_embed, file=coin_file)
    else:
        await ctx.send(embed=daily_embed)

@bot.command(name="riddle", aliases=["riddles"])
@commands.cooldown(1, 3, commands.BucketType.user)
async def galaxy_riddle_cmd(ctx, *, guess: str = None):
    """Staff: +riddle [1-3] starts in this channel. Everyone: +riddle <answer> guesses."""
    active = active_galaxy_questions.get(ctx.guild.id)
    is_staff_creator = can_start_riddles(ctx.author)

    # Staff can start a new round in the channel where they invoke this command.
    if is_staff_creator and (guess is None or guess.strip().isdigit()):
        hint_limit = 3 if not guess else int(guess.strip())
        if not 1 <= hint_limit <= 3:
            return await ctx.send("Hint count must be between **1 and 3**. Example: `+riddle 2`.")
        await _post_galaxy_riddle(ctx.guild, ctx.channel, hint_limit=hint_limit, ping_role=True)
        return

    if guess:
        if not active:
            return await ctx.send(
                embed=build_galaxy_embed(
                    "There’s no active riddle right now. Scheduled riddles post in the riddle channel every **30 minutes**.",
                    title="GALAXY  /  RIDDLE", color=WARN_COLOR, footer="GALAXY  ·  COMMUNITY GAME",
                )
            )
        await _handle_riddle_guess(ctx.channel, ctx.author, guess)
        return

    if active:
        msg_id = active.get("message_id")
        jump = f"\n[Jump to riddle message](https://discord.com/channels/{ctx.guild.id}/{active.get('channel_id', GALAXY_GAME_CHANNEL_ID)}/{msg_id})" if msg_id else ""
        await ctx.send(embed=build_galaxy_embed(
            f"**{active.get('question', '???')}**\n\n"
            f"Reply to the riddle message or type your answer in <#{active.get('channel_id', GALAXY_GAME_CHANNEL_ID)}> / use `{PREFIX}riddle <answer>`.\n"
            f"Hints used: **{active.get('hints_used', 0)}/{active.get('hint_limit', 3)}** · Reward: **{GALAXY_QUESTION_REWARD_MIN}–{GALAXY_QUESTION_REWARD_MAX}** coins{jump}",
            title="GALAXY  /  RIDDLE", color=GALAXY_ACCENT, footer="GALAXY  ·  COMMUNITY GAME",
        ))
        return

    last = _get_last_riddle_at(ctx.guild.id)
    if last is None:
        wait_txt = "about **30 minutes** after the bot starts tracking"
    else:
        remaining = max(0, GALAXY_GAME_INTERVAL_SECONDS - (time.time() - last))
        mins, secs = int(remaining // 60), int(remaining % 60)
        wait_txt = f"**{mins}m {secs}s**" if mins else f"**{secs}s**"
    await ctx.send(embed=build_galaxy_embed(
        f"No riddle is active right now.\nNext scheduled one posts in <#{GALAXY_GAME_CHANNEL_ID}> in {wait_txt}.",
        title="GALAXY  /  RIDDLE", color=GALAXY_COLOR, footer="GALAXY  ·  COMMUNITY GAME",
    ))


@bot.command(name="coinflip", aliases=["cf", "gamble"])
@commands.cooldown(1, 2, commands.BucketType.user)
async def galaxy_gamble(ctx, amount: str, side: str = None):
    """Flip a coin. +cf 500 heads · +cf all t · +cf 2k. No edit-loop animation."""
    try:
        amount = parse_coin_amount(amount, current_balance=get_coin_balance(ctx.guild.id, ctx.author.id))
    except ValueError:
        return await ctx.send(
            f"Enter a valid bet.\n"
            f"Examples: `{PREFIX}cf 100` · `{PREFIX}cf 2k heads` · `{PREFIX}cf all tails`"
        )
    if amount < 1:
        return await ctx.send("Minimum bet is **1 Galaxy Coin**.")

    choice = None
    if side is not None:
        cleaned = side.strip().lower()
        if cleaned in ("heads", "head", "h"):
            choice = "heads"
        elif cleaned in ("tails", "tail", "t"):
            choice = "tails"
        else:
            return await ctx.send(
                f"Side has to be **heads** or **tails** (or leave it blank).\n"
                f"Example: `{PREFIX}cf 250 tails`"
            )

    # Debit first, atomically. No Discord animation/edit loop is used.
    try:
        success, balance = spend_coins(ctx.guild.id, ctx.author.id, amount)
    except Exception:
        logger.exception("Coinflip debit failed for user %s", ctx.author.id)
        return await ctx.send("The bet couldn't safely be processed. Please try again.")
    if not success:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")

    landed = random.choice(("heads", "tails"))
    won = (choice is None and landed == "heads") or (choice is not None and landed == choice)
    bet_line = f"🪙 {ctx.author.mention} bet **{amount:,}**" + (f" on **{choice}**" if choice else "")

    if won:
        # Award gross payout of 2x the wager; SQLite balances clamp at signed-int max.
        try:
            change_coins(ctx.guild.id, ctx.author.id, amount * 2)
            new_bal = get_coin_balance(ctx.guild.id, ctx.author.id)
            panel = build_galaxy_embed(
                f"``\n  🪙  {landed.upper()}\n``\n"
                f"**It landed on {landed.upper()}!**\n\n"
                f"{ctx.author.mention} **won +{amount:,}** Galaxy Coins\n"
                f"Balance: **{new_bal:,}**",
                title="GALAXY  /  YOU WON", color=SUCCESS_COLOR, footer="GALAXY  ·  CASINO",
            )
        except Exception:
            logger.exception("Coinflip payout failed for user %s bet %s", ctx.author.id, amount)
            # Best-effort return of the original stake if payout processing failed.
            try:
                change_coins(ctx.guild.id, ctx.author.id, amount)
            except Exception:
                logger.exception("Coinflip fallback refund failed for %s", ctx.author.id)
            panel = build_galaxy_embed(
                f"It landed on **{landed.upper()}**.\n\n"
                "The payout encountered a database error; a refund was attempted.",
                title="GALAXY  /  COINFLIP ERROR", color=WARN_COLOR, footer="GALAXY  ·  CASINO",
            )
    else:
        try:
            new_bal = get_coin_balance(ctx.guild.id, ctx.author.id)
        except Exception:
            new_bal = 0
        panel = build_galaxy_embed(
            f"``\n  🪙  {landed.upper()}\n``\n"
            f"**It landed on {landed.upper()}.**\n\n"
            f"{ctx.author.mention} **lost -{amount:,}** Galaxy Coins\n"
            f"Balance: **{new_bal:,}**",
            title="GALAXY  /  YOU LOST", color=ERROR_COLOR, footer="GALAXY  ·  CASINO",
        )

    # One final send only. Avoid message-edit animation, sleeps, and repeated
    # Discord API requests that can make the command appear frozen.
    try:
        await ctx.send(embed=panel)
    except discord.HTTPException:
        logger.exception("Coinflip result message failed for %s", ctx.author.id)
        # Gameplay is finalized even if Discord fails to display the result.


@bot.command(name="slots")
@commands.cooldown(1, 2, commands.BucketType.user)
async def galaxy_slots(ctx, amount: str):
    """Spin the slots. 💎 is rare and pays big."""
    try:
        amount = parse_coin_amount(amount, current_balance=get_coin_balance(ctx.guild.id, ctx.author.id))
    except ValueError:
        return await ctx.send(f"Enter a valid bet (example: `{PREFIX}slots 100`, `{PREFIX}slots 2k`, `{PREFIX}slots all`).")
    if amount < 1:
        return await ctx.send("Minimum bet is **1 Galaxy Coin**.")
    if amount > MAX_SQLITE_INT:
        return await ctx.send("That bet is way too large.")
    success, balance = spend_coins(ctx.guild.id, ctx.author.id, amount)
    if not success:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")

    # weighted reel — 💎 is the rare one
    reel_pool = (
        ["🌌"] * 30
        + ["⭐"] * 25
        + ["🪐"] * 20
        + ["☄️"] * 15
        + ["💎"] * 10
    )
    show_pool = ["🌌", "⭐", "🪐", "☄️", "💎"]

    result = [random.choice(reel_pool) for _ in range(3)]
    bet_line = f"🎰 {ctx.author.mention} spun for **{amount:,}**"

    spin = await ctx.send(
        embed=build_galaxy_embed(
            f"{bet_line}\n\n『 ❔  │  ❔  │  ❔ 』\n\n*Rolling...*",
            title="GALAXY  /  SLOTS",
            color=GALAXY_ACCENT,
            footer="GALAXY  ·  CASINO",
        )
    )
    # animate a few random reel frames, then lock in the real result
    for _ in range(5):
        await asyncio.sleep(0.32)
        fake = [random.choice(show_pool) for _ in range(3)]
        try:
            await spin.edit(
                embed=build_galaxy_embed(
                    f"{bet_line}\n\n『 {fake[0]}  │  {fake[1]}  │  {fake[2]} 』\n\n*Rolling...*",
                    title="GALAXY  /  SLOTS",
                    color=GALAXY_ACCENT,
                    footer="GALAXY  ·  CASINO",
                )
            )
        except (discord.NotFound, discord.HTTPException):
            break
    await asyncio.sleep(0.35)

    reels = f"『 {result[0]}  │  {result[1]}  │  {result[2]} 』"

    if result[0] == result[1] == result[2]:
        if result[0] == "💎":
            mult, tag = 12, "💎 MEGA JACKPOT"
        elif result[0] == "☄️":
            mult, tag = 8, "☄️ SUPER WIN"
        elif result[0] == "🪐":
            mult, tag = 6, "🪐 JACKPOT"
        else:
            mult, tag = 5, "⭐ TRIPLE"
        payout = amount * mult
        change_coins(ctx.guild.id, ctx.author.id, payout)
        net = payout - amount
        panel = build_galaxy_embed(
            f"{reels}\n\n"
            f"**{tag}** — paid **{mult}x**\n"
            f"{ctx.author.mention} net **+{net:,}** Galaxy Coins\n"
            f"Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**",
            title="GALAXY  /  JACKPOT",
            color=SUCCESS_COLOR,
            footer="GALAXY  ·  CASINO",
        )
    elif result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
        change_coins(ctx.guild.id, ctx.author.id, amount * 2)
        panel = build_galaxy_embed(
            f"{reels}\n\n"
            f"**Two match!** — paid **2x**\n"
            f"{ctx.author.mention} net **+{amount:,}** Galaxy Coins\n"
            f"Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**",
            title="GALAXY  /  NICE HIT",
            color=SUCCESS_COLOR,
            footer="GALAXY  ·  CASINO",
        )
    else:
        panel = build_galaxy_embed(
            f"{reels}\n\n"
            f"**No match.**\n"
            f"{ctx.author.mention} lost **-{amount:,}** Galaxy Coins\n"
            f"Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**",
            title="GALAXY  /  NO LUCK",
            color=ERROR_COLOR,
            footer="GALAXY  ·  CASINO",
        )
    try:
        await spin.edit(embed=panel)
    except (discord.NotFound, discord.HTTPException):
        await ctx.send(embed=panel)


@bot.command(name="dice", aliases=["roll"])
@commands.cooldown(1, 2, commands.BucketType.user)
async def galaxy_dice(ctx, amount: str):
    """Roll a d6. 4–5 pays 1.5x, 6 pays 3x. 1–3 loses."""
    try:
        amount = parse_coin_amount(amount, current_balance=get_coin_balance(ctx.guild.id, ctx.author.id))
    except ValueError:
        return await ctx.send(f"Enter a valid bet (example: `{PREFIX}dice 100`, `{PREFIX}dice all`).")
    if amount < 1:
        return await ctx.send("Minimum bet is **1 Galaxy Coin**.")
    if amount > MAX_SQLITE_INT:
        return await ctx.send("That bet is way too large.")
    success, balance = spend_coins(ctx.guild.id, ctx.author.id, amount)
    if not success:
        return await ctx.send(f"You only have **{balance:,} Galaxy Coins**.")

    roll = random.randint(1, 6)
    bet_line = f"🎲 {ctx.author.mention} wagered **{amount:,}**"
    spin = await ctx.send(
        embed=build_galaxy_embed(
            f"{bet_line}\n\n🎲 · · ·\n\n*Rolling the die...*",
            title="GALAXY  /  DICE",
            color=GALAXY_ACCENT,
            footer="GALAXY  ·  CASINO",
        )
    )
    for face in (random.randint(1, 6) for _ in range(4)):
        await asyncio.sleep(0.3)
        try:
            await spin.edit(
                embed=build_galaxy_embed(
                    f"{bet_line}\n\n🎲 **{face}**\n\n*Rolling the die...*",
                    title="GALAXY  /  DICE",
                    color=GALAXY_ACCENT,
                    footer="GALAXY  ·  CASINO",
                )
            )
        except (discord.NotFound, discord.HTTPException):
            break
    await asyncio.sleep(0.35)

    if roll == 6:
        payout = int(amount * 3)
        change_coins(ctx.guild.id, ctx.author.id, payout)
        net = payout - amount
        panel = build_galaxy_embed(
            f"🎲 Rolled a **{roll}**!\n\n"
            f"**Critical!** paid **3x**\n"
            f"{ctx.author.mention} net **+{net:,}** Galaxy Coins\n"
            f"Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**",
            title="GALAXY  /  BIG ROLL",
            color=SUCCESS_COLOR,
            footer="GALAXY  ·  CASINO",
        )
    elif roll >= 4:
        payout = int(amount * 1.5)
        change_coins(ctx.guild.id, ctx.author.id, payout)
        net = payout - amount
        panel = build_galaxy_embed(
            f"🎲 Rolled a **{roll}**!\n\n"
            f"**Win!** paid **1.5x**\n"
            f"{ctx.author.mention} net **+{net:,}** Galaxy Coins\n"
            f"Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**",
            title="GALAXY  /  YOU HIT",
            color=SUCCESS_COLOR,
            footer="GALAXY  ·  CASINO",
        )
    else:
        panel = build_galaxy_embed(
            f"🎲 Rolled a **{roll}**.\n\n"
            f"**Bust.**\n"
            f"{ctx.author.mention} lost **-{amount:,}** Galaxy Coins\n"
            f"Balance: **{get_coin_balance(ctx.guild.id, ctx.author.id):,}**",
            title="GALAXY  /  BUST",
            color=ERROR_COLOR,
            footer="GALAXY  ·  CASINO",
        )
    try:
        await spin.edit(embed=panel)
    except (discord.NotFound, discord.HTTPException):
        await ctx.send(embed=panel)


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
        return ctx.author.id == ctx.guild.owner_id or ctx.author.id in SPECIAL_USERS or has_staff_rank(ctx.author, 5)
    return commands.check(predicate)


def can_moderate_target(ctx, member):
    """Respect staff ladder + Discord hierarchy. Special users bypass both."""
    # Special users / server owner can run mod commands on anyone, including themselves.
    if ctx.author.id in SPECIAL_USERS or ctx.author.id == ctx.guild.owner_id:
        return True
    if member.id == ctx.author.id:
        return False
    if ctx.author.guild_permissions.administrator and member.id != ctx.guild.owner_id:
        # Admins still can't act on equal/higher Discord roles or equal+ staff ranks
        pass

    # Never allow a moderator to act on someone whose Discord highest role is
    # equal to or above theirs.
    if member.top_role >= ctx.author.top_role:
        return False

    actor_rank = effective_staff_rank(ctx.author)
    target_rank = effective_staff_rank(member)
    # Staff ranks provide an additional safeguard even if the role ordering
    # happens to be misconfigured; equal- and higher-ranked staff remain protected.
    if target_rank:
        return actor_rank > target_rank
    return True


async def sync_staff_strike_roles(guild, member_or_id):
    """Give/remove the strike-1/2/3 roles so they match how many active strikes they have."""
    if guild is None:
        return
    user_id = member_or_id.id if hasattr(member_or_id, "id") else int(member_or_id)
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    # purge expired first so the count is honest
    db.execute(
        f"DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-{STAFF_STRIKE_EXPIRE_DAYS} days')"
    )
    db.commit()
    count = db.execute(
        "SELECT COUNT(*) FROM staff_strikes WHERE guild_id=? AND user_id=?",
        (guild.id, user_id),
    ).fetchone()[0]
    count = int(count or 0)

    wanted = set()
    if count >= 1:
        wanted.add(STAFF_STRIKE_ONE_ROLE_ID)
    if count >= 2:
        wanted.add(STAFF_STRIKE_TWO_ROLE_ID)
    if count >= 3:
        wanted.add(STAFF_STRIKE_THREE_ROLE_ID)

    current = {r.id for r in member.roles}
    to_add = []
    to_remove = []
    for role_id in STAFF_STRIKE_LEVEL_ROLE_IDS:
        role = guild.get_role(role_id)
        if role is None:
            continue
        if role_id in wanted and role_id not in current:
            to_add.append(role)
        elif role_id not in wanted and role_id in current:
            to_remove.append(role)

    try:
        if to_add:
            await member.add_roles(*to_add, reason="Staff strike role sync")
        if to_remove:
            await member.remove_roles(*to_remove, reason="Staff strike role sync")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not sync strike roles for member %s in guild %s", user_id, guild.id)


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
            "assistant_head_manager", "head_manager", "trial_director", "head_director",
            "senior_drc", "drc", "axelle", "dev"],
        6: ["main_founder", "founder", "co_owner"],
    }
    labels = {1: "MODERATION", 2: "SENIOR MODERATION", 3: "ADMINISTRATION",
              4: "SENIOR ADMINISTRATION", 5: "LEADERSHIP", 6: "OWNERSHIP"}
    sections = []
    for level in range(1, 7):
        roles = "  ·  ".join(f"<@&{ROLE_IDS[key]}>" for key in role_order[level])
        sections.append(f"**PERMISSION {level}  /  {labels[level]}**\n{roles}")
    embed = discord.Embed(title="Staff permissions", description="\n\n".join(sections), color=GALAXY_COLOR)
    embed.add_field(name="COMMAND ACCESS", value=(
        f"`{PREFIX}sanctions`  Perm 1+\n"
        f"`{PREFIX}del sanction`  Perm 2+\n`{PREFIX}kick`  Perm 3+  ·  `{PREFIX}ban`  Perm 4+\n"
        f"`{PREFIX}bl` / `{PREFIX}blacklist`  Perm 6+"), inline=False)
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
async def warn(ctx, member: FlexibleMember, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t warn that member because of role hierarchy.")
    db.execute(
        "INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, member.id, ctx.author.id, reason),
    )
    db.commit()
    warning_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Warn", reason,
                                  source_warning_id=warning_id)
    warn_count = db.execute(
        "SELECT COUNT(*) FROM warnings WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, member.id),
    ).fetchone()[0]
    panel = build_mod_action_embed(
        action_title="Warning issued",
        member_display=f"{member.mention}\n`{member.id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        case_id=sanction_id,
        extra_fields=[("Active warnings", f"`{warn_count}`", True)],
        color=WARN_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    await send_mod_log(
        ctx.guild,
        f"⚠️ **Warning**\nUser: {member} (`{member.id}`)\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}\nCase: #{sanction_id}",
    )


@bot.command()
@require_perm1()
async def warnings(ctx, member: FlexibleMember):
    rows = db.execute(
        "SELECT id, reason, created_at, moderator_id FROM warnings "
        "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
        (ctx.guild.id, member.id),
    ).fetchall()
    embed = discord.Embed(
        title="GALAXY  /  WARNING HISTORY",
        color=WARN_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name="STEAL A GALACTICROT  ·  MODERATION")
    embed.description = f"Records for {member.mention} (`{member.id}`)"
    if not rows:
        embed.add_field(name="Warnings", value="No warnings on record.", inline=False)
    else:
        lines = []
        for row in rows:
            wid, reason, created_at, mod_id = row
            safe = discord.utils.escape_markdown(discord.utils.escape_mentions(reason or "No reason"))
            lines.append(f"`#{wid}` — {safe}\n┗ <@{mod_id}> · {created_at}")
        embed.add_field(name=f"Latest {len(rows)}", value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="GALAXY  ·  STAFF ACTION")
    await ctx.send(embed=embed)


@bot.command()
@require_perm1()
async def clearwarns(ctx, member: FlexibleMember):
    # Keep the legacy warnings table and unified sanctions history in sync.
    warning_ids = [
        row[0] for row in db.execute(
            "SELECT id FROM warnings WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, member.id),
        ).fetchall()
    ]
    if warning_ids:
        placeholders = ",".join("?" for _ in warning_ids)
        db.execute(
            f"DELETE FROM sanctions WHERE guild_id=? AND sanction_type='Warn' "
            f"AND source_warning_id IN ({placeholders})",
            (ctx.guild.id, *warning_ids),
        )
    db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    db.commit()
    panel = build_mod_action_embed(
        action_title="GALAXY  /  WARNINGS CLEARED",
        member_display=f"{member.mention}\n`{member.id}`",
        reason=f"Cleared {len(warning_ids)} warning(s) and matching sanction history.",
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        color=SUCCESS_COLOR,
    )
    await ctx.send(embed=panel)
    await send_mod_log(ctx.guild, f"🧹 **Warnings cleared**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="strike")
@commands.guild_only()
@require_staff_strike_access()
async def staff_strike(ctx, member: FlexibleMember, *, reason="No reason provided"):
    """Issue a staff strike and send a compact branded audit panel."""
    db.execute(
        f"DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-{STAFF_STRIKE_EXPIRE_DAYS} days')"
    )
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
    # apply strike-1 / strike-2 / strike-3 roles to match the new count
    await sync_staff_strike_roles(ctx.guild, member)
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
    sent_response = await ctx.send(embed=response, allowed_mentions=discord.AllowedMentions.none())
    if sent_response:
        db.execute(
            "UPDATE staff_strikes SET response_message_id=? WHERE guild_id=? AND id=?",
            (sent_response.id, ctx.guild.id, strike_id),
        )
        db.commit()

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
    audit_msg = await send_strike_log(ctx.guild, audit)
    if audit_msg:
        db.execute(
            "UPDATE staff_strikes SET audit_message_id=? WHERE guild_id=? AND id=?",
            (audit_msg.id, ctx.guild.id, strike_id),
        )
        db.commit()
    else:
        await ctx.send("The strike was saved, but I couldn’t deliver its audit entry. Check the bot’s access to the configured strike-log channel.")


@bot.command(name="strikes", aliases=["staffstrikes"])
@commands.guild_only()
@require_staff_strike_access()
async def staff_strikes(ctx, member: FlexibleMember):
    db.execute(
        f"DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-{STAFF_STRIKE_EXPIRE_DAYS} days')"
    )
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
    history.add_field(name="Retention", value=f"`{STAFF_STRIKE_EXPIRE_DAYS} days`", inline=True)
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


@bot.command(name="removestrike", aliases=["unstrike", "delstrike"])
@commands.guild_only()
@require_staff_strike_access()  # Assistant Staff Manager+ (perm 5) and up
async def remove_staff_strike(ctx, identifier: str = None, *, reason="No reason provided"):
    """Remove a staff strike.
    Works with:
      +removestrike <strike_id>
      +removestrike <message_id>
      reply to the strike message with +removestrike
    """
    db.execute(
        f"DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-{STAFF_STRIKE_EXPIRE_DAYS} days')"
    )
    db.commit()

    ref_msg = None
    # if they replied to a message, grab that message id + the message itself
    if ctx.message.reference and ctx.message.reference.message_id:
        ref_id = ctx.message.reference.message_id
        if identifier is None:
            identifier = str(ref_id)
        # try to resolve the actual message so we can read the embed
        try:
            ref_msg = ctx.message.reference.resolved
            if ref_msg is None:
                ref_msg = await ctx.channel.fetch_message(ref_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            ref_msg = None

    if identifier is None:
        return await ctx.send(
            f"Usage: `{PREFIX}removestrike <strike_id or message_id> [reason]`\n"
            f"Or just reply to the strike message with `{PREFIX}removestrike`."
        )

    # allow pasting a raw snowflake or a full discord message link
    id_match = re.search(r"(\d{15,22})", str(identifier))
    if not id_match:
        return await ctx.send("That doesn’t look like a strike ID or message ID.")
    identifier = int(id_match.group(1))

    row = db.execute(
        "SELECT id, user_id, reason, moderator_id, created_at FROM staff_strikes "
        "WHERE guild_id=? AND (id=? OR response_message_id=? OR audit_message_id=?)",
        (ctx.guild.id, identifier, identifier, identifier),
    ).fetchone()

    # if we still don't have a row, try reading the strike id out of the message embed
    if row is None:
        candidates = []
        if ref_msg is not None:
            candidates.append(ref_msg)
        # also try fetching by the id they gave us (current channel + strike log channel)
        if identifier > 1000000000000000:
            for ch in (ctx.channel, await fetch_guild_text_channel(ctx.guild, STRIKE_LOG_CHANNEL_ID)):
                if ch is None:
                    continue
                try:
                    fetched = await ch.fetch_message(identifier)
                    if fetched and fetched not in candidates:
                        candidates.append(fetched)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    continue

        for msg in candidates:
            if not msg.embeds:
                continue
            emb = msg.embeds[0]
            embed_blob = " ".join(
                [emb.title or "", emb.description or ""]
                + [f"{f.name} {f.value}" for f in emb.fields]
            )
            # look for "#2" style strike ids (prefer the one next to "Strike ID" / "Strike record")
            m = re.search(r"(?:Strike ID|Strike record|Strike)\s*[`:]?\s*#?(\d+)", embed_blob, re.I)
            if not m:
                m = re.search(r"#(\d+)", embed_blob)
            if m:
                parsed_id = int(m.group(1))
                row = db.execute(
                    "SELECT id, user_id, reason, moderator_id, created_at FROM staff_strikes WHERE guild_id=? AND id=?",
                    (ctx.guild.id, parsed_id),
                ).fetchone()
                if row:
                    break

    if row is None:
        return await ctx.send(
            "Couldn’t find that strike. Reply to the original strike message "
            f"(or the audit one), or use `{PREFIX}removestrike <strike_id>`."
        )
    strike_id, target_user_id, orig_reason, orig_mod_id, orig_created_at = row
    row = (target_user_id, orig_reason, orig_mod_id, orig_created_at)
    db.execute("DELETE FROM staff_strikes WHERE guild_id=? AND id=?", (ctx.guild.id, strike_id))
    db.commit()
    # drop/adjust strike roles to match what's left
    await sync_staff_strike_roles(ctx.guild, target_user_id)
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


def bot_can_timeout_member(guild, member):
    """Preflight Discord's bot-side permission and role-hierarchy requirements."""
    me = guild.me
    if me is None:
        return False, "I couldn't resolve my bot member in this server."
    if not me.guild_permissions.moderate_members:
        return False, "I need the **Timeout Members** permission."
    if member.id == guild.owner_id:
        return False, "Discord does not allow a timeout on the server owner."
    if member.id == me.id:
        return False, "I can't timeout myself."
    if member.top_role >= me.top_role:
        return False, "My highest role must be **above the target's highest role** to timeout them. Move my bot role higher in Server Settings → Roles."
    return True, None


@bot.command(name="tempmute", aliases=["timeout"])
@require_perm1()
async def tempmute(ctx, member: FlexibleMember, duration: str, *, reason="No reason provided"):
    if not isinstance(member, discord.Member):
        return await ctx.send("I couldn't find that member in this server. Please mention them or use their server member ID.")
    try:
        total_seconds = parse_duration_seconds(duration)
    except ValueError:
        return await ctx.send(
            "Invalid duration. Examples: `10` (10 minutes), `30m`, `1h`, `7d`, `7days`, `2w` (max **28 days**)."
        )
    if total_seconds < 60 or total_seconds > 28 * 86400:
        return await ctx.send("Timeout length must be between **1 minute and 28 days**.")
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can't timeout that member because of staff-rank protection.")
    bot_ok, bot_error = bot_can_timeout_member(ctx.guild, member)
    if not bot_ok:
        return await ctx.send(f"❌ Timeout not applied: {bot_error}")
    duration_label = format_duration_label(total_seconds)
    try:
        await member.timeout(timedelta(seconds=total_seconds), reason=f"{ctx.author}: {reason}")
    except discord.Forbidden:
        return await ctx.send("❌ Discord denied the timeout. Check that I have **Timeout Members** permission and my highest role is above the target's.")
    except discord.HTTPException as exc:
        logger.exception("Discord HTTP error applying timeout")
        return await ctx.send(f"❌ Discord couldn't apply the timeout (HTTP error {exc.status}). Please try again.")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Timeout", reason,
                                  details=f"Duration: {duration_label}")
    panel = build_mod_action_embed(
        action_title="Timeout applied",
        member_display=f"{member.mention}\n`{member.id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        case_id=sanction_id,
        extra_fields=[("Duration", f"**{duration_label}**", True)],
        color=WARN_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    asyncio.create_task(send_mod_log(
        ctx.guild,
        f"⏳ **Timeout**\nUser: {member} (`{member.id}`)\nDuration: {duration_label}\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}"
    ))


@bot.command()
@require_perm1()
async def unmute(ctx, member: FlexibleMember):
    if not isinstance(member, discord.Member):
        return await ctx.send("I couldn't find that member in this server. Please mention them or use their server member ID.")
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can't unmute that member because of staff-rank protection.")
    bot_ok, bot_error = bot_can_timeout_member(ctx.guild, member)
    # Removing a timeout can be allowed even if the target is above the bot's role;
    # Discord's API may still enforce hierarchy, so attempt it and report failures.
    try:
        await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
    except discord.Forbidden:
        return await ctx.send("❌ Discord denied unmute. Check my permissions and role hierarchy. " + (bot_error or ""))
    except discord.HTTPException as exc:
        logger.exception("Discord HTTP error removing timeout")
        return await ctx.send(f"❌ Discord couldn't remove the timeout (HTTP error {exc.status}). Please try again.")
    panel = build_mod_action_embed(
        action_title="Timeout lifted",
        member_display=f"{member.mention}\n`{member.id}`",
        reason="Timeout removed — they can speak again.",
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        color=SUCCESS_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    await send_mod_log(ctx.guild, f"🔊 **Timeout removed**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


async def _schedule_vc_lift(guild, user_id, kind, seconds, moderator):
    """After `seconds`, clear server mute or deafen on the member."""
    key = (guild.id, user_id, kind)
    old = temp_vc_tasks.pop(key, None)
    if old and not old.done():
        old.cancel()

    async def _lift():
        try:
            await asyncio.sleep(seconds)
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    return
            kwargs = {"mute": False} if kind == "mute" else {"deafen": False}
            await member.edit(**kwargs, reason="Temporary VC restriction expired")
            label = "unmute" if kind == "mute" else "undeafen"
            await send_mod_log(
                guild,
                f"🔊 **VC {label} (timer)**\nUser: {member} (`{member.id}`)\n"
                f"Original mod: {moderator}",
            )
        except asyncio.CancelledError:
            return
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to auto-%s user %s", kind, user_id)
        finally:
            temp_vc_tasks.pop(key, None)

    temp_vc_tasks[key] = asyncio.create_task(_lift())


@bot.command(name="vcmute", aliases=["voicemute", "servermute"])
@commands.guild_only()
@require_staff_rank(4)
async def vcmute(ctx, member: FlexibleMember = None, duration: str = None, *, reason="No reason provided"):
    """Server-mute someone in VC. Optional time: +vcmute @user 10m / 30s / 10 secs"""
    if member is None:
        return await ctx.send(
            f"Usage: `{PREFIX}vcmute @user [time] [reason]`\n"
            f"Or reply to them: `{PREFIX}vcmute [time] [reason]`\n"
            f"Time is optional (e.g. `30s`, `10 secs`, `10m`, `1h`). No time = stays muted until unmuted."
        )
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t VC mute that member because of role hierarchy.")

    seconds, reason = parse_vc_duration_arg(duration, reason)
    if seconds is not None and (seconds < 1 or seconds > 28 * 86400):
        return await ctx.send("VC mute timer must be between **1 second and 28 days**.")

    try:
        await member.edit(mute=True, reason=f"{ctx.author}: {reason}")
    except discord.Forbidden:
        return await ctx.send("I can’t server-mute them. I need **Mute Members** and a higher role.")
    except discord.HTTPException:
        logger.exception("vcmute failed for %s", member.id)
        return await ctx.send("Discord couldn’t apply the VC mute. Are they in the server?")

    if seconds:
        await _schedule_vc_lift(ctx.guild, member.id, "mute", seconds, str(ctx.author))
        label = format_duration_label(seconds)
        extras = [("Duration", f"**{label}**", True)]
    else:
        key = (ctx.guild.id, member.id, "mute")
        old = temp_vc_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()
        label = "until unmuted"
        extras = [("Duration", "**Permanent**", True)]
    panel = build_mod_action_embed(
        action_title="VC mute",
        member_display=f"{member.mention}\n`{member.id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        extra_fields=extras,
        color=WARN_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    await send_mod_log(
        ctx.guild,
        f"🔇 **VC mute**\nUser: {member} (`{member.id}`)\nDuration: {label}\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}",
    )


@bot.command(name="vcunmute", aliases=["voiceunmute", "serverunmute"])
@commands.guild_only()
@require_staff_rank(4)
async def vcunmute(ctx, member: FlexibleMember = None):
    if member is None:
        return await ctx.send(f"Usage: `{PREFIX}vcunmute @user` (or reply to them).")
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t VC unmute that member because of role hierarchy.")
    key = (ctx.guild.id, member.id, "mute")
    old = temp_vc_tasks.pop(key, None)
    if old and not old.done():
        old.cancel()
    try:
        await member.edit(mute=False, reason=f"VC unmute by {ctx.author}")
    except discord.Forbidden:
        return await ctx.send("I can’t unmute them. I need **Mute Members** and a higher role.")
    except discord.HTTPException:
        return await ctx.send("Discord couldn’t unmute them.")
    panel = build_mod_action_embed(
        action_title="VC unmute",
        member_display=f"{member.mention}\n`{member.id}`",
        reason="Server mute removed.",
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        color=SUCCESS_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    await send_mod_log(
        ctx.guild,
        f"🔊 **VC unmute**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)",
    )


@bot.command(name="vcdeafen", aliases=["vcdeaf", "voicedeafen", "serverdeafen"])
@commands.guild_only()
@require_staff_rank(4)
async def vcdeafen(ctx, member: FlexibleMember = None, duration: str = None, *, reason="No reason provided"):
    """Server-deafen someone in VC. Optional time: +vcdeafen @user 10m / 30s / 10 secs"""
    if member is None:
        return await ctx.send(
            f"Usage: `{PREFIX}vcdeafen @user [time] [reason]`\n"
            f"Or reply to them: `{PREFIX}vcdeafen [time] [reason]`\n"
            f"Time is optional (e.g. `30s`, `10 secs`, `10m`, `1h`). No time = stays deafened until undeafened."
        )
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t VC deafen that member because of role hierarchy.")

    seconds, reason = parse_vc_duration_arg(duration, reason)
    if seconds is not None and (seconds < 1 or seconds > 28 * 86400):
        return await ctx.send("VC deafen timer must be between **1 second and 28 days**.")

    try:
        await member.edit(deafen=True, reason=f"{ctx.author}: {reason}")
    except discord.Forbidden:
        return await ctx.send("I can’t server-deafen them. I need **Deafen Members** and a higher role.")
    except discord.HTTPException:
        logger.exception("vcdeafen failed for %s", member.id)
        return await ctx.send("Discord couldn’t apply the VC deafen. Are they in the server?")

    if seconds:
        await _schedule_vc_lift(ctx.guild, member.id, "deafen", seconds, str(ctx.author))
        label = format_duration_label(seconds)
        extras = [("Duration", f"**{label}**", True)]
    else:
        key = (ctx.guild.id, member.id, "deafen")
        old = temp_vc_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()
        label = "until undeafened"
        extras = [("Duration", "**Permanent**", True)]
    panel = build_mod_action_embed(
        action_title="VC deafen",
        member_display=f"{member.mention}\n`{member.id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        extra_fields=extras,
        color=WARN_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    await send_mod_log(
        ctx.guild,
        f"🔇 **VC deafen**\nUser: {member} (`{member.id}`)\nDuration: {label}\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}",
    )


@bot.command(name="vcundeafen", aliases=["vcundeaf", "voiceundeafen", "serverundeafen"])
@commands.guild_only()
@require_staff_rank(4)
async def vcundeafen(ctx, member: FlexibleMember = None):
    if member is None:
        return await ctx.send(f"Usage: `{PREFIX}vcundeafen @user` (or reply to them).")
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t VC undeafen that member because of role hierarchy.")
    key = (ctx.guild.id, member.id, "deafen")
    old = temp_vc_tasks.pop(key, None)
    if old and not old.done():
        old.cancel()
    try:
        await member.edit(deafen=False, reason=f"VC undeafen by {ctx.author}")
    except discord.Forbidden:
        return await ctx.send("I can’t undeafen them. I need **Deafen Members** and a higher role.")
    except discord.HTTPException:
        return await ctx.send("Discord couldn’t undeafen them.")
    panel = build_mod_action_embed(
        action_title="VC undeafen",
        member_display=f"{member.mention}\n`{member.id}`",
        reason="Server deafen removed.",
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        color=SUCCESS_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    await send_mod_log(
        ctx.guild,
        f"🔊 **VC undeafen**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)",
    )


@bot.command()
@require_ban()
async def ban(ctx, target_input: str, *, reason="No reason provided"):
    # normal server ban — kicks them out and keeps them out
    try:
        member = await resolve_member_arg(ctx, target_input)
        if not can_moderate_target(ctx, member):
            return await ctx.send("You can’t ban that member because of role hierarchy.")
        await member.ban(reason=f"{ctx.author}: {reason}")
        target_id = member.id
        target_label = str(member)
    except commands.BadArgument:
        cleaned = re.sub(r"^[<@!]+|[>]+$", "", str(target_input).strip())
        if not cleaned.isdigit():
            # try resolving as a user (username / id) so it works offline too
            try:
                user = await resolve_user_arg(ctx, target_input)
                target_id = user.id
                target_label = str(user)
                await ctx.guild.ban(discord.Object(id=target_id), reason=f"{ctx.author}: {reason}")
            except commands.BadArgument:
                return await ctx.send(f"Couldn’t find that user. Use a mention, ID, or exact username.")
        else:
            target_id = int(cleaned)
            try:
                fetched = await bot.fetch_user(target_id)
                target_label = f"{fetched} (`{target_id}`)"
            except (discord.NotFound, discord.HTTPException):
                target_label = f"User `{target_id}`"
            await ctx.guild.ban(discord.Object(id=target_id), reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, target_id, ctx.author.id, "Ban", reason)
    panel = build_mod_action_embed(
        action_title="Member banned",
        member_display=f"{target_label}\n`{target_id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        case_id=sanction_id,
        color=ERROR_COLOR,
    )
    await ctx.send(embed=panel)
    await send_mod_log(ctx.guild, f"🔨 **Ban**\nUser: {target_label} (`{target_id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_ban()
async def unban(ctx, user_id: int, *, reason="No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.send("I couldn’t find an active ban for that ID. Check the ID and try again.")
    panel = build_mod_action_embed(
        action_title="Member unbanned",
        member_display=f"{user}\n`{user.id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        color=SUCCESS_COLOR,
    )
    await ctx.send(embed=panel)
    await send_mod_log(ctx.guild, f"♻️ **Unban**\nUser: {user} (`{user.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="softban")
@require_staff_rank(5)
async def softban(ctx, member: FlexibleMember, *, reason="No reason provided"):
    """Ban then immediately unban to purge recent messages (1 day of history). Perm 5+ only."""
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t softban that member because of role hierarchy.")
    reason = (reason or "No reason provided").strip()[:900] or "No reason provided"
    target_id = member.id
    target_label = str(member)
    try:
        await member.ban(reason=f"Softban by {ctx.author}: {reason}", delete_message_days=1)
        await ctx.guild.unban(discord.Object(id=target_id), reason=f"Softban complete by {ctx.author}")
    except discord.Forbidden:
        return await ctx.send("I don’t have permission to ban/unban that member. Check Ban Members and role order.")
    except discord.HTTPException:
        logger.exception("Softban failed for %s", target_id)
        return await ctx.send("Discord blocked the softban. They may already be banned, or something else went wrong.")
    sanction_id = record_sanction(ctx.guild.id, target_id, ctx.author.id, "Softban", reason,
                                  details="Ban + unban; 1 day of messages purged")
    panel = build_mod_action_embed(
        action_title="Member softbanned",
        member_display=f"{target_label}\n`{target_id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        case_id=sanction_id,
        extra_fields=[("Effect", "Kicked + recent messages purged (1 day)", False)],
        color=WARN_COLOR,
    )
    await ctx.send(embed=panel)
    await send_mod_log(
        ctx.guild,
        f"🧼 **Softban**\nUser: {target_label} (`{target_id}`)\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}",
    )


@bot.command()
@require_admin_actions()
async def kick(ctx, member: FlexibleMember, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t kick that member because of role hierarchy.")
    await member.kick(reason=f"{ctx.author}: {reason}")
    sanction_id = record_sanction(ctx.guild.id, member.id, ctx.author.id, "Kick", reason)
    panel = build_mod_action_embed(
        action_title="Member kicked",
        member_display=f"{member.mention}\n`{member.id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        case_id=sanction_id,
        color=WARN_COLOR,
        thumbnail_url=getattr(member.display_avatar, "url", None),
    )
    await ctx.send(embed=panel)
    await send_mod_log(ctx.guild, f"👢 **Kick**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="bl", aliases=["blacklist"])
@require_owner_actions()
async def blacklist_user(ctx, user: FlexibleUser, *, reason="Blacklisted"):
    # blacklist = permanent ban + mark in the db so they get re-banned if they sneak back
    reason = (reason or "Blacklisted").strip()[:900] or "Blacklisted"
    ban_reason = f"Blacklisted: {reason}"

    # ban them from the server if they're in it (or by id if not)
    member = ctx.guild.get_member(user.id)
    try:
        if member is not None:
            if not can_moderate_target(ctx, member) and ctx.author.id != ctx.guild.owner_id:
                return await ctx.send("You can’t blacklist that member because of role hierarchy.")
            await member.ban(reason=f"{ctx.author}: {ban_reason}")
        else:
            await ctx.guild.ban(discord.Object(id=user.id), reason=f"{ctx.author}: {ban_reason}")
    except discord.Forbidden:
        return await ctx.send("I don’t have permission to ban that user. Check my role is above theirs and I have Ban Members.")
    except discord.HTTPException:
        logger.exception("Failed to ban user %s during blacklist", user.id)
        return await ctx.send("Discord blocked the ban. They might already be banned, or something else went wrong.")

    db.execute(
        "INSERT OR REPLACE INTO blacklist(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, user.id, ctx.author.id, reason),
    )
    db.commit()
    sanction_id = record_sanction(ctx.guild.id, user.id, ctx.author.id, "Blacklist", reason)
    panel = build_mod_action_embed(
        action_title="GALAXY  /  BLACKLIST",
        member_display=f"{user}\n`{user.id}`",
        reason=reason,
        moderator=f"{ctx.author.mention}\n`{ctx.author.id}`",
        case_id=sanction_id,
        extra_fields=[("Action", "Blacklisted + banned from the server", False)],
        color=ERROR_COLOR,
    )
    await ctx.send(embed=panel)
    await send_mod_log(
        ctx.guild,
        f"⛔ **Blacklist + Ban**\nUser: {user} (`{user.id}`)\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}",
    )


@bot.command(name="unbl", aliases=["unblacklist"])
@require_owner_actions()
async def unblacklist_user(ctx, user_id: int, *, reason="Removed from blacklist"):
    cursor = db.execute(
        "DELETE FROM blacklist WHERE guild_id=? AND user_id=?",
        (ctx.guild.id, user_id),
    )
    db.commit()
    if cursor.rowcount == 0:
        return await ctx.send("That user isn’t on the blacklist.")

    # also unban so they can actually come back
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
        label = str(user)
    except discord.NotFound:
        label = f"User `{user_id}`"
    except (discord.Forbidden, discord.HTTPException):
        label = f"User `{user_id}`"
        await ctx.send(
            f"✅ Removed `{user_id}` from the blacklist, but I couldn’t unban them "
            f"(maybe they weren’t banned, or I’m missing permission)."
        )
        await send_mod_log(
            ctx.guild,
            f"✅ **Blacklist removal** (unban failed)\nUser ID: `{user_id}`\n"
            f"Moderator: {ctx.author} (`{ctx.author.id}`)",
        )
        return

    await ctx.send(f"✅ **{label}** removed from the blacklist and unbanned.")
    await send_mod_log(
        ctx.guild,
        f"✅ **Blacklist removal + Unban**\nUser: {label} (`{user_id}`)\n"
        f"Moderator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}",
    )


@bot.command(name="sanctions", aliases=["cases", "history"])
@require_staff_rank(1)  # Trial Moderator+
async def sanctions(ctx, user: FlexibleUser):
    rows = db.execute(
        """SELECT id, sanction_type, reason, details, moderator_id, created_at
           FROM sanctions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 25""",
        (ctx.guild.id, user.id),
    ).fetchall()
    embed = discord.Embed(
        title="Sanction history",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(user), icon_url=getattr(getattr(user, "display_avatar", None), "url", None))
    embed.description = f"{getattr(user, 'mention', user)} · `{user.id}`"
    if not rows:
        embed.add_field(name="Records", value="No sanctions on file.", inline=False)
    else:
        lines = []
        for case_id, kind, reason, details, moderator_id, created_at in rows:
            safe = discord.utils.escape_markdown(
                discord.utils.escape_mentions(reason or "No reason")
            )
            if len(safe) > 100:
                safe = safe[:97] + "..."
            extra = f" · {details}" if details else ""
            lines.append(
                f"`#{case_id}` **{kind}** — {safe}{extra}\n"
                f"┗ <@{moderator_id}> · `{created_at}`"
            )
        embed.add_field(
            name=f"{len(rows)} shown (newest first)",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    embed.set_footer(text=f"Steal A Galacticrot  ·  {PREFIX}del sanction <id>")
    await ctx.send(embed=embed)


@bot.command(name="del", aliases=["delsanction", "deletesanction"])
@require_staff_rank(2)  # Senior Moderator+
async def delete_sanction(ctx, *, args: str = ""):
    """Delete a sanction from +sanctions. Supports:
    - +del sanction <sanction_id> [reason]
    - +del sanction @user <sanction_id> [reason]
    - +del sanction @user #<position_in_list> [reason]
    - +del sanction @user all [reason] / +del sanction all @user [reason]
    - +delsanction <sanction_id> [reason] or +delsanction @user <sanction_id> [reason]
    """
    tokens = args.split()
    invoked = (ctx.invoked_with or "del").lower()
    if invoked == "del":
        if not tokens or tokens[0].lower() not in {"sanction", "sanctions", "case", "cases"}:
            return await ctx.send(
                f"Usage: `{PREFIX}del sanction <sanction_id> [reason]` or `{PREFIX}del sanction @user <sanction_id> [reason]`."
            )
        tokens = tokens[1:]
    elif tokens and tokens[0].lower() in {"sanction", "sanctions", "case", "cases"}:
        tokens = tokens[1:]

    if not tokens:
        return await ctx.send(
            f"Usage: `{PREFIX}del sanction <sanction_id> [reason]` or `{PREFIX}del sanction @user <sanction_id> [reason]`."
        )

    target_user = None
    user_match = re.fullmatch(r"<@!?(\d+)>", tokens[0])
    if user_match:
        uid = int(user_match.group(1))
        try:
            target_user = ctx.guild.get_member(uid) or await bot.fetch_user(uid)
            tokens = tokens[1:]
        except discord.NotFound:
            return await ctx.send("I couldn’t find that user. Check the mention or user ID.")
    elif tokens[0].isdigit() and len(tokens[0]) >= 15:
        uid = int(tokens[0])
        try:
            target_user = ctx.guild.get_member(uid) or await bot.fetch_user(uid)
            tokens = tokens[1:]
        except discord.NotFound:
            return await ctx.send("I couldn’t find a user with that ID.")

    if not tokens:
        return await ctx.send(
            f"Please include the sanction ID (or `all`) to delete. Example: `{PREFIX}del sanction @user 2` or `{PREFIX}del sanction 2`."
        )

    if tokens[0].lower() == "all":
        tokens = tokens[1:]
        if target_user is None and tokens:
            m2 = re.fullmatch(r"<@!?(\d+)>", tokens[0])
            if m2:
                uid = int(m2.group(1))
                try:
                    target_user = ctx.guild.get_member(uid) or await bot.fetch_user(uid)
                    tokens = tokens[1:]
                except discord.NotFound:
                    return await ctx.send("I couldn’t find that user.")
            elif tokens[0].isdigit() and len(tokens[0]) >= 15:
                uid = int(tokens[0])
                try:
                    target_user = ctx.guild.get_member(uid) or await bot.fetch_user(uid)
                    tokens = tokens[1:]
                except discord.NotFound:
                    return await ctx.send("I couldn’t find a user with that ID.")
        if target_user is None:
            return await ctx.send(f"Specify which member's sanctions to clear: `{PREFIX}del sanction @user all [reason]`.")
        reason = " ".join(tokens).strip() or "No reason provided"
        rows = db.execute(
            "SELECT id, sanction_type, source_warning_id FROM sanctions WHERE guild_id=? AND user_id=?",
            (ctx.guild.id, target_user.id),
        ).fetchall()
        if not rows:
            return await ctx.send(f"📋 **{target_user}** (`{target_user.id}`) has no sanctions to delete.")
        warning_ids = [r[2] for r in rows if (r[1] or "").lower() == "warn" and r[2] is not None]
        if warning_ids:
            placeholders = ",".join("?" for _ in warning_ids)
            db.execute(f"DELETE FROM warnings WHERE guild_id=? AND id IN ({placeholders})", (ctx.guild.id, *warning_ids))
        db.execute("DELETE FROM sanctions WHERE guild_id=? AND user_id=?", (ctx.guild.id, target_user.id))
        db.execute("DELETE FROM blacklist WHERE guild_id=? AND user_id=?", (ctx.guild.id, target_user.id))
        db.commit()
        await ctx.send(f"✅ Deleted all **{len(rows)}** sanction(s) for **{target_user}** (`{target_user.id}`).")
        asyncio.create_task(send_mod_log(
            ctx.guild,
            f"🧹 **All sanctions deleted**\nUser: {target_user} (`{target_user.id}`)\n"
            f"Count: **{len(rows)}**\nDeleted by: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}"
        ))
        return

    raw_id = tokens[0].lstrip("#")
    if not raw_id.isdigit():
        return await ctx.send(
            f"Invalid sanction ID `{discord.utils.escape_markdown(tokens[0])}`. Use a case number like `1` or `#1`."
        )
    sanction_id = int(raw_id)
    reason = " ".join(tokens[1:]).strip() or "No reason provided"

    if target_user is not None:
        row = db.execute(
            """SELECT id, user_id, sanction_type, reason, source_warning_id
               FROM sanctions WHERE guild_id=? AND user_id=? AND id=?""",
            (ctx.guild.id, target_user.id, sanction_id),
        ).fetchone()
        if row is None and sanction_id >= 1:
            user_rows = db.execute(
                """SELECT id, user_id, sanction_type, reason, source_warning_id
                   FROM sanctions WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 25""",
                (ctx.guild.id, target_user.id),
            ).fetchall()
            if sanction_id <= len(user_rows):
                row = user_rows[sanction_id - 1]
    else:
        row = db.execute(
            """SELECT id, user_id, sanction_type, reason, source_warning_id
               FROM sanctions WHERE guild_id=? AND id=?""",
            (ctx.guild.id, sanction_id),
        ).fetchone()

    if row is None:
        return await ctx.send("That sanction ID doesn’t match anything in the records. Check `+sanctions @user` and try again.")

    actual_id, user_id, sanction_type, original_reason, warning_id = row
    db.execute("DELETE FROM sanctions WHERE guild_id=? AND id=?", (ctx.guild.id, actual_id))
    if (sanction_type or "").lower() == "warn" and warning_id is not None:
        db.execute("DELETE FROM warnings WHERE guild_id=? AND id=?", (ctx.guild.id, warning_id))
    if (sanction_type or "").lower() == "blacklist":
        remaining_bl = db.execute(
            "SELECT 1 FROM sanctions WHERE guild_id=? AND user_id=? AND LOWER(sanction_type)='blacklist'",
            (ctx.guild.id, user_id),
        ).fetchone()
        if not remaining_bl:
            db.execute("DELETE FROM blacklist WHERE guild_id=? AND user_id=?", (ctx.guild.id, user_id))
    db.commit()
    await ctx.send(f"✅ Deleted sanction `#{actual_id}` ({sanction_type}) for <@{user_id}> (`{user_id}`).")
    asyncio.create_task(send_mod_log(
        ctx.guild,
        f"🧹 **Sanction deleted**\nSanction: `#{actual_id}` ({sanction_type})"
        f"\nUser ID: `{user_id}`\nOriginal reason: {original_reason}"
        f"\nDeleted by: {ctx.author} (`{ctx.author.id}`)\nDeletion reason: {reason}",
    ))

@bot.command(name="clear", aliases=["purge"])
@commands.guild_only()
@require_admin_actions()
async def clear_messages(ctx, target_or_amount: str = None, amount: int = None):
    """Delete messages in the channel (Admin / Perm 3+ only).
    Supports:
    - +clear <amount> (deletes the <amount> most recent messages)
    - Reply to a user with +clear <amount> (deletes up to <amount> of their recent messages)
    - +clear @user <amount> (deletes up to <amount> of that user's recent messages)
    """
    target_member = None
    delete_count = None

    if target_or_amount is not None and amount is not None:
        try:
            target_member = await resolve_member_arg(ctx, target_or_amount)
        except commands.BadArgument:
            return await ctx.send(f"Usage: `{PREFIX}clear <amount>` or reply to a member's message with `{PREFIX}clear <amount>`.")
        delete_count = amount
    elif target_or_amount is not None and amount is None:
        if target_or_amount.isdigit():
            delete_count = int(target_or_amount)
            if ctx.message.reference and ctx.message.reference.message_id:
                try:
                    ref_msg = ctx.message.reference.resolved
                    if ref_msg is None:
                        ref_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                    if ref_msg and getattr(ref_msg, "author", None):
                        target_member = ref_msg.author
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
        else:
            return await ctx.send(f"Usage: `{PREFIX}clear <amount>` or reply to a member's message with `{PREFIX}clear <amount>`.")

    if delete_count is None:
        return await ctx.send(f"Usage: `{PREFIX}clear <amount>` or reply to a member's message with `{PREFIX}clear <amount>`.")
    if delete_count < 1 or delete_count > 100:
        return await ctx.send("Please choose an amount between **1 and 100** messages.")

    cleared_message_ids.add(ctx.message.id)

    try:
        to_delete = []
        if target_member is None:
            async for msg in ctx.channel.history(limit=delete_count + 1):
                if msg.id == ctx.message.id:
                    continue
                to_delete.append(msg)
                if len(to_delete) >= delete_count:
                    break
        else:
            async for msg in ctx.channel.history(limit=300):
                if msg.id == ctx.message.id:
                    continue
                if msg.author.id == target_member.id:
                    to_delete.append(msg)
                    if len(to_delete) >= delete_count:
                        break

        # include the command message itself so it disappears with the rest
        to_delete.append(ctx.message)
        for m in to_delete:
            cleared_message_ids.add(m.id)

        now_utc = discord.utils.utcnow()
        bulk_eligible = [m for m in to_delete if (now_utc - m.created_at).total_seconds() < 1209500]
        older_msgs = [m for m in to_delete if (now_utc - m.created_at).total_seconds() >= 1209500]

        actual_deleted = 0
        if len(bulk_eligible) >= 2:
            await ctx.channel.delete_messages(bulk_eligible)
            actual_deleted += len(bulk_eligible)
        elif len(bulk_eligible) == 1:
            await bulk_eligible[0].delete()
            actual_deleted += 1

        for old_msg in older_msgs:
            try:
                await old_msg.delete()
                actual_deleted += 1
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        # don't count the command message itself in the "deleted" number we log
        if ctx.message in to_delete or ctx.message.id in {m.id for m in to_delete}:
            actual_deleted = max(0, actual_deleted - 1)

        # only log it, no public "X messages deleted" spam in the channel
        log_lines = [
            "🧹 **Messages cleared**",
            f"Channel: {ctx.channel.mention}",
        ]
        if target_member is not None:
            log_lines.append(f"Target: {target_member} (`{target_member.id}`)")
        log_lines.append(f"Deleted: **{actual_deleted}**")
        log_lines.append(f"By: {ctx.author} (`{ctx.author.id}`)")
        asyncio.create_task(send_mod_log(ctx.guild, "\n".join(log_lines)))
    except discord.Forbidden:
        await ctx.send("I don’t have the **Manage Messages** permission needed to delete messages here.")
    except discord.HTTPException as exc:
        logger.warning("Clear command HTTP error in channel %s: %s", ctx.channel.id, exc)
        await ctx.send("Discord prevented deleting some of those messages. Try again with a smaller amount.")

@bot.command()
@require_perm1()
async def lock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {ctx.author}")
    await ctx.send(
        embed=build_galaxy_embed(
            f"{ctx.channel.mention} is locked.\nMembers can’t send messages here until it’s unlocked.",
            title="GALAXY  /  CHANNEL LOCKED",
            color=WARN_COLOR,
            footer="GALAXY  ·  STAFF ACTION",
        )
    )
    await send_mod_log(ctx.guild, f"🔒 **Channel locked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def unlock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {ctx.author}")
    await ctx.send(
        embed=build_galaxy_embed(
            f"{ctx.channel.mention} is unlocked.\nMembers can chat here again.",
            title="GALAXY  /  CHANNEL UNLOCKED",
            color=SUCCESS_COLOR,
            footer="GALAXY  ·  STAFF ACTION",
        )
    )
    await send_mod_log(ctx.guild, f"🔓 **Channel unlocked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def slowmode(ctx, duration: str):
    raw = str(duration).strip().lower()
    if raw in {"0", "off", "disable", "none"}:
        seconds = 0
    elif raw.isdigit():
        seconds = int(raw)
    else:
        try:
            seconds = parse_duration_seconds(raw)
        except ValueError:
            return await ctx.send("Invalid slowmode duration. Examples: `0` (off), `10`, `30s`, `2m`, `1h` (max **6 hours**).")
    if not 0 <= seconds <= 21600:
        return await ctx.send("Slowmode can be anywhere from **0 seconds to 6 hours (21,600s)**.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {ctx.author}")
    label = "off" if seconds == 0 else format_duration_label(seconds)
    await ctx.send(
        embed=build_galaxy_embed(
            f"Slowmode in {ctx.channel.mention} is now **{label}**.",
            title="GALAXY  /  SLOWMODE",
            color=GALAXY_ACCENT,
            footer="GALAXY  ·  STAFF ACTION",
        )
    )
    asyncio.create_task(send_mod_log(
        ctx.guild,
        f"⏱️ **Slowmode changed**\nChannel: {ctx.channel.mention}\nSlowmode: {label}\nBy: {ctx.author} (`{ctx.author.id}`)"
    ))


@bot.command()
@require_perm1()
async def nick(ctx, member: FlexibleMember, *, nickname: str = None):
    if not can_moderate_target(ctx, member):
        return await ctx.send("You can’t change that member’s nickname because of role hierarchy.")
    await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
    await ctx.send(f"✏️ Changed {member.mention}’s nickname.")
    await send_mod_log(ctx.guild, f"✏️ **Nickname changed**\nMember: {member} (`{member.id}`)\nNew nickname: {nickname or 'Reset to username'}\nBy: {ctx.author} (`{ctx.author.id}`)")


async def find_role_by_name(ctx, role_name):
    """Find a role by mention, id, full name, or a short partial name."""
    text = (role_name or "").strip()
    if not text:
        return None
    # role ping: <@&123...>
    mention = re.fullmatch(r"<@&(\d{15,22})>", text)
    if mention:
        role = ctx.guild.get_role(int(mention.group(1)))
        if role is not None:
            return role
    # raw snowflake
    if text.isdigit() and len(text) >= 15:
        role = ctx.guild.get_role(int(text))
        if role is not None:
            return role
    # mention buried in the string
    buried = re.search(r"<@&(\d{15,22})>", text)
    if buried:
        role = ctx.guild.get_role(int(buried.group(1)))
        if role is not None:
            return role

    wanted = text.casefold()
    roles = [r for r in ctx.guild.roles if not r.is_default()]

    # 1) exact name
    exact = [r for r in roles if r.name.casefold() == wanted]
    if exact:
        return exact[0]

    # 2) starts with what they typed (e.g. "partner" -> "Partnership Manager")
    starts = [r for r in roles if r.name.casefold().startswith(wanted)]
    if len(starts) == 1:
        return starts[0]
    if len(starts) > 1:
        starts.sort(key=lambda r: len(r.name))
        return starts[0]

    # 3) name contains what they typed
    contains = [r for r in roles if wanted in r.name.casefold()]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        contains.sort(key=lambda r: len(r.name))
        return contains[0]

    return None


async def update_member_role(ctx, member, role_name, adding):
    role = await find_role_by_name(ctx, role_name)
    if role is None:
        return await ctx.send(f"I couldn’t find that role. Ping it, use the name, or paste the role id.")
    if role.is_default() or role.managed:
        return await ctx.send("I can’t manage the @everyone role or a role managed by an integration.")
    # Special users + server owner bypass the *author* role hierarchy.
    # The bot's own role must still sit above the target role (Discord rule).
    if (
        ctx.author.id not in SPECIAL_USERS
        and ctx.author.id != ctx.guild.owner_id
        and role >= ctx.author.top_role
    ):
        return await ctx.send("I can’t manage that role because it is equal to or higher than your highest role.")
    me = ctx.guild.me
    if me is None or role >= me.top_role:
        bot_role = me.top_role.mention if me and me.top_role else "my bot role"
        return await ctx.send(
            f"Discord won’t let me manage {role.mention} because that role is **equal to or above** {bot_role}.\n"
            f"**Fix:** Server Settings → Roles → drag **my bot role** *above* **{role.name}**, then try again."
        )
    # Special users can manage anyone (including themselves); others still respect hierarchy.
    if member.id != ctx.author.id and not can_moderate_target(ctx, member):
        return await ctx.send("You can’t manage roles for that member because of role hierarchy.")
    if adding and role in member.roles:
        return await ctx.send(f"{member.mention} already has {role.mention}.")
    if not adding and role not in member.roles:
        return await ctx.send(f"{member.mention} doesn’t have {role.mention}.")
    action = "added" if adding else "removed"
    try:
        if adding:
            await member.add_roles(role, reason=f"Role added by {ctx.author} ({ctx.author.id})")
        else:
            await member.remove_roles(role, reason=f"Role removed by {ctx.author} ({ctx.author.id})")
    except discord.Forbidden:
        return await ctx.send("I don’t have permission to manage that role. Check that I have Manage Roles and that my bot role is above it.")
    except discord.HTTPException:
        logger.exception("Role update failed for member %s, role %s", member.id, role.id)
        return await ctx.send("Discord couldn’t complete that role update. Please try again.")
    await ctx.send(f"✅ {role.mention} {action} {member.mention}.")
    await send_mod_log(ctx.guild, f"🎭 **Role {action}**\nRole: {role.name} (`{role.id}`)\nMember: {member} (`{member.id}`)\nBy: {ctx.author} (`{ctx.author.id}`)")


async def _parse_role_command_args(ctx, raw: str):
    """Parse optional member + role name for +addrole / +delrole.

    Forms:
      @member Role Name
      Role Name            -> target self
      (reply) Role Name    -> reply author already injected into message content
    """
    raw = (raw or "").strip()
    if not raw:
        return None, None

    # Prefer explicit mention / snowflake at the start as the member
    mention = re.match(r"^(<@!?(\d+)>|(\d{15,22}))\s+(.+)$", raw)
    if mention:
        member_token = mention.group(1)
        role_name = mention.group(4).strip()
        try:
            member = await resolve_member_arg(ctx, member_token)
            return member, role_name
        except commands.BadArgument:
            pass

    # Try first token as member; if that fails, whole string is the role (target = self)
    parts = raw.split(None, 1)
    if len(parts) == 2:
        try:
            member = await resolve_member_arg(ctx, parts[0])
            return member, parts[1].strip()
        except commands.BadArgument:
            pass

    # No member resolved — role is the full text, target self
    return ctx.author, raw


@bot.command(name="addrole")
@commands.guild_only()
@require_staff_rank(5)
async def addrole(ctx, *, args: str = None):
    """Add a role to a member (or yourself).
    Examples:
      +addrole @user Staff Manager
      +addrole Staff Manager          (gives it to you)
      reply to someone: +addrole Staff Manager
    """
    if not args or not args.strip():
        return await ctx.send(
            f"Usage: `{PREFIX}addrole @member Role Name`\n"
            f"`{PREFIX}addrole Role Name` (yourself)\n"
            f"Or reply to them: `{PREFIX}addrole Role Name`"
        )
    member, role_name = await _parse_role_command_args(ctx, args)
    if member is None or not role_name:
        return await ctx.send(
            f"Usage: `{PREFIX}addrole @member Role Name` or `{PREFIX}addrole Role Name`"
        )
    await update_member_role(ctx, member, role_name, adding=True)


@bot.command(name="delrole")
@commands.guild_only()
@require_staff_rank(5)
async def delrole(ctx, *, args: str = None):
    """Remove a role from a member (or yourself).
    Examples:
      +delrole @user Manager Team
      +delrole Manager Team           (removes it from you)
      reply to someone: +delrole Manager Team
    """
    if not args or not args.strip():
        return await ctx.send(
            f"Usage: `{PREFIX}delrole @member Role Name`\n"
            f"`{PREFIX}delrole Role Name` (yourself)\n"
            f"Or reply to them: `{PREFIX}delrole Role Name`"
        )
    member, role_name = await _parse_role_command_args(ctx, args)
    if member is None or not role_name:
        return await ctx.send(
            f"Usage: `{PREFIX}delrole @member Role Name` or `{PREFIX}delrole Role Name`"
        )
    await update_member_role(ctx, member, role_name, adding=False)


@bot.command(name="temprole", aliases=["temproles", "temproleadd"])
@commands.guild_only()
@require_staff_rank(5)
async def temprole(ctx, member: FlexibleMember = None, duration: str = None, *, role_name: str = None):
    """Give a role for a limited time.
    Examples:
      +temprole @user 1h Cool Role
      reply to someone +temprole 30m Cool Role
      +temprole @user 2d Event Access
    """
    # reply can supply the member (handled by FlexibleMember / reply injector)
    if member is None or duration is None or not role_name:
        return await ctx.send(
            f"Usage: `{PREFIX}temprole @user <time> <role name>`\n"
            f"Or reply to their message: `{PREFIX}temprole <time> <role name>`\n"
            f"Time examples: `10m`, `1h`, `6h`, `1d`, `7d`"
        )

    try:
        seconds = parse_duration_seconds(duration)
    except ValueError:
        return await ctx.send(
            f"Invalid time. Examples: `10m`, `30m`, `1h`, `6h`, `1d`.\n"
            f"Full usage: `{PREFIX}temprole @user 1h Role Name`"
        )
    if seconds < 60:
        return await ctx.send("Temp role has to last at least **1 minute**.")
    if seconds > 30 * 86400:
        return await ctx.send("Temp role can’t be longer than **30 days**.")

    role = await find_role_by_name(ctx, role_name)
    if role is None:
        return await ctx.send(f"I couldn’t find a role named **{discord.utils.escape_markdown(role_name)}**.")
    if role.is_default() or role.managed:
        return await ctx.send("I can’t manage the @everyone role or a role managed by an integration.")
    if (
        ctx.author.id not in SPECIAL_USERS
        and ctx.author.id != ctx.guild.owner_id
        and role >= ctx.author.top_role
    ):
        return await ctx.send("That role is equal to or higher than your highest role.")
    me = ctx.guild.me
    if me is None or role >= me.top_role:
        bot_role = me.top_role.mention if me and me.top_role else "my bot role"
        return await ctx.send(
            f"Discord won’t let me manage {role.mention} because that role is **equal to or above** {bot_role}.\n"
            f"**Fix:** Server Settings → Roles → drag **my bot role** *above* **{role.name}**, then try again."
        )
    if member.id != ctx.author.id and not can_moderate_target(ctx, member):
        return await ctx.send("You can’t manage roles for that member because of role hierarchy.")

    already_had = role in member.roles
    if not already_had:
        try:
            await member.add_roles(role, reason=f"Temp role by {ctx.author} ({ctx.author.id}) for {format_duration_label(seconds)}")
        except discord.Forbidden:
            return await ctx.send("I don’t have permission to add that role. Check Manage Roles + role order.")
        except discord.HTTPException:
            logger.exception("Temp role add failed for %s / %s", member.id, role.id)
            return await ctx.send("Discord couldn’t add that role. Try again.")

    key = (ctx.guild.id, member.id, role.id)
    old_task = temp_role_tasks.pop(key, None)
    if old_task and not old_task.done():
        old_task.cancel()

    async def _remove_later():
        try:
            await asyncio.sleep(seconds)
            # re-fetch in case they left / rejoined
            target = ctx.guild.get_member(member.id)
            if target is None:
                return
            current_role = ctx.guild.get_role(role.id)
            if current_role is None or current_role not in target.roles:
                return
            await target.remove_roles(current_role, reason="Temp role expired")
            await send_mod_log(
                ctx.guild,
                f"⌛ **Temp role expired**\nRole: {current_role.name} (`{current_role.id}`)\n"
                f"Member: {target} (`{target.id}`)",
            )
        except asyncio.CancelledError:
            return
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to remove temp role %s from %s", role.id, member.id)
        finally:
            temp_role_tasks.pop(key, None)

    temp_role_tasks[key] = asyncio.create_task(_remove_later())
    label = format_duration_label(seconds)
    if already_had:
        await ctx.send(
            f"⏱️ {member.mention} already had {role.mention}. Timer reset — it will be removed in **{label}**."
        )
    else:
        await ctx.send(
            f"⏱️ Gave {member.mention} {role.mention} for **{label}**. It’ll be removed automatically."
        )
    await send_mod_log(
        ctx.guild,
        f"⏱️ **Temp role added**\nRole: {role.name} (`{role.id}`)\n"
        f"Member: {member} (`{member.id}`)\nDuration: {label}\n"
        f"By: {ctx.author} (`{ctx.author.id}`)",
    )


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


@bot.command(aliases=["whois"])
async def userinfo(ctx, target_input: str = None):
    if target_input is None:
        target = ctx.author
    else:
        try:
            target = await resolve_member_arg(ctx, target_input)
        except commands.BadArgument:
            target = await resolve_user_arg(ctx, target_input)
    is_member = isinstance(target, discord.Member)
    embed = discord.Embed(
        title="GALAXY  /  USER INFO",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(target), icon_url=target.display_avatar.url)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="User ID", value=f"`{target.id}`", inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(target.created_at, style="R"), inline=True)
    if is_member:
        embed.add_field(
            name="Joined",
            value=discord.utils.format_dt(target.joined_at, style="R") if target.joined_at else "Unknown",
            inline=True,
        )
        embed.add_field(name="Top role", value=target.top_role.mention, inline=True)
        rank = effective_staff_rank(target)
        embed.add_field(name="Staff rank", value=f"Perm **{rank}**" if rank else "Member", inline=True)
        coins = get_coin_balance(ctx.guild.id, target.id)
        embed.add_field(name="Galaxy Coins", value=f"**{coins:,}**", inline=True)
    else:
        embed.add_field(name="Status", value="Not currently in this server", inline=True)
    embed.set_footer(text="GALAXY  ·  LOOKUP")
    await ctx.send(embed=embed)


@bot.command()
@require_perm1()
async def serverinfo(ctx):
    guild = ctx.guild
    embed = discord.Embed(
        title="GALAXY  /  SERVER INFO",
        description=f"**{guild.name}**",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.add_field(name="Server ID", value=f"`{guild.id}`", inline=True)
    embed.add_field(name="Members", value=f"**{guild.member_count:,}**", inline=True)
    embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, style="R"), inline=True)
    embed.add_field(name="Channels", value=f"**{len(guild.channels)}**", inline=True)
    embed.add_field(name="Roles", value=f"**{len(guild.roles)}**", inline=True)
    embed.set_footer(text="GALAXY  ·  LOOKUP")
    await ctx.send(embed=embed)


@bot.command()
async def avatar(ctx, target_input: str = None):
    if target_input is None:
        target = ctx.author
    else:
        try:
            target = await resolve_member_arg(ctx, target_input)
        except commands.BadArgument:
            target = await resolve_user_arg(ctx, target_input)
    display = getattr(target, "display_name", getattr(target, "global_name", None) or target.name)
    embed = discord.Embed(
        title="GALAXY  /  AVATAR",
        description=f"**{discord.utils.escape_markdown(display)}**",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_image(url=target.display_avatar.url)
    embed.set_footer(text="GALAXY  ·  LOOKUP")
    await ctx.send(embed=embed)


@bot.command(aliases=["mc"])
async def membercount(ctx):
    embed = discord.Embed(
        title="GALAXY  /  MEMBER COUNT",
        description=f"**{ctx.guild.name}** has **{ctx.guild.member_count:,}** members.",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="GALAXY  ·  LOOKUP")
    await ctx.send(embed=embed)


@bot.command()
@require_perm1()
async def roleinfo(ctx, role: discord.Role):
    embed = discord.Embed(
        title="GALAXY  /  ROLE INFO",
        description=f"{role.mention} · **{discord.utils.escape_markdown(role.name)}**",
        color=role.color if role.color.value else GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Role ID", value=f"`{role.id}`", inline=True)
    embed.add_field(name="Members", value=f"**{len(role.members):,}**", inline=True)
    embed.add_field(name="Position", value=f"**{role.position}**", inline=True)
    embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No", inline=True)
    embed.add_field(name="Hoisted", value="Yes" if role.hoist else "No", inline=True)
    embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, style="R"), inline=True)
    embed.set_footer(text="GALAXY  ·  LOOKUP")
    await ctx.send(embed=embed)


@bot.command()
@require_perm1()
async def ping(ctx):
    ms = round(bot.latency * 1000)
    embed = discord.Embed(
        title="GALAXY  /  PING",
        description=f"Right here.\nLatency: **`{ms} ms`**",
        color=SUCCESS_COLOR if ms < 200 else WARN_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="GALAXY  ·  STATUS")
    await ctx.send(embed=embed)


@bot.command(name="botinfo", aliases=["uptime", "about"])
async def botinfo(ctx):
    """Bot status, uptime, and basic diagnostics."""
    uptime_s = max(0, int(time.time() - BOT_STARTED_AT))
    days, rem = divmod(uptime_s, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    uptime_label = " ".join(parts)
    latency = round(bot.latency * 1000)
    guilds = len(bot.guilds)
    members = sum(g.member_count or 0 for g in bot.guilds)
    coin_holders = 0
    coin_total = 0
    try:
        row = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(balance), 0) FROM galaxy_coins WHERE balance > 0"
        ).fetchone()
        coin_holders = int(row[0] or 0)
        coin_total = int(row[1] or 0)
    except Exception:
        logger.exception("botinfo coin stats failed")
    embed = discord.Embed(
        title="GALAXY  /  BOT INFO",
        description="Steal A Galacticrot community systems.",
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Uptime", value=f"**{uptime_label}**", inline=True)
    embed.add_field(name="Latency", value=f"**{latency} ms**", inline=True)
    embed.add_field(name="Servers", value=f"**{guilds}**", inline=True)
    embed.add_field(name="Members (cached)", value=f"**{members:,}**", inline=True)
    embed.add_field(name="Coin holders", value=f"**{coin_holders:,}**", inline=True)
    embed.add_field(name="Coins in circulation", value=f"**{coin_total:,}**", inline=True)
    embed.add_field(name="Prefix", value=f"`{PREFIX}`", inline=True)
    embed.add_field(name="discord.py", value=f"`{getattr(discord, '__version__', '?')}`", inline=True)
    if bot.user:
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.set_footer(text=f"GALAXY  ·  {bot.user}  ·  ID {bot.user.id}")
    else:
        embed.set_footer(text="GALAXY  ·  STATUS")
    await ctx.send(embed=embed)


@bot.command(name="poll")
@commands.cooldown(1, 10, commands.BucketType.user)
@commands.guild_only()
async def poll(ctx, *, question_and_options: str = None):
    """Create a real Discord poll with a timer.

    Examples:
      +poll "whos better" "larper" "spiderman"
      +poll 24h "who is the best moderator" "spiderman" "larper"
      +poll Question? | Option A | Option B
      +poll Should we do this?          (Yes / No, 24h)
    """
    if not question_and_options or not question_and_options.strip():
        return await ctx.send(
            f"Usage:\n"
            f"`{PREFIX}poll \"question\" \"option 1\" \"option 2\"`\n"
            f"`{PREFIX}poll 24h \"question\" \"option 1\" \"option 2\"`\n"
            f"`{PREFIX}poll Question? | Option A | Option B`\n"
            f"`{PREFIX}poll Should we do this?`  (Yes/No)\n\n"
            f"Timer options: `1h` `4h` `8h` `24h` `3d` `7d` (default **24h**)."
        )

    raw = question_and_options.strip()

    # Discord native polls only allow these durations (in hours).
    ALLOWED_HOURS = (1, 4, 8, 24, 72, 168)

    def _snap_hours(hours: float) -> int:
        hours = max(1, int(round(hours)))
        return min(ALLOWED_HOURS, key=lambda h: abs(h - hours))

    duration_hours = 24  # default
    duration_token = None

    # Pull an optional duration from the start or end (1h, 24h, 3d, 7days, etc.)
    duration_match = re.match(
        r"^(?P<dur>\d+(?:\.\d+)?\s*(?:h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|m|min|mins|minute|minutes))\s+(.+)$",
        raw,
        flags=re.I,
    )
    if duration_match:
        duration_token = duration_match.group("dur")
        raw = duration_match.group(2).strip()
    else:
        duration_match = re.match(
            r"^(.+?)\s+(?P<dur>\d+(?:\.\d+)?\s*(?:h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks))$",
            raw,
            flags=re.I,
        )
        if duration_match:
            # only treat as duration if it isn't the only "option"
            duration_token = duration_match.group("dur")
            raw = duration_match.group(1).strip()

    if duration_token:
        try:
            seconds = parse_duration_seconds(duration_token)
            duration_hours = _snap_hours(seconds / 3600)
        except ValueError:
            duration_hours = 24

    # Parse quoted arguments: "question" "opt1" "opt2"
    question = None
    options = []
    try:
        import shlex
        tokens = shlex.split(raw)
    except ValueError:
        tokens = []

    if len(tokens) >= 2 and (
        (raw.startswith('"') or raw.startswith("'"))
        or (raw.count('"') >= 2 or raw.count("'") >= 2)
    ):
        question = tokens[0]
        options = [t for t in tokens[1:] if t.strip()][:10]
    elif "|" in raw:
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        if parts:
            question = parts[0]
            options = parts[1:10]
    elif len(tokens) >= 3:
        # Unquoted multi-word is ambiguous; treat first token as question only if
        # remaining tokens look like single-word options: +poll better larper spiderman
        # Prefer: first token question, rest options when all options are single tokens.
        question = tokens[0]
        options = tokens[1:10]
    else:
        question = raw
        options = []

    if not question:
        return await ctx.send("I need a question for the poll.")

    question = question.strip().strip('"').strip("'")
    options = [o.strip().strip('"').strip("'") for o in options if o and o.strip()]

    if len(options) < 2:
        options = ["Yes", "No"]

    # Native Discord poll limits
    if len(question) > 300:
        return await ctx.send("Keep the question under **300** characters.")
    if len(options) > 10:
        return await ctx.send("Discord polls support at most **10** options.")
    for opt in options:
        if len(opt) > 55:
            return await ctx.send(
                f"Each option must be **55 characters or fewer** (Discord limit).\n"
                f"Too long: `{opt[:60]}…`"
            )
        if not opt:
            return await ctx.send("Options can’t be empty.")

    # Prefer real Discord polls (looks native + has a built-in timer).
    if hasattr(discord, "Poll"):
        try:
            poll_obj = discord.Poll(
                question=question[:300],
                duration=timedelta(hours=duration_hours),
                multiple=False,
            )
            for opt in options:
                poll_obj.add_answer(text=opt[:55])
            # Try to hide the invoke message for a cleaner poll post
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
            await ctx.send(poll=poll_obj)
            return
        except TypeError:
            # Older discord.py signature differences — try hours as int
            try:
                poll_obj = discord.Poll(question=question[:300], duration=duration_hours)
                for opt in options:
                    poll_obj.add_answer(text=opt[:55])
                try:
                    await ctx.message.delete()
                except (discord.Forbidden, discord.HTTPException):
                    pass
                await ctx.send(poll=poll_obj)
                return
            except Exception:
                logger.exception("Native poll creation failed; falling back to reaction poll")
        except Exception:
            logger.exception("Native poll creation failed; falling back to reaction poll")

    # Fallback: reaction poll with visible timer in the embed
    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    ends_at = discord.utils.utcnow() + timedelta(hours=duration_hours)
    lines = [f"**{discord.utils.escape_markdown(question)}**", ""]
    for i, opt in enumerate(options):
        lines.append(f"{number_emojis[i]}  {discord.utils.escape_markdown(opt[:100])}")
    lines.append("")
    lines.append(f"⏱ Ends {discord.utils.format_dt(ends_at, style='R')} · closes <t:{int(ends_at.timestamp())}:f>")
    embed = discord.Embed(
        title="GALAXY  /  POLL",
        description="\n".join(lines),
        color=GALAXY_ACCENT,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"Poll by {ctx.author}  ·  react to vote  ·  {duration_hours}h")
    msg = await ctx.send(embed=embed)
    for i in range(len(options)):
        try:
            await msg.add_reaction(number_emojis[i])
        except (discord.Forbidden, discord.HTTPException):
            break


@bot.command(name="remind", aliases=["remindme"])
@commands.cooldown(1, 5, commands.BucketType.user)
async def remind(ctx, duration: str = None, *, text: str = None):
    """Set a personal reminder. Example: +remind 30m check the event"""
    if duration is None or not text:
        return await ctx.send(
            f"Usage: `{PREFIX}remind <time> <message>`\n"
            f"Examples: `{PREFIX}remind 10m stretch` · `{PREFIX}remind 2h staff meeting`"
        )
    try:
        seconds = parse_duration_seconds(duration)
    except ValueError:
        return await ctx.send(
            f"Invalid time. Examples: `10m`, `1h`, `2h30m` is not supported — use `2h`.\n"
            f"Try: `{PREFIX}remind 30m check applications`"
        )
    if seconds < 30:
        return await ctx.send("Reminders need to be at least **30 seconds**.")
    if seconds > 7 * 86400:
        return await ctx.send("Reminders can’t be longer than **7 days**.")
    label = format_duration_label(seconds)
    await ctx.send(
        embed=build_galaxy_embed(
            f"Okay {ctx.author.mention} — I’ll remind you in **{label}**:\n> {discord.utils.escape_markdown(text[:500])}",
            title="GALAXY  /  REMINDER SET",
            color=SUCCESS_COLOR,
            footer="GALAXY  ·  REMINDERS",
        )
    )

    async def _fire():
        try:
            await asyncio.sleep(seconds)
            panel = build_galaxy_embed(
                f"⏰ {ctx.author.mention} reminder:\n> {discord.utils.escape_markdown(text[:900])}",
                title="GALAXY  /  REMINDER",
                color=GALAXY_ACCENT,
                footer="GALAXY  ·  REMINDERS",
            )
            try:
                await ctx.author.send(embed=panel)
            except (discord.Forbidden, discord.HTTPException):
                try:
                    await ctx.channel.send(
                        content=ctx.author.mention,
                        embed=panel,
                        allowed_mentions=discord.AllowedMentions(users=[ctx.author]),
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("Could not deliver reminder to %s", ctx.author.id)
        except asyncio.CancelledError:
            return

    task = asyncio.create_task(_fire())
    active_reminders.setdefault(ctx.author.id, []).append(task)


# ---------------------------------------------------------------------------
# GAMES: +code (first to type the code wins coins) is Trial Director
# and above ONLY, by ROLE. Discord Administrator permission, server ownership
# and lower staff roles (Head Admin and below) do NOT grant access.
# +poker (5-card draw card game, 2-5 players) is open to everyone.
# ---------------------------------------------------------------------------
def require_asm_plus():
    async def predicate(ctx):
        return ctx.guild is not None and can_start_riddles(ctx.author)
    return commands.check(predicate)


# +code: Trial Director and above ONLY, by role (Head Director is above Trial Director,
# so it is included). Discord Administrator permission does not bypass this.
CODE_COMMAND_ROLE_IDS = {
    ROLE_IDS["trial_director"], ROLE_IDS["head_director"],
    ROLE_IDS["senior_drc"], ROLE_IDS["drc"], ROLE_IDS["axelle"], ROLE_IDS["dev"],
    ROLE_IDS["main_founder"], ROLE_IDS["founder"], ROLE_IDS["co_owner"],
}


def require_director_plus():
    async def predicate(ctx):
        return ctx.guild is not None and bool(member_role_ids(ctx.author) & CODE_COMMAND_ROLE_IDS)
    return commands.check(predicate)


CODE_EXPIRE_SECONDS = 10 * 60      # an unclaimed code expires after 10 minutes
CODE_PING_ROLE_ID = 1571339142816858132  # role that can get pinged when a code drops
CODE_PING_CHANCE = 0.45  # chance (0.0-1.0) that an AUTOMATIC drop pings the role (+code always pings)
MAX_CODE_REWARD = 1_000_000        # safety cap so a typo can't print a fortune
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I confusion
active_codes = {}                  # (guild_id, "manual"|"auto") -> {"code", "amount", "channel_id", ...}


async def _award_code_claim(message, active):
    """Pay the first person who typed the active code."""
    guild_id = message.guild.id
    try:
        change_coins(guild_id, message.author.id, active["amount"])
        new_bal = get_coin_balance(guild_id, message.author.id)
    except Exception:
        logger.exception("Code payout failed for user %s", message.author.id)
        # Restore this claim if the payout failed, so stock is never consumed unfairly.
        active["remaining"] = active.get("remaining", 1) + 1
        active.setdefault("claimed_by", set()).discard(message.author.id)
        active_codes[(guild_id, active.get("slot", "manual"))] = active
        return False
    panel = build_galaxy_embed(
        f"› {message.author.mention} typed the code first and won "
        f"**{active['amount']:,} Galaxy Coins**!\n"
        f"Balance: **{new_bal:,}**",
        title="GALAXY  /  CODE CLAIMED", color=SUCCESS_COLOR, footer="GALAXY  ·  CODE DROP",
    )
    try:
        await message.channel.send(
            embed=panel,
            allowed_mentions=discord.AllowedMentions(users=[message.author], roles=False, everyone=False),
        )
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to announce code winner in %s", message.channel.id)
    # Update the drop panel after each successful claim; close it when stock is empty.
    if active.get("message_id"):
        try:
            drop_channel = message.guild.get_channel(active["channel_id"]) or message.channel
            drop_message = await drop_channel.fetch_message(active["message_id"])
            if active.get("remaining", 0) <= 0:
                updated = build_galaxy_embed(
                    f"This code is out of stock. Last claim: {message.author.mention}.",
                    title="GALAXY  /  CODE DROP (SOLD OUT)", color=GALAXY_COLOR, footer="GALAXY  ·  CODE DROP",
                )
            else:
                updated = drop_message.embeds[0].copy() if drop_message.embeds else build_galaxy_embed(
                    "Code still available.", title="GALAXY  /  CODE DROP", color=GALAXY_COLOR,
                    footer="GALAXY  ·  CODE DROP",
                )
                updated.description = (
                    f"First **{active['remaining']}** people to type this code in chat can still win "
                    f"**{active['amount']:,} Galaxy Coins** each.\n\n"
                    f"# `{_anti_paste_code(active['code'])}`\n\n"
                    f"You have to **type it out** — copy and paste won't work.\n"
                    f"Expires in **{CODE_EXPIRE_SECONDS // 60} minutes**."
                )
            await drop_message.edit(embed=updated)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            pass
    await send_mod_log(
        message.guild,
        f"🎟️ **Code claimed**\nWinner: {message.author} (`{message.author.id}`)"
        f"\nAmount: {active['amount']:,}\nStarted by: <@{active['starter_id']}>",
    )
    return True


# Invisible characters hidden inside the displayed code. They are copied along with
# the code, so a pasted code no longer matches and has to be typed by hand.
CODE_INVISIBLE_CHARS = ("\u200b", "\u200c", "\u200d", "\u2060")


def _anti_paste_code(code):
    """The code as shown to members: looks normal, but pasting it won't work."""
    return "".join(ch + random.choice(CODE_INVISIBLE_CHARS) for ch in code)


def _strip_invisible(text):
    for ch in CODE_INVISIBLE_CHARS:
        text = text.replace(ch, "")
    return text


AUTO_CODE_CHANNEL_ID = 1575961199047348366   # channel where the bot drops codes by itself
AUTO_CODE_MIN = 1                            # smallest automatic reward (coins)
AUTO_CODE_MAX = 50_000_000_000               # biggest automatic reward: 50b coins
AUTO_CODE_INTERVAL_SECONDS = 30 * 60         # an automatic drop every 30 minutes, whether or not staff used +code
AUTO_CODE_STOCK = 3                           # stock when an automatic drop gets the bonus stock
AUTO_CODE_STOCK_CHANCE = 0.10                 # 10% chance an automatic code gets 3 claims; otherwise it gets 1
MAX_CODE_STOCK = 10_000                       # prevents accidental huge stock values
last_auto_code_at = {}                       # guild_id -> time of the latest AUTOMATIC drop


def _code_drop_embed(reward, code, remaining):
    """Build the code panel with the current number of claims remaining."""
    return build_galaxy_embed(
        f"First **{remaining}** people to type this code in chat (any channel) each win "
        f"**{reward:,} Galaxy Coins**!\n\n"
        f"# `{_anti_paste_code(code)}`\n\n"
        f"You have to **type it out** — copy and paste won't work.\n"
        f"Expires in **{CODE_EXPIRE_SECONDS // 60} minutes**.",
        title="GALAXY  /  CODE DROP", color=GALAXY_ACCENT, footer="GALAXY  ·  CODE DROP",
    )


async def _send_code_drop(channel, reward, starter_id, starter_label, always_ping=False, slot="manual", stock=1):
    """Post a code drop with a limited number of claims."""
    guild = channel.guild
    code = "".join(random.choice(CODE_ALPHABET) for _ in range(8))
    panel = _code_drop_embed(reward, code, stock)
    # +code always pings the role; automatic drops only ping some of the time.
    if always_ping or random.random() < CODE_PING_CHANCE:
        sent = await channel.send(
            content=f"<@&{CODE_PING_ROLE_ID}>",
            embed=panel,
            allowed_mentions=discord.AllowedMentions(
                roles=[discord.Object(id=CODE_PING_ROLE_ID)], users=False, everyone=False),
        )
    else:
        sent = await channel.send(embed=panel, allowed_mentions=discord.AllowedMentions.none())
    now = time.time()
    active_codes[(guild.id, slot)] = {
        "code": code, "amount": reward, "channel_id": channel.id, "slot": slot,
        "message_id": sent.id, "starter_id": starter_id, "expires_at": now + CODE_EXPIRE_SECONDS,
        "remaining": stock, "stock": stock, "claimed_by": set(),
    }
    await send_mod_log(
        guild,
        f"🎟️ **Code dropped**\nBy: {starter_label}\nChannel: {channel.mention}"
        f"\nReward per claim: {reward:,}\nStock: {stock}",
    )
    return sent


async def auto_code_scheduler():
    """Drop a code by itself in the code channel every AUTO_CODE_INTERVAL_SECONDS (30 min)."""
    await bot.wait_until_ready()
    # Start the clock at boot so a restart never fires a code instantly.
    for guild in bot.guilds:
        last_auto_code_at.setdefault(guild.id, time.time())
    while not bot.is_closed():
        await asyncio.sleep(15)
        try:
            for guild in bot.guilds:
                channel = guild.get_channel(AUTO_CODE_CHANNEL_ID)
                if not isinstance(channel, discord.TextChannel):
                    continue
                now = time.time()
                last = last_auto_code_at.setdefault(guild.id, now)
                if now - last < AUTO_CODE_INTERVAL_SECONDS:
                    continue
                last_auto_code_at[guild.id] = now  # reset first so a failure can't loop-spam
                reward = random.randint(AUTO_CODE_MIN, AUTO_CODE_MAX)
                # Automatic drops normally have 1 claim. Only 10% (configurable)
                # receive bonus stock (3 claims by default). Each member can claim
                # a particular code only once.
                auto_stock = AUTO_CODE_STOCK if random.random() < AUTO_CODE_STOCK_CHANCE else 1
                await _send_code_drop(
                    channel, reward, bot.user.id, "automatic drop (bot)",
                    slot="auto", stock=auto_stock,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic code drop failed")


@bot.command(name="code", aliases=["dropcode", "coincode"])
@commands.guild_only()
@require_director_plus()
async def code_cmd(ctx, amount: str = None, stock: str = "1"):
    """Drop a code with limited claims. Usage: +code [reward] [stock]"""
    current = active_codes.get((ctx.guild.id, "manual"))
    if current and time.time() < current["expires_at"]:
        return await ctx.send(
            f"There's already an active code in <#{current['channel_id']}>. "
            "Wait for it to be claimed or to expire."
        )
    if amount is None:
        reward = random.randint(GALAXY_QUESTION_REWARD_MIN, GALAXY_QUESTION_REWARD_MAX)
    else:
        try:
            reward = parse_coin_amount(amount)
        except ValueError:
            return await ctx.send(
                f"Usage: `{PREFIX}code [reward] [stock]` — example: `{PREFIX}code 500 10`."
            )
        if not 1 <= reward <= MAX_CODE_REWARD:
            return await ctx.send(f"The reward must be between **1** and **{MAX_CODE_REWARD:,}** coins.")

    try:
        claim_stock = int(stock)
    except (TypeError, ValueError):
        return await ctx.send("Stock must be a whole number.")
    if not 1 <= claim_stock <= MAX_CODE_STOCK:
        return await ctx.send(f"Stock must be between **1** and **{MAX_CODE_STOCK:,}** claims.")

    await _send_code_drop(
        ctx.channel, reward, ctx.author.id, f"{ctx.author} (`{ctx.author.id}`)",
        always_ping=True, stock=claim_stock,
    )


# ----------------------------- 1v1 POKER -----------------------------------
POKER_TURN_TIMEOUT = 120           # seconds to choose cards before auto-standing
POKER_SUITS = ("♠", "♥", "♦", "♣")
POKER_RANK_LABELS = {11: "J", 12: "Q", 13: "K", 14: "A"}
POKER_HAND_NAMES = (
    "High Card", "Pair", "Two Pair", "Three of a Kind", "Straight",
    "Flush", "Full House", "Four of a Kind", "Straight Flush",
)
poker_busy = set()                 # (guild_id, user_id) in a pending/active duel


def _card_text(card):
    return f"{POKER_RANK_LABELS.get(card[0], str(card[0]))}{POKER_SUITS[card[1]]}"


def _hand_text(cards):
    return "  ".join(f"`{_card_text(c)}`" for c in cards)


def evaluate_poker_hand(cards):
    """Return a comparable (category, tiebreakers) tuple for a 5-card hand."""
    ranks = sorted((c[0] for c in cards), reverse=True)
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    ordered = [r for r, _ in groups]
    flush = len({c[1] for c in cards}) == 1
    unique = sorted(set(ranks), reverse=True)
    straight_high = None
    if len(unique) == 5:
        if unique[0] - unique[4] == 4:
            straight_high = unique[0]
        elif unique == [14, 5, 4, 3, 2]:  # A-2-3-4-5 "wheel"
            straight_high = 5
    if straight_high and flush:
        return (8, [straight_high])
    if groups[0][1] == 4:
        return (7, ordered)
    if groups[0][1] == 3 and groups[1][1] == 2:
        return (6, ordered)
    if flush:
        return (5, ranks)
    if straight_high:
        return (4, [straight_high])
    if groups[0][1] == 3:
        return (3, ordered)
    if groups[0][1] == 2 and groups[1][1] == 2:
        return (2, ordered)
    if groups[0][1] == 2:
        return (1, ordered)
    return (0, ranks)


def poker_hand_name(cards):
    score = evaluate_poker_hand(cards)
    if score[0] == 8 and score[1][0] == 14:
        return "Royal Flush"
    return POKER_HAND_NAMES[score[0]]


MAX_POKER_PLAYERS = 5  # host + up to 4 invited (5 cards + up to 5 draws each still fits a 52-card deck)


class PokerGame:
    def __init__(self, guild, players, amount):
        self.guild = guild
        self.players = list(players)      # members who accepted and paid the ante
        self.amount = amount
        self.pot = amount * len(self.players)
        deck = [(r, s) for r in range(2, 15) for s in range(4)]
        random.shuffle(deck)
        self.deck = deck
        self.hands = {p.id: [self.deck.pop() for _ in range(5)] for p in self.players}
        self.locked = set()
        self.finished = False
        self.message = None
        self.watch_task = None

    def release(self):
        for p in self.players:
            poker_busy.discard((self.guild.id, p.id))

    def status_embed(self):
        lines = []
        for p in self.players:
            state = "✅ locked in" if p.id in self.locked else "⏳ choosing cards"
            lines.append(f"{p.mention} — {state}")
        return build_galaxy_embed(
            f"**Poker · 5-card draw · {len(self.players)} players**\n"
            f"Pot: **{self.pot:,}** Galaxy Coins (**{self.amount:,}** each)\n\n" + "\n".join(lines) +
            "\n\nPress **View Hand / Draw** to see your cards privately and swap any of them once.",
            title="GALAXY  /  POKER DUEL", color=GALAXY_ACCENT, footer="GALAXY  ·  CASINO",
        )

    async def refresh(self):
        if self.message is None or self.finished:
            return
        try:
            await self.message.edit(embed=self.status_embed())
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def lock_in(self, user_id, discard):
        if self.finished or user_id in self.locked:
            return False
        hand = self.hands[user_id]
        for i in sorted(discard):
            hand[i] = self.deck.pop()
        self.locked.add(user_id)
        return True

    async def watch(self):
        try:
            await asyncio.sleep(POKER_TURN_TIMEOUT)
        except asyncio.CancelledError:
            return
        try:
            await self.finish(timed_out=True)
        except Exception:
            logger.exception("Poker timeout resolution failed")

    async def finish(self, timed_out=False):
        if self.finished:
            return
        self.finished = True
        if self.watch_task and self.watch_task is not asyncio.current_task():
            self.watch_task.cancel()

        scores = {p.id: evaluate_poker_hand(self.hands[p.id]) for p in self.players}
        best = max(scores.values())
        winners = [p for p in self.players if scores[p.id] == best]
        # Work out every payout first, then pay them one by one.
        if len(winners) == 1:
            payouts = {winners[0].id: self.pot}
            result = f"🏆 {winners[0].mention} wins the pot of **{self.pot:,}** Galaxy Coins!"
            color = SUCCESS_COLOR
        else:
            share, extra = divmod(self.pot, len(winners))
            payouts = {w.id: share + (extra if i == 0 else 0) for i, w in enumerate(winners)}
            result = (
                f"🤝 **Tie!** {', '.join(w.mention for w in winners)} split the pot of "
                f"**{self.pot:,}** coins (**{share:,}** each)."
            )
            color = WARN_COLOR

        paid = set()
        try:
            for uid, amount in payouts.items():
                change_coins(self.guild.id, uid, amount)
                paid.add(uid)
        except Exception:
            logger.exception("Poker payout failed")
            if not paid:
                # Nothing was paid yet, so safely return every stake.
                for p in self.players:
                    try:
                        change_coins(self.guild.id, p.id, self.amount)
                    except Exception:
                        logger.exception("Poker fallback refund failed for %s", p.id)
                result = "⚠️ A database error happened, so every stake was refunded."
                color = WARN_COLOR
            else:
                # Some winners were already paid, so just retry the unpaid ones.
                for uid, amount in payouts.items():
                    if uid not in paid:
                        try:
                            change_coins(self.guild.id, uid, amount)
                        except Exception:
                            logger.exception("Poker payout retry failed for %s", uid)
        finally:
            self.release()

        embed = build_galaxy_embed(
            result + ("\n*(Time ran out, so players who hadn't drawn kept their hands.)*" if timed_out else ""),
            title="GALAXY  /  POKER RESULT", color=color, footer="GALAXY  ·  CASINO",
        )
        for p in self.players:
            embed.add_field(
                name=p.display_name,
                value=f"{_hand_text(self.hands[p.id])}\n**{poker_hand_name(self.hands[p.id])}**",
                inline=False,
            )
        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                try:
                    await self.message.channel.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass
        await send_mod_log(
            self.guild,
            f"🃏 **Poker duel finished**\nPlayers: " + ", ".join(f"{p} (`{p.id}`)" for p in self.players) +
            f"\nStake: {self.amount:,} each · Pot: {self.pot:,}"
            f"\nResult: " + ("tie between " if len(winners) > 1 else "winner: ") +
            ", ".join(f"{w} (`{w.id}`)" for w in winners),
        )


class PokerDrawView(discord.ui.View):
    """Private (ephemeral) view where one player marks cards to discard."""

    def __init__(self, game, user_id):
        super().__init__(timeout=POKER_TURN_TIMEOUT)
        self.game = game
        self.user_id = user_id
        self.discard = set()
        self._build()

    def _build(self):
        self.clear_items()
        for i, card in enumerate(self.game.hands[self.user_id]):
            marked = i in self.discard
            button = discord.ui.Button(
                label=_card_text(card),
                style=discord.ButtonStyle.danger if marked else discord.ButtonStyle.secondary,
                row=0,
            )
            button.callback = self._make_toggle(i)
            self.add_item(button)
        confirm = discord.ui.Button(
            label="Keep all cards" if not self.discard else f"Draw {len(self.discard)} new card(s)",
            style=discord.ButtonStyle.success, emoji="✅", row=1,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

    def build_embed(self, final=False):
        hand = self.game.hands[self.user_id]
        text = f"{_hand_text(hand)}\n**{poker_hand_name(hand)}**\n\n"
        if final:
            text += "You're locked in. Waiting for the other players..." if not self.game.finished else "Game over."
        else:
            text += "Tap cards to mark them for discard (they turn red), then press the green button."
        return build_galaxy_embed(
            text, title="GALAXY  /  YOUR HAND", color=GALAXY_ACCENT, footer="GALAXY  ·  ONLY YOU CAN SEE THIS",
        )

    def _make_toggle(self, index):
        async def toggle(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("That's not your hand.", ephemeral=True)
            if self.game.finished or self.user_id in self.game.locked:
                return await interaction.response.edit_message(embed=self.build_embed(final=True), view=None)
            self.discard.symmetric_difference_update({index})
            self._build()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        return toggle

    async def _confirm(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("That's not your hand.", ephemeral=True)
        game = self.game
        if game.finished:
            return await interaction.response.edit_message(
                content="This game has already ended.", embed=None, view=None)
        ok = await game.lock_in(self.user_id, self.discard)
        await interaction.response.edit_message(embed=self.build_embed(final=True), view=None)
        self.stop()
        if not ok:
            return
        if len(game.locked) == len(game.players):
            await game.finish()
        else:
            await game.refresh()


class PokerTableView(discord.ui.View):
    def __init__(self, game):
        super().__init__(timeout=None)
        self.game = game

    @discord.ui.button(label="View Hand / Draw", emoji="🃏", style=discord.ButtonStyle.primary)
    async def view_hand(self, interaction: discord.Interaction, button: discord.ui.Button):
        game = self.game
        if interaction.user.id not in game.hands:
            return await interaction.response.send_message("You're not playing in this duel.", ephemeral=True)
        if game.finished:
            return await interaction.response.send_message("This duel has already ended.", ephemeral=True)
        draw_view = PokerDrawView(game, interaction.user.id)
        if interaction.user.id in game.locked:
            return await interaction.response.send_message(
                embed=draw_view.build_embed(final=True), ephemeral=True)
        await interaction.response.send_message(
            embed=draw_view.build_embed(), view=draw_view, ephemeral=True)


class PokerChallengeView(discord.ui.View):
    """Lobby: every invited player is pinged and chooses Accept or Decline."""

    def __init__(self, guild, host, invited, amount):
        super().__init__(timeout=60)
        self.guild = guild
        self.host = host
        self.invited = list(invited)
        self.amount = amount
        self.responses = {m.id: None for m in self.invited}  # None = waiting, True = accepted, False = declined
        self.message = None
        self.resolved = False

    def _release_all(self):
        poker_busy.discard((self.guild.id, self.host.id))
        for m in self.invited:
            poker_busy.discard((self.guild.id, m.id))

    def lobby_embed(self):
        lines = []
        for m in self.invited:
            state = self.responses[m.id]
            label = "⏳ waiting" if state is None else ("✅ accepted" if state else "❌ declined")
            lines.append(f"• {m.mention} — {label}")
        return build_galaxy_embed(
            f"{self.host.mention} invited you to a **Poker** duel!\n\n"
            f"**Ante:** {self.amount:,} each · **Pot:** {self.amount:,} × the number of players\n"
            "5-card draw: everyone gets 5 cards, swaps any of them once, and the best hand wins the pot.\n\n"
            + "\n".join(lines) +
            "\n\nInvited players, press **Accept** or **Decline** within 60 seconds. "
            f"{self.host.mention} can press **Start Now** once at least one person has accepted.",
            title="GALAXY  /  POKER DUEL", color=GALAXY_ACCENT, footer="GALAXY  ·  CASINO",
        )

    async def _swap(self, interaction, embed, view=None):
        """Edit the lobby message, using the interaction when we have one."""
        if interaction is not None:
            await interaction.response.edit_message(content=None, embed=embed, view=view)
        elif self.message is not None:
            try:
                await self.message.edit(content=None, embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def _cancel(self, interaction, text, color=None):
        self.resolved = True
        self.stop()
        self._release_all()
        await self._swap(interaction, build_galaxy_embed(
            text, title="GALAXY  /  POKER DUEL", color=color or GALAXY_COLOR, footer="GALAXY  ·  CASINO"))

    async def _begin(self, interaction=None):
        """Take the antes from the host and everyone who accepted, then deal."""
        self.resolved = True
        self.stop()
        gid, amount = self.guild.id, self.amount
        accepted = [m for m in self.invited if self.responses[m.id]]
        charged = []
        try:
            ok, _ = spend_coins(gid, self.host.id, amount)
            if not ok:
                self._release_all()
                return await self._swap(interaction, build_galaxy_embed(
                    f"{self.host.mention} no longer has enough coins. Duel cancelled.",
                    title="GALAXY  /  POKER DUEL", color=ERROR_COLOR, footer="GALAXY  ·  CASINO"))
            charged.append(self.host)
            for m in accepted:
                ok, _ = spend_coins(gid, m.id, amount)
                if ok:
                    charged.append(m)
        except Exception:
            logger.exception("Poker escrow failed")
            for m in charged:
                try:
                    change_coins(gid, m.id, amount)
                except Exception:
                    logger.exception("Poker escrow refund failed for %s", m.id)
            self._release_all()
            return await self._swap(interaction, build_galaxy_embed(
                "Couldn't safely take the stakes, so the duel was cancelled.",
                title="GALAXY  /  POKER DUEL", color=ERROR_COLOR, footer="GALAXY  ·  CASINO"))

        if len(charged) < 2:
            for m in charged:
                change_coins(gid, m.id, amount)  # give the host their ante back
            self._release_all()
            return await self._swap(interaction, build_galaxy_embed(
                "Not enough players joined (or the others couldn't afford the ante), so the duel was cancelled.",
                title="GALAXY  /  POKER DUEL", color=GALAXY_COLOR, footer="GALAXY  ·  CASINO"))

        # Anyone invited who isn't playing (declined, no answer, too poor) is free again.
        playing = {m.id for m in charged}
        for m in [self.host] + self.invited:
            if m.id not in playing:
                poker_busy.discard((gid, m.id))

        game = PokerGame(self.guild, charged, amount)
        await self._swap(interaction, game.status_embed(), PokerTableView(game))
        game.message = interaction.message if interaction is not None else self.message
        game.watch_task = asyncio.create_task(game.watch())

    async def _after_response(self, interaction):
        if all(v is not None for v in self.responses.values()):
            if any(self.responses.values()):
                return await self._begin(interaction)
            return await self._cancel(interaction, "Everyone declined the poker duel.")
        await interaction.response.edit_message(embed=self.lobby_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.host.id and interaction.user.id not in self.responses:
            await interaction.response.send_message("This poker duel isn't for you.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        if self.resolved:
            return
        try:
            if any(self.responses.values()):
                await self._begin(None)  # start with whoever accepted
            else:
                await self._cancel(None, "Nobody accepted in time, so the poker duel expired.")
        except Exception:
            logger.exception("Poker lobby timeout failed")
            self._release_all()

    @discord.ui.button(label="Accept", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in self.responses:
            return await interaction.response.send_message("Only invited players can accept.", ephemeral=True)
        if self.resolved:
            return await interaction.response.send_message("This duel is no longer active.", ephemeral=True)
        if self.responses[uid] is not None:
            return await interaction.response.send_message("You already answered.", ephemeral=True)
        self.responses[uid] = True
        await self._after_response(interaction)

    @discord.ui.button(label="Decline", emoji="✖️", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid not in self.responses:
            return await interaction.response.send_message("Only invited players can decline.", ephemeral=True)
        if self.resolved:
            return await interaction.response.send_message("This duel is no longer active.", ephemeral=True)
        if self.responses[uid] is not None:
            return await interaction.response.send_message("You already answered.", ephemeral=True)
        self.responses[uid] = False
        await self._after_response(interaction)

    @discord.ui.button(label="Start Now", emoji="▶️", style=discord.ButtonStyle.primary)
    async def start_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message("Only the host can start the duel.", ephemeral=True)
        if self.resolved:
            return await interaction.response.send_message("This duel is no longer active.", ephemeral=True)
        if not any(self.responses.values()):
            return await interaction.response.send_message("Nobody has accepted yet.", ephemeral=True)
        await self._begin(interaction)

    @discord.ui.button(label="Cancel Duel", emoji="🛑", style=discord.ButtonStyle.secondary)
    async def cancel_duel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.host.id:
            return await interaction.response.send_message("Only the host can cancel the duel.", ephemeral=True)
        if self.resolved:
            return await interaction.response.send_message("This duel is no longer active.", ephemeral=True)
        await self._cancel(interaction, f"{self.host.mention} cancelled the poker duel.")


@bot.command(name="poker", aliases=["pokerduel", "1v1"])
@commands.guild_only()
async def poker_cmd(ctx, member: commands.Greedy[discord.Member], amount: str):
    """Invite one or more people to 5-card draw poker for coins. +poker @a [@b ...] <amount>"""
    # `member` is the parameter name on purpose: replying to someone with the command still targets them.
    invited, seen = [], {ctx.author.id}
    for m in member:
        if m.bot or m.id in seen:
            continue
        seen.add(m.id)
        invited.append(m)
    if not invited:
        return await ctx.send(
            f"Mention at least one real member to play against, e.g. `{PREFIX}poker @user 500` "
            f"or `{PREFIX}poker @user1 @user2 500`.")
    if len(invited) > MAX_POKER_PLAYERS - 1:
        return await ctx.send(
            f"A poker game can have at most **{MAX_POKER_PLAYERS} players** (you plus {MAX_POKER_PLAYERS - 1}).")
    try:
        stake = parse_coin_amount(amount, current_balance=get_coin_balance(ctx.guild.id, ctx.author.id))
    except ValueError:
        return await ctx.send(f"Enter a valid amount, e.g. `{PREFIX}poker @user 500`.")
    if stake < 1:
        return await ctx.send("The minimum ante is **1 Galaxy Coin**.")
    for person in [ctx.author] + invited:
        if (ctx.guild.id, person.id) in poker_busy:
            return await ctx.send(f"{person.display_name} is already in a poker duel.",
                                  allowed_mentions=discord.AllowedMentions.none())
    mine = get_coin_balance(ctx.guild.id, ctx.author.id)
    if mine < stake:
        return await ctx.send(f"You only have **{mine:,} Galaxy Coins**.")
    broke = [m for m in invited if get_coin_balance(ctx.guild.id, m.id) < stake]
    if broke:
        return await ctx.send(
            "These players can't match that ante: " + ", ".join(m.display_name for m in broke) + ".",
            allowed_mentions=discord.AllowedMentions.none())

    poker_busy.add((ctx.guild.id, ctx.author.id))
    for m in invited:
        poker_busy.add((ctx.guild.id, m.id))
    view = PokerChallengeView(ctx.guild, ctx.author, invited, stake)
    try:
        view.message = await ctx.send(
            content=" ".join(m.mention for m in invited),  # pings every invited player
            embed=view.lobby_embed(), view=view,
            allowed_mentions=discord.AllowedMentions(users=invited, roles=False, everyone=False),
        )
    except Exception:
        view._release_all()
        raise


@bot.command(name="endriddle", aliases=["skipriddle", "closeriddle"])
@commands.guild_only()
@require_staff_rank(5)
async def endriddle(ctx):
    """End the active riddle early and reveal the answer (Perm 5+)."""
    active = active_galaxy_questions.pop(ctx.guild.id, None)
    if not active:
        return await ctx.send("There’s no active riddle to end.")
    answer = active.get("answer", "???")
    channel_id = active.get("channel_id")
    await ctx.send(
        embed=build_galaxy_embed(
            f"Riddle closed by {ctx.author.mention}.\nThe answer was **{discord.utils.escape_markdown(str(answer))}**.",
            title="GALAXY  /  RIDDLE ENDED",
            color=WARN_COLOR,
            footer="GALAXY  ·  COMMUNITY GAME",
        )
    )
    if channel_id and channel_id != ctx.channel.id:
        channel = ctx.guild.get_channel(channel_id)
        if channel is not None:
            try:
                await send_panel(
                    channel,
                    f"⌛ Round closed by staff. The answer was **{answer}**.",
                    title="GALAXY  /  RIDDLE",
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
    await send_mod_log(
        ctx.guild,
        f"⌛ **Riddle ended early**\nBy: {ctx.author} (`{ctx.author.id}`)\nAnswer: {answer}",
    )


@bot.command()
async def help(ctx):
    """Compact command directory, displayed as one Galaxy panel."""
    embed = discord.Embed(
        title="GALAXY  /  COMMAND GUIDE",
        description=(
            f"Commands use the `{PREFIX}` prefix. Reply to a member’s message to use them as the target "
            f"for supported moderation commands. `{PREFIX}snipe` browses deleted and edited messages (with before/after) using ◀ ▶ arrows; `{PREFIX}editsnipe` does the same for edits."
        ),
        color=GALAXY_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="START HERE",
        value=(
            f"`{PREFIX}help`  `{PREFIX}ping`  `{PREFIX}perms`  `{PREFIX}botinfo`\n"
            f"`{PREFIX}userinfo` / `{PREFIX}whois`  `{PREFIX}avatar`  `{PREFIX}mc`\n"
            f"`{PREFIX}serverinfo`  `{PREFIX}roleinfo`  `{PREFIX}snipe`  `{PREFIX}editsnipe`\n"
            f"`{PREFIX}poll \"question\" \"opt1\" \"opt2\"`  `{PREFIX}remind <time> <text>`"
        ),
        inline=False,
    )
    embed.add_field(
        name="MODERATION",
        value=(
            f"`{PREFIX}warn`  `{PREFIX}warnings`  `{PREFIX}clearwarns`  `{PREFIX}unwarn` (Perm 1+)\n"
            f"`{PREFIX}sanctions @user` (Perm 1+)  `{PREFIX}del sanction <id>` (Perm 2+)\n"
            f"`{PREFIX}tempmute` / `{PREFIX}timeout <member> <10m/1h/7d>`  `{PREFIX}unmute`\n"
            f"`{PREFIX}vcmute` / `{PREFIX}vcdeafen` [time] (Perm 4+)  `{PREFIX}vcunmute`  `{PREFIX}vcundeafen`\n"
            f"`{PREFIX}clear <amount>` / reply `{PREFIX}clear <amount>`  `{PREFIX}kick` (Perm 3+)\n"
            f"`{PREFIX}ban` (Perm 4+)  `{PREFIX}softban` (Perm 5+)  `{PREFIX}unban`"
        ),
        inline=False,
    )
    embed.add_field(
        name="SERVER & STAFF TOOLS",
        value=(
            f"`{PREFIX}lock`  `{PREFIX}unlock`  `{PREFIX}slowmode`\n"
            f"`{PREFIX}nick`  `{PREFIX}addrole` / `{PREFIX}delrole` / `{PREFIX}temprole` (Perm 5+)\n"
            f"`{PREFIX}antiraid on/off/status`\n"
            f"`{PREFIX}bl` / `{PREFIX}blacklist`  `{PREFIX}unbl` / `{PREFIX}unblacklist`"
        ),
        inline=False,
    )
    embed.add_field(
        name="STAFF STRIKES",
        value=(
            f"`{PREFIX}strike`  `{PREFIX}strikes`  `{PREFIX}removestrike <id or message_id>`\n"
            f"Or reply to the strike message with `{PREFIX}removestrike`. "
            "Assistant Staff Manager+ only. 1st/2nd/3rd strike roles are applied automatically. "
            f"Strikes (and those roles) expire after {STAFF_STRIKE_EXPIRE_DAYS} days."
        ),
        inline=False,
    )
    embed.add_field(
        name="GALAXY COINS",
        value=(
            f"`{PREFIX}balance` / `{PREFIX}bal`  `{PREFIX}daily`  `{PREFIX}tradecoins` / `{PREFIX}givecoins`\n"
            f"`{PREFIX}cf <amount> [heads/tails]`  `{PREFIX}slots`  `{PREFIX}dice`\n"
            f"`{PREFIX}riddle`  `{PREFIX}endriddle` (staff)  `{PREFIX}leaderboard`\n"
            f"`{PREFIX}addcoins`  `{PREFIX}removecoins` / `{PREFIX}takecoins`  `{PREFIX}setcoins` (Assistant Staff Manager+)\n"
            "Anyone can trade their own coins. You can bet everything with `all` and pick heads or tails "
            "(fair 50/50 coin). Riddles post every **30m** — reply to the riddle message "
            f"or use `{PREFIX}riddle <answer>`. Wrong answers give up to 3 hints. Leaderboard every 3 hours."
        ),
        inline=False,
    )
    embed.add_field(
        name="CODE DROPS & POKER",
        value=(
            f"`{PREFIX}code [amount]` (Trial Director+) — drops a code; first person to type it wins coins.\n"
            f"`{PREFIX}poker @user [@user2 ...] <amount>` (everyone) — 5-card draw poker for coins; everyone invited gets pinged and chooses Accept or Decline."
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
            "**Perm 5 — Head Admin / Server Manager / Management / DRC / Dev / Axelle:** Inherits Perm 1–4; staff-strike tools.\n"
            "**Perm 6 — Main Founder / Founder / Co-Owner:** All permission levels, including `+bl` / `+blacklist`.\n\n"
            "Higher permission levels inherit lower-level access. Staff can moderate only "
            "members below their rank; equal- or higher-ranked staff are protected."
        ),
        inline=False,
    )
    embed.set_footer(text=f"GALAXY  ·  COMMAND GUIDE  ·  Prefix: {PREFIX}")
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
        panel = discord.Embed(
            title="GALAXY  /  COMMAND HELP",
            description=(
                f"› {ctx.author.mention} I still need **{missing}** to run that.\n\n"
                f"**Try:** {usage}\n"
                f"If you’re not sure what goes there, use `{PREFIX}help`."
            ),
            color=GALAXY_COLOR,
        )
        panel.set_footer(text=f"GALAXY  •  COMMAND SUPPORT  •  {PREFIX}help")
        await ctx.send(embed=panel, allowed_mentions=discord.AllowedMentions.none())
        if ctx.guild:
            await send_mod_log(
                ctx.guild,
                f"⚠️ **Command missing an argument**\nCommand: `{command_name}`"
                f"\nMissing: `{error.param.name}`\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\nChannel: {ctx.channel.mention}"
            )

    elif isinstance(error, commands.BadArgument):
        panel = discord.Embed(
            title="GALAXY  /  CHECK YOUR COMMAND",
            description=(
                f"› {ctx.author.mention} I couldn’t match one of those details.\n\n"
                "Make sure the member is mentioned or their ID is correct, and check that any numbers are valid.\n\n"
                f"**Try:** {usage}\n"
                f"Need the full command list? Use `{PREFIX}help`."
            ),
            color=GALAXY_COLOR,
        )
        panel.set_footer(text=f"GALAXY  •  COMMAND SUPPORT  •  {PREFIX}help")
        await ctx.send(embed=panel, allowed_mentions=discord.AllowedMentions.none())
        if ctx.guild:
            await send_mod_log(
                ctx.guild,
                f"⚠️ **Command argument couldn’t be read**\nCommand: `{command_name}`"
                f"\nUser: {ctx.author} (`{ctx.author.id}`)\nChannel: {ctx.channel.mention}"
            )

    elif isinstance(error, commands.CheckFailure):
        return

    elif isinstance(error, commands.CommandNotFound):
        return

    elif isinstance(error, commands.CommandOnCooldown):
        wait = max(1, round(error.retry_after))
        panel = discord.Embed(
            title="GALAXY  /  COOLDOWN",
            description=f"› {ctx.author.mention} Give it a second — you can use that command again in **{wait}s**.",
            color=GALAXY_COLOR,
        )
        panel.set_footer(text="GALAXY  •  PLEASE TRY AGAIN SHORTLY")
        await ctx.send(embed=panel, allowed_mentions=discord.AllowedMentions.none())

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
                f"🚫 **Discord blocked a command**\nCommand: `{command_name}`"
                f"\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\nDetails: `{discord.utils.escape_markdown(str(original)[:500])}`"
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
                f"⚠️ **Discord API error**\nCommand: `{command_name}`"
                f"\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\nDetails: `{discord.utils.escape_markdown(str(original)[:500])}`"
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
                f"❗ **Unexpected bot error**\nCommand: `{command_name}`"
                f"\nUser: {ctx.author} (`{ctx.author.id}`)"
                f"\nError: `{discord.utils.escape_markdown(str(original)[:700])}`"
            )


if not TOKEN:
    raise SystemExit("Galaxy Bot cannot start: DISCORD_TOKEN is missing. Add DISCORD_TOKEN=your_bot_token to the host environment or .env file, then restart.")

bot.run(TOKEN)
