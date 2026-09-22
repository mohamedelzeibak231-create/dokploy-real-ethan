import os
import re
import time
import random
import sqlite3
import asyncio
import datetime
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ============================================================
# CONFIG
# ============================================================
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "skullix.db")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # gets DMed/pinged on auto-mod spam actions

BLACK = 0x000000
MAX_WARNINGS = 5

# Spam auto-mod: N messages within WINDOW seconds triggers an auto mute + warn
SPAM_MESSAGE_THRESHOLD = 5
SPAM_WINDOW_SECONDS = 5
SPAM_MUTE_MINUTES = 10

# Prefixes are grouped by what they're allowed to run:
#   !!  -> auto-mod / moderation commands ONLY
#   e.  -> everything else (general/economy/utility commands)
#   g.  -> giveaway reroll ONLY
PREFIXES_AUTOMOD = ("!!",)
PREFIXES_GENERAL = ("e.", "E.")
PREFIXES_GIVEAWAY = ("g.", "G.")
ALL_PREFIXES = PREFIXES_AUTOMOD + PREFIXES_GENERAL + PREFIXES_GIVEAWAY

XP_MIN, XP_MAX = 5, 15


def xp_for_level(level: int) -> int:
    return 100 * (level + 1)


# ============================================================
# BOT SETUP
# ============================================================
intents = discord.Intents.default()
intents.members = True
intents.message_content = True


async def get_prefix(_bot: commands.Bot, _message: discord.Message):
    return list(ALL_PREFIXES)


bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)


def black_embed(**kwargs) -> discord.Embed:
    return discord.Embed(color=BLACK, **kwargs)


