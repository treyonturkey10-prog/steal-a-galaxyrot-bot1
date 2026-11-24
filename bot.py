import os
import time
import sqlite3
from collections import deque
from datetime import timedelta

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# Keep the bot token outside this file, in .env as DISCORD_TOKEN=your_token.
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("PREFIX", "+")
INVITES_CHANNEL_ID = int(os.getenv("INVITES_CHANNEL_ID", "1549961077473808504"))
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "1549961164262215761"))
JOIN_WINDOW_SECONDS = int(os.getenv("JOIN_WINDOW_SECONDS", "20"))
JOIN_BURST_LIMIT = int(os.getenv("JOIN_BURST_LIMIT", "6"))

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
# Assistant Head Manager and the higher explicitly eligible staff roles.
# Dev and Axelle remain at Perm 1, as previously specified.
STAFF_STRIKE_ROLE_IDS = {
    ROLE_IDS["assistant_head_manager"], ROLE_IDS["head_manager"],
    ROLE_IDS["trial_director"], ROLE_IDS["drc"], ROLE_IDS["senior_drc"],
    ROLE_IDS["founder"], ROLE_IDS["co_owner"],
}

# Special users supplied by the server owner.
# NOTE: This list is only a registry until a permission rule checks it.
SPECIAL_USERS = {
    1464807318637121618,
    1400805489889120286,
    1566616591267930112,
    1244774065231761458,
    841710745897009212,
    1504581544042434731,
}
BAN_USER_ALLOWLIST = set()
KICK_USER_ALLOWLIST = set()
BLACKLIST_USER_ALLOWLIST = set()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)
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
db.commit()

recent_joins = {}
invite_cache = {}


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


