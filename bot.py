import os
import json
import time
import sqlite3
import asyncio
import random
import re
from collections import deque
from datetime import timedelta

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

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

# Every listed staff role can use Perm 1 commands.
PERM1_ROLE_IDS = set(ROLE_IDS.values())

# Head Moderator can ban, but cannot unban or kick unless they also have a higher role.
BAN_ROLE_IDS = {
    ROLE_IDS["head_moderator"],
    ROLE_IDS["senior_admin"], ROLE_IDS["head_admin"], ROLE_IDS["admin"],
    ROLE_IDS["staff_manager"], ROLE_IDS["assistant_staff_manager"],
    ROLE_IDS["manager"], ROLE_IDS["assistant_head_manager"],
    ROLE_IDS["head_manager"], ROLE_IDS["trial_director"],
    ROLE_IDS["drc"], ROLE_IDS["senior_drc"],
    ROLE_IDS["founder"], ROLE_IDS["co_owner"],
}
# Junior Admin, Dev, and Axelle stay at Perm 1 unless they hold another eligible role.
ADMIN_ACTION_ROLE_IDS = {
    ROLE_IDS["admin"], ROLE_IDS["senior_admin"], ROLE_IDS["head_admin"],
    ROLE_IDS["staff_manager"], ROLE_IDS["assistant_staff_manager"],
    ROLE_IDS["manager"], ROLE_IDS["assistant_head_manager"],
    ROLE_IDS["head_manager"], ROLE_IDS["trial_director"],
    ROLE_IDS["drc"], ROLE_IDS["senior_drc"],
    ROLE_IDS["founder"], ROLE_IDS["co_owner"],
}
OWNER_ACTION_ROLE_IDS = {ROLE_IDS["founder"], ROLE_IDS["co_owner"]}

# New-command permission tiers follow the configured staff hierarchy.
ROLE_RANK = {
    ROLE_IDS["trial_moderator"]: 1,
    ROLE_IDS["moderator"]: 2,
    ROLE_IDS["senior_moderator"]: 3,
    ROLE_IDS["head_moderator"]: 4,
    ROLE_IDS["junior_admin"]: 5,
    ROLE_IDS["admin"]: 6,
    ROLE_IDS["senior_admin"]: 7,
    ROLE_IDS["head_admin"]: 8,
    ROLE_IDS["assistant_staff_manager"]: 9,
    ROLE_IDS["staff_manager"]: 10,
    ROLE_IDS["manager"]: 11,
    ROLE_IDS["assistant_head_manager"]: 12,
    ROLE_IDS["head_manager"]: 13,
    ROLE_IDS["trial_director"]: 14,
    ROLE_IDS["senior_drc"]: 15,
    ROLE_IDS["drc"]: 16,
    ROLE_IDS["axelle"]: 17,
    ROLE_IDS["dev"]: 18,
    ROLE_IDS["co_owner"]: 19,
    ROLE_IDS["founder"]: 20,
}

def has_staff_rank(member, minimum_rank):
    return any(ROLE_RANK.get(role.id, 0) >= minimum_rank for role in getattr(member, "roles", []))

def require_staff_rank(minimum_rank):
    async def predicate(ctx):
        return has_staff_rank(ctx.author, minimum_rank)
    return commands.check(predicate)

def require_founder_or_coowner():
    async def predicate(ctx):
        return has_any_role(ctx.author, OWNER_ACTION_ROLE_IDS)
    return commands.check(predicate)
# Assistant Head Manager and the higher explicitly eligible staff roles.
# Dev and Axelle remain at Perm 1, as previously specified.
STAFF_STRIKE_ROLE_IDS = {
    ROLE_IDS["assistant_head_manager"], ROLE_IDS["head_manager"],
    ROLE_IDS["trial_director"], ROLE_IDS["drc"], ROLE_IDS["senior_drc"],
    ROLE_IDS["founder"], ROLE_IDS["co_owner"],
}

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
db.execute("""CREATE TABLE IF NOT EXISTS staff_strikes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)""")
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
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_any_role(member, ADMIN_ACTION_ROLE_IDS) or member.id in KICK_USER_ALLOWLIST or member.id in BAN_USER_ALLOWLIST


def has_owner_actions(member):
    return member.guild_permissions.administrator or member.id == member.guild.owner_id or has_any_role(member, OWNER_ACTION_ROLE_IDS) or member.id in BLACKLIST_USER_ALLOWLIST


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
    channel_id = configured_guild_channel_id(
        guild, MOD_LOG_CHANNEL_IDS, DEFAULT_MOD_LOG_CHANNEL_ID
    )
    channel = await fetch_guild_text_channel(guild, channel_id)
    if channel is None:
        return
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.before_invoke
async def delete_and_log_command(ctx):
    """Only runs after every command permission check has passed."""
    if not ctx.guild:
        return
    # Delete the invocation quietly; command results are sent as normal channel panels.
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass
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
    print(f"Logged in as {bot.user} | Prefix: {PREFIX}")
    for guild in bot.guilds:
        await refresh_invites(guild)
    if not getattr(bot, "galaxy_game_task", None) or bot.galaxy_game_task.done():
        bot.galaxy_game_task = asyncio.create_task(galaxy_game_scheduler())


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
async def on_member_update(before, after):
    # Log timeout changes made directly in Discord as well as through bot commands.
    if before.communication_disabled_until != after.communication_disabled_until:
        if after.communication_disabled_until:
            detail = f"Timeout applied/changed for {after.mention} until {after.communication_disabled_until}."
        else:
            detail = f"Timeout removed from {after.mention}."
        await send_mod_log(after.guild, f"📝 **Discord audit update:** {detail}")