def is_staff(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.manage_messages or perms.ban_members


# ---- prefix-category checks, attached to individual commands ----
def is_automod_prefix(ctx: commands.Context) -> bool:
    return ctx.prefix in PREFIXES_AUTOMOD


def is_general_prefix(ctx: commands.Context) -> bool:
    return ctx.prefix in PREFIXES_GENERAL


def is_giveaway_prefix(ctx: commands.Context) -> bool:
    return ctx.prefix in PREFIXES_GIVEAWAY


def automod_only():
    return commands.check(is_automod_prefix)


def general_only():
    return commands.check(is_general_prefix)


def giveaway_only():
    return commands.check(is_giveaway_prefix)


def staff_only():
    async def predicate(ctx: commands.Context) -> bool:
        return isinstance(ctx.author, discord.Member) and is_staff(ctx.author)
    return commands.check(predicate)


DURATION_RE = re.compile(r"^(\d+)([smhd])$", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> Optional[int]:
    """'10m' / '2h' / '1d' / '30s' -> seconds. None if invalid."""
    match = DURATION_RE.match(text.strip())
    if not match:
        return None
    amount, unit = match.groups()
    return int(amount) * UNIT_SECONDS[unit.lower()]


# ============================================================
# DATABASE
# ============================================================
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def init_db():
    cur = db.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            welcome_channel_id INTEGER,
            welcome_message TEXT,
            goodbye_channel_id INTEGER,
            goodbye_message TEXT,
            log_channel_id INTEGER,
            ticket_category_id INTEGER,
            ticket_staff_role_id INTEGER,
            case_counter INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            case_number INTEGER,
            target_id INTEGER,
            moderator_id INTEGER,
            action TEXT,
            reason TEXT,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            moderator_id INTEGER,
            reason TEXT,
            created_at INTEGER
        );

        CREATE TABLE IF NOT EXISTS credits (
            guild_id INTEGER,
            user_id INTEGER,
            balance INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS shop_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            name TEXT,
            price INTEGER,
            role_id INTEGER
        );

        CREATE TABLE IF NOT EXISTS levels (
            guild_id INTEGER,
            user_id INTEGER,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS level_milestones (
            guild_id INTEGER,
            level INTEGER,
            role_id INTEGER,
            PRIMARY KEY (guild_id, level)
        );

        CREATE TABLE IF NOT EXISTS message_counts (
            guild_id INTEGER,
            user_id INTEGER,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS reaction_roles (
            message_id INTEGER,
            guild_id INTEGER,
            emoji TEXT,
            role_id INTEGER,
            PRIMARY KEY (message_id, emoji)
        );

        CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            channel_id INTEGER,
            message_id INTEGER,
            prize TEXT,
            host_id INTEGER,
            requirement_role_id INTEGER,
            winners_count INTEGER,
            end_time INTEGER,
            ended INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS giveaway_entries (
            giveaway_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (giveaway_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS tickets (
            channel_id INTEGER PRIMARY KEY,
            guild_id INTEGER,
            user_id INTEGER,
            open INTEGER DEFAULT 1
        );
        """
    )
    db.commit()


def get_guild_config(guild_id: int) -> sqlite3.Row:
    cur = db.cursor()
    cur.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
    row = cur.fetchone()
    if row is None:
        cur.execute("INSERT INTO guild_config (guild_id) VALUES (?)", (guild_id,))
        db.commit()
        cur.execute("SELECT * FROM guild_config WHERE guild_id=?", (guild_id,))
        row = cur.fetchone()
    return row


def next_case_number(guild_id: int) -> int:
    cur = db.cursor()
    cur.execute(
        "UPDATE guild_config SET case_counter = case_counter + 1 WHERE guild_id=?",
        (guild_id,),
    )
    db.commit()
    cur.execute("SELECT case_counter FROM guild_config WHERE guild_id=?", (guild_id,))
    return cur.fetchone()["case_counter"]


def create_case(guild_id: int, target_id: int, moderator_id: int, action: str, reason: str) -> int:
    get_guild_config(guild_id)
    number = next_case_number(guild_id)
    cur = db.cursor()
    cur.execute(
        "INSERT INTO cases (guild_id, case_number, target_id, moderator_id, action, reason, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, number, target_id, moderator_id, action, reason, int(time.time())),
    )
    db.commit()
    return number


def get_credits(guild_id: int, user_id: int) -> int:
    cur = db.cursor()
    cur.execute("SELECT balance FROM credits WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cur.fetchone()
    return row["balance"] if row else 0


def add_credits(guild_id: int, user_id: int, amount: int) -> int:
    cur = db.cursor()
    cur.execute(
        "INSERT INTO credits (guild_id, user_id, balance) VALUES (?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET balance = balance + excluded.balance",
        (guild_id, user_id, amount),
    )
    db.commit()
    return get_credits(guild_id, user_id)


# ============================================================
# DM HELPER (warn / mute / unmute / ban / unban / massban)
# ============================================================
async def dm_user(user: discord.abc.User, action: str, guild: discord.Guild, reason: str, case_number: int):
    try:
        embed = black_embed(
            title=f"You have been {action} in {guild.name}",
            description=f"**Reason:** {reason or 'No reason provided'}\n**Case:** #{case_number}",
        )
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def notify_owner(text: str):
    if not OWNER_ID:
        return
    try:
        owner = await bot.fetch_user(OWNER_ID)
        await owner.send(embed=black_embed(title="⚠️ Auto-mod alert", description=text))
    except (discord.Forbidden, discord.HTTPException):
        pass


# ============================================================
# EVENTS
# ============================================================
_spam_tracker = {}  # (guild_id, user_id) -> list[timestamps]


@bot.event
async def on_ready():
    init_db()
    if not giveaway_loop.is_running():
        giveaway_loop.start()
    try:
        await bot.tree.sync()
    except discord.HTTPException:
        pass
    print(f"Logged in as {bot.user} ({bot.user.id})")


@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_guild_config(member.guild.id)
    if cfg["welcome_channel_id"] and cfg["welcome_message"]:
        channel = member.guild.get_channel(cfg["welcome_channel_id"])
        if channel:
            msg = cfg["welcome_message"].replace("{user}", member.mention).replace("{server}", member.guild.name)
            try:
                await channel.send(embed=black_embed(description=msg))
            except discord.HTTPException:
                pass


@bot.event
async def on_member_remove(member: discord.Member):
    cfg = get_guild_config(member.guild.id)
    if cfg["goodbye_channel_id"] and cfg["goodbye_message"]:
        channel = member.guild.get_channel(cfg["goodbye_channel_id"])
        if channel:
            msg = cfg["goodbye_message"].replace("{user}", str(member)).replace("{server}", member.guild.name)
            try:
                await channel.send(embed=black_embed(description=msg))
            except discord.HTTPException:
                pass


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.member is None or payload.member.bot:
        return
    cur = db.cursor()
    cur.execute(
        "SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=?",
        (payload.message_id, str(payload.emoji)),
    )
    row = cur.fetchone()
    if row:
        role = payload.member.guild.get_role(row["role_id"])
        if role:
            try:
                await payload.member.add_roles(role, reason="Reaction role")
            except discord.Forbidden:
                pass


@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    guild = bot.get_guild(payload.guild_id)
    if guild is None:
        return
    cur = db.cursor()
    cur.execute(
        "SELECT role_id FROM reaction_roles WHERE message_id=? AND emoji=?",
        (payload.message_id, str(payload.emoji)),
    )
    row = cur.fetchone()
    if row:
        member = guild.get_member(payload.user_id)
        role = guild.get_role(row["role_id"])
        if member and role:
            try:
                await member.remove_roles(role, reason="Reaction role removed")
            except discord.Forbidden:
                pass


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    guild_id = message.guild.id
    user_id = message.author.id

    # ---- message stats tracking ----
    cur = db.cursor()
    cur.execute(
        "INSERT INTO message_counts (guild_id, user_id, count) VALUES (?, ?, 1) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET count = count + 1",
        (guild_id, user_id),
    )
    db.commit()

    # ---- leveling xp ----
    cur.execute("SELECT xp, level FROM levels WHERE guild_id=? AND user_id=?", (guild_id, user_id))
    row = cur.fetchone()
    xp_gain = random.randint(XP_MIN, XP_MAX)
    if row is None:
        cur.execute(
            "INSERT INTO levels (guild_id, user_id, xp, level) VALUES (?, ?, ?, 0)",
            (guild_id, user_id, xp_gain),
        )
        db.commit()
    else:
        new_xp = row["xp"] + xp_gain
        new_level = row["level"]
        leveled_up = False
        while new_xp >= xp_for_level(new_level):
            new_xp -= xp_for_level(new_level)
            new_level += 1
            leveled_up = True
        cur.execute(
            "UPDATE levels SET xp=?, level=? WHERE guild_id=? AND user_id=?",
            (new_xp, new_level, guild_id, user_id),
        )
        db.commit()
        if leveled_up:
            cur.execute(
                "SELECT role_id FROM level_milestones WHERE guild_id=? AND level=?",
                (guild_id, new_level),
            )
            mrow = cur.fetchone()
            if mrow:
                role = message.guild.get_role(mrow["role_id"])
                if role:
                    try:
                        await message.author.add_roles(role, reason=f"Reached level {new_level}")
                    except discord.Forbidden:
                        pass

    # ---- spam auto-mod ----
    key = (guild_id, user_id)
    now = time.time()
    timestamps = _spam_tracker.get(key, [])
    timestamps = [t for t in timestamps if now - t <= SPAM_WINDOW_SECONDS]
    timestamps.append(now)
    _spam_tracker[key] = timestamps

    if len(timestamps) >= SPAM_MESSAGE_THRESHOLD and isinstance(message.author, discord.Member):
        _spam_tracker[key] = []
        member = message.author
        if not is_staff(member):
            reason = "Automatic action: message spam detected"
            until = discord.utils.utcnow() + datetime.timedelta(minutes=SPAM_MUTE_MINUTES)
            try:
                await member.timeout(until, reason=reason)
            except discord.Forbidden:
                pass
            case_mute = create_case(guild_id, user_id, bot.user.id, "mute", reason)
            cur.execute(
                "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, bot.user.id, reason, int(time.time())),
            )
            db.commit()
            case_warn = create_case(guild_id, user_id, bot.user.id, "warn", reason)
            await dm_user(member, "muted", message.guild, reason, case_mute)
            await dm_user(member, "warned", message.guild, reason, case_warn)
            await notify_owner(
                f"Auto-muted and warned {member} ({member.id}) in {message.guild.name} for spam. "
                f"Cases #{case_mute} / #{case_warn}."
            )

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=black_embed(description=f"Missing argument: `{error.param.name}`"))
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=black_embed(description="Couldn't parse one of your arguments."))
        return
    raise error


# ============================================================
# AUTOMOD COMMANDS (!! only)
# ============================================================
@bot.command(name="ban")
@automod_only()
@staff_only()
async def ban_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    case_number = create_case(ctx.guild.id, member.id, ctx.author.id, "ban", reason)
    await dm_user(member, "banned", ctx.guild, reason, case_number)
    await ctx.guild.ban(member, reason=reason)
    await ctx.send(embed=black_embed(description=f"🔨 Banned {member.mention} — case #{case_number}"))


@bot.command(name="unban")
@automod_only()
@staff_only()
async def unban_cmd(ctx: commands.Context, user_id: int, *, reason: str = "No reason provided"):
    user = discord.Object(id=user_id)
    await ctx.guild.unban(user, reason=reason)
    case_number = create_case(ctx.guild.id, user_id, ctx.author.id, "unban", reason)
    try:
        fetched = await bot.fetch_user(user_id)
        await dm_user(fetched, "unbanned", ctx.guild, reason, case_number)
    except discord.NotFound:
        pass
    await ctx.send(embed=black_embed(description=f"✅ Unbanned `{user_id}` — case #{case_number}"))


@bot.command(name="mute")
@automod_only()
@staff_only()
async def mute_cmd(ctx: commands.Context, member: discord.Member, duration: str = "10m", *, reason: str = "No reason provided"):
    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send(embed=black_embed(description="Invalid duration. Use formats like `10m`, `2h`, `1d`."))
        return
    until = discord.utils.utcnow() + datetime.timedelta(seconds=seconds)
    await member.timeout(until, reason=reason)
    case_number = create_case(ctx.guild.id, member.id, ctx.author.id, "mute", reason)
    await dm_user(member, "muted", ctx.guild, reason, case_number)
    await ctx.send(embed=black_embed(description=f"🔇 Muted {member.mention} for `{duration}` — case #{case_number}"))


@bot.command(name="unmute")
@automod_only()
@staff_only()
async def unmute_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    await member.timeout(None, reason=reason)
    case_number = create_case(ctx.guild.id, member.id, ctx.author.id, "unmute", reason)
    await dm_user(member, "unmuted", ctx.guild, reason, case_number)
    await ctx.send(embed=black_embed(description=f"🔊 Unmuted {member.mention} — case #{case_number}"))


@bot.command(name="warn")
@automod_only()
@staff_only()
async def warn_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
    cur = db.cursor()
    cur.execute(
        "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
        (ctx.guild.id, member.id, ctx.author.id, reason, int(time.time())),
    )
    db.commit()
    case_number = create_case(ctx.guild.id, member.id, ctx.author.id, "warn", reason)
    await dm_user(member, "warned", ctx.guild, reason, case_number)

    cur.execute("SELECT COUNT(*) AS c FROM warnings WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    count = cur.fetchone()["c"]
    await ctx.send(embed=black_embed(description=f"⚠️ Warned {member.mention} ({count}/{MAX_WARNINGS}) — case #{case_number}"))

    if count >= MAX_WARNINGS:
        reason2 = f"Reached {MAX_WARNINGS} warnings"
        until = discord.utils.utcnow() + datetime.timedelta(minutes=SPAM_MUTE_MINUTES)
        try:
            await member.timeout(until, reason=reason2)
        except discord.Forbidden:
            pass
        auto_case = create_case(ctx.guild.id, member.id, bot.user.id, "mute", reason2)
        await dm_user(member, "muted", ctx.guild, reason2, auto_case)
        await ctx.send(embed=black_embed(description=f"🔇 {member.mention} hit max warnings and was auto-muted — case #{auto_case}"))


@bot.command(name="unwarn")
@automod_only()
@staff_only()
async def unwarn_cmd(ctx: commands.Context, member: discord.Member, *, reason: str = "Warning removed"):
    cur = db.cursor()
    cur.execute(
        "SELECT id FROM warnings WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
        (ctx.guild.id, member.id),
    )
    row = cur.fetchone()
    if row is None:
        await ctx.send(embed=black_embed(description=f"{member.mention} has no warnings to remove."))
        return
    cur.execute("DELETE FROM warnings WHERE id=?", (row["id"],))
    db.commit()
    case_number = create_case(ctx.guild.id, member.id, ctx.author.id, "unwarn", reason)
    await dm_user(member, "unwarned", ctx.guild, reason, case_number)
    await ctx.send(embed=black_embed(description=f"✅ Removed a warning from {member.mention} — case #{case_number}"))


@bot.command(name="massban")
@automod_only()
@staff_only()
async def massban_cmd(ctx: commands.Context, user_ids: commands.Greedy[int], *, reason: str = "Mass ban"):
    if not user_ids:
        await ctx.send(embed=black_embed(description="Provide at least one user ID."))
        return
    banned = []
    for uid in user_ids:
        try:
            fetched = None
            try:
                fetched = await bot.fetch_user(uid)
            except discord.NotFound:
                pass
            await ctx.guild.ban(discord.Object(id=uid), reason=reason)
            case_number = create_case(ctx.guild.id, uid, ctx.author.id, "massban", reason)
            if fetched:
                await dm_user(fetched, "banned", ctx.guild, reason, case_number)
            banned.append(str(uid))
        except discord.HTTPException:
            continue
    await ctx.send(embed=black_embed(description=f"🔨 Mass-banned {len(banned)} user(s): {', '.join(banned) or 'none'}"))


@bot.group(name="case", invoke_without_command=True)
@automod_only()
@staff_only()
async def case_group(ctx: commands.Context):
    await ctx.send(embed=black_embed(description="Use `!!case view <number>`"))


@case_group.command(name="view")
@automod_only()
@staff_only()
async def case_view(ctx: commands.Context, number: int):
    cur = db.cursor()
    cur.execute("SELECT * FROM cases WHERE guild_id=? AND case_number=?", (ctx.guild.id, number))
    row = cur.fetchone()
    if row is None:
        await ctx.send(embed=black_embed(description=f"No case #{number} found."))
        return
    embed = black_embed(title=f"Case #{number}")
    embed.add_field(name="Action", value=row["action"], inline=True)
    embed.add_field(name="Target", value=f"<@{row['target_id']}>", inline=True)
    embed.add_field(name="Moderator", value=f"<@{row['moderator_id']}>", inline=True)
    embed.add_field(name="Reason", value=row["reason"] or "No reason provided", inline=False)
    embed.timestamp = datetime.datetime.fromtimestamp(row["created_at"], tz=datetime.timezone.utc)
    await ctx.send(embed=embed)


@bot.command(name="help")
@automod_only()
async def help_cmd(ctx: commands.Context):
    embed = black_embed(title="Skullix — help")
    embed.add_field(
        name="!! — moderation (staff only)",
        value="`!!ban` `!!unban <id>` `!!mute <member> [dur]` `!!unmute` `!!warn` `!!unwarn` "
              "`!!massban <ids...>` `!!case view <n>` `!!help`",
        inline=False,
    )
    embed.add_field(name="e. — general", value="`e.m [member]` — message stats", inline=False)
    embed.add_field(name="g. — giveaway", value="`g. reroll <message_id>`", inline=False)
    embed.add_field(
        name="Slash commands",
        value="`/giveaway start` `/welcome set` `/goodbye set` `/message role set` "
              "`/shop view` `/shop manage` `/credits` `/ticket setup` `/level milestone set`",
        inline=False,
    )
    await ctx.send(embed=embed)


# ============================================================
# GENERAL COMMANDS (e. / E. only)
# ============================================================
@bot.command(name="m", aliases=["stats", "messagestats"])
@general_only()
async def message_stats_cmd(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author
    cur = db.cursor()
    cur.execute("SELECT count FROM message_counts WHERE guild_id=? AND user_id=?", (ctx.guild.id, member.id))
    row = cur.fetchone()
    count = row["count"] if row else 0
    await ctx.send(embed=black_embed(description=f"📊 {member.mention} has sent **{count}** messages."))


# ============================================================
# GIVEAWAY REROLL (g. only)
# ============================================================
@bot.command(name="reroll")
@giveaway_only()
@staff_only()
async def reroll_cmd(ctx: commands.Context, message_id: int):
    cur = db.cursor()
    cur.execute("SELECT * FROM giveaways WHERE guild_id=? AND message_id=?", (ctx.guild.id, message_id))
    giveaway = cur.fetchone()
    if giveaway is None:
        await ctx.send(embed=black_embed(description="No giveaway found with that message ID."))
        return
    cur.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (giveaway["id"],))
    entrants = [r["user_id"] for r in cur.fetchall()]
    if not entrants:
        await ctx.send(embed=black_embed(description="No entrants to reroll from."))
        return
    winner_id = random.choice(entrants)
    await ctx.send(embed=black_embed(description=f"🎉 New winner for **{giveaway['prize']}**: <@{winner_id}>"))


# ============================================================
# GIVEAWAY VIEW (entry button) + background end loop
# ============================================================
class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: int):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="🎉 Enter", style=discord.ButtonStyle.primary, custom_id="giveaway_enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        cur = db.cursor()
        cur.execute("SELECT * FROM giveaways WHERE id=?", (self.giveaway_id,))
        giveaway = cur.fetchone()
        if giveaway is None or giveaway["ended"]:
            await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
            return
        if giveaway["requirement_role_id"]:
            role = interaction.guild.get_role(giveaway["requirement_role_id"])
            if role and role not in interaction.user.roles:
                await interaction.response.send_message(
                    f"You need the {role.mention} role to enter.", ephemeral=True
                )
                return
        cur.execute(
            "INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id) VALUES (?, ?)",
            (self.giveaway_id, interaction.user.id),
        )
        db.commit()
        await interaction.response.send_message("You're entered! Good luck 🎉", ephemeral=True)


@tasks.loop(seconds=15)
async def giveaway_loop():
    cur = db.cursor()
    now = int(time.time())
    cur.execute("SELECT * FROM giveaways WHERE ended=0 AND end_time<=?", (now,))
    ended = cur.fetchall()
    for giveaway in ended:
        cur.execute("UPDATE giveaways SET ended=1 WHERE id=?", (giveaway["id"],))
        db.commit()
        channel = bot.get_channel(giveaway["channel_id"])
        if channel is None:
            continue
        cur.execute("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (giveaway["id"],))
        entrants = [r["user_id"] for r in cur.fetchall()]
        winners_count = min(giveaway["winners_count"], len(entrants)) if entrants else 0
        winners = random.sample(entrants, winners_count) if winners_count else []
        if winners:
            mentions = ", ".join(f"<@{w}>" for w in winners)
            desc = f"🎉 Congrats {mentions}! You won **{giveaway['prize']}**.\nHosted by <@{giveaway['host_id']}>"
        else:
            desc = f"No valid entries — nobody won **{giveaway['prize']}**."
        try:
            await channel.send(embed=black_embed(title="Giveaway ended", description=desc))
        except discord.HTTPException:
            pass


# ============================================================
# SLASH COMMANDS
# ============================================================
@bot.tree.command(name="giveaway", description="Giveaway commands")
@app_commands.describe(
    prize="What is being given away",
    duration="Duration, e.g. 10m / 2h / 1d",
    winners="Number of winners",
    requirement_role="Role required to enter (optional)",
    host="Who is hosting (defaults to you)",
)
async def giveaway_start(
    interaction: discord.Interaction,
    prize: str,
    duration: str,
    winners: int = 1,
    requirement_role: Optional[discord.Role] = None,
    host: Optional[discord.Member] = None,
):
    seconds = parse_duration(duration)
    if seconds is None:
        await interaction.response.send_message("Invalid duration. Use `10m`, `2h`, `1d`, etc.", ephemeral=True)
        return
    host = host or interaction.user
    end_time = int(time.time()) + seconds
    embed = black_embed(
        title="🎉 Giveaway",
        description=f"**Prize:** {prize}\n**Hosted by:** {host.mention}\n"
                     f"**Winners:** {winners}\n**Ends:** <t:{end_time}:R>"
                     + (f"\n**Requirement:** {requirement_role.mention}" if requirement_role else ""),
    )
    await interaction.response.send_message(embed=embed)
    msg = await interaction.original_response()

    cur = db.cursor()
    cur.execute(
        "INSERT INTO giveaways (guild_id, channel_id, message_id, prize, host_id, requirement_role_id, "
        "winners_count, end_time, ended) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            interaction.guild_id,
            interaction.channel_id,
            msg.id,
            prize,
            host.id,
            requirement_role.id if requirement_role else None,
            winners,
            end_time,
        ),
    )
    db.commit()
    giveaway_id = cur.lastrowid
    view = GiveawayView(giveaway_id)
    await msg.edit(embed=embed, view=view)


welcome_group = app_commands.Group(name="welcome", description="Welcome message settings")
goodbye_group = app_commands.Group(name="goodbye", description="Goodbye message settings")


@welcome_group.command(name="set", description="Set the welcome channel and message")
@app_commands.checks.has_permissions(manage_guild=True)
async def welcome_set(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    get_guild_config(interaction.guild_id)
    cur = db.cursor()
    cur.execute(
        "UPDATE guild_config SET welcome_channel_id=?, welcome_message=? WHERE guild_id=?",
        (channel.id, message, interaction.guild_id),
    )
    db.commit()
    await interaction.response.send_message(
        f"Welcome messages will be sent in {channel.mention}. Use `{{user}}` and `{{server}}` as placeholders.",
        ephemeral=True,
    )


@goodbye_group.command(name="set", description="Set the goodbye channel and message")
@app_commands.checks.has_permissions(manage_guild=True)
async def goodbye_set(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    get_guild_config(interaction.guild_id)
    cur = db.cursor()
    cur.execute(
        "UPDATE guild_config SET goodbye_channel_id=?, goodbye_message=? WHERE guild_id=?",
        (channel.id, message, interaction.guild_id),
    )
    db.commit()
    await interaction.response.send_message(
        f"Goodbye messages will be sent in {channel.mention}. Use `{{user}}` and `{{server}}` as placeholders.",
        ephemeral=True,
    )


bot.tree.add_command(welcome_group)
bot.tree.add_command(goodbye_group)

message_group = app_commands.Group(name="message", description="Message-related settings")
message_role_group = app_commands.Group(name="role", description="Reaction role settings", parent=message_group)


@message_role_group.command(name="set", description="Give a role when a user reacts to a message with an emoji")
@app_commands.checks.has_permissions(manage_roles=True)
async def message_role_set(interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
    cur = db.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO reaction_roles (message_id, guild_id, emoji, role_id) VALUES (?, ?, ?, ?)",
        (int(message_id), interaction.guild_id, emoji, role.id),
    )
    db.commit()
    await interaction.response.send_message(
        f"Reacting with {emoji} on that message now grants {role.mention}.", ephemeral=True
    )


bot.tree.add_command(message_group)

shop_group = app_commands.Group(name="shop", description="Server shop")


@shop_group.command(name="view", description="View items in the shop")
async def shop_view(interaction: discord.Interaction):
    cur = db.cursor()
    cur.execute("SELECT * FROM shop_items WHERE guild_id=?", (interaction.guild_id,))
    items = cur.fetchall()
    if not items:
        await interaction.response.send_message("The shop is empty.", ephemeral=True)
        return
    embed = black_embed(title="Shop")
    for item in items:
        role_text = f" — grants <@&{item['role_id']}>" if item["role_id"] else ""
        embed.add_field(name=f"{item['name']} (#{item['id']})", value=f"{item['price']} credits{role_text}", inline=False)
    await interaction.response.send_message(embed=embed)


@shop_group.command(name="manage", description="Add or remove a shop item")
@app_commands.describe(action="add or remove", name="Item name", price="Price in credits", role="Role granted on purchase (optional)")
@app_commands.choices(action=[app_commands.Choice(name="add", value="add"), app_commands.Choice(name="remove", value="remove")])
@app_commands.checks.has_permissions(manage_guild=True)
async def shop_manage(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    name: str,
    price: Optional[int] = None,
    role: Optional[discord.Role] = None,
):
    cur = db.cursor()
    if action.value == "add":
        if price is None:
            await interaction.response.send_message("Provide a price to add an item.", ephemeral=True)
            return
        cur.execute(
            "INSERT INTO shop_items (guild_id, name, price, role_id) VALUES (?, ?, ?, ?)",
            (interaction.guild_id, name, price, role.id if role else None),
        )
        db.commit()
        await interaction.response.send_message(f"Added **{name}** for {price} credits.", ephemeral=True)
    else:
        cur.execute("DELETE FROM shop_items WHERE guild_id=? AND name=?", (interaction.guild_id, name))
        db.commit()
        await interaction.response.send_message(f"Removed **{name}** from the shop.", ephemeral=True)


bot.tree.add_command(shop_group)

credits_group = app_commands.Group(name="credits", description="Server credits / economy")


@credits_group.command(name="balance", description="Check your (or someone's) credit balance")
async def credits_balance(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    member = member or interaction.user
    balance = get_credits(interaction.guild_id, member.id)
    await interaction.response.send_message(f"{member.mention} has **{balance}** credits.")


@credits_group.command(name="add", description="Add credits to a member")
@app_commands.checks.has_permissions(manage_guild=True)
async def credits_add(interaction: discord.Interaction, member: discord.Member, amount: int):
    new_balance = add_credits(interaction.guild_id, member.id, amount)
    await interaction.response.send_message(f"Added {amount} credits to {member.mention} (now {new_balance}).")


@credits_group.command(name="remove", description="Remove credits from a member")
@app_commands.checks.has_permissions(manage_guild=True)
async def credits_remove(interaction: discord.Interaction, member: discord.Member, amount: int):
    new_balance = add_credits(interaction.guild_id, member.id, -amount)
    await interaction.response.send_message(f"Removed {amount} credits from {member.mention} (now {new_balance}).")


bot.tree.add_command(credits_group)

ticket_group = app_commands.Group(name="ticket", description="Support ticket system")


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open ticket", style=discord.ButtonStyle.success, custom_id="ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = get_guild_config(interaction.guild_id)
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        staff_role = interaction.guild.get_role(cfg["ticket_staff_role_id"]) if cfg["ticket_staff_role_id"] else None
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        category = interaction.guild.get_channel(cfg["ticket_category_id"]) if cfg["ticket_category_id"] else None
        channel = await interaction.guild.create_text_channel(
            f"ticket-{interaction.user.name}", overwrites=overwrites, category=category
        )
        cur = db.cursor()
        cur.execute(
            "INSERT INTO tickets (channel_id, guild_id, user_id, open) VALUES (?, ?, ?, 1)",
            (channel.id, interaction.guild_id, interaction.user.id),
        )
        db.commit()
        await channel.send(
            content=interaction.user.mention,
            embed=black_embed(description="Support will be with you shortly. Describe your issue below."),
            view=TicketCloseView(),
        )
        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)


class TicketCloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cur = db.cursor()
        cur.execute("UPDATE tickets SET open=0 WHERE channel_id=?", (interaction.channel_id,))
        db.commit()
        await interaction.response.send_message("Closing ticket...")
        await asyncio.sleep(3)
        await interaction.channel.delete()


@ticket_group.command(name="setup", description="Post the ticket panel in this channel")
@app_commands.describe(staff_role="Role that can see tickets", category="Category tickets are created under (optional)")
@app_commands.checks.has_permissions(manage_guild=True)
async def ticket_setup(
    interaction: discord.Interaction,
    staff_role: discord.Role,
    category: Optional[discord.CategoryChannel] = None,
):
    get_guild_config(interaction.guild_id)
    cur = db.cursor()
    cur.execute(
        "UPDATE guild_config SET ticket_staff_role_id=?, ticket_category_id=? WHERE guild_id=?",
        (staff_role.id, category.id if category else None, interaction.guild_id),
    )
    db.commit()
    await interaction.response.send_message(
        embed=black_embed(title="Support", description="Click below to open a ticket."),
        view=TicketPanelView(),
    )


bot.tree.add_command(ticket_group)

level_group = app_commands.Group(name="level", description="Leveling settings")
level_milestone_group = app_commands.Group(name="milestone", description="Level milestone rewards", parent=level_group)


@level_milestone_group.command(name="set", description="Set a role reward for reaching a level")
@app_commands.checks.has_permissions(manage_guild=True)
async def level_milestone_set(interaction: discord.Interaction, level: int, role: discord.Role):
    cur = db.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO level_milestones (guild_id, level, role_id) VALUES (?, ?, ?)",
        (interaction.guild_id, level, role.id),
    )
    db.commit()
    await interaction.response.send_message(f"Reaching level {level} now grants {role.mention}.", ephemeral=True)


bot.tree.add_command(level_group)


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the DISCORD_TOKEN environment variable before running.")

    # ---- TEMPORARY DEBUG: remove once the login issue is confirmed fixed ----
    _preview = f"{TOKEN[:6]}...{TOKEN[-6:]}" if len(TOKEN) > 12 else "(too short to preview)"
    print(f"[DEBUG] TOKEN length={len(TOKEN)} preview={_preview} repr={TOKEN!r}"[:200])
    # --------------------------------------------------------------------

    bot.run(TOKEN)