async def send_mod_log(guild, text):
    if not MOD_LOG_CHANNEL_ID:
        return
    channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    if channel is None:
        try:
            channel = await guild.fetch_channel(MOD_LOG_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
    try:
        await channel.send(text)
    except (discord.Forbidden, discord.HTTPException):
        pass


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
    print(f"Logged in as {bot.user} | Prefix: {PREFIX}")
    for guild in bot.guilds:
        await refresh_invites(guild)


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
    channel = guild.get_channel(INVITES_CHANNEL_ID)
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
    row = db.execute(
        "SELECT 1 FROM blacklist WHERE guild_id=? AND user_id=?",
        (message.guild.id, message.author.id),
    ).fetchone()
    if row and not message.content.startswith(PREFIX + "unbl"):
        return
    await bot.process_commands(message)


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
        access.append("Perm 1 commands: warn, warnings, clearwarns, tempmute/timeout, unmute, purge, antiraid")
    if has_ban(ctx.author):
        access.append("Ban: +ban")
    if has_admin_actions(ctx.author):
        access.append("Admin actions: +unban and +kick")
    if has_owner_actions(ctx.author):
        access.append("Founder/Co-Owner actions: +bl and +unbl")
    await ctx.reply("**Your bot permissions**\n"
                    f"Staff roles detected: {', '.join(role_names) if role_names else 'Administrator/Server Owner'}\n"
                    + ("\n".join(access) if access else "No staff permissions detected."))


@bot.command()
@require_perm1()
async def antiraid(ctx, action: str = "status"):
    action = action.lower()
    if action == "on":
        set_antiraid(ctx.guild.id, True)
        await ctx.reply("✅ Anti-raid join monitoring enabled.")
        await send_mod_log(ctx.guild, f"🛡️ Anti-raid monitoring enabled by {ctx.author.mention}.")
    elif action == "off":
        set_antiraid(ctx.guild.id, False)
        await ctx.reply("Anti-raid join monitoring disabled.")
        await send_mod_log(ctx.guild, f"🛡️ Anti-raid monitoring disabled by {ctx.author.mention}.")
    elif action == "status":
        await ctx.reply(f"Anti-raid join monitoring: **{'ON' if antiraid_enabled(ctx.guild.id) else 'OFF'}**")
    else:
        await ctx.reply("Usage: `+antiraid on`, `+antiraid off`, or `+antiraid status`.")


@bot.command()
@require_perm1()
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return await ctx.reply("You cannot moderate someone with an equal or higher role.")
    db.execute(
        "INSERT INTO warnings(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, member.id, ctx.author.id, reason),
    )
    db.commit()
    await ctx.reply(f"⚠️ Warned {member.mention}. Reason: {reason}")
    await send_mod_log(ctx.guild, f"⚠️ **Warning**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_perm1()
async def warnings(ctx, member: discord.Member):
    rows = db.execute(
        "SELECT id, reason, created_at FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 10",
        (ctx.guild.id, member.id),
    ).fetchall()
    if not rows:
        return await ctx.reply(f"{member} has no recorded warnings.")
    lines = [f"`#{row[0]}` {row[1]} — {row[2]}" for row in rows]
    await ctx.reply(f"Warnings for {member.mention}:\n" + "\n".join(lines))


@bot.command()
@require_perm1()
async def clearwarns(ctx, member: discord.Member):
    db.execute("DELETE FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    db.commit()
    await ctx.reply(f"Cleared warnings for {member.mention}.")
    await send_mod_log(ctx.guild, f"🧹 **Warnings cleared**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command(name="strike")
@require_staff_strike_access()
async def staff_strike(ctx, member: discord.Member, *, reason="No reason provided"):
    if not has_any_role(member, set(ROLE_IDS.values())):
        return await ctx.reply("Staff strikes can only be issued to members who hold a configured staff role.")
    if not can_moderate_target(ctx, member):
        return await ctx.reply("You cannot strike a staff member with an equal or higher Discord role.")
    cursor = db.execute(
        "INSERT INTO staff_strikes(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, member.id, ctx.author.id, reason),
    )
    db.commit()
    strike_id = cursor.lastrowid
    await ctx.reply(f"⚠️ Staff strike **#{strike_id}** issued to {member.mention}. Reason: {reason}")
    await send_mod_log(
        ctx.guild,
        f"⚠️ **Staff strike issued**\nStrike: `#{strike_id}`\nStaff member: {member} (`{member.id}`)\nIssued by: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}",
    )


@bot.command(name="strikes", aliases=["staffstrikes"])
@require_staff_strike_access()
async def staff_strikes(ctx, member: discord.Member):
    if not has_any_role(member, set(ROLE_IDS.values())):
        return await ctx.reply("That member does not hold a configured staff role.")
    rows = db.execute(
        "SELECT id, reason, created_at, moderator_id FROM staff_strikes "
        "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 15",
        (ctx.guild.id, member.id),
    ).fetchall()
    if not rows:
        return await ctx.reply(f"**{member}** has no recorded staff strikes.")
    lines = [f"`#{row[0]}` {row[1]} — {row[2]} (by <@{row[3]}>)" for row in rows]
    await ctx.reply(f"**Staff strikes for {member.mention}:**\n" + "\n".join(lines))


@bot.command(name="removestrike", aliases=["unstrike"])
@require_staff_strike_access()
async def remove_staff_strike(ctx, strike_id: int, *, reason="No reason provided"):
    row = db.execute(
        "SELECT user_id, reason FROM staff_strikes WHERE guild_id=? AND id=?",
        (ctx.guild.id, strike_id),
    ).fetchone()
    if row is None:
        return await ctx.reply("I couldn't find that staff strike ID.")
    db.execute("DELETE FROM staff_strikes WHERE guild_id=? AND id=?", (ctx.guild.id, strike_id))
    db.commit()
    await ctx.reply(f"✅ Removed staff strike `#{strike_id}` from user `{row[0]}`.")
    await send_mod_log(
        ctx.guild,
        f"🧹 **Staff strike removed**\nStrike: `#{strike_id}`\nStaff member ID: `{row[0]}`\nOriginal reason: {row[1]}\nRemoved by: {ctx.author} (`{ctx.author.id}`)\nRemoval reason: {reason}",
    )


@bot.command(name="tempmute", aliases=["timeout"])
@require_perm1()
async def tempmute(ctx, member: discord.Member, minutes: int, *, reason="No reason provided"):
    if minutes < 1 or minutes > 40320:
        return await ctx.reply("Choose a timeout from 1 to 40,320 minutes.")
    if not can_moderate_target(ctx, member):
        return await ctx.reply("You cannot moderate someone with an equal or higher role.")
    await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
    await ctx.reply(f"⏳ Timed out {member.mention} for {minutes} minutes.")
    await send_mod_log(ctx.guild, f"⏳ **Timeout**\nUser: {member} (`{member.id}`)\nDuration: {minutes} minutes\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_perm1()
async def unmute(ctx, member: discord.Member):
    if not can_moderate_target(ctx, member):
        return await ctx.reply("You cannot moderate someone with an equal or higher role.")
    await member.timeout(None, reason=f"Timeout removed by {ctx.author}")
    await ctx.reply(f"Removed timeout for {member.mention}.")
    await send_mod_log(ctx.guild, f"🔊 **Timeout removed**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_ban()
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return await ctx.reply("You cannot moderate someone with an equal or higher role.")
    await member.ban(reason=f"{ctx.author}: {reason}")
    await ctx.reply(f"🔨 Banned {member}. Reason: {reason}")
    await send_mod_log(ctx.guild, f"🔨 **Ban**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_admin_actions()
async def unban(ctx, user_id: int, *, reason="No reason provided"):
    try:
        user = await bot.fetch_user(user_id)
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    except discord.NotFound:
        return await ctx.reply("That user is not banned or the ID is incorrect.")
    await ctx.reply(f"Unbanned **{user}**.")
    await send_mod_log(ctx.guild, f"♻️ **Unban**\nUser: {user} (`{user.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command()
@require_admin_actions()
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    if not can_moderate_target(ctx, member):
        return await ctx.reply("You cannot moderate someone with an equal or higher role.")
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.reply(f"👢 Kicked {member}. Reason: {reason}")
    await send_mod_log(ctx.guild, f"👢 **Kick**\nUser: {member} (`{member.id}`)\nModerator: {ctx.author} (`{ctx.author.id}`)\nReason: {reason}")


@bot.command(name="bl", aliases=["blacklist"])
@require_owner_actions()
async def blacklist_user(ctx, user: discord.User, *, reason="No reason provided"):
    db.execute(
        "INSERT OR REPLACE INTO blacklist(guild_id,user_id,moderator_id,reason) VALUES(?,?,?,?)",
        (ctx.guild.id, user.id, ctx.author.id, reason),
    )
    db.commit()
    await ctx.reply(f"⛔ Added **{user}** to the bot blacklist.")
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
        return await ctx.reply("That user is not on the bot blacklist.")
    await ctx.reply(f"✅ Removed user `{user_id}` from the bot blacklist.")
    await send_mod_log(ctx.guild, f"✅ **Bot blacklist removal**\nUser ID: `{user_id}`\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
@require_perm1()
async def purge(ctx, amount: int):
    if amount < 1 or amount > 100:
        return await ctx.reply("Choose a number from 1 to 100.")
    deleted = await ctx.channel.purge(limit=amount + 1)
    notice = await ctx.send(f"Deleted {len(deleted) - 1} messages.")
    await notice.delete(delay=5)
    await send_mod_log(ctx.guild, f"🧹 **Purge**\nChannel: {ctx.channel.mention}\nMessages deleted: {len(deleted) - 1}\nModerator: {ctx.author} (`{ctx.author.id}`)")


@bot.command()
async def ping(ctx):
    await ctx.reply(f"Pong! `{round(bot.latency * 1000)} ms`")


@bot.command()
async def help(ctx):
    await ctx.reply(
        "**Steal a Galacticrot — Moderation Commands**\n"
        f"`{PREFIX}ping` — bot latency\n"
        f"`{PREFIX}help` — show this help\n"
        f"`{PREFIX}perms` — show your staff permissions\n"
        f"`{PREFIX}antiraid on/off/status` — join-raid monitoring\n\n"
        "**Perm 1 — Moderator and higher staff roles**\n"
        f"`{PREFIX}warn @member reason` / `{PREFIX}warnings @member` / `{PREFIX}clearwarns @member`\n"
        f"`{PREFIX}tempmute @member minutes reason` (alias: `{PREFIX}timeout`)\n"
        f"`{PREFIX}unmute @member` / `{PREFIX}purge amount`\n\n"
        "**Head Moderator and higher eligible roles**\n"
        f"`{PREFIX}ban @member reason`\n\n"
        "**Admin and higher eligible roles**\n"
        f"`{PREFIX}unban user_id reason` / `{PREFIX}kick @member reason`\n\n"
        "**Assistant Head Manager and higher — Staff strikes**\n"
        f"`{PREFIX}strike @staff reason` — issue a staff strike\n"
        f"`{PREFIX}strikes @staff` — view a staff member's strike history\n"
        f"`{PREFIX}removestrike strike_id reason` — remove a recorded strike\n\n"
        "**Founder and Co-Owner only**\n"
        f"`{PREFIX}bl @user reason` / `{PREFIX}unbl user_id`\n\n"
        "**Credits**\n"
        "Bot maker: **Larper**\n"
        "Founders: **RGX** and **Brawlka**\n"
        "Moderation logs and join tracking use their configured channels."
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Missing an argument. Try `{PREFIX}help`.")
    elif isinstance(error, commands.BadArgument):
        await ctx.reply(f"I couldn't understand that member or number. Try `{PREFIX}help`.")
    elif isinstance(error, commands.CheckFailure):
        await ctx.reply("You don't have permission to use that command.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    elif isinstance(error, discord.Forbidden):
        await ctx.reply("I don't have the Discord permissions needed for that action.")
    elif isinstance(error, discord.HTTPException):
        await ctx.reply("Discord could not complete that action. Check the bot's permissions and role position.")
    else:
        print(f"Command error: {error}")
        await ctx.reply("Something went wrong. Check the bot's permissions and role position.")


if not TOKEN:
    raise SystemExit("Set DISCORD_TOKEN in your .env file before starting the bot.")

bot.run(TOKEN)