@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
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

@bot.command(name="balance", aliases=["bal", "coins"])
async def galaxy_balance(ctx, member: discord.Member = None):
    member = member or ctx.author
    balance = get_coin_balance(ctx.guild.id, member.id)
    await ctx.send(f"🌌 **{member.display_name}** has **{balance:,} Galaxy Coins**.")

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
    return member.id != ctx.author.id and (
        ctx.author.id == ctx.guild.owner_id or member.top_role < ctx.author.top_role
    )


@bot.command()
@require_perm1()
async def perms(ctx):
    role_names = [role.name for role in ctx.author.roles if role.id in PERM1_ROLE_IDS]
    access = []
    if has_perm1(ctx.author):
        access.append("Perm 1 commands: warn, warnings, clearwarns, tempmute/timeout, unmute, antiraid; purge is Senior Moderator+")
    if has_ban(ctx.author):
        access.append("Ban: +ban")
    if has_admin_actions(ctx.author):
        access.append("Admin actions: +unban and +kick")
    if has_owner_actions(ctx.author):
        access.append("Founder/Co-Owner actions: +bl and +unbl")
    await ctx.send("**Your bot permissions**\n"
                    f"Staff roles detected: {', '.join(role_names) if role_names else 'Administrator/Server Owner'}\n"
                    + ("\n".join(access) if access else "No staff permissions detected."))


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
        return await ctx.send("You cannot moderate someone with an equal or higher role.")
    db.execute(
        "INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, member.id, ctx.author.id, reason),
    )
    db.commit()
    await ctx.send(f"⚠️ Warned {member.mention}. Reason: {reason}")
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
@require_staff_strike_access()
async def staff_strike(ctx, member: discord.Member, *, reason="No reason provided"):
    db.execute("DELETE FROM staff_strikes WHERE datetime(created_at) <= datetime('now', '-30 days')")
    db.commit()
    if not has_any_role(member, set(ROLE_IDS.values())):
        return await ctx.send("Staff strikes can only be issued to members who hold a configured staff role.")
    if not can_moderate_target(ctx, member):
        return await ctx.send("You cannot strike a staff member with an equal or higher Discord role.")
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
@require_staff_strike_access()
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
@require_staff_strike_access()
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
        return await ctx.send("You cannot moderate someone with an equal or higher role.")
    await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
    await ctx.send(f"⏳ Timed out {member.mention} for {minutes} minutes.")
    await send_mod_log(ctx.guild, f"⏳ **Timeout**\nUser: {member} (`{member.id}`)\nDuration: {minutes} minutes\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_perm1()
async def unmute(ctx, member: discord.Member):
    if not can_moderate_target(ctx, member):
        return await ctx.send("You cannot moderate someone with an equal or higher role.")
    await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
    await ctx.send(f"Removed timeout for {member.mention}.")
    await send_mod_log(ctx.guild, f"🔊 **Timeout removed**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_ban()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return await ctx.send("You cannot moderate someone with an equal or higher role.")
    await member.ban(reason=f"{ctx.author}: {reason}")
    await ctx.send(f"🔨 Banned {member}. Reason: {reason}")
    await send_mod_log(ctx.guild, f"🔨 **Ban**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_admin_actions()
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
        return await ctx.send("You cannot moderate someone with an equal or higher role.")
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.send(f"👢 Kicked {member}. Reason: {reason}")
    await send_mod_log(ctx.guild, f"👢 **Kick**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="bl", aliases=["blacklist"])
@require_owner_actions()
async def blacklist_user(ctx, user: discord.User, *, reason="No reason provided"):
    db.execute(
        "INSERT OR REPLACE INTO blacklist(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, user.id, ctx.author.id, reason),
    )
    db.commit()
    await ctx.send(f"⛔ Added **{user}** to the bot blacklist.")
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


@bot.command(name="purge", aliases=["clear"])
@require_staff_rank(3)
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.send("Choose a number from 1 to 100.")
    deleted = await ctx.channel.purge(limit=amount)
    await send_mod_log(ctx.guild, f"🧹 **Purge**\nChannel: #{ctx.channel.name} (`{ctx.channel.id}`)\nMessages deleted: {len(deleted)}\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_founder_or_coowner()
async def lock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel locked by {ctx.author}")
    await ctx.send("🔒 This channel is now locked.")
    await send_mod_log(ctx.guild, f"🔒 **Channel locked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_founder_or_coowner()
async def unlock(ctx):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = None
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=f"Channel unlocked by {ctx.author}")
    await ctx.send("🔓 This channel is now unlocked.")
    await send_mod_log(ctx.guild, f"🔓 **Channel unlocked**\nChannel: {ctx.channel.mention}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_staff_rank(11)
async def slowmode(ctx, seconds: int):
    if not 0 <= seconds <= 21600:
        return await ctx.send("Slowmode must be between 0 and 21,600 seconds.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode changed by {ctx.author}")
    await ctx.send(f"⏱️ Slowmode set to **{seconds} seconds**.")
    await send_mod_log(ctx.guild, f"⏱️ **Slowmode changed**\nChannel: {ctx.channel.mention}\nSeconds: {seconds}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_staff_rank(3)
async def nick(ctx, member: discord.Member, *, nickname: str = None):
    if not can_moderate_target(ctx, member):
        return await ctx.send("You cannot change the nickname of someone with an equal or higher role.")
    await member.edit(nick=nickname, reason=f"Nickname changed by {ctx.author}")
    await ctx.send(f"✏️ Updated {member.mention}'s nickname.")
    await send_mod_log(ctx.guild, f"✏️ **Nickname changed**\nMember: {member} (`{member.id}`)\nNew nickname: {nickname or 'Reset to username'}\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="role")
@require_staff_rank(11)
async def role_command(ctx, action: str, member: discord.Member, role: discord.Role):
    action = action.lower()
    if action not in ("add", "remove"):
        return await ctx.send(f"Usage: `{PREFIX}role add @member @role` or `{PREFIX}role remove @member @role`.")
    if role.is_default() or role.managed or role >= ctx.author.top_role or role >= ctx.guild.me.top_role:
        return await ctx.send("I can't edit that role because it is managed or not below your and the bot's top roles.")
    if not can_moderate_target(ctx, member):
        return await ctx.send("You cannot edit roles for someone with an equal or higher role.")
    if action == "add":
        await member.add_roles(role, reason=f"Role added by {ctx.author}")
        result = "added"
    else:
        await member.remove_roles(role, reason=f"Role removed by {ctx.author}")
        result = "removed"
    await ctx.send(f"✅ {role.mention} {result} {member.mention}.")
    await send_mod_log(ctx.guild, f"🎭 **Role {result}**\nRole: {role.name} (`{role.id}`)\nMember: {member} (`{member.id}`)\nBy: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="unwarn")
@require_staff_rank(1)
async def unwarn(ctx, warning_id: int):
    row = db.execute("SELECT user_id, reason FROM warnings WHERE guild_id=? AND id=?", (ctx.guild.id, warning_id)).fetchone()
    if row is None:
        return await ctx.send("I couldn't find that warning ID.")
    db.execute("DELETE FROM warnings WHERE guild_id=? AND id=?", (ctx.guild.id, warning_id))
    db.commit()
    await ctx.send(f"✅ Removed warning `#{warning_id}` from user `{row[0]}`.")
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
@require_founder_or_coowner()
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
@require_staff_rank(3)
async def membercount(ctx):
    await ctx.send(f"👥 **{ctx.guild.name}** has **{ctx.guild.member_count:,}** members.")


@bot.command()
@require_staff_rank(6)
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
        description=f"Use `{PREFIX}` before each command. Staff tools require the listed roles.",
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
            f"`{PREFIX}warn`  `{PREFIX}warnings`  `{PREFIX}clearwarns`  `{PREFIX}unwarn`\n"
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
            "Available to eligible Assistant Head Manager+ roles. Strikes expire after 30 days."
        ),
        inline=False,
    )
    embed.add_field(
        name="✧ Galaxy Coins",
        value=(
            f"`{PREFIX}balance` / `{PREFIX}bal`  `{PREFIX}daily`\n"
            f"`{PREFIX}gamble` / `{PREFIX}coinflip`  `{PREFIX}slots`\n"
            "Solve the hourly channel riddle to earn coins and use them in games."
        ),
        inline=False,
    )
    embed.set_footer(text="✧ The Multiverse • Galaxy Bot")
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing an argument. Try `{PREFIX}help`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"I couldn't understand that member or number. Try `{PREFIX}help`.")
    elif isinstance(error, commands.CheckFailure):
        # Unauthorized command attempts are intentionally silent and are not deleted.
        return
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, discord.Forbidden):
        await ctx.send("I don't have the Discord permissions needed for that action.")
    elif isinstance(error, discord.HTTPException):
        await ctx.send("Discord could not complete that action. Check the bot's permissions and role position.")
    else:
        print(f"Command error: {error}")
        await ctx.send("Something went wrong. Check the bot's permissions and role position.")


if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in your .env file before starting the bot.")

bot.run(TOKEN)
