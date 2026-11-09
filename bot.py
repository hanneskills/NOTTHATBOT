import os
import re
import random
import math
import unicodedata
from fractions import Fraction
import asyncio
import discord
import requests
from discord.ext import commands, tasks
from threading import Thread
from flask import Flask
from datetime import datetime, timezone, timedelta

# =================================================================
# 1. MINI WEB SERVER (keeps Render service alive)
# =================================================================

app = Flask('')

@app.route('/')
def home(): return "Bot is alive!"

def run_web_server(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run_web_server).start()

# =================================================================
# 2. BOT SETUP
# =================================================================

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

ROLE_NAME      = "gamer"
SIGNUP_CHANNEL = "general"

# Holds the single active poll globally (one per bot instance)
# Structure: {
#   "message_id": int,
#   "channel_id": int,
#   "players": set of user_ids,
#   "game_ts": int or None   (unix timestamp of game start, if provided)
# }
active_poll = {
    "message_id": None,
    "channel_id": None,
    "players": set(),
    "game_ts": None,
}

# =================================================================
# 3. SUPABASE SETUP
# =================================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

def get_supabase_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }

def db_load_tracked():
    """Returns (tracked_players, player_discord_ids, player_rank_bracket, player_balance)
    — four dicts keyed by steam_id, loaded in one round trip."""
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/tracked_players?select=steam_id,display_name,discord_user_id,rank_bracket,balance",
            headers=get_supabase_headers(), timeout=10
        )
        if res.status_code == 200:
            rows = res.json()
            tracked = {row["steam_id"]: row["display_name"] for row in rows}
            discord_ids = {
                row["steam_id"]: int(row["discord_user_id"])
                for row in rows if row.get("discord_user_id")
            }
            rank_brackets = {
                row["steam_id"]: row["rank_bracket"]
                for row in rows if row.get("rank_bracket")
            }
            balances = {
                row["steam_id"]: int(row.get("balance") or 0)
                for row in rows
            }
            return tracked, discord_ids, rank_brackets, balances
        print(f"[Supabase] Failed to load: {res.status_code} {res.text}")
        return {}, {}, {}, {}
    except Exception as e:
        print(f"[Supabase] db_load_tracked error: {e}")
        return {}, {}, {}, {}

def db_add_player(steam_id, display_name, discord_user_id=None):
    try:
        payload = {"steam_id": steam_id, "display_name": display_name}
        if discord_user_id is not None:
            payload["discord_user_id"] = str(discord_user_id)
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/tracked_players",
            headers={**get_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
            json=payload, timeout=10
        )
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"[Supabase] db_add_player error: {e}")
        return False

def db_remove_player(steam_id):
    try:
        res = requests.delete(
            f"{SUPABASE_URL}/rest/v1/tracked_players?steam_id=eq.{steam_id}",
            headers=get_supabase_headers(), timeout=10
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[Supabase] db_remove_player error: {e}")
        return False

def db_link_discord(steam_id, discord_user_id):
    try:
        res = requests.patch(
            f"{SUPABASE_URL}/rest/v1/tracked_players?steam_id=eq.{steam_id}",
            headers=get_supabase_headers(),
            json={"discord_user_id": str(discord_user_id)}, timeout=10
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[Supabase] db_link_discord error: {e}")
        return False

def db_update_rank_bracket(steam_id, bracket):
    try:
        res = requests.patch(
            f"{SUPABASE_URL}/rest/v1/tracked_players?steam_id=eq.{steam_id}",
            headers=get_supabase_headers(),
            json={"rank_bracket": bracket}, timeout=10
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[Supabase] db_update_rank_bracket error: {e}")
        return False

def db_update_balance(steam_id, balance):
    """Writes the absolute balance for one player."""
    try:
        res = requests.patch(
            f"{SUPABASE_URL}/rest/v1/tracked_players?steam_id=eq.{steam_id}",
            headers=get_supabase_headers(),
            json={"balance": int(balance)}, timeout=10
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[Supabase] db_update_balance error: {e}")
        return False

# =================================================================
# 3.5 NAME SANITIZATION
# =================================================================
# Player names come from Steam/Leetify and are user-controlled, so they can
# contain characters that break Discord's monospace scoreboard tables:
#   - backticks (`) close the code block early and mangle everything after it
#   - right-to-left script (Arabic, Hebrew, etc.) flips the visual direction
#     of the whole row instead of just the name
#   - zero-width / control characters can silently corrupt the layout too
# Rather than trying to escape these, we just swap the whole name out for a
# neutral placeholder — trying to keep *part* of the name isn't worth the
# risk of missing an edge case.

NAME_SANITIZE_FALLBACK = "John Doe"

def sanitize_display_name(name: str, fallback: str = NAME_SANITIZE_FALLBACK) -> str:
    """Returns `name` unchanged if it's safe to embed in a Discord monospace
    table, otherwise returns `fallback`."""
    if not name:
        return fallback

    # Backticks (regular + fullwidth) end a ``` / ` code block early.
    if "`" in name or "\uFF40" in name:
        return fallback

    for ch in name:
        bidi = unicodedata.bidirectional(ch)
        # R/AL = right-to-left letters (Arabic, Hebrew, ...); RLE/RLO/RLI/PDI/
        # LRI/FSI are explicit bidi-control characters that can flip direction.
        if bidi in ("R", "AL", "RLE", "RLO", "RLI", "PDI", "LRI", "FSI"):
            return fallback
        # Cf = invisible "format" characters (zero-width joiners, etc.),
        # Cc = control characters. Both can break table alignment silently.
        if unicodedata.category(ch) in ("Cf", "Cc"):
            return fallback
        # Wide/fullwidth script (CJK, etc.). The Unicode standard treats these
        # as exactly double-width, but Discord's actual font doesn't render
        # them at a perfectly clean 2x — there's a small per-character rounding
        # error that's invisible on 1-2 characters but compounds into a visible
        # drift over a full name. Since that can't be reliably corrected for,
        # names using it get swapped out instead of risking misalignment.
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            return fallback

    return name


# ── Visual-width-aware padding ─────────────────────────────────────
# Discord's monospace font renders CJK characters (Japanese/Chinese/Korean)
# and most emoji roughly TWICE as wide as a Latin character, even though
# each is still just one Python character. Padding/truncating by len()
# assumes 1 character = 1 column, so any name containing a wide character
# (or the ⭐ tracked-player marker) throws every column after it out of
# alignment. These helpers pad/truncate by rendered column width instead,
# so a table's stat columns always start at the same horizontal position
# no matter what's in the name.

def _char_visual_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1

def _visual_width(text: str) -> int:
    return sum(_char_visual_width(ch) for ch in text)

def pad_visual(text: str, width: int, ellipsis: bool = False) -> str:
    """Left-align `text` to exactly `width` visual columns, truncating on
    overflow (optionally with a trailing '…')."""
    text = str(text)
    reserve = 1 if (ellipsis and _visual_width(text) > width) else 0
    out, w = "", 0
    for ch in text:
        cw = _char_visual_width(ch)
        if w + cw > width - reserve:
            break
        out += ch
        w += cw
    if reserve:
        out += "…"
        w += 1
    return out + " " * (width - w)

def rpad_visual(text: str, width: int) -> str:
    """Right-align text to a fixed visual width for monospace tables."""
    text = str(text)
    return " " * max(0, width - _visual_width(text)) + text


# =================================================================
# 4. POLL HELPERS
# =================================================================

def parse_time_offset(text):
    """
    Looks for 'in 2 hours', 'in 20 minutes', 'in 1.5 hours', 'in 1h30m',
    'in 1 hour 30 minutes', etc.
    Recognizes hour units: h, hr, hrs, hour, hours
    Recognizes minute units: m, min, mins, minute, minutes
    Returns a UTC datetime if found, else None.
    """
    text = text.lower()
    hour_unit   = r'h(?:r|rs|our|ours)?'
    minute_unit = r'm(?:in|ins|inute|inutes)?'

    # Both hours and minutes given together, e.g. "in 1h30m" / "in 1 hour 30 min"
    # (uses a negative lookahead instead of \b since "h" immediately followed by a
    # digit, like in "1h30m", isn't a \b word boundary)
    combined = re.search(
        rf'in\s+(\d+(?:\.\d+)?)\s*{hour_unit}(?![a-z])\s*(?:and\s*)?(\d+(?:\.\d+)?)\s*{minute_unit}(?![a-z])',
        text
    )
    if combined:
        hours   = float(combined.group(1))
        minutes = float(combined.group(2))
        return datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)

    # Just one unit given
    match = re.search(rf'in\s+(\d+(?:\.\d+)?)\s*({hour_unit}|{minute_unit})(?![a-z])', text)
    if not match:
        return None
    amount = float(match.group(1))
    unit   = match.group(2)
    delta  = timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount)
    return datetime.now(timezone.utc) + delta


def build_poll_embed(player_ids: set, game_ts: int | None) -> discord.Embed:
    """
    Builds the signup embed. Title changes based on whether a time was given.
    Uses Discord's adaptive timestamp for the game time.
    """
    if game_ts:
        # "Who's playing in 2 hours?" — uses Discord's relative timestamp in the title
        title = f"🎮 Who's playing <t:{game_ts}:R>?"
        footer = f"Game starts at <t:{game_ts}:t> your time · React ✅ to join · ❌ to leave · 🗑️ to remove poll"
    else:
        title  = "🎮 Who's playing tonight?"
        footer = "React ✅ to join · ❌ to leave · 🗑️ to remove poll"

    player_mentions = (
        "\n".join(f"• <@{uid}>" for uid in player_ids)
        if player_ids else "*No one yet...*"
    )

    embed = discord.Embed(title=title, color=discord.Color.blurple())
    embed.add_field(name=f"Players ({len(player_ids)}):", value=player_mentions, inline=False)
    embed.set_footer(text=footer)
    return embed


async def delete_active_poll():
    """Tries to delete the currently active poll message."""
    if active_poll["message_id"] is None:
        return
    try:
        channel = bot.get_channel(active_poll["channel_id"])
        if channel:
            msg = await channel.fetch_message(active_poll["message_id"])
            await msg.delete()
    except Exception:
        pass  # Message may already be gone


def reset_poll_state(keep_players=False, game_ts=None):
    """Resets tracking state, optionally carrying over the player list."""
    players = set(active_poll["players"]) if keep_players else set()
    active_poll["message_id"] = None
    active_poll["channel_id"] = None
    active_poll["players"]    = players
    active_poll["game_ts"]    = game_ts


# =================================================================
# 5. POLL EVENTS
# =================================================================

@bot.listen('on_message')
async def handle_game_signups(message):
    if message.author == bot.user:
        return
    if message.content.startswith("!"):
        return
    if message.channel.name != SIGNUP_CHANNEL:
        return

    # Only create a poll when the bot itself is pinged/mentioned.
    if bot.user not in message.mentions:
        return

    # Detect optional time
    game_time = parse_time_offset(message.content)
    game_ts   = int(game_time.timestamp()) if game_time else None

    # Delete the old poll, carry over the player list
    await delete_active_poll()
    reset_poll_state(keep_players=True, game_ts=game_ts)
    # Explicitly write game_ts after reset so it always reflects the latest message
    active_poll["game_ts"] = game_ts

    embed = build_poll_embed(active_poll["players"], active_poll["game_ts"])

    signup_message = await message.channel.send(embed=embed)
    await signup_message.add_reaction("✅")
    await signup_message.add_reaction("❌")
    await signup_message.add_reaction("🗑️")

    active_poll["message_id"] = signup_message.id
    active_poll["channel_id"] = signup_message.channel.id


@bot.event
async def on_reaction_add(reaction, user):
    if user == bot.user:
        return
    if reaction.message.id != active_poll["message_id"]:
        return

    emoji = str(reaction.emoji)

    # Always remove the user's reaction so only the bot's stays visible
    try:
        await reaction.message.remove_reaction(reaction.emoji, user)
    except Exception:
        pass

    if emoji == "✅":
        active_poll["players"].add(user.id)
        embed = build_poll_embed(active_poll["players"], active_poll["game_ts"])
        await reaction.message.edit(embed=embed)

    elif emoji == "❌":
        active_poll["players"].discard(user.id)
        embed = build_poll_embed(active_poll["players"], active_poll["game_ts"])
        await reaction.message.edit(embed=embed)

    elif emoji == "🗑️":
        await reaction.message.delete()
        reset_poll_state(keep_players=False)





# --- Reset poll completely at 3 AM UTC every day ---
@tasks.loop(minutes=1)
async def reset_poll_at_3am():
    now = datetime.now(timezone.utc)
    if now.hour == 3 and now.minute == 0:
        await delete_active_poll()
        reset_poll_state(keep_players=False)
        print("[Poll] Nightly reset at 03:00 UTC.")

# --- Same automatic catch-up + rank sync that runs on restart, but every
# --- midnight UTC too, so long uptimes without a restart don't drift. ---
@tasks.loop(minutes=1)
async def midnight_catchup_task():
    now = datetime.now(timezone.utc)
    if now.hour == 0 and now.minute == 0:
        for guild in bot.guilds:
            await run_automatic_catchup(guild)
        print("[catch_up_missed_matches] Midnight catch-up run complete.")

# =================================================================
# 6. VOICE ROLE
# =================================================================

@bot.event
async def on_voice_state_update(member, before, after):
    gamer_role = discord.utils.get(member.guild.roles, name=ROLE_NAME)
    if not gamer_role:
        return
    if before.channel is None and after.channel is not None:
        await member.add_roles(gamer_role)
    elif before.channel is not None and after.channel is None:
        await member.remove_roles(gamer_role)

# =================================================================
# 7. LEETIFY — PROFILE STATS ON STEAM LINK
# =================================================================

LEETIFY_API_KEY  = os.environ.get('LEETIFY_API_KEY')
LEETIFY_HEADERS  = {"_leetify_key": LEETIFY_API_KEY} if LEETIFY_API_KEY else {}
LEETIFY_BASE     = "https://api-public.cs-prod.leetify.com"

# Channel names used throughout the Leetify integration.
# Match scoreboards (auto-posted + !getmatch) still go to LEETIFY_CHANNEL_NAME.
# The weekly leaderboard/recap is posted to WEEKLY_CHANNEL_NAME instead.
LEETIFY_CHANNEL_NAME = "leetify"
WEEKLY_CHANNEL_NAME   = "weekly"

STEAM_API_KEY    = os.environ.get('STEAM_API_KEY')  # Needed for custom URL resolution

STEAMID64_RE     = re.compile(r'\b(7656119\d{10})\b')
STEAM_PROFILE_RE = re.compile(r'steamcommunity\.com/profiles/(\d+)')
STEAM_CUSTOM_RE  = re.compile(r'steamcommunity\.com/id/([^/\s?]+)')
last_seen_matches = {}
TRACKED_PLAYERS   = {}

# Discord role-per-rank-bracket state
PLAYER_DISCORD_IDS  = {}   # steam_id -> discord_user_id
PLAYER_RANK_BRACKET = {}   # steam_id -> last-assigned bracket string

# ── Economy ─────────────────────────────────────────────────────────
# Balances live in the `balance` column of tracked_players (bigint, default 0).
# Weekly payouts (paid by the automatic Sunday recap only):
#   +1 point per kill, +5 per top frag, +50 for the best kills/game.
PLAYER_BALANCE = {}        # steam_id -> int balance
BALANCE_LOCK   = asyncio.Lock()
POINTS_PER_KILL     = 1
POINTS_PER_TOPFRAG  = 5
POINTS_BEST_KPG     = 50
# Minimum games in the week to be eligible for the best-K/G bonus, so a
# single lucky game can't win it. Set to 1 to disable this restriction.
MIN_GAMES_FOR_BEST_KPG = 3

def steam_id_for_discord(discord_id: int) -> str | None:
    for sid, did in PLAYER_DISCORD_IDS.items():
        if did == discord_id:
            return sid
    return None

async def adjust_balance(steam_id: str, delta: int) -> int | None:
    """Adds `delta` (may be negative) to a player's balance and persists it.
    Returns the new balance, or None if it would go below zero."""
    async with BALANCE_LOCK:
        new = PLAYER_BALANCE.get(steam_id, 0) + delta
        if new < 0:
            return None
        PLAYER_BALANCE[steam_id] = new
        if not await asyncio.to_thread(db_update_balance, steam_id, new):
            print(f"[economy] Supabase write failed for {steam_id} (balance {new}).")
        return new

async def transfer_balance(from_sid: str, to_sid: str, amount: int) -> bool:
    """Moves `amount` between two players atomically (under the lock).
    Returns False if the sender can't afford it."""
    async with BALANCE_LOCK:
        if PLAYER_BALANCE.get(from_sid, 0) < amount:
            return False
        PLAYER_BALANCE[from_sid] = PLAYER_BALANCE.get(from_sid, 0) - amount
        PLAYER_BALANCE[to_sid]   = PLAYER_BALANCE.get(to_sid, 0) + amount
        for sid in (from_sid, to_sid):
            if not await asyncio.to_thread(db_update_balance, sid, PLAYER_BALANCE[sid]):
                print(f"[economy] Supabase write failed for {sid}.")
        return True

def compute_weekly_payouts(player_stats: dict) -> dict:
    """player_stats: {display_name: {kills, games, topfrags, adr_sum, ...}}
    Returns {steam_id: {"name", "kills", "topfrags", "best_kpg", "total"}} for
    tracked players only. Ties for best K/G all receive the bonus."""
    name_to_sid = {name: sid for sid, name in TRACKED_PLAYERS.items()}

    eligible = {
        n: s["kills"] / s["games"]
        for n, s in player_stats.items()
        if s["games"] >= MIN_GAMES_FOR_BEST_KPG
    }
    best = max(eligible.values()) if eligible else None

    payouts = {}
    for name, s in player_stats.items():
        sid = name_to_sid.get(name)
        if not sid:
            continue
        kill_pts = s["kills"] * POINTS_PER_KILL
        top_pts  = s["topfrags"] * POINTS_PER_TOPFRAG
        kpg_pts  = POINTS_BEST_KPG if (best is not None and name in eligible
                                       and abs(eligible[name] - best) < 1e-9) else 0
        total = kill_pts + top_pts + kpg_pts
        if total > 0:
            payouts[sid] = {"name": name, "kills": kill_pts, "topfrags": top_pts,
                            "best_kpg": kpg_pts, "total": total}
    return payouts

# Bracket thresholds used for the auto-assigned Discord rank roles. These are
# intentionally coarser than PREMIER_RANKS (which drives the !stats label) —
# this is a role-per-tier system, not a display label.
RANK_BRACKETS = [
    (5000,  "0-5k"),
    (10000, "5-10k"),
    (15000, "10-15k"),
    (20000, "15-20k"),
    (25000, "20-25k"),
    (30000, "25-30k"),
]
RANK_ROLE_NAMES = [b[1] for b in RANK_BRACKETS] + ["30k+", "unranked"]

def get_rank_bracket(premier_rating: int | None) -> str:
    if not premier_rating or premier_rating <= 0:
        return "unranked"
    for threshold, label in RANK_BRACKETS:
        if premier_rating < threshold:
            return label
    return "30k+"


def resolve_vanity_url(vanity_name: str) -> str | None:
    """
    Resolves a Steam custom URL (vanity name) to a Steam64 ID.
    Requires STEAM_API_KEY env var (free key from https://steamcommunity.com/dev/apikey).
    """
    if not STEAM_API_KEY:
        return None
    try:
        res = requests.get(
            "https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/",
            params={"key": STEAM_API_KEY, "vanityurl": vanity_name},
            timeout=10
        )
        if res.status_code == 200:
            data = res.json().get("response", {})
            if data.get("success") == 1:
                return data.get("steamid")
    except Exception as e:
        print(f"[resolve_vanity_url] {e}")
    return None

# Premier rank thresholds (CS2 as of 2025)
PREMIER_RANKS = [
    (5000,  "Silver 1"),   (7000,  "Silver 2"),  (9000,  "Gold 1"),
    (11000, "Gold 2"),     (13000, "Platinum 1"), (15000, "Platinum 2"),
    (17000, "Diamond 1"),  (19000, "Diamond 2"),  (21000, "Elite"),
    (23000, "Supreme"),    (float("inf"), "Global Elite"),
]

def premier_rank_label(rating: int) -> str:
    for threshold, label in PREMIER_RANKS:
        if rating < threshold:
            return label
    return "Global Elite"


def fetch_profile(steam_id: str) -> dict | None:
    try:
        res = requests.get(
            f"{LEETIFY_BASE}/v3/profile",
            headers=LEETIFY_HEADERS,
            params={"steam64_id": steam_id},
            timeout=10
        )
        return res.json() if res.status_code == 200 else None
    except Exception as e:
        print(f"[fetch_profile] {e}")
        return None



def fetch_profile_matches(steam_id: str) -> list | None:
    """
    Calls /v3/profile/matches which returns a list of matches each with a full
    stats array (total_kills, total_deaths, leetify_rating etc. per player).
    """
    try:
        res = requests.get(
            f"{LEETIFY_BASE}/v3/profile/matches",
            headers=LEETIFY_HEADERS,
            params={"steam64_id": steam_id},
            timeout=10
        )
        if res.status_code != 200:
            return None
        # Rush matches are ignored everywhere in the bot.
        return [m for m in res.json() if not is_rush_match(m)]
    except Exception as e:
        print(f"[fetch_profile_matches] {e}")
        return None


async def update_rank_role(guild: discord.Guild, steam_id: str, new_bracket: str):
    """Assigns `new_bracket`'s Discord role to the linked member, removing any other
    bracket role they're holding. No-ops if the player has no linked Discord account
    or isn't a member of this guild."""
    discord_id = PLAYER_DISCORD_IDS.get(steam_id)
    if not discord_id:
        return

    member = guild.get_member(discord_id)
    if not member:
        return

    try:
        for role in list(member.roles):
            if role.name in RANK_ROLE_NAMES and role.name != new_bracket:
                await member.remove_roles(role)

        target_role = discord.utils.get(guild.roles, name=new_bracket)
        if target_role is None:
            target_role = await guild.create_role(name=new_bracket)
        if target_role not in member.roles:
            await member.add_roles(target_role)
    except discord.Forbidden:
        print(f"[update_rank_role] Missing permissions to manage roles for {member} in {guild.name}.")
    except Exception as e:
        print(f"[update_rank_role] {steam_id}: {e}")


def build_profile_embeds(data: dict, steam_id: str, profile_matches: list | None = None) -> list[discord.Embed]:
    """
    Returns a list of embeds:
      [0] — overall stat card
      [1] — last 5 matches
    """
    embeds = []

    name         = sanitize_display_name(data.get("name", steam_id))
    rating       = data.get("rating", {})
    stats        = data.get("stats", {})
    ranks        = data.get("ranks", {})
    recent       = [m for m in data.get("recent_matches", []) if not is_rush_match(m)]

    aim_rtg      = rating.get("aim", 0)
    util_rtg     = rating.get("utility", 0)
    leetify_ct   = rating.get("ct_leetify", 0)
    leetify_t    = rating.get("t_leetify", 0)
    leetify_avg  = (leetify_ct + leetify_t) / 2

    reaction_ms  = stats.get("reaction_time_ms", 0)

    # Premier rank & peak
    premier_rating = ranks.get("premier", 0) or 0
    # Peak: highest premier rating seen across recent matches
    recent_ratings = [m.get("rank", 0) for m in recent
                      if m.get("rank_type") == 11 and isinstance(m.get("rank"), int) and m["rank"] > 0]
    peak_rating = max(recent_ratings) if recent_ratings else premier_rating
    rank_label   = premier_rank_label(premier_rating) if premier_rating else "Unranked"
    peak_label   = premier_rank_label(peak_rating)    if peak_rating    else "—"

    # ── Embed 1: stat card ────────────────────────────────────────
    e1 = discord.Embed(
        title=f"📊 {name}",
        url=f"https://leetify.com/app/profile/{steam_id}",
        color=discord.Color.blurple()
    )

    # Row 1: Aim + Utility
    e1.add_field(name="🎯 Aim Rating",     value=f"**{aim_rtg:.1f}**",  inline=True)
    e1.add_field(name="💣 Utility Rating", value=f"**{util_rtg:.1f}**", inline=True)
    e1.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

    # Row 2: Reaction Time + Leetify Rating (average only)
    e1.add_field(name="⚡ Reaction Time",  value=f"**{reaction_ms:.0f} ms**", inline=True)
    e1.add_field(
        name="📈 Leetify Rating",
        value=f"**{leetify_avg*100:+.1f}**",
        inline=True
    )
    e1.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

    # Row 3: Current rank number + Peak rank number (thousands-separated)
    rank_val = f"{premier_rating:,}" if premier_rating else "Unranked"
    peak_val = f"{peak_rating:,}"    if peak_rating    else "—"
    e1.add_field(name="🏆 Current Rank", value=f"**{rank_val}**", inline=True)
    e1.add_field(name="👑 Highest Rank", value=f"**{peak_val}**", inline=True)
    e1.add_field(name="\u200b", value="\u200b", inline=True)  # spacer

    e1.set_footer(text=f"Steam ID: {steam_id}")
    embeds.append(e1)

    # ── Embed 2: last 5 matches ───────────────────────────────────
    # Use profile_matches (from /v3/profile/matches) which includes full stats per player.
    # Fall back to recent_matches from the profile for map/outcome/score only.
    display_matches = (profile_matches or [])[:5]

    if display_matches:
        e2 = discord.Embed(
            title=f"🕹️ Last {len(display_matches)} Matches — {name}",
            color=discord.Color.dark_blue()
        )

        lines = ["`{:<10} {:>6} {:>3} {:>3} {:>5} {:>6}`".format(
            "MAP", "SCORE", "K", "D", "LTF", "RESULT"
        )]

        for m in display_matches:
            map_short = m.get("map_name", "?").replace("de_", "").replace("cs_", "")[:10]

            # Score: team_scores list [{team_number, score}, ...]
            team_scores = m.get("team_scores", [])
            s2 = next((s.get("score", 0) for s in team_scores if s.get("team_number") == 2), 0)
            s3 = next((s.get("score", 0) for s in team_scores if s.get("team_number") == 3), 0)
            score_str = f"{s2}-{s3}"

            # Player stats from the full stats array
            player_stat = next(
                (s for s in m.get("stats", []) if str(s.get("steam64_id")) == str(steam_id)),
                None
            )
            kills  = player_stat.get("total_kills",  0) or 0 if player_stat else 0
            deaths = player_stat.get("total_deaths", 0) or 0 if player_stat else 0
            ltf    = player_stat.get("leetify_rating", 0) or 0 if player_stat else 0

            # Outcome: compare player's initial_team_number to winning team
            my_team    = player_stat.get("initial_team_number") if player_stat else None
            win_score  = max(s2, s3)
            win_team   = next((s.get("team_number") for s in team_scores if s.get("score") == win_score), None)
            if s2 == s3:
                result = "➖T"
            elif my_team == win_team:
                result = "✅W"
            else:
                result = "❌L"

            lines.append("`{:<10} {:>6} {:>3} {:>3} {:>+5.1f} {:>6}`".format(
                map_short, score_str, kills, deaths, ltf * 100, result
            ))

        e2.description = "\n".join(lines)
        embeds.append(e2)

    return embeds


@bot.listen('on_message')
async def handle_steamid_lookup(message):
    """Auto-detect Steam profile links or Steam64 IDs pasted in any channel."""
    if message.author == bot.user:
        return
    if message.content.startswith("!"):
        return

    steam_id = None
    url_match    = STEAM_PROFILE_RE.search(message.content)
    custom_match = STEAM_CUSTOM_RE.search(message.content)
    id_match     = STEAMID64_RE.search(message.content)

    if url_match:
        steam_id = url_match.group(1)
    elif custom_match:
        vanity = custom_match.group(1)
        steam_id = await asyncio.to_thread(resolve_vanity_url, vanity)
        if not steam_id:
            if not STEAM_API_KEY:
                await message.reply(
                    "⚠️ Custom Steam URLs need a `STEAM_API_KEY` env var to resolve. "
                    "Get a free key at <https://steamcommunity.com/dev/apikey> and add it to your environment."
                )
            else:
                await message.reply(f"❌ Couldn't resolve custom URL `{vanity}` to a Steam64 ID.")
            return
    elif id_match:
        steam_id = id_match.group(1)

    if not steam_id:
        return

    if not LEETIFY_API_KEY:
        await message.reply("⚠️ `LEETIFY_API_KEY` is not set.")
        return

    async with message.channel.typing():
        data = await asyncio.to_thread(fetch_profile, steam_id)
        if not data:
            # NOTE: As of April 2026, Leetify's public API only returns data for
            # registered users. Unregistered players DO have stats tracked internally
            # (visible on leetify.com and third-party sites like csst.at that use the
            # web frontend), but Leetify explicitly restricted their public API to
            # registered accounts for privacy compliance reasons. There is currently
            # no supported bypass via the public API.
            await message.reply(
                f"❌ No Leetify data for `{steam_id}`.\n"
                "ℹ️ This player may not be **registered** on Leetify. "
                "As of April 2026, Leetify's public API only returns stats for registered users — "
                "unregistered players' data is visible on [leetify.com](https://leetify.com) directly "
                "but is not accessible via the API. "
                f"You can check manually: <https://leetify.com/app/profile/{steam_id}>"
            )
            return

        profile_matches = await asyncio.to_thread(fetch_profile_matches, steam_id)
        embeds = build_profile_embeds(data, steam_id, profile_matches)
        await message.reply(embeds=embeds)


# =================================================================
# 8. LEETIFY — MATCH TRACKING (periodic new-match announcer)
# =================================================================

def fetch_full_match(match_id: str) -> dict | None:
    try:
        res = requests.get(
            f"{LEETIFY_BASE}/v2/matches/{match_id}",
            headers=LEETIFY_HEADERS, timeout=10
        )
        return res.json() if res.status_code == 200 else None
    except Exception as e:
        print(f"[fetch_full_match] {e}")
        return None


def is_rush_match(match: dict | None) -> bool:
    """True if this Leetify match record is from CS2's Rush game mode.
    Rush matches show up with data_source "manual_rush" and map_name "rush_001"
    (works for both the v3 profile-match summaries and the v2 full match record).
    We never post or track these."""
    if not match:
        return False
    source = str(match.get("data_source") or "").lower()
    map_name = str(match.get("map_name") or "").lower()
    return "rush" in source or map_name.startswith("rush")


def build_match_embed(match_data: dict) -> discord.Embed | None:
    """
    Scoreboard-style embed with monospace table.
    NAME             K   D   ADR  HS%  RTG
    """
    if is_rush_match(match_data):
        print(f"[build_match_embed] Skipping Rush match {match_data.get('id')}.")
        return None
    try:
        map_name  = match_data.get("map_name", "Unknown").replace("de_", "").title()
        match_id  = match_data.get("id", "")
        game_date = (match_data.get("finished_at") or match_data.get("game_finished_at") or "")[:10]

        team_scores = match_data.get("team_scores", [])
        s_ct = next((s.get("score", 0) for s in team_scores if s.get("team_number") == 3), 0)
        s_t  = next((s.get("score", 0) for s in team_scores if s.get("team_number") == 2), 0)

        # Determine result from tracked players' perspective.
        # Find which team number the majority of tracked players were on.
        def _player_adr(p):
            rounds = p.get("rounds_count", 1) or 1
            return (p.get("total_damage", 0) or 0) / rounds

        # Sorted by kills, then by ADR as a tiebreaker (higher ADR ranks higher).
        all_stats = sorted(
            match_data.get("stats", []),
            key=lambda p: (p.get("total_kills", 0) or 0, _player_adr(p)),
            reverse=True
        )
        tracked_teams = [
            p.get("initial_team_number")
            for p in all_stats
            if str(p.get("steam64_id", "")) in TRACKED_PLAYERS
        ]
        if tracked_teams:
            from collections import Counter
            our_team = Counter(tracked_teams).most_common(1)[0][0]
            our_score  = s_ct if our_team == 3 else s_t
            opp_score  = s_t  if our_team == 3 else s_ct
            if our_score > opp_score:
                result_str = "✅ Win"
            elif our_score < opp_score:
                result_str = "❌ Loss"
            else:
                result_str = "➖ Tie"
        else:
            # No tracked players in this match — just show higher score side
            if s_ct > s_t:
                result_str = "✅ CT Win"
            elif s_t > s_ct:
                result_str = "✅ T Win"
            else:
                result_str = "➖ Tie"

        embed = discord.Embed(
            title=f"🎯  {map_name}  —  CT {s_ct} : {s_t} T",
            description=(
                f"{result_str}  ·  📅 {game_date}  ·  "
                f"[View on Leetify](https://leetify.com/app/match-details/{match_id})"
            ),
            color=discord.Color.gold()
        )

        ct_players = [p for p in all_stats if p.get("initial_team_number") == 3]
        t_players  = [p for p in all_stats if p.get("initial_team_number") != 3]

        def format_side(players):
            lines = ["`{:<16} {:>3} {:>3} {:>5} {:>4} {:>8}`".format(
                "NAME", "K", "D", "ADR", "HS%", "RATING"
            )]
            for p in players:
                sid        = str(p.get("steam64_id", ""))
                is_tracked = sid in TRACKED_PLAYERS
                name       = sanitize_display_name(TRACKED_PLAYERS.get(sid) or p.get("name") or sid)
                # pad_visual accounts for the ⭐ marker and any wide (CJK) characters
                # rendering ~2 columns wide, so the stats always start at column 17
                # regardless of what the name looks like.
                display = pad_visual(name + ("⭐" if is_tracked else ""), 16)
                k      = p.get("total_kills", 0)
                d      = p.get("total_deaths", 0)
                damage = p.get("total_damage", 0)
                rounds = p.get("rounds_count", 1)
                adr    = round(damage / rounds, 1) if rounds else 0
                rating = p.get("leetify_rating", None)
                hs_pct = round((p.get("total_hs_kills", 0) / k * 100)) if k else 0
                rtg    = f"{rating * 100:.1f}" if rating is not None else "—"
                lines.append("`{} {:>3} {:>3} {:>5} {:>3}% {:>8}`".format(
                    display, k, d, adr, hs_pct, rtg
                ))
            return "\n".join(lines)

        if ct_players:
            embed.add_field(name="🔵  CT Side", value=format_side(ct_players), inline=False)
        if t_players:
            embed.add_field(name="🟡  T Side",  value=format_side(t_players),  inline=False)

        return embed
    except Exception as e:
        print(f"[build_match_embed] {e}")
        return None


async def match_already_posted(channel: discord.TextChannel, match_id: str, lookback_days: int = 30) -> bool:
    """
    Checks whether a scoreboard for this match has already been posted in `channel`,
    by scanning recent bot messages for an embed containing this match's Leetify link.
    Used to stop the same match from being posted twice — whether that's an accidental
    double automatic post, or someone running !getmatch on a match that's already up.
    """
    match_link = f"leetify.com/app/match-details/{match_id}"
    try:
        after = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        async for message in channel.history(after=after, limit=1000, oldest_first=False):
            if message.author != bot.user:
                continue
            for embed in message.embeds:
                if embed.description and match_link in embed.description:
                    return True
    except Exception as e:
        print(f"[match_already_posted] {e}")
    return False


@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY or not TRACKED_PLAYERS:
        return

    seen_this_tick = set()

    for steam_id in list(TRACKED_PLAYERS.keys()):
        try:
            res = await asyncio.to_thread(
                requests.get,
                f"{LEETIFY_BASE}/v3/profile/matches",
                headers=LEETIFY_HEADERS,
                params={"steam64_id": steam_id},
                timeout=10
            )
            if res.status_code != 200:
                continue

            matches = [m for m in res.json() if not is_rush_match(m)]
            if not matches:
                continue

            latest    = matches[0]
            latest_id = latest.get("id")

            if steam_id not in last_seen_matches:
                last_seen_matches[steam_id] = latest_id
            elif latest_id != last_seen_matches[steam_id]:
                last_seen_matches[steam_id] = latest_id
                if latest_id not in seen_this_tick:
                    seen_this_tick.add(latest_id)
                    match_data = await asyncio.to_thread(fetch_full_match, latest_id)
                    if match_data:
                        embed = build_match_embed(match_data)
                        if embed:
                            for guild in bot.guilds:
                                channel = discord.utils.get(guild.text_channels, name=LEETIFY_CHANNEL_NAME)
                                if channel:
                                    if await match_already_posted(channel, latest_id):
                                        print(f"[check_leetify_stats] Skipping duplicate post for match {latest_id}.")
                                        continue
                                    await channel.send(embed=embed)

                # A new match landed — re-check the player's current rating and
                # update their rank-bracket Discord role if it moved.
                if steam_id in PLAYER_DISCORD_IDS:
                    profile = await asyncio.to_thread(fetch_profile, steam_id)
                    new_rating  = (profile or {}).get("ranks", {}).get("premier", 0)
                    new_bracket = get_rank_bracket(new_rating)
                    if PLAYER_RANK_BRACKET.get(steam_id) != new_bracket:
                        PLAYER_RANK_BRACKET[steam_id] = new_bracket
                        await asyncio.to_thread(db_update_rank_bracket, steam_id, new_bracket)
                        for guild in bot.guilds:
                            await update_rank_role(guild, steam_id, new_bracket)

        except Exception as e:
            print(f"[check_leetify_stats] {steam_id}: {e}")


MAX_CATCHUP_MATCHES_PER_PLAYER = 15

def _parse_match_timestamp(ts: str):
    """Parses a Leetify match timestamp (e.g. '2024-01-15T10:30:00Z') into an
    aware UTC datetime. Returns None if it can't be parsed."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None

def catchup_week_start() -> datetime:
    """Start of the current (in-progress) week — most recent Monday 00:00 UTC.
    Used to limit !catchup to this week's matches only."""
    now = datetime.now(timezone.utc)
    days_since_monday = now.weekday()  # Monday=0, Sunday=6
    return (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)

async def catch_up_missed_matches(
    guild: discord.Guild,
    max_per_player: int = MAX_CATCHUP_MATCHES_PER_PLAYER,
    since=None,
) -> int:
    """
    Scans each tracked player's recent Leetify matches (this includes Faceit games,
    since Leetify syncs those in automatically) and posts any that are missing from
    #leetify — e.g. matches that happened while the bot was offline, or matches that
    never got auto-announced for some other reason.

    Dedupes match IDs across players (since one match usually involves several tracked
    players), checks each against channel history via match_already_posted(), and posts
    anything missing oldest-first so the channel stays in chronological order.

    Also rebaselines last_seen_matches to each player's current latest match, so the
    periodic check_leetify_stats loop doesn't try to re-announce something we just
    caught up on. This rebaseline always uses each player's true latest match,
    regardless of `since`.

    `since`: if provided (an aware UTC datetime), only matches whose full match
    record finished at or after this time are posted — used by !catchup to only
    fetch matches from the current week, leaving older missing matches alone.
    Checked against the full match data (fetch_full_match), not the summary
    list, since the summary list's date can be unreliable for newly-added
    players (see comment below).

    Returns the number of matches posted.
    """
    leetify_channel = discord.utils.get(guild.text_channels, name=LEETIFY_CHANNEL_NAME)
    if not leetify_channel or not TRACKED_PLAYERS or not LEETIFY_API_KEY:
        return 0

    candidates = {}          # match_id -> finished_at timestamp (for sort order)
    latest_per_player = {}   # steam_id -> most recent match_id seen this run

    for steam_id in list(TRACKED_PLAYERS.keys()):
        try:
            matches = await asyncio.to_thread(fetch_profile_matches, steam_id)
        except Exception as e:
            print(f"[catch_up_missed_matches] fetch {steam_id}: {e}")
            continue
        if not matches:
            continue

        latest_per_player[steam_id] = matches[0].get("id")
        for m in matches[:max_per_player]:
            mid = m.get("id")
            if not mid or is_rush_match(m):
                continue
            # NOTE: we deliberately do NOT date-filter using this summary row's
            # finished_at/game_finished_at here. For newly-added players whose
            # whole history gets bulk-imported at once, this field can reflect
            # when Leetify synced the match rather than when it was actually
            # played, which would let stale matches slip through a "this week"
            # filter. The real date filtering (when `since` is given) happens
            # below against the full match data instead, right before posting.
            candidates[mid] = m.get("finished_at") or m.get("game_finished_at") or ""

    if not candidates:
        return 0

    posted = 0
    for mid in sorted(candidates, key=lambda i: candidates[i]):
        try:
            if await match_already_posted(leetify_channel, mid):
                continue
            match_data = await asyncio.to_thread(fetch_full_match, mid)
            if not match_data:
                continue
            if since is not None:
                # Authoritative check against the full match record — the same
                # finished_at field the embed itself displays — instead of the
                # summary list's (unreliable) date.
                match_ts = match_data.get("finished_at") or match_data.get("game_finished_at") or ""
                match_dt = _parse_match_timestamp(match_ts)
                if match_dt is None or match_dt < since:
                    print(f"[catch_up_missed_matches] Skipping {mid} — dated {match_ts or 'unknown'}, before this week ({since}).")
                    continue
            embed = build_match_embed(match_data)
            if not embed:
                continue
            await leetify_channel.send(embed=embed)
            posted += 1
        except Exception as e:
            print(f"[catch_up_missed_matches] {mid}: {e}")

    # Rebaseline so the periodic checker treats these as already handled.
    for steam_id, mid in latest_per_player.items():
        if mid:
            last_seen_matches[steam_id] = mid

    return posted


async def sync_rank_roles(guild: discord.Guild):
    """
    Re-syncs each Discord-linked tracked player's rank-bracket role to match
    their current Leetify rating. Only touches players whose stored bracket
    doesn't match a role they currently hold (or who don't have a stored
    bracket yet, in which case it's fetched fresh).

    Run on bot startup, by the nightly midnight catch-up, and whenever
    !catchup is run.
    """
    for steam_id, discord_id in list(PLAYER_DISCORD_IDS.items()):
        try:
            member = guild.get_member(discord_id)
            if not member:
                continue
            bracket = PLAYER_RANK_BRACKET.get(steam_id)
            if not bracket:
                profile = await asyncio.to_thread(fetch_profile, steam_id)
                new_rating = (profile or {}).get("ranks", {}).get("premier", 0)
                bracket = get_rank_bracket(new_rating)
                PLAYER_RANK_BRACKET[steam_id] = bracket
                await asyncio.to_thread(db_update_rank_bracket, steam_id, bracket)
            current_role_names = {r.name for r in member.roles}
            if bracket not in current_role_names:
                await update_rank_role(guild, steam_id, bracket)
        except Exception as e:
            print(f"[sync_rank_roles] {steam_id}: {e}")


async def run_automatic_catchup(guild: discord.Guild):
    """
    Full automatic catch-up flow for a guild: posts any matches missing from
    #leetify that finished this week, then re-syncs rank roles. Used on startup
    and by the nightly midnight catch-up task.

    Restricted to this week for the same reason as !catchup: a newly-tracked
    player's whole match history is visible via the API, and without this
    filter a restart (which happens on every redeploy) would flood #leetify
    with backlog matches going back months.
    """
    try:
        posted = await catch_up_missed_matches(guild, since=catchup_week_start())
        if posted:
            print(f"[catch_up_missed_matches] Posted {posted} missed match(es) in {guild.name}.")
    except Exception as e:
        print(f"[catch_up_missed_matches] {guild.name}: {e}")
    try:
        await sync_rank_roles(guild)
    except Exception as e:
        print(f"[sync_rank_roles] {guild.name}: {e}")


# =================================================================
# 9. WEEKLY RECAP (Sunday 21:00 UTC)
# =================================================================

def parse_match_embed(embed: discord.Embed) -> dict | None:
    """
    Extract structured data from a match embed posted by the bot.
    Returns a dict with: map, score_ct, score_t, players [{name, k, d, rating}],
    or None if the embed doesn't look like a match embed.
    """
    try:
        # Match embeds have a title like "🎯  Mirage  —  CT 13 : 8 T"
        title = embed.title or ""
        score_match = re.search(r'CT\s+(\d+)\s*:\s*(\d+)\s*T', title)
        if not score_match:
            return None
        map_name = re.sub(r'🎯\s*', '', title.split('—')[0]).strip()
        s_ct = int(score_match.group(1))
        s_t  = int(score_match.group(2))

        players = []
        for field in embed.fields:
            # Determine which team this field belongs to from the field name
            field_name = field.name or ""
            if "CT" in field_name:
                team_label = "CT"
            elif "T Side" in field_name or "🟡" in field_name:
                team_label = "T"
            else:
                team_label = "?"

            # Each field value is a monospace table; first line is the header row
            lines = (field.value or "").split("\n")
            for line in lines[1:]:  # skip header
                # Strip backticks and parse: NAME  K  D  ADR  HS%  RATING
                clean = line.strip().strip("`")
                if not clean:
                    continue
                parts = clean.split()
                if len(parts) < 4:
                    continue
                # Name is everything up to the last 5 tokens (K D ADR HS% RTG)
                try:
                    rtg_str = parts[-1]
                    rating  = float(rtg_str) if rtg_str != "—" else None
                    adr_str = parts[-3]
                    adr     = float(adr_str) if adr_str != "—" else 0.0
                    deaths  = int(parts[-4])
                    kills   = int(parts[-5])
                    name    = " ".join(parts[:-5]).rstrip("⭐").strip()
                    players.append({
                        "name":   name,
                        "kills":  kills,
                        "deaths": deaths,
                        "adr":    adr,
                        "rating": rating,   # already ×100 (displayed value)
                        "team":   team_label,
                    })
                except (ValueError, IndexError):
                    continue

        # Wingman is CS2's 2v2 mode (max 4 players total on the scoreboard, first
        # to 9 rounds instead of 13). Its K/D/ADR/rating numbers aren't comparable
        # to 5v5 games, so we flag it here and skip it in the weekly recap below.
        is_wingman = len(players) > 0 and len(players) <= 4

        # Rush (3v3 mode) recaps posted before we started filtering them out.
        is_rush = map_name.lower().startswith("rush")

        return {
            "is_rush":    is_rush,
            "map":        map_name,
            "score_ct":   s_ct,
            "score_t":    s_t,
            "players":    players,
            "is_wingman": is_wingman,
        }
    except Exception as e:
        print(f"[parse_match_embed] {e}")
        return None


def _pad(text: str, width: int) -> str:
    """Left-align/truncate text to a fixed *visual* width for monospace tables.
    Delegates to pad_visual so CJK names pulled from parsed embeds don't drift
    out of alignment the same way the scoreboard used to."""
    return pad_visual(text, width, ellipsis=True)


def _rpad(text: str, width: int) -> str:
    """Right-align text to a fixed visual width for monospace tables."""
    return rpad_visual(text, width)


def _join_capped(lines: list[str], limit: int = 1024) -> str:
    """Join table lines for an embed field, staying under Discord's per-field
    character limit (1024). If the full table would overflow, keep the
    header plus as many rows as fit and append a '+N more' note instead of
    letting the overflow raise an HTTPException on send (which would silently
    abort the rest of the weekly recap messages)."""
    joined = "\n".join(lines)
    if len(joined) <= limit:
        return joined

    header, *rows = lines
    kept = [header]
    # Reserve room for a trailing "+N more" note.
    note_reserve = 20
    total = len(header)
    for i, row in enumerate(rows):
        if total + len(row) + 1 + note_reserve > limit:
            remaining = len(rows) - i
            kept.append(f"… +{remaining} more")
            break
        kept.append(row)
        total += len(row) + 1
    return "\n".join(kept)


async def build_weekly_recap(channel: discord.TextChannel, weeks_ago: int = 0,
                             stats_out: dict | None = None) -> list[discord.Embed] | None:
    """
    Scrapes messages in `channel` for one week's worth of bot match embeds, and
    returns a list of Weekly Recap embeds: [maps_embed, kills_embed, frags_embed].

    weeks_ago=0  -> the current (in-progress) week: most recent Monday 00:00 UTC
                    through right now.
    weeks_ago=1  -> last week: the Monday 00:00 UTC before that, through the
                    following Sunday 23:59:59 UTC (i.e. the midnight that starts
                    the next Monday).
    weeks_ago=2,3,... -> further back, one full week at a time.
    """
    now = datetime.now(timezone.utc)
    # Start of the CURRENT week = most recent Monday at 00:00 UTC
    days_since_monday = now.weekday()  # Monday=0, Sunday=6
    this_week_start = (now - timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Shift back by however many full weeks we want to look at.
    week_start = this_week_start - timedelta(weeks=weeks_ago)

    if weeks_ago == 0:
        # Current week is still in progress — end the range "now" like before.
        week_end = now
    else:
        # A completed past week: Monday 00:00 UTC through the following
        # Monday 00:00 UTC (exclusive), i.e. up to Sunday midnight.
        week_end = week_start + timedelta(days=7)

    matches = []  # list of parsed match dicts

    history_kwargs = {"after": week_start, "limit": 2000, "oldest_first": False}
    if weeks_ago != 0:
        history_kwargs["before"] = week_end

    async for message in channel.history(**history_kwargs):
        if message.author != bot.user:
            continue
        for embed in message.embeds:
            parsed = parse_match_embed(embed)
            if parsed and not parsed.get("is_wingman") and not parsed.get("is_rush"):
                matches.append(parsed)

    if not matches:
        return None

    # ── Aggregate ───────────────────────────────────────────────────
    total   = len(matches)
    wins    = 0
    losses  = 0
    ties    = 0

    # Map stats: count / wins / losses / ties per map
    map_stats: dict[str, dict] = {}

    # Per-player: kills, deaths, games, topfrag count, bottomfrag count
    # topfrag  = most kills on their team that game
    # bottomfrag = fewest kills on their team that game
    player_stats: dict[str, dict] = {}

    for m in matches:
        s_ct, s_t = m["score_ct"], m["score_t"]

        # A team wins if they reach 13 first OR the other team surrenders (score can be
        # anything like 10-2). We determine W/L by which score is higher — the embed
        # title is always "CT X : Y T" from the perspective of the match, so whichever
        # side has the higher score won. We track the match result as a single event
        # (one win or one loss for the group) using whichever side scored more.
        map_name = m["map"]
        if map_name not in map_stats:
            map_stats[map_name] = {"count": 0, "wins": 0, "losses": 0, "ties": 0}
        map_stats[map_name]["count"] += 1

        if s_ct == s_t:
            ties += 1
            map_stats[map_name]["ties"] += 1
        elif s_ct > s_t:
            wins += 1
            map_stats[map_name]["wins"] += 1
        else:
            losses += 1
            map_stats[map_name]["losses"] += 1

        players = m["players"]
        if not players:
            continue

        # Only count tracked players for leaderboard. If none of THIS match's
        # players are currently tracked (e.g. everyone in it was removed from
        # tracking, or it was a solo/duo game with people outside the group),
        # skip the match for leaderboard purposes rather than falling back to
        # counting every player in it — that fallback used to dump entire
        # enemy teams of strangers into the kill/frag leaderboards, which
        # could blow past Discord's 1024-char embed field limit and cause
        # the kills/frags messages to silently fail to send.
        tracked_names = set(TRACKED_PLAYERS.values())
        tracked_in_match = [
            p for p in players
            if p["name"].rstrip("⭐").strip() in tracked_names
        ]
        if not tracked_in_match:
            continue

        # Top/bottom frag is determined per team by kills (the classic "top frag"
        # meaning — most kills on your side that game). The embed already has CT
        # players and T players in separate fields; we reconstruct that here using
        # the "team" field added in parse_match_embed.
        for team_label in ("CT", "T"):
            # Full team (everyone on this side, tracked or not) — used to find the
            # actual highest/lowest kills on the team so we don't falsely award
            # topfrag/bottomfrag to a tracked player who merely has the best/worst
            # kills among tracked players.
            full_team    = [p for p in players if p.get("team") == team_label]
            team_players = [p for p in tracked_in_match if p.get("team") == team_label]
            if not team_players or not full_team:
                continue

            # Most/fewest kills across the ENTIRE team (all 5 players)
            full_kills = [p.get("kills") for p in full_team if p.get("kills") is not None]
            if not full_kills:
                continue
            most_kills   = max(full_kills)
            fewest_kills = min(full_kills)

            for p in team_players:
                n = p["name"].rstrip("⭐").strip()
                if n not in player_stats:
                    player_stats[n] = {"kills": 0, "deaths": 0, "games": 0, "topfrags": 0, "bottomfrags": 0, "adr_sum": 0.0}
                player_stats[n]["kills"]   += p["kills"]
                player_stats[n]["deaths"]  += p["deaths"]
                player_stats[n]["games"]   += 1
                player_stats[n]["adr_sum"] += p.get("adr", 0.0)
                k = p.get("kills")
                if k is not None and k == most_kills:
                    player_stats[n]["topfrags"] += 1
                if k is not None and k == fewest_kills:
                    player_stats[n]["bottomfrags"] += 1

    # Hand the aggregated per-player stats back to the caller (used for payouts).
    if stats_out is not None:
        stats_out.update(player_stats)

    # ── Build embeds ────────────────────────────────────────────────
    # For a completed week, week_end is the *next* Monday 00:00 UTC — display
    # the Sunday date it actually ended on instead of that following Monday.
    label_end_dt = now if weeks_ago == 0 else (week_end - timedelta(days=1))
    week_label = f"{week_start.strftime('%b %d')} – {label_end_dt.strftime('%b %d, %Y')}"

    if weeks_ago == 0:
        when_text = "this week"
    elif weeks_ago == 1:
        when_text = "last week"
    else:
        when_text = f"{weeks_ago} weeks ago"
    footer_text = f"Based on {total} match{'es' if total != 1 else ''} tracked {when_text}"

    # ── Message 1: Maps ───────────────────────────────────────────
    maps_embed = discord.Embed(
        title=f"📅 Weekly Recap — {week_label}",
        color=discord.Color.og_blurple()
    )
    maps_embed.add_field(
        name="🎮 Games Played",
        value=f"**{total}** — ✅ {wins}W / ➖ {ties}T / ❌ {losses}L",
        inline=False
    )

    if map_stats:
        sorted_maps = sorted(map_stats.items(), key=lambda x: x[1]["count"], reverse=True)

        header = f"`{_pad('MAP', 14)}{_rpad('PLAYED', 7)}{_rpad('W-L-T', 9)}{_rpad('WIN%', 6)}`"
        map_lines = [header]
        for name, s in sorted_maps:
            decisive = s["wins"] + s["losses"]
            win_pct  = f"{round(s['wins'] / decisive * 100)}%" if decisive > 0 else "-"
            record   = f"{s['wins']}-{s['losses']}-{s['ties']}"
            map_lines.append(
                f"`{_pad(name, 14)}{_rpad(s['count'], 7)}{_rpad(record, 9)}{_rpad(win_pct, 6)}`"
            )

        maps_embed.add_field(name="🗺️ Maps Played", value=_join_capped(map_lines), inline=False)

    maps_embed.set_footer(text=footer_text)

    embeds = [maps_embed]

    if player_stats:
        # ── Message 2: Kill leaderboard ────────────────────────────
        kills_embed = discord.Embed(
            title="🔫 Weekly Kill Leaderboard",
            color=discord.Color.red()
        )

        # Sorted by kills, then by average ADR as a tiebreaker (higher ADR wins ties)
        sorted_by_kills = sorted(
            player_stats.items(),
            key=lambda x: (x[1]["kills"], x[1]["adr_sum"] / x[1]["games"] if x[1]["games"] else 0),
            reverse=True
        )

        header = f"`{_pad('PLAYER', 16)}{_rpad('KILLS', 6)}{_rpad('GAMES', 6)}{_rpad('K/G', 6)}`"
        lb_lines = [header]
        for name, s in sorted_by_kills:
            kpg = f"{s['kills'] / s['games']:.1f}" if s["games"] > 0 else "0.0"
            lb_lines.append(
                f"`{_pad(name, 16)}{_rpad(s['kills'], 6)}{_rpad(s['games'], 6)}{_rpad(kpg, 6)}`"
            )

        kills_embed.add_field(name="Kills — most to least", value=_join_capped(lb_lines), inline=False)
        kills_embed.set_footer(text=footer_text)
        embeds.append(kills_embed)

        # ── Message 3: Top / bottom frag leaderboard ───────────────
        frags_embed = discord.Embed(
            title="🎯 Top & Bottom Frags",
            color=discord.Color.gold()
        )

        def _avg_adr(s):
            return s["adr_sum"] / s["games"] if s["games"] else 0

        # Ties broken by average ADR — higher ADR ranks higher in both lists.
        sorted_top = sorted(
            (p for p in player_stats.items() if p[1]["topfrags"] > 0),
            key=lambda x: (x[1]["topfrags"], _avg_adr(x[1])),
            reverse=True
        )
        sorted_bottom = sorted(
            (p for p in player_stats.items() if p[1]["bottomfrags"] > 0),
            key=lambda x: (x[1]["bottomfrags"], _avg_adr(x[1])),
            reverse=True
        )

        if sorted_top:
            header = f"`{_pad('PLAYER', 16)}{_rpad('×', 4)}{_rpad('GAMES', 6)}{_rpad('%', 5)}`"
            top_lines = [header]
            for name, s in sorted_top:
                pct = f"{round(s['topfrags'] / s['games'] * 100)}%" if s["games"] > 0 else "-"
                top_lines.append(
                    f"`{_pad(name, 16)}{_rpad(s['topfrags'], 4)}{_rpad(s['games'], 6)}{_rpad(pct, 5)}`"
                )
            frags_embed.add_field(name="👑 Most Top Frags", value=_join_capped(top_lines), inline=False)

        if sorted_bottom:
            header = f"`{_pad('PLAYER', 16)}{_rpad('×', 4)}{_rpad('GAMES', 6)}{_rpad('%', 5)}`"
            bottom_lines = [header]
            for name, s in sorted_bottom:
                pct = f"{round(s['bottomfrags'] / s['games'] * 100)}%" if s["games"] > 0 else "-"
                bottom_lines.append(
                    f"`{_pad(name, 16)}{_rpad(s['bottomfrags'], 4)}{_rpad(s['games'], 6)}{_rpad(pct, 5)}`"
                )
            frags_embed.add_field(name="💀 Most Bottom Frags", value=_join_capped(bottom_lines), inline=False)

        frags_embed.set_footer(text=footer_text)
        embeds.append(frags_embed)

    return embeds


def build_payout_embed(payouts: dict) -> discord.Embed:
    """The '💰 Weekly Payouts' embed shown after a weekly recap."""
    pay_embed = discord.Embed(title="💰 Weekly Payouts", color=discord.Color.green())
    pay_lines = [f"`{_pad('PLAYER', 16)}{_rpad('KILLS', 6)}{_rpad('TOP', 5)}{_rpad('K/G', 5)}{_rpad('TOTAL', 7)}`"]
    for sid, p in sorted(payouts.items(), key=lambda x: x[1]["total"], reverse=True):
        pay_lines.append(
            f"`{_pad(p['name'], 16)}{_rpad(p['kills'], 6)}{_rpad(p['topfrags'], 5)}"
            f"{_rpad(p['best_kpg'], 5)}{_rpad(p['total'], 7)}`"
        )
    pay_embed.add_field(name="Points earned", value=_join_capped(pay_lines), inline=False)
    pay_embed.set_footer(text="1/kill · 5/top frag · 50 for best K/G")
    return pay_embed


@tasks.loop(minutes=1)
async def weekly_recap_task():
    """Posts weekly recap every Sunday at 22:00 UTC (midnight Belgian time)."""
    now = datetime.now(timezone.utc)
    if now.weekday() != 6:          # 6 = Sunday
        return
    if not (now.hour == 22 and now.minute == 0):
        return

    for guild in bot.guilds:
        leetify_channel = discord.utils.get(guild.text_channels, name=LEETIFY_CHANNEL_NAME)
        weekly_channel  = discord.utils.get(guild.text_channels, name=WEEKLY_CHANNEL_NAME)
        if not leetify_channel or not weekly_channel:
            continue
        try:
            # Matches are scraped from #leetify (where scoreboards get posted),
            # but the resulting recap embeds are posted to #weekly.
            stats = {}
            embeds = await build_weekly_recap(leetify_channel, stats_out=stats)
            if embeds:
                # Pay out this week's points and add a payout embed to the recap.
                payouts = compute_weekly_payouts(stats)
                if payouts:
                    for sid, p in payouts.items():
                        await adjust_balance(sid, p["total"])
                    embeds.append(build_payout_embed(payouts))
                for i, e in enumerate(embeds):
                    try:
                        await weekly_channel.send(embed=e)
                    except discord.HTTPException as send_err:
                        print(f"[weekly_recap_task] {guild.name}: failed to send embed {i+1}/{len(embeds)}: {send_err}")
                        await weekly_channel.send(
                            f"⚠️ Weekly recap: one of the recap messages ({i+1}/{len(embeds)}) failed to send — {send_err}"
                        )
                        break
            else:
                await weekly_channel.send("📅 Weekly recap: no matches tracked this week.")
        except Exception as e:
            print(f"[weekly_recap_task] {guild.name}: {e}")


# =================================================================
# 10. COMMANDS
# =================================================================

@bot.command(name="addplayer")
async def add_player(ctx, steam_id: str, discord_user: discord.Member = None, *, display_name: str = None):
    """!addplayer <steam64id> [@discord_user] <display name>
    The @discord_user mention is optional — omit it to track a Steam-only player
    with no rank role assignment. Use !linkdiscord later to add it."""
    if not STEAMID64_RE.fullmatch(steam_id):
        await ctx.send("❌ Invalid Steam64 ID (17 digits starting with 7656119...).")
        return
    if not display_name:
        await ctx.send("❌ Usage: `!addplayer <steam64id> [@discord_user] <display name>`")
        return
    TRACKED_PLAYERS[steam_id] = display_name
    if discord_user is not None:
        PLAYER_DISCORD_IDS[steam_id] = discord_user.id
    ok = await asyncio.to_thread(
        db_add_player, steam_id, display_name,
        discord_user.id if discord_user is not None else None
    )
    if ok:
        linked_note = f" (linked to {discord_user.mention})" if discord_user is not None else ""
        await ctx.send(f"✅ Now tracking **{display_name}** (`{steam_id}`){linked_note}.")
    else:
        await ctx.send("⚠️ Saved in memory but Supabase write failed — check credentials.")

@bot.command(name="linkdiscord")
async def link_discord(ctx, steam_id: str, discord_user: discord.Member):
    """!linkdiscord <steam64id> <@discord_user> — link a Discord account to an
    already-tracked Steam ID, without removing/re-adding the player."""
    if steam_id not in TRACKED_PLAYERS:
        await ctx.send("That Steam ID isn't being tracked. Use `!addplayer` first.")
        return
    PLAYER_DISCORD_IDS[steam_id] = discord_user.id
    ok = await asyncio.to_thread(db_link_discord, steam_id, discord_user.id)
    if ok:
        await ctx.send(f"🔗 Linked **{TRACKED_PLAYERS[steam_id]}** (`{steam_id}`) to {discord_user.mention}.")
    else:
        await ctx.send("⚠️ Linked in memory but Supabase write failed — check credentials.")
    # Immediately assign a rank role so the person doesn't have to wait for a match.
    profile = await asyncio.to_thread(fetch_profile, steam_id)
    new_rating  = (profile or {}).get("ranks", {}).get("premier", 0)
    new_bracket = get_rank_bracket(new_rating)
    PLAYER_RANK_BRACKET[steam_id] = new_bracket
    await asyncio.to_thread(db_update_rank_bracket, steam_id, new_bracket)
    if ctx.guild:
        await update_rank_role(ctx.guild, steam_id, new_bracket)

@bot.command(name="removeplayer")
async def remove_player(ctx, steam_id: str):
    """!removeplayer <steam64id>"""
    if steam_id not in TRACKED_PLAYERS:
        await ctx.send("That Steam ID isn't being tracked.")
        return
    name = TRACKED_PLAYERS.pop(steam_id)
    discord_id = PLAYER_DISCORD_IDS.pop(steam_id, None)
    PLAYER_RANK_BRACKET.pop(steam_id, None)
    PLAYER_BALANCE.pop(steam_id, None)
    ok = await asyncio.to_thread(db_remove_player, steam_id)

    # Strip whatever rank role they were holding, since they're no longer tracked.
    if discord_id and ctx.guild:
        member = ctx.guild.get_member(discord_id)
        if member:
            try:
                for role in list(member.roles):
                    if role.name in RANK_ROLE_NAMES:
                        await member.remove_roles(role)
            except discord.Forbidden:
                pass
            except Exception as e:
                print(f"[remove_player] role cleanup for {steam_id}: {e}")

    if ok:
        await ctx.send(f"🗑️ Removed **{name}** from tracking.")
    else:
        await ctx.send(f"Removed **{name}** from memory but Supabase delete failed.")

@bot.command(name="balance", aliases=["bal"])
async def balance_command(ctx, member: discord.Member = None):
    """!balance [@user] — show your balance (or someone else's)."""
    target = member or ctx.author
    sid = steam_id_for_discord(target.id)
    if not sid:
        who = "You don't" if target == ctx.author else f"{target.display_name} doesn't"
        await ctx.send(f"❌ {who} have a linked tracked player. Use `!linkdiscord` first.")
        return
    await ctx.send(f"💰 **{target.display_name}** has **{PLAYER_BALANCE.get(sid, 0):,}** points.")

@bot.command(name="transfer")
async def transfer_command(ctx, amount: int, recipient: discord.Member):
    """!transfer <amount> @user — give some of your balance to someone else."""
    if amount <= 0:
        await ctx.send("❌ Amount must be a positive whole number.")
        return
    if recipient.id == ctx.author.id:
        await ctx.send("❌ You can't transfer to yourself.")
        return
    if recipient.bot:
        await ctx.send("❌ You can't transfer to a bot.")
        return
    from_sid = steam_id_for_discord(ctx.author.id)
    to_sid   = steam_id_for_discord(recipient.id)
    if not from_sid:
        await ctx.send("❌ You don't have a linked tracked player, so you have no balance.")
        return
    if not to_sid:
        await ctx.send(f"❌ {recipient.display_name} doesn't have a linked tracked player.")
        return
    if not await transfer_balance(from_sid, to_sid, amount):
        await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(from_sid, 0):,}**.")
        return
    await ctx.send(
        f"✅ Transferred **{amount:,}** to {recipient.mention}. "
        f"Your balance: **{PLAYER_BALANCE[from_sid]:,}**."
    )

@bot.command(name="syncranks")
async def sync_ranks(ctx):
    """!syncranks — re-fetch every linked player's current rank and re-assign their
    Discord role, regardless of whether it changed. Useful for testing and for
    retroactively fixing roles after first enabling this feature."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ `LEETIFY_API_KEY` is not set.")
        return
    if not PLAYER_DISCORD_IDS:
        await ctx.send("No linked Discord accounts yet. Use `!addplayer` with a mention or `!linkdiscord`.")
        return
    async with ctx.typing():
        updated = 0
        for steam_id in list(PLAYER_DISCORD_IDS.keys()):
            profile = await asyncio.to_thread(fetch_profile, steam_id)
            new_rating  = (profile or {}).get("ranks", {}).get("premier", 0)
            new_bracket = get_rank_bracket(new_rating)
            PLAYER_RANK_BRACKET[steam_id] = new_bracket
            await asyncio.to_thread(db_update_rank_bracket, steam_id, new_bracket)
            await update_rank_role(ctx.guild, steam_id, new_bracket)
            updated += 1
    await ctx.send(f"✅ Synced rank roles for {updated} linked player(s).")

@bot.command(name="players")
async def list_players(ctx):
    """!players — list all tracked players"""
    if not TRACKED_PLAYERS:
        await ctx.send("No players tracked yet. Use `!addplayer <steam64id> <name>`.")
        return
    lines = []
    for sid, name in TRACKED_PLAYERS.items():
        bracket = PLAYER_RANK_BRACKET.get(sid)
        bracket_note = f" `[{bracket}]`" if bracket else ""
        lines.append(f"• **{name}** — `{sid}`{bracket_note}")
    await ctx.send("**Tracked players:**\n" + "\n".join(lines))

@bot.command(name="lastmatch")
async def last_match(ctx, steam_id: str):
    """!lastmatch <steam64id> — force-post the most recent match"""
    res = await asyncio.to_thread(
        requests.get,
        f"{LEETIFY_BASE}/v3/profile/matches",
        headers=LEETIFY_HEADERS,
        params={"steam64_id": steam_id},
        timeout=10
    )
    if res.status_code != 200:
        await ctx.send(f"❌ Leetify returned `{res.status_code}`.")
        return
    matches = [m for m in res.json() if not is_rush_match(m)]
    if not matches:
        await ctx.send("No matches found.")
        return
    match_id   = matches[0].get("id")
    match_data = await asyncio.to_thread(fetch_full_match, match_id)
    if not match_data:
        await ctx.send("Could not fetch full match data.")
        return
    if is_rush_match(match_data):
        await ctx.send("⚠️ That's a Rush match — Rush games are ignored.")
        return
    embed = build_match_embed(match_data)
    if embed:
        await ctx.send(embed=embed)
    else:
        await ctx.send("Could not parse match data.")

@bot.command(name="getmatch")
async def get_match(ctx, match_id: str):
    """!getmatch <matchid or leetify url> — force-post a specific match (e.g. one that
    didn't get auto-posted, like a match uploaded right after a faceit game) to #leetify"""
    leetify_channel = discord.utils.get(ctx.guild.text_channels, name=LEETIFY_CHANNEL_NAME)
    if not leetify_channel:
        await ctx.send("❌ No `#leetify` channel found in this server.")
        return

    # Allow pasting a full https://leetify.com/app/match-details/<id> link too
    url_match = re.search(r'match-details/([a-zA-Z0-9\-]+)', match_id)
    if url_match:
        match_id = url_match.group(1)

    async with ctx.typing():
        if await match_already_posted(leetify_channel, match_id):
            await ctx.send(f"⚠️ That match has already been posted in {leetify_channel.mention} — skipping.")
            return
        match_data = await asyncio.to_thread(fetch_full_match, match_id)
        if not match_data:
            await ctx.send(f"❌ Could not fetch match `{match_id}`. Double check the match ID.")
            return
        if is_rush_match(match_data):
            await ctx.send("⚠️ That's a Rush match — Rush games are ignored.")
            return
        embed = build_match_embed(match_data)
        if not embed:
            await ctx.send("❌ Could not parse match data.")
            return
        await leetify_channel.send(embed=embed)
        if ctx.channel != leetify_channel:
            await ctx.send(f"✅ Match posted in {leetify_channel.mention}.")

@bot.command(name="testgetmatch")
async def test_get_match(ctx, match_id: str):
    """!testgetmatch <matchid or leetify url> — TEST ONLY. Same as !getmatch but
    skips the 'already posted?' check, so you can re-post the same match repeatedly
    while testing embed formatting changes."""
    leetify_channel = discord.utils.get(ctx.guild.text_channels, name=LEETIFY_CHANNEL_NAME)
    if not leetify_channel:
        await ctx.send("❌ No `#leetify` channel found in this server.")
        return

    url_match = re.search(r'match-details/([a-zA-Z0-9\-]+)', match_id)
    if url_match:
        match_id = url_match.group(1)

    async with ctx.typing():
        match_data = await asyncio.to_thread(fetch_full_match, match_id)
        if not match_data:
            await ctx.send(f"❌ Could not fetch match `{match_id}`. Double check the match ID.")
            return
        if is_rush_match(match_data):
            await ctx.send("⚠️ That's a Rush match — Rush games are ignored.")
            return
        embed = build_match_embed(match_data)
        if not embed:
            await ctx.send("❌ Could not parse match data.")
            return
        await leetify_channel.send(embed=embed)
        if ctx.channel != leetify_channel:
            await ctx.send(f"✅ [TEST] Match posted in {leetify_channel.mention} (duplicate check skipped).")

@bot.command(name="stats")
async def stats_command(ctx, steam_id: str):
    """!stats <steam64id> — show profile stat card"""
    if not STEAMID64_RE.fullmatch(steam_id):
        await ctx.send("❌ Invalid Steam64 ID.")
        return
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ `LEETIFY_API_KEY` is not set.")
        return
    async with ctx.typing():
        data = await asyncio.to_thread(fetch_profile, steam_id)
        if not data:
            await ctx.reply(
                f"❌ No Leetify data for `{steam_id}`.\n"
                "ℹ️ This player may not be registered on Leetify — the public API only returns stats for registered users."
            )
            return
        profile_matches = await asyncio.to_thread(fetch_profile_matches, steam_id)
        embeds = build_profile_embeds(data, steam_id, profile_matches)
        await ctx.reply(embeds=embeds)

@bot.command(name="catchup")
async def catchup_command(ctx):
    """!catchup — check every tracked player's matches from this week (including
    Faceit games synced through Leetify) and post any that are missing from
    #leetify, e.g. games that happened while the bot was down. Matches from
    previous weeks are left alone. Also re-syncs everyone's rank roles."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ `LEETIFY_API_KEY` is not set.")
        return
    if not TRACKED_PLAYERS:
        await ctx.send("No players tracked yet. Use `!addplayer <steam64id> <name>`.")
        return
    leetify_channel = discord.utils.get(ctx.guild.text_channels, name=LEETIFY_CHANNEL_NAME)
    if not leetify_channel:
        await ctx.send("❌ No `#leetify` channel found in this server.")
        return
    async with ctx.typing():
        posted = await catch_up_missed_matches(ctx.guild, since=catchup_week_start())
        await sync_rank_roles(ctx.guild)
    if posted:
        await ctx.send(f"✅ Caught up — posted {posted} missing match{'es' if posted != 1 else ''} from this week in {leetify_channel.mention}.")
    else:
        await ctx.send("👍 Nothing missing — all of this week's matches are already posted.")

@bot.command(name="weeklyrecap")
async def force_weekly_recap(ctx, weeks_ago: int = 0):
    """!weeklyrecap [N] — manually trigger the weekly recap, posted in the #weekly channel.
    With no argument, recaps the current (in-progress) week — same as before.
    !weeklyrecap 1 -> last week, !weeklyrecap 2 -> the week before that, !weeklyrecap 3 -> the
    week before that one. Each past week runs Monday 00:00 UTC through the following Sunday
    24:00 UTC (i.e. the midnight that starts the next Monday)."""
    if weeks_ago < 0:
        await ctx.send("❌ Weeks back can't be negative — use `!weeklyrecap`, `!weeklyrecap 1`, `!weeklyrecap 2`, etc.")
        return
    leetify_channel = discord.utils.get(ctx.guild.text_channels, name=LEETIFY_CHANNEL_NAME)
    weekly_channel  = discord.utils.get(ctx.guild.text_channels, name=WEEKLY_CHANNEL_NAME)
    if not leetify_channel:
        await ctx.send("❌ No `#leetify` channel found in this server.")
        return
    if not weekly_channel:
        await ctx.send("❌ No `#weekly` channel found in this server.")
        return
    if weeks_ago == 0:
        when_text = "this week"
    elif weeks_ago == 1:
        when_text = "last week"
    else:
        when_text = f"{weeks_ago} weeks ago"
    async with ctx.typing():
        embeds = await build_weekly_recap(leetify_channel, weeks_ago=weeks_ago)
        if embeds:
            sent = 0
            for e in embeds:
                try:
                    await weekly_channel.send(embed=e)
                    sent += 1
                except discord.HTTPException as send_err:
                    await ctx.send(
                        f"⚠️ Sent {sent}/{len(embeds)} recap message(s), then one failed to send — `{send_err}`"
                    )
                    return
            if ctx.channel != weekly_channel:
                await ctx.send(f"✅ Weekly recap ({when_text}) posted in {weekly_channel.mention}.")
        else:
            await ctx.send(f"📅 No matches found in `#leetify` for {when_text}.")


# =================================================================
# 10.5 GAMES — COINFLIP
# =================================================================
# Flow: !cf <amount> posts a box. The creator picks a side with a reaction
# (👑 heads / 🦅 tails). The box then offers: another player clicking the
# opposite side's emoji to take the bet, or the creator clicking 🤖 to flip
# against the bot, or ❌ to cancel. Stakes are only checked/taken at flip
# time (no escrow); unaccepted boxes expire after COINFLIP_TIMEOUT seconds.
# Vs a player: winner takes the pot (loser pays `amount` to the winner).
# Vs the bot: win = +amount (minted), lose = -amount.

COINFLIP_TIMEOUT = 120
CF_HEADS, CF_TAILS, CF_BOT, CF_CANCEL = "👑", "🦅", "🤖", "❌"
CF_SIDE_EMOJI = {"heads": CF_HEADS, "tails": CF_TAILS}
COINFLIPS = {}   # message_id -> game dict

def _cf_embed(game: dict, status: str, color=None) -> discord.Embed:
    embed = discord.Embed(
        title=f"🪙 Coinflip — {game['amount']:,} points",
        description=status,
        color=color or discord.Color.gold()
    )
    return embed

def _cf_finish(message_id: int):
    game = COINFLIPS.pop(message_id, None)
    if game:
        task = game.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()

async def _cf_expire(message_id: int):
    await asyncio.sleep(COINFLIP_TIMEOUT)
    game = COINFLIPS.get(message_id)
    if not game or game["state"] not in ("pick", "open"):
        return
    game["state"] = "done"
    COINFLIPS.pop(message_id, None)
    try:
        await game["message"].edit(embed=_cf_embed(
            game, f"⌛ Expired — nobody took <@{game['creator_id']}>'s bet. Nothing was charged.",
            discord.Color.greyple()
        ))
        await game["message"].clear_reactions()
    except Exception as e:
        print(f"[coinflip] expire {message_id}: {e}")

async def _cf_resolve(creator_sid: str, opp_sid: str | None, amount: int, creator_side: str):
    """Checks balances and flips under the balance lock. Returns
    (error, result_side, creator_won); error is None, 'creator' or 'opponent'."""
    async with BALANCE_LOCK:
        if PLAYER_BALANCE.get(creator_sid, 0) < amount:
            return "creator", None, None
        if opp_sid is not None and PLAYER_BALANCE.get(opp_sid, 0) < amount:
            return "opponent", None, None
        result = random.choice(["heads", "tails"])
        creator_won = (result == creator_side)
        delta = amount if creator_won else -amount
        PLAYER_BALANCE[creator_sid] = PLAYER_BALANCE.get(creator_sid, 0) + delta
        touched = [creator_sid]
        if opp_sid is not None:
            PLAYER_BALANCE[opp_sid] = PLAYER_BALANCE.get(opp_sid, 0) - delta
            touched.append(opp_sid)
        for sid in touched:
            if not await asyncio.to_thread(db_update_balance, sid, PLAYER_BALANCE[sid]):
                print(f"[economy] Supabase write failed for {sid}.")
        return None, result, creator_won

async def _cf_notice(channel_id: int, text: str):
    channel = bot.get_channel(channel_id)
    if channel:
        try:
            await channel.send(text, delete_after=6)
        except Exception:
            pass

async def _cf_animate(game: dict):
    """Flip animation: the box alternates Heads/Tails, slowing down, before the
    result is shown. (Balances are already settled; this is just for show.)"""
    msg = game["message"]
    try:
        await msg.clear_reactions()
    except Exception:
        pass
    side = random.choice(["heads", "tails"])
    for delay in (0.6, 0.6, 0.7, 0.8, 0.9, 1.1, 1.3):
        try:
            await msg.edit(embed=_cf_embed(
                game,
                f"🪙 **The coin is in the air…**\n\n"
                f"{CF_SIDE_EMOJI[side]}  **{side.title()}**",
                discord.Color.gold()
            ))
        except Exception:
            return
        side = "tails" if side == "heads" else "heads"
        await asyncio.sleep(delay)

async def _cf_conclude(game: dict, message_id: int, opp_id: int | None):
    """Runs the flip (vs `opp_id`, or the bot if None) and edits the box."""
    creator_sid = steam_id_for_discord(game["creator_id"])
    opp_sid = steam_id_for_discord(opp_id) if opp_id is not None else None
    err, result, creator_won = await _cf_resolve(creator_sid, opp_sid, game["amount"], game["side"])

    if err == "opponent":
        # Opponent can't cover it — let the box stay open for someone else.
        game["state"] = "open"
        await _cf_notice(game["channel_id"], f"❌ <@{opp_id}> doesn't have enough balance for this bet.")
        return
    if err == "creator":
        game["state"] = "done"
        _cf_finish(message_id)
        await game["message"].edit(embed=_cf_embed(
            game, f"❌ Cancelled — <@{game['creator_id']}> no longer has enough balance.",
            discord.Color.red()
        ))
        await game["message"].clear_reactions()
        return

    game["state"] = "done"
    _cf_finish(message_id)
    winner_id = game["creator_id"] if creator_won else opp_id
    opp_label = f"<@{opp_id}>" if opp_id is not None else "the bot 🤖"
    if winner_id is None:   # bot won
        outcome = f"🤖 The bot wins — <@{game['creator_id']}> loses **{game['amount']:,}**."
    elif opp_id is None:    # creator beat the bot
        outcome = f"🎉 <@{game['creator_id']}> wins **{game['amount']:,}** from the bot!"
    else:
        outcome = f"🎉 <@{winner_id}> wins **{game['amount']:,}** from <@{opp_id if creator_won else game['creator_id']}>!"
    await _cf_animate(game)
    await game["message"].edit(embed=_cf_embed(
        game,
        f"<@{game['creator_id']}> picked {CF_SIDE_EMOJI[game['side']]} **{game['side'].title()}** vs {opp_label}\n"
        f"The coin lands on {CF_SIDE_EMOJI[result]} **{result.title()}**!\n\n{outcome}",
        discord.Color.green() if (creator_won or winner_id == opp_id) else discord.Color.red()
    ))
    await game["message"].clear_reactions()

@bot.command(name="coinflip", aliases=["cf"])
async def coinflip_command(ctx, amount: int):
    """!coinflip <amount> (alias !cf) — start a coinflip box."""
    if amount <= 0:
        await ctx.send("❌ Amount must be a positive whole number.")
        return
    sid = steam_id_for_discord(ctx.author.id)
    if not sid:
        await ctx.send("❌ You don't have a linked tracked player, so you have no balance.")
        return
    if PLAYER_BALANCE.get(sid, 0) < amount:
        await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(sid, 0):,}**.")
        return

    game = {
        "creator_id": ctx.author.id, "amount": amount, "side": None, "state": "pick",
        "channel_id": ctx.channel.id, "message": None, "task": None,
    }
    msg = await ctx.send(embed=_cf_embed(
        game,
        f"<@{ctx.author.id}>, pick your side:\n{CF_HEADS} Heads   {CF_TAILS} Tails   {CF_CANCEL} Cancel"
    ))
    game["message"] = msg
    COINFLIPS[msg.id] = game
    game["task"] = asyncio.create_task(_cf_expire(msg.id))
    for e in (CF_HEADS, CF_TAILS, CF_CANCEL):
        await msg.add_reaction(e)

@bot.listen('on_raw_reaction_add')
async def coinflip_reactions(payload):
    if payload.user_id == bot.user.id:
        return
    game = COINFLIPS.get(payload.message_id)
    if not game:
        return

    emoji = str(payload.emoji)
    user_id = payload.user_id
    is_creator = (user_id == game["creator_id"])
    msg = game["message"]

    async def drop_reaction():
        try:
            await msg.remove_reaction(payload.emoji, discord.Object(id=user_id))
        except Exception:
            pass

    await drop_reaction()   # keep only the bot's reactions visible

    state = game["state"]
    if state not in ("pick", "open"):
        return

    # ── Cancel (creator only, either stage) ───────────────────────
    if emoji == CF_CANCEL:
        if not is_creator:
            return
        game["state"] = "done"
        _cf_finish(payload.message_id)
        await msg.edit(embed=_cf_embed(game, "🚫 Cancelled by the creator.", discord.Color.greyple()))
        await msg.clear_reactions()
        return

    # ── Stage 1: creator picks a side ────────────────────────────
    if state == "pick":
        if not is_creator or emoji not in (CF_HEADS, CF_TAILS):
            return
        game["state"] = "open"   # set before any await-heavy work to block double picks
        game["side"] = "heads" if emoji == CF_HEADS else "tails"
        other = "tails" if game["side"] == "heads" else "heads"
        await msg.edit(embed=_cf_embed(
            game,
            f"<@{game['creator_id']}> picked {CF_SIDE_EMOJI[game['side']]} **{game['side'].title()}**.\n\n"
            f"• Anyone else: click {CF_SIDE_EMOJI[other]} to take **{other.title()}**\n"
            f"• <@{game['creator_id']}>: click {CF_BOT} to flip against the bot, or {CF_CANCEL} to cancel\n\n"
            f"Expires in {COINFLIP_TIMEOUT // 60} min."
        ))
        await msg.clear_reactions()
        for e in (CF_SIDE_EMOJI[other], CF_BOT, CF_CANCEL):
            await msg.add_reaction(e)
        return

    # ── Stage 2: someone joins, or creator picks the bot ─────────
    other_emoji = CF_TAILS if game["side"] == "heads" else CF_HEADS
    if emoji == CF_BOT:
        if not is_creator:
            return
        game["state"] = "resolving"
        await _cf_conclude(game, payload.message_id, None)
    elif emoji == other_emoji:
        if is_creator:
            return
        if not steam_id_for_discord(user_id):
            await _cf_notice(game["channel_id"], f"❌ <@{user_id}> you need a linked tracked player to join.")
            return
        game["state"] = "resolving"
        await _cf_conclude(game, payload.message_id, user_id)

# =================================================================
# 10.6 GAMES — HIGHER OR LOWER
# =================================================================
# !hilo <amount>: the bot draws from a single shuffled 52-card deck (no
# repeats, aces high) and shows a card. The starter guesses ⬆️ higher or
# ⬇️ lower for the next card, and may ✅ cash out between guesses.
#
# Payouts are FAIR (no house edge): with H cards higher and L cards lower
# left in the deck, a correct guess multiplies the pot by (H+L)/H (higher)
# or (H+L)/L (lower). Equal ranks are a push (the new card becomes the
# current one, pot unchanged), which keeps those odds exact. Because the
# multiplier is computed from the real remaining deck, card counting gives
# no advantage.
#
# Anti-abuse: the bet is taken up front (so walking away = a loss), one run
# per player at a time, only the starter can click, the pot is tracked as an
# exact fraction and the payout is rounded DOWN to whole points at the end
# (rounding up would let people farm free points with 1-point bets on
# near-certain guesses). An idle run auto-cashes out after HL_IDLE_TIMEOUT.

HL_UP, HL_DOWN, HL_CASH = "⬆️", "⬇️", "✅"
HL_IDLE_TIMEOUT = 120
HL_SUITS = ["♥️", "♦️", "♣️", "♠️"]
HL_RANK_LABELS = {11: "J", 12: "Q", 13: "K", 14: "A"}
HILO = {}            # message_id -> game dict
HILO_ACTIVE = set()  # discord user ids with a run in progress

def _hl_label(card) -> str:
    rank, suit = card
    return f"{suit} **{HL_RANK_LABELS.get(rank, rank)}**"

def _hl_counts(game):
    cur = game["current"][0]
    higher = sum(1 for r, _ in game["deck"] if r > cur)
    lower  = sum(1 for r, _ in game["deck"] if r < cur)
    return higher, lower

def _hl_mult(total: int, part: int):
    return Fraction(total, part) if part else None

def _hl_fmt_mult(m) -> str:
    if m is None:
        return "—"
    return f"×{math.floor(m * 100) / 100:.2f}"   # displayed rounded down, never overstated

def _hl_embed(game, final_status: str | None = None, color=None) -> discord.Embed:
    pot = math.floor(game["pot"])
    embed = discord.Embed(title=f"🃏 Higher or Lower — {game['amount']:,} points",
                          color=color or discord.Color.blurple())
    history = "\n".join(game["history"])
    if final_status:
        embed.description = f"<@{game['creator_id']}>\n{final_status}\n\n**Steps:**\n{history}"
    else:
        higher, lower = _hl_counts(game)
        total = higher + lower
        embed.description = (
            f"<@{game['creator_id']}>\n"
            f"Current card: {_hl_label(game['current'])}\n"
            f"Pot: **{pot:,}**\n\n"
            f"{HL_UP} Higher {_hl_fmt_mult(_hl_mult(total, higher))}\n"
            f"{HL_DOWN} Lower {_hl_fmt_mult(_hl_mult(total, lower))}\n"
            f"{HL_CASH} Cash out **{pot:,}**\n\n"
            f"**Steps so far:**\n{history}"
        )
        embed.set_footer(text=f"Idle for {HL_IDLE_TIMEOUT // 60} min = auto cash out · {len(game['deck'])} cards left")
    return embed

def _hl_reset_timer(message_id: int, game: dict):
    old = game.get("task")
    if old and old is not asyncio.current_task():
        old.cancel()
    game["task"] = asyncio.create_task(_hl_idle(message_id))

async def _hl_idle(message_id: int):
    await asyncio.sleep(HL_IDLE_TIMEOUT)
    game = HILO.get(message_id)
    if game and game["state"] == "active" and not game["busy"]:
        game["busy"] = True
        await _hl_finish(game, message_id, "idle")

async def _hl_finish(game: dict, message_id: int, kind: str):
    """kind: 'cash' | 'idle' | 'exhausted' (all pay out the pot) or 'lost'."""
    game["state"] = "done"
    HILO.pop(message_id, None)
    HILO_ACTIVE.discard(game["creator_id"])
    task = game.get("task")
    if task and task is not asyncio.current_task():
        task.cancel()

    payout = 0
    if kind != "lost":
        payout = math.floor(game["pot"])
        await adjust_balance(game["sid"], payout)
    net = payout - game["amount"]

    if kind == "lost":
        status = f"💥 **Lost!** You lose your **{game['amount']:,}** bet."
        color = discord.Color.red()
    else:
        prefix = {"cash": "💰 **Cashed out**", "idle": "⌛ **Idle — auto cashed out**",
                  "exhausted": "🃏 **Deck ran out — cashed out**"}[kind]
        status = f"{prefix} **{payout:,}** (net {net:+,})."
        color = discord.Color.green() if net >= 0 else discord.Color.orange()
    try:
        await game["message"].edit(embed=_hl_embed(game, final_status=status, color=color))
        await game["message"].clear_reactions()
    except Exception as e:
        print(f"[hilo] finish {message_id}: {e}")

@bot.command(name="higherlower", aliases=["hilo"])
async def higherlower_command(ctx, amount: int):
    """!higherlower <amount> (alias !hilo) — start a higher/lower run."""
    if amount <= 0:
        await ctx.send("❌ Amount must be a positive whole number.")
        return
    sid = steam_id_for_discord(ctx.author.id)
    if not sid:
        await ctx.send("❌ You don't have a linked tracked player, so you have no balance.")
        return
    if ctx.author.id in HILO_ACTIVE:
        await ctx.send("❌ You already have a Higher or Lower run going — finish it first.")
        return

    HILO_ACTIVE.add(ctx.author.id)   # claim the slot before any await
    if await adjust_balance(sid, -amount) is None:
        HILO_ACTIVE.discard(ctx.author.id)
        await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(sid, 0):,}**.")
        return

    deck = [(r, s) for r in range(2, 15) for s in HL_SUITS]
    random.shuffle(deck)
    first = deck.pop()
    game = {
        "creator_id": ctx.author.id, "sid": sid, "amount": amount,
        "pot": Fraction(amount), "deck": deck, "current": first,
        "history": [f"🃏 Start: {_hl_label(first)}"],
        "state": "active", "busy": False, "message": None, "task": None,
    }
    try:
        msg = await ctx.send(embed=_hl_embed(game))
        game["message"] = msg
        HILO[msg.id] = game
        for e in (HL_UP, HL_DOWN, HL_CASH):
            await msg.add_reaction(e)
    except Exception as e:
        # Couldn't post the game — give the bet back.
        print(f"[hilo] start failed: {e}")
        HILO.pop(getattr(game["message"], "id", None), None)
        HILO_ACTIVE.discard(ctx.author.id)
        await adjust_balance(sid, amount)
        return
    _hl_reset_timer(msg.id, game)

@bot.listen('on_raw_reaction_add')
async def hilo_reactions(payload):
    if payload.user_id == bot.user.id:
        return
    game = HILO.get(payload.message_id)
    if not game:
        return

    msg = game["message"]
    try:
        await msg.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
    except Exception:
        pass

    if payload.user_id != game["creator_id"]:
        return
    if game["state"] != "active" or game["busy"]:
        return

    emoji = str(payload.emoji)
    if emoji not in (HL_UP, HL_DOWN, HL_CASH):
        return

    game["busy"] = True
    message_id = payload.message_id

    if emoji == HL_CASH:
        await _hl_finish(game, message_id, "cash")
        return

    higher, lower = _hl_counts(game)
    total = higher + lower
    guess_up = (emoji == HL_UP)
    if (guess_up and higher == 0) or (not guess_up and lower == 0):
        game["busy"] = False
        await _cf_notice(game["message"].channel.id,
                         f"❌ <@{game['creator_id']}> no card can be {'higher' if guess_up else 'lower'} — pick the other option or cash out.")
        return

    try:
        mult = _hl_mult(total, higher if guess_up else lower)
        prev_label = _hl_label(game["current"])
        prev_rank  = game["current"][0]
        nxt = game["deck"].pop()
        game["current"] = nxt
        nxt_label = _hl_label(nxt)

        if nxt[0] == prev_rank:
            game["history"].append(f"{emoji} {prev_label} → {nxt_label} ➖ push (pot unchanged)")
        elif (nxt[0] > prev_rank) == guess_up:
            game["pot"] = game["pot"] * mult
            game["history"].append(
                f"{emoji} {prev_label} → {nxt_label} ✅ {_hl_fmt_mult(mult)} → pot {math.floor(game['pot']):,}"
            )
        else:
            game["history"].append(f"{emoji} {prev_label} → {nxt_label} ❌ wrong")
            await _hl_finish(game, message_id, "lost")
            return

        # Nothing left that could be higher/lower than the new card → end the run.
        h2, l2 = _hl_counts(game)
        if h2 + l2 == 0:
            await _hl_finish(game, message_id, "exhausted")
            return

        await msg.edit(embed=_hl_embed(game))
        _hl_reset_timer(message_id, game)
    finally:
        if game["state"] == "active":
            game["busy"] = False

# =================================================================
# 10.7 GAMES — BLACKJACK
# =================================================================
# !bj <amount> (or !blackjack): solo vs the dealer, fresh shuffled 52-card deck
# per game. Reactions: 🃏 hit · ✋ stand · 💰 double down (first move only).
# Dealer stands on all 17s. Payouts (total returned on a wager `bet`):
#   blackjack (21 on the first two cards) 3x · win 2x · push 1x · loss 0.
# Double down doubles the wager, draws exactly one card, then stands.
# The wager is taken up front; one game per player at a time; an idle game
# auto-stands after BJ_IDLE_TIMEOUT.

BJ_HIT, BJ_STAND, BJ_DOUBLE = "🃏", "✋", "💰"
BJ_IDLE_TIMEOUT = 120
BLACKJACKS = {}      # message_id -> game dict
BJ_ACTIVE = set()    # discord user ids with a game in progress

def _bj_card_value(rank: int) -> int:
    if rank == 14:
        return 11
    return min(rank, 10)

def _bj_value(cards) -> tuple[int, bool]:
    """Returns (best total, is_soft) — soft means an ace is still counted as 11."""
    total = sum(_bj_card_value(r) for r, _ in cards)
    aces = sum(1 for r, _ in cards if r == 14)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0

def _bj_hand_text(cards, hide_second=False) -> str:
    if hide_second:
        return f"{_hl_label(cards[0])}  ❓"
    total, soft = _bj_value(cards)
    return f"{'  '.join(_hl_label(c) for c in cards)}\n**{'Soft ' if soft and total <= 21 else ''}{total}**"

def _bj_embed(game, final_status: str | None = None, color=None) -> discord.Embed:
    embed = discord.Embed(title=f"♠️ Blackjack — {game['bet']:,} points",
                          color=color or discord.Color.dark_green())
    reveal = final_status is not None
    embed.add_field(name="Dealer", value=_bj_hand_text(game["dealer"], hide_second=not reveal), inline=True)
    embed.add_field(name="You", value=_bj_hand_text(game["player"]), inline=True)
    if final_status:
        embed.description = f"<@{game['creator_id']}>\n{final_status}"
    else:
        controls = f"{BJ_HIT} Hit   {BJ_STAND} Stand"
        if len(game["player"]) == 2 and not game["doubled"]:
            controls += f"   {BJ_DOUBLE} Double down (+{game['bet']:,})"
        embed.description = f"<@{game['creator_id']}>\n{controls}"
        embed.set_footer(text=f"Dealer stands on all 17s · idle {BJ_IDLE_TIMEOUT // 60} min = auto stand")
    return embed

def _bj_reset_timer(message_id: int, game: dict):
    old = game.get("task")
    if old and old is not asyncio.current_task():
        old.cancel()
    game["task"] = asyncio.create_task(_bj_idle(message_id))

async def _bj_idle(message_id: int):
    await asyncio.sleep(BJ_IDLE_TIMEOUT)
    game = BLACKJACKS.get(message_id)
    if game and game["state"] == "active" and not game["busy"]:
        game["busy"] = True
        await _bj_dealer_turn(game, message_id, note="⌛ Idle — auto stand.\n")

async def _bj_finish(game: dict, message_id: int, outcome: str, note: str = ""):
    """outcome: 'blackjack' | 'win' | 'push' | 'lose' | 'bust'."""
    game["state"] = "done"
    BLACKJACKS.pop(message_id, None)
    BJ_ACTIVE.discard(game["creator_id"])
    task = game.get("task")
    if task and task is not asyncio.current_task():
        task.cancel()

    factor = {"blackjack": 3, "win": 2, "push": 1, "lose": 0, "bust": 0}[outcome]
    payout = game["bet"] * factor
    if payout:
        await adjust_balance(game["sid"], payout)
    net = payout - game["bet"]

    text = {
        "blackjack": f"🎉 **Blackjack!** You win **{net:,}**.",
        "win":       f"✅ **You win!** +**{net:,}**.",
        "push":      "➖ **Push** — your bet is returned.",
        "lose":      f"❌ **Dealer wins.** You lose **{game['bet']:,}**.",
        "bust":      f"💥 **Bust!** You lose **{game['bet']:,}**.",
    }[outcome]
    color = (discord.Color.green() if net > 0 else
             discord.Color.light_grey() if net == 0 else discord.Color.red())
    try:
        await game["message"].edit(embed=_bj_embed(game, final_status=note + text, color=color))
        await game["message"].clear_reactions()
    except Exception as e:
        print(f"[blackjack] finish {message_id}: {e}")

async def _bj_dealer_turn(game: dict, message_id: int, note: str = ""):
    while _bj_value(game["dealer"])[0] < 17:
        game["dealer"].append(game["deck"].pop())
    p = _bj_value(game["player"])[0]
    d = _bj_value(game["dealer"])[0]
    if d > 21 or p > d:
        outcome = "win"
    elif p == d:
        outcome = "push"
    else:
        outcome = "lose"
    await _bj_finish(game, message_id, outcome, note)

@bot.command(name="blackjack", aliases=["bj"])
async def blackjack_command(ctx, amount: int):
    """!blackjack <amount> (alias !bj) — play a hand of blackjack vs the dealer."""
    if amount <= 0:
        await ctx.send("❌ Amount must be a positive whole number.")
        return
    sid = steam_id_for_discord(ctx.author.id)
    if not sid:
        await ctx.send("❌ You don't have a linked tracked player, so you have no balance.")
        return
    if ctx.author.id in BJ_ACTIVE:
        await ctx.send("❌ You already have a blackjack game going — finish it first.")
        return

    BJ_ACTIVE.add(ctx.author.id)   # claim the slot before any await
    if await adjust_balance(sid, -amount) is None:
        BJ_ACTIVE.discard(ctx.author.id)
        await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(sid, 0):,}**.")
        return

    deck = [(r, s) for r in range(2, 15) for s in HL_SUITS]
    random.shuffle(deck)
    game = {
        "creator_id": ctx.author.id, "sid": sid, "bet": amount, "doubled": False,
        "deck": deck, "player": [deck.pop(), deck.pop()], "dealer": [deck.pop(), deck.pop()],
        "state": "active", "busy": True, "message": None, "task": None,
    }

    p_bj = _bj_value(game["player"])[0] == 21
    d_bj = _bj_value(game["dealer"])[0] == 21
    try:
        if p_bj or d_bj:
            # Naturals are settled immediately.
            outcome = "push" if (p_bj and d_bj) else ("blackjack" if p_bj else "lose")
            msg = await ctx.send(embed=_bj_embed(game, final_status="…"))
            game["message"] = msg
            BLACKJACKS[msg.id] = game
            await _bj_finish(game, msg.id, outcome,
                             note="Dealer has blackjack. " if d_bj and not p_bj else "")
            return

        msg = await ctx.send(embed=_bj_embed(game))
        game["message"] = msg
        BLACKJACKS[msg.id] = game
        for e in (BJ_HIT, BJ_STAND, BJ_DOUBLE):
            await msg.add_reaction(e)
    except Exception as e:
        # Couldn't post the game — give the wager back.
        print(f"[blackjack] start failed: {e}")
        if game["state"] == "active":
            BLACKJACKS.pop(getattr(game["message"], "id", None), None)
            BJ_ACTIVE.discard(ctx.author.id)
            await adjust_balance(sid, amount)
        return
    game["busy"] = False
    _bj_reset_timer(msg.id, game)

@bot.listen('on_raw_reaction_add')
async def blackjack_reactions(payload):
    if payload.user_id == bot.user.id:
        return
    game = BLACKJACKS.get(payload.message_id)
    if not game:
        return

    msg = game["message"]
    try:
        await msg.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
    except Exception:
        pass

    if payload.user_id != game["creator_id"]:
        return
    if game["state"] != "active" or game["busy"]:
        return

    emoji = str(payload.emoji)
    if emoji not in (BJ_HIT, BJ_STAND, BJ_DOUBLE):
        return

    game["busy"] = True
    message_id = payload.message_id
    try:
        if emoji == BJ_STAND:
            await _bj_dealer_turn(game, message_id)
            return

        if emoji == BJ_DOUBLE:
            if len(game["player"]) != 2 or game["doubled"]:
                return
            if await adjust_balance(game["sid"], -game["bet"]) is None:
                await _cf_notice(msg.channel.id,
                                 f"❌ <@{game['creator_id']}> not enough balance to double down.")
                return
            game["bet"] *= 2
            game["doubled"] = True
            game["player"].append(game["deck"].pop())
            if _bj_value(game["player"])[0] > 21:
                await _bj_finish(game, message_id, "bust")
            else:
                await _bj_dealer_turn(game, message_id)
            return

        # Hit
        game["player"].append(game["deck"].pop())
        total = _bj_value(game["player"])[0]
        if total > 21:
            await _bj_finish(game, message_id, "bust")
        elif total == 21:
            await _bj_dealer_turn(game, message_id)   # 21 — stand automatically
        else:
            if len(game["player"]) == 3:
                # Double down is no longer available — drop its reaction.
                try:
                    await msg.remove_reaction(BJ_DOUBLE, bot.user)
                except Exception:
                    pass
            await msg.edit(embed=_bj_embed(game))
            _bj_reset_timer(message_id, game)
    finally:
        if game["state"] == "active":
            game["busy"] = False

# =================================================================
# 10.8 GAMES — ROULETTE (shared wheel, colors only)
# =================================================================
# !roulette <amount> <red|black|green> (alias !rl): the first command in a
# channel opens a wheel and makes the caller its owner; later commands from
# other players in that channel join the same wheel (one bet per player per
# wheel). The owner clicks ▶️ to spin, otherwise it auto-spins
# RL_AUTO_SPIN seconds after creation. One wheel per channel.
#
# Classic 15-slot wheel: 0 = green, 1-7 = red, 8-14 = black.
#   red / black: 1:1 (bet returned + 1x) · green: 14:1 (bet returned + 14x)
#   A 0 makes every red/black bet lose.
# Bets are taken the moment they're placed.

RL_AUTO_SPIN = 60
RL_PLAY = "▶️"
RL_COLOR_EMOJI  = {"red": "🔴", "black": "⚫", "green": "🟢"}
RL_COLOR_ALIASES = {"red": "red", "r": "red", "black": "black", "b": "black",
                    "green": "green", "g": "green"}
RL_RETURN_FACTOR = {"red": 2, "black": 2, "green": 15}   # total returned per point bet
ROULETTES = {}   # channel_id -> wheel dict

def _rl_slot_color(n: int) -> str:
    return "green" if n == 0 else ("red" if n <= 7 else "black")

# Order of the slots around the wheel: green, then red/black alternating.
RL_WHEEL = [0, 1, 8, 2, 9, 3, 10, 4, 11, 5, 12, 6, 13, 7, 14]
RL_SQUARE = {"green": "🟩", "red": "🟥", "black": "⬛"}

def _rl_strip(center: int, width: int = 9) -> str:
    """A straight strip of colored squares (in real wheel order) with a pointer
    marking the slot at `center`."""
    half = width // 2
    squares = "".join(
        RL_SQUARE[_rl_slot_color(RL_WHEEL[(center + off) % len(RL_WHEEL)])]
        for off in range(-half, half + 1)
    )
    filler = "▫️" * half
    return f"{filler}🔻{filler}\n{squares}\n{filler}🔺{filler}"

def _rl_embed(game, status: str | None = None, color=None) -> discord.Embed:
    embed = discord.Embed(title="🎡 Roulette", color=color or discord.Color.dark_red())
    lines = []
    for uid, b in game["bets"].items():
        if b is None:
            continue
        line = f"<@{uid}> — **{b['amount']:,}** on {RL_COLOR_EMOJI[b['color']]} {b['color']}"
        if b.get("result"):
            line += f"  {b['result']}"
        lines.append(line)
    pot = sum(b["amount"] for b in game["bets"].values() if b)
    body = "\n".join(lines) if lines else "*No bets yet*"
    if status:
        embed.description = f"{status}\n\n**Bets:**\n{body}"
    else:
        embed.description = (
            f"Owner: <@{game['owner_id']}> — click {RL_PLAY} to spin, or it spins <t:{game['spin_ts']}:R>.\n"
            f"Join with `!roulette <amount> <red|black|green>` in this channel.\n\n"
            f"**Bets ({len(lines)} · {pot:,} total):**\n{body}"
        )
        embed.set_footer(text="red/black 1:1 · green 14:1 · 0 (green) beats red/black")
    return embed

async def _rl_auto_spin(channel_id: int):
    await asyncio.sleep(RL_AUTO_SPIN)
    game = ROULETTES.get(channel_id)
    if game and game["state"] == "open":
        await _rl_spin(game, channel_id)

async def _rl_spin(game: dict, channel_id: int):
    if game["state"] != "open":
        return
    game["state"] = "spinning"
    task = game.get("task")
    if task and task is not asyncio.current_task():
        task.cancel()

    msg = game["message"]
    try:
        await msg.clear_reactions()
    except Exception as e:
        print(f"[roulette] clear reactions: {e}")

    # Decide the result first, then scroll the strip so it decelerates onto it.
    number = random.randint(0, 14)
    landed = _rl_slot_color(number)
    target = RL_WHEEL.index(number)
    steps  = [3, 3, 3, 2, 2, 2, 1, 1, 1, 1]
    delays = [0.7, 0.7, 0.7, 0.8, 0.8, 0.9, 1.0, 1.1, 1.2, 1.4]
    center = (target - sum(steps)) % len(RL_WHEEL)
    for step, delay in zip(steps, delays):
        center = (center + step) % len(RL_WHEEL)
        try:
            await msg.edit(embed=_rl_embed(
                game, status=f"🎡 **Spinning…**\n\n{_rl_strip(center)}", color=discord.Color.gold()))
        except Exception as e:
            print(f"[roulette] spin frame: {e}")
            break
        await asyncio.sleep(delay)
    for uid, b in game["bets"].items():
        if b is None:
            continue
        if b["color"] == landed:
            payout = b["amount"] * RL_RETURN_FACTOR[landed]
            await adjust_balance(b["sid"], payout)
            b["result"] = f"✅ +{payout - b['amount']:,}"
        else:
            b["result"] = f"❌ −{b['amount']:,}"

    ROULETTES.pop(channel_id, None)
    try:
        await msg.edit(embed=_rl_embed(
            game,
            status=(f"🎡 The ball lands on {RL_COLOR_EMOJI[landed]} **{number}** ({landed})!\n\n"
                    f"{_rl_strip(target)}"),
            color=discord.Color.green() if landed == "green" else
                  (discord.Color.red() if landed == "red" else discord.Color.dark_grey())
        ))
    except Exception as e:
        print(f"[roulette] result edit: {e}")

@bot.command(name="roulette", aliases=["rl"])
async def roulette_command(ctx, amount: int, color: str):
    """!roulette <amount> <red|black|green> (alias !rl) — open a wheel or join
    the one in this channel."""
    if amount <= 0:
        await ctx.send("❌ Amount must be a positive whole number.")
        return
    color = RL_COLOR_ALIASES.get(color.lower())
    if not color:
        await ctx.send("❌ Pick a color: `red`, `black` or `green`.")
        return
    sid = steam_id_for_discord(ctx.author.id)
    if not sid:
        await ctx.send("❌ You don't have a linked tracked player, so you have no balance.")
        return

    channel_id = ctx.channel.id
    game = ROULETTES.get(channel_id)

    # ── Join an existing wheel ────────────────────────────────────
    if game is not None:
        if game["state"] != "open":
            await ctx.send("⏳ The wheel is spinning — wait for the result, then open a new one.")
            return
        if ctx.author.id in game["bets"]:
            await ctx.send("❌ You already have a bet on this wheel.")
            return
        game["bets"][ctx.author.id] = None   # claim the slot before any await
        if await adjust_balance(sid, -amount) is None:
            game["bets"].pop(ctx.author.id, None)
            await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(sid, 0):,}**.")
            return
        if game["state"] != "open":
            # Spin started while we were taking the bet — refund.
            game["bets"].pop(ctx.author.id, None)
            await adjust_balance(sid, amount)
            await ctx.send("⏳ Too late — the wheel just started spinning. Bet refunded.")
            return
        game["bets"][ctx.author.id] = {"sid": sid, "amount": amount, "color": color}
        try:
            await game["message"].edit(embed=_rl_embed(game))
            await ctx.message.add_reaction("✅")
        except Exception as e:
            print(f"[roulette] join edit: {e}")
        return

    # ── Open a new wheel ──────────────────────────────────────────
    game = {
        "owner_id": ctx.author.id, "state": "open", "message": None, "task": None,
        "bets": {ctx.author.id: None},
        "spin_ts": int(datetime.now(timezone.utc).timestamp()) + RL_AUTO_SPIN,
    }
    ROULETTES[channel_id] = game   # claim the channel's wheel before any await
    if await adjust_balance(sid, -amount) is None:
        ROULETTES.pop(channel_id, None)
        await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(sid, 0):,}**.")
        return
    game["bets"][ctx.author.id] = {"sid": sid, "amount": amount, "color": color}
    try:
        msg = await ctx.send(embed=_rl_embed(game))
        game["message"] = msg
        await msg.add_reaction(RL_PLAY)
    except Exception as e:
        print(f"[roulette] open failed: {e}")
        ROULETTES.pop(channel_id, None)
        for b in game["bets"].values():
            if b:
                await adjust_balance(b["sid"], b["amount"])
        return
    game["task"] = asyncio.create_task(_rl_auto_spin(channel_id))

@bot.listen('on_raw_reaction_add')
async def roulette_reactions(payload):
    if payload.user_id == bot.user.id:
        return
    game = ROULETTES.get(payload.channel_id)
    if not game or not game["message"] or game["message"].id != payload.message_id:
        return
    try:
        await game["message"].remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
    except Exception:
        pass
    if payload.user_id == game["owner_id"] and str(payload.emoji) == RL_PLAY:
        await _rl_spin(game, payload.channel_id)

# =================================================================
# 10.9 GAMES — RUSSIAN ROULETTE
# =================================================================
# !russianroulette <amount> (aliases !rr, !russian) opens a box. Then either:
#   • another player clicks 🔫 to join for the same amount → a DUEL starts
#     immediately. The revolver has 6 chambers with ONE bullet hidden in a
#     pre-set random chamber (no re-spin), so every pull is a step closer
#     and a duel always ends within 6 pulls; each player has exactly a 50%
#     chance. Players alternate pulling (🔫, only the player whose turn it
#     is can click; auto-pulls after RR_TURN_TIMEOUT seconds). Whoever hits
#     the bullet loses; the winner takes the pot.
#   • the creator clicks 🤖 to play alone vs the revolver: same gun, but the
#     player chooses after every survived pull to pull again (🔫) or cash
#     out (💰). Payouts are fair: surviving k pulls pays 6/(6-k) x the bet
#     (1.2x, 1.5x, 2x, 3x, 6x), rounded DOWN to whole points.
# An unclaimed box auto-cancels (bet refunded) after RR_OPEN_TIMEOUT seconds.
# Bets are taken up front; one Russian roulette per player at a time.

RR_PULL, RR_BOT, RR_CANCEL, RR_CASH = "🔫", "🤖", "❌", "💰"
RR_CHAMBERS      = 6
RR_TURN_TIMEOUT  = 10    # duel: seconds before an auto-pull
RR_OPEN_TIMEOUT  = 120   # unclaimed box: seconds before auto-cancel
RR_BOT_IDLE      = 120   # vs-bot run: idle seconds before auto cash-out
RUSSIAN = {}             # message_id -> game dict
RR_ACTIVE = set()        # discord user ids currently in a game

def _rr_mult(survived: int) -> Fraction:
    return Fraction(RR_CHAMBERS, RR_CHAMBERS - survived)

def _rr_fmt(m: Fraction) -> str:
    return f"×{math.floor(m * 100) / 100:.2f}"

def _rr_pot(game) -> int:
    return math.floor(game["amount"] * _rr_mult(game["pulls"]))

def _rr_embed(game, status: str | None = None, color=None) -> discord.Embed:
    amount = game["amount"]
    embed = discord.Embed(title=f"🔫 Russian Roulette — {amount:,} points",
                          color=color or discord.Color.dark_red())
    history = "\n".join(game["history"])
    if status:
        embed.description = f"{status}" + (f"\n\n{history}" if history else "")
        return embed

    if game["state"] == "open":
        table = " · ".join(_rr_fmt(_rr_mult(k)) for k in range(1, RR_CHAMBERS))
        embed.description = (
            f"<@{game['creator_id']}> put **{amount:,}** on the table.\n\n"
            f"**How it works:** a revolver with **1 bullet in {RR_CHAMBERS} chambers**.\n"
            f"• **Duel:** another player clicks {RR_PULL} to match the bet. You take turns pulling the trigger "
            f"(only on your turn, auto-pull after {RR_TURN_TIMEOUT}s). Whoever hits the bullet loses; "
            f"the winner takes the pot (**{2 * amount:,}**).\n"
            f"• **Vs bot:** <@{game['creator_id']}> clicks {RR_BOT} to pull alone. Each survived pull raises "
            f"the payout ({table}) — pull again or cash out {RR_CASH} any time.\n"
            f"• {RR_CANCEL} cancels (creator only).\n\n"
            f"Cancels automatically <t:{game['expire_ts']}:R> if nobody joins."
        )
    elif game["mode"] == "duel":
        cur = game["players"][game["turn"]]
        a, b = game["players"]
        embed.description = (
            f"⚔️ **Duel:** <@{a}> vs <@{b}> — pot **{2 * amount:,}**\n\n"
            f"{history}\n\n"
            f"Turn: <@{cur}> — click {RR_PULL}, or it auto-pulls <t:{game['turn_ts']}:R>."
        )
    else:
        k = game["pulls"]
        pot = _rr_pot(game)
        next_m = _rr_mult(k + 1)
        embed.description = (
            f"<@{game['creator_id']}> vs the revolver — bet **{amount:,}**\n\n"
            f"{history}\n\n"
            f"Survived: **{k}/{RR_CHAMBERS - 1}** · Pot now: **{pot:,}** ({_rr_fmt(_rr_mult(k))})\n"
            f"{RR_PULL} Pull again — survive chance {RR_CHAMBERS - k - 1}/{RR_CHAMBERS - k}, pays {_rr_fmt(next_m)}\n"
            f"{RR_CASH} Cash out **{pot:,}**"
        )
        embed.set_footer(text=f"Idle {RR_BOT_IDLE // 60} min = auto cash out")
    return embed

def _rr_set_timer(game: dict, coro):
    old = game.get("task")
    if old and old is not asyncio.current_task():
        old.cancel()
    game["task"] = asyncio.create_task(coro)

def _rr_cleanup(game: dict, message_id: int):
    game["state"] = "done"
    RUSSIAN.pop(message_id, None)
    for uid in game.get("players") or [game["creator_id"]]:
        RR_ACTIVE.discard(uid)
    RR_ACTIVE.discard(game["creator_id"])
    task = game.get("task")
    if task and task is not asyncio.current_task():
        task.cancel()

async def _rr_edit_final(game, embed):
    try:
        await game["message"].edit(embed=embed)
        await game["message"].clear_reactions()
    except Exception as e:
        print(f"[russian] final edit: {e}")

# ── Unclaimed box ──────────────────────────────────────────────────
async def _rr_expire(message_id: int):
    await asyncio.sleep(RR_OPEN_TIMEOUT)
    game = RUSSIAN.get(message_id)
    if not game or game["state"] != "open":
        return
    _rr_cleanup(game, message_id)
    await adjust_balance(game["sid"], game["amount"])
    await _rr_edit_final(game, _rr_embed(
        game, status=f"⌛ Nobody joined — <@{game['creator_id']}>'s bet of **{game['amount']:,}** was refunded.",
        color=discord.Color.greyple()))

# ── Duel ───────────────────────────────────────────────────────────
async def _rr_duel_turn_timer(message_id: int):
    await asyncio.sleep(RR_TURN_TIMEOUT)
    game = RUSSIAN.get(message_id)
    if game and game["state"] == "active" and game["mode"] == "duel" and not game["busy"]:
        game["busy"] = True
        try:
            await _rr_duel_pull(game, message_id, auto=True)
        finally:
            if game["state"] == "active":
                game["busy"] = False

async def _rr_duel_pull(game: dict, message_id: int, auto: bool = False):
    cur = game["players"][game["turn"]]
    other = game["players"][1 - game["turn"]]
    idx = game["pulls"]
    game["pulls"] += 1
    tag = " *(auto)*" if auto else ""

    # A little suspense before the result.
    try:
        await game["message"].edit(embed=_rr_embed(
            game, status=f"🔫 <@{cur}> raises the revolver to their head…{tag}", color=discord.Color.gold()))
    except Exception:
        pass
    await asyncio.sleep(1.5)

    if idx == game["bullet"]:
        game["history"].append(f"💥 <@{cur}> pulls the trigger{tag} — **BANG!**")
        _rr_cleanup(game, message_id)
        payout = 2 * game["amount"]
        await adjust_balance(game["sids"][other], payout)
        await _rr_edit_final(game, _rr_embed(
            game,
            status=(f"💀 <@{cur}> is dead.\n🏆 <@{other}> wins **{game['amount']:,}** "
                    f"(pot {payout:,})!"),
            color=discord.Color.red()))
        return

    game["history"].append(f"🔫 <@{cur}> pulls the trigger{tag} — *click*. Safe.")
    game["turn"] = 1 - game["turn"]
    game["turn_ts"] = int(datetime.now(timezone.utc).timestamp()) + RR_TURN_TIMEOUT
    await game["message"].edit(embed=_rr_embed(game))
    _rr_set_timer(game, _rr_duel_turn_timer(message_id))

# ── Vs bot ─────────────────────────────────────────────────────────
async def _rr_bot_idle(message_id: int):
    await asyncio.sleep(RR_BOT_IDLE)
    game = RUSSIAN.get(message_id)
    if game and game["state"] == "active" and game["mode"] == "bot" and not game["busy"]:
        game["busy"] = True
        await _rr_bot_finish(game, message_id, "idle")

async def _rr_bot_finish(game: dict, message_id: int, kind: str):
    """kind: 'died' | 'cash' | 'idle' | 'perfect'."""
    _rr_cleanup(game, message_id)
    if kind == "died":
        await _rr_edit_final(game, _rr_embed(
            game, status=f"💀 **BANG!** You lose **{game['amount']:,}**.", color=discord.Color.red()))
        return
    payout = _rr_pot(game)
    await adjust_balance(game["sid"], payout)
    net = payout - game["amount"]
    prefix = {"cash": "💰 **Cashed out**", "idle": "⌛ **Idle — auto cashed out**",
              "perfect": "🏆 **You survived every safe chamber — cashed out**"}[kind]
    await _rr_edit_final(game, _rr_embed(
        game, status=f"{prefix} **{payout:,}** ({_rr_fmt(_rr_mult(game['pulls']))}, net {net:+,}).",
        color=discord.Color.green() if net > 0 else discord.Color.light_grey()))

async def _rr_bot_pull(game: dict, message_id: int):
    idx = game["pulls"]
    try:
        await game["message"].edit(embed=_rr_embed(
            game, status="🔫 You raise the revolver to your head…", color=discord.Color.gold()))
    except Exception:
        pass
    await asyncio.sleep(1.5)
    if idx == game["bullet"]:
        game["history"].append(f"💥 Pull {idx + 1}: **BANG!**")
        await _rr_bot_finish(game, message_id, "died")
        return
    game["pulls"] += 1
    game["history"].append(f"🔫 Pull {game['pulls']}: *click* — safe ({_rr_fmt(_rr_mult(game['pulls']))})")
    if game["pulls"] >= RR_CHAMBERS - 1:
        # Only the loaded chamber is left — pulling again would be certain death.
        await _rr_bot_finish(game, message_id, "perfect")
        return
    await game["message"].edit(embed=_rr_embed(game))
    _rr_set_timer(game, _rr_bot_idle(message_id))

# ── Command + reactions ────────────────────────────────────────────
@bot.command(name="russianroulette", aliases=["rr", "russian"])
async def russian_command(ctx, amount: int):
    """!russianroulette <amount> (aliases !rr, !russian) — open a Russian roulette box."""
    if amount <= 0:
        await ctx.send("❌ Amount must be a positive whole number.")
        return
    sid = steam_id_for_discord(ctx.author.id)
    if not sid:
        await ctx.send("❌ You don't have a linked tracked player, so you have no balance.")
        return
    if ctx.author.id in RR_ACTIVE:
        await ctx.send("❌ You're already in a Russian roulette game.")
        return

    RR_ACTIVE.add(ctx.author.id)   # claim the slot before any await
    if await adjust_balance(sid, -amount) is None:
        RR_ACTIVE.discard(ctx.author.id)
        await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(sid, 0):,}**.")
        return

    game = {
        "creator_id": ctx.author.id, "sid": sid, "amount": amount,
        "state": "open", "mode": None, "busy": False, "message": None, "task": None,
        "players": None, "sids": {ctx.author.id: sid}, "turn": 0, "turn_ts": 0,
        "bullet": random.randrange(RR_CHAMBERS), "pulls": 0, "history": [],
        "expire_ts": int(datetime.now(timezone.utc).timestamp()) + RR_OPEN_TIMEOUT,
    }
    try:
        msg = await ctx.send(embed=_rr_embed(game))
        game["message"] = msg
        RUSSIAN[msg.id] = game
        for e in (RR_PULL, RR_BOT, RR_CANCEL):
            await msg.add_reaction(e)
    except Exception as e:
        print(f"[russian] open failed: {e}")
        RUSSIAN.pop(getattr(game["message"], "id", None), None)
        RR_ACTIVE.discard(ctx.author.id)
        await adjust_balance(sid, amount)
        return
    _rr_set_timer(game, _rr_expire(msg.id))

@bot.listen('on_raw_reaction_add')
async def russian_reactions(payload):
    if payload.user_id == bot.user.id:
        return
    game = RUSSIAN.get(payload.message_id)
    if not game:
        return
    msg = game["message"]
    try:
        await msg.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
    except Exception:
        pass
    if game["busy"] or game["state"] in ("done", "starting"):
        return

    emoji = str(payload.emoji)
    uid = payload.user_id
    mid = payload.message_id
    is_creator = (uid == game["creator_id"])

    # ── Waiting for an opponent ───────────────────────────────────
    if game["state"] == "open":
        if emoji == RR_CANCEL and is_creator:
            game["busy"] = True
            _rr_cleanup(game, mid)
            await adjust_balance(game["sid"], game["amount"])
            await _rr_edit_final(game, _rr_embed(
                game, status="🚫 Cancelled — bet refunded.", color=discord.Color.greyple()))

        elif emoji == RR_BOT and is_creator:
            game["busy"] = True
            game["state"], game["mode"] = "active", "bot"
            try:
                await msg.clear_reactions()
                await msg.edit(embed=_rr_embed(game))
                await msg.add_reaction(RR_PULL)
                await msg.add_reaction(RR_CASH)
            except Exception as e:
                print(f"[russian] bot start: {e}")
            _rr_set_timer(game, _rr_bot_idle(mid))
            game["busy"] = False

        elif emoji == RR_PULL and not is_creator:
            if uid in RR_ACTIVE:
                await _cf_notice(msg.channel.id, f"❌ <@{uid}> you're already in a Russian roulette game.")
                return
            joiner_sid = steam_id_for_discord(uid)
            if not joiner_sid:
                await _cf_notice(msg.channel.id, f"❌ <@{uid}> you need a linked tracked player to join.")
                return
            game["state"] = "starting"   # block other joiners while we take the bet
            RR_ACTIVE.add(uid)
            if await adjust_balance(joiner_sid, -game["amount"]) is None:
                RR_ACTIVE.discard(uid)
                game["state"] = "open"
                await _cf_notice(msg.channel.id,
                                 f"❌ <@{uid}> not enough balance — you need **{game['amount']:,}**.")
                return
            game["sids"][uid] = joiner_sid
            game["players"] = [game["creator_id"], uid]
            random.shuffle(game["players"])   # random who pulls first (odds are 50/50 either way)
            game["turn"] = 0
            game["mode"] = "duel"
            game["turn_ts"] = int(datetime.now(timezone.utc).timestamp()) + RR_TURN_TIMEOUT
            game["state"] = "active"
            try:
                await msg.clear_reactions()
                await msg.edit(embed=_rr_embed(game))
                await msg.add_reaction(RR_PULL)
            except Exception as e:
                print(f"[russian] duel start: {e}")
            _rr_set_timer(game, _rr_duel_turn_timer(mid))
        return

    # ── Duel in progress ──────────────────────────────────────────
    if game["state"] == "active" and game["mode"] == "duel":
        if emoji == RR_PULL and uid == game["players"][game["turn"]]:
            game["busy"] = True
            try:
                await _rr_duel_pull(game, mid)
            finally:
                if game["state"] == "active":
                    game["busy"] = False
        return

    # ── Solo vs the revolver ──────────────────────────────────────
    if game["state"] == "active" and game["mode"] == "bot" and is_creator:
        if emoji == RR_PULL:
            game["busy"] = True
            try:
                await _rr_bot_pull(game, mid)
            finally:
                if game["state"] == "active":
                    game["busy"] = False
        elif emoji == RR_CASH:
            game["busy"] = True
            await _rr_bot_finish(game, mid, "cash")

# =================================================================
# 10.10 GAMES — MINEFIELD (button grid)
# =================================================================
# !minefield <amount> (alias !mines): the bet is taken up front, then the
# starter reacts 1️⃣-9️⃣ to choose how many mines hide in a 5x5 grid of 25
# buttons. Clicking a button reveals it: 💎 = safe, 💣 = mine (game over).
# The starter can react ✅ at any time to cash out.
#
# Payouts are FAIR (no house edge), like hilo / russian roulette: with M mines
# in 25 tiles, surviving k reveals in a row has probability
#   prod_{i<k} (25-M-i)/(25-i)
# so the multiplier after k safe tiles is the inverse of that. More mines =
# bigger jumps per tile. The pot is an exact Fraction and the final payout is
# rounded DOWN to whole points (so tiny bets can't farm free points).
#
# Only the starter can click; one minefield per player at a time; an idle
# game auto-cashes out after MN_IDLE_TIMEOUT; an unstarted one (no mine count
# picked) is cancelled and refunded after MN_PICK_TIMEOUT.

MN_SIZE         = 25
MN_COLS         = 5
MN_CASH, MN_CANCEL = "✅", "❌"
MN_NUM_EMOJI    = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
MN_SAFE_EMOJI   = "💎"
MN_MINE_EMOJI   = "💣"
MN_PICK_TIMEOUT = 120
MN_IDLE_TIMEOUT = 120
MINES        = {}    # message_id -> game dict
MINES_ACTIVE = set() # discord user ids with a minefield in progress

def _mn_mult(mines: int, k: int) -> Fraction:
    """Fair multiplier after revealing k safe tiles in a row."""
    m = Fraction(1)
    for i in range(k):
        m *= Fraction(MN_SIZE - i, MN_SIZE - mines - i)
    return m

def _mn_fmt(m: Fraction) -> str:
    return f"×{math.floor(m * 100) / 100:.2f}"   # rounded down, never overstated

def _mn_pot(game: dict, k: int | None = None) -> int:
    k = len(game["revealed"]) if k is None else k
    return math.floor(game["amount"] * _mn_mult(game["mines"], k))

def _mn_embed(game: dict, final_status: str | None = None, color=None) -> discord.Embed:
    embed = discord.Embed(title=f"💣 Minefield — {game['amount']:,} points",
                          color=color or discord.Color.dark_orange())
    if final_status:
        embed.description = f"<@{game['creator_id']}>\n{final_status}"
        return embed

    if game["state"] == "pick":
        table = "  ".join(f"{MN_NUM_EMOJI[n - 1]} {_mn_fmt(_mn_mult(n, 1))}" for n in range(1, 10))
        embed.description = (
            f"<@{game['creator_id']}>, how many mines should hide in the **5×5** grid?\n"
            f"React with {MN_NUM_EMOJI[0]}–{MN_NUM_EMOJI[-1]}  ·  {MN_CANCEL} cancels (bet refunded).\n\n"
            f"More mines = bigger payout per safe tile, but a bigger chance of blowing up.\n"
            f"**Multiplier for the first safe tile:**\n{table}\n\n"
            f"Cancels automatically <t:{game['pick_ts']}:R>."
        )
        return embed

    k = len(game["revealed"])
    safe_total = MN_SIZE - game["mines"]
    cur, nxt = _mn_pot(game), _mn_pot(game, k + 1)
    embed.description = (
        f"<@{game['creator_id']}>\n"
        f"Mines: **{game['mines']}** · Safe tiles found: **{k}/{safe_total}**\n\n"
        f"💰 Current payout: **{cur:,}** ({_mn_fmt(_mn_mult(game['mines'], k))})\n"
        f"➡️ If the next tile is safe: **{nxt:,}** ({_mn_fmt(_mn_mult(game['mines'], k + 1))})\n\n"
        f"Click a tile below, or react {MN_CASH} to cash out **{cur:,}**."
    )
    embed.set_footer(text=f"Idle {MN_IDLE_TIMEOUT // 60} min = auto cash out")
    return embed

class MineButton(discord.ui.Button):
    def __init__(self, game: dict, idx: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="?", row=idx // MN_COLS)
        self.game = game
        self.idx = idx

    async def callback(self, interaction: discord.Interaction):
        await _mn_click(self.game, self, interaction)

class MinesView(discord.ui.View):
    def __init__(self, game: dict):
        super().__init__(timeout=None)
        self.buttons = []
        for i in range(MN_SIZE):
            b = MineButton(game, i)
            self.buttons.append(b)
            self.add_item(b)

def _mn_style_safe(button: MineButton):
    button.label = None
    button.emoji = MN_SAFE_EMOJI
    button.style = discord.ButtonStyle.success
    button.disabled = True

def _mn_reveal_board(game: dict, lost: bool):
    """Disables every button and shows what was hiding under them."""
    for b in game["view"].buttons:
        b.disabled = True
        b.label = None
        if b.idx in game["mine_set"]:
            b.emoji = MN_MINE_EMOJI
            b.style = discord.ButtonStyle.danger if lost else discord.ButtonStyle.secondary
        elif b.idx in game["revealed"]:
            b.emoji = MN_SAFE_EMOJI
            b.style = discord.ButtonStyle.success
        else:
            b.emoji = MN_SAFE_EMOJI
            b.style = discord.ButtonStyle.secondary

def _mn_cleanup(game: dict):
    game["state"] = "done"
    if game["message"]:
        MINES.pop(game["message"].id, None)
    MINES_ACTIVE.discard(game["creator_id"])
    task = game.get("task")
    if task and task is not asyncio.current_task():
        task.cancel()

def _mn_reset_timer(game: dict):
    old = game.get("task")
    if old and old is not asyncio.current_task():
        old.cancel()
    game["task"] = asyncio.create_task(_mn_idle(game))

async def _mn_idle(game: dict):
    await asyncio.sleep(MN_IDLE_TIMEOUT)
    if game["state"] == "active" and not game["busy"]:
        game["busy"] = True
        await _mn_finish(game, "idle")

async def _mn_pick_expire(game: dict):
    await asyncio.sleep(MN_PICK_TIMEOUT)
    if game["state"] != "pick" or game["busy"]:
        return
    _mn_cleanup(game)
    await adjust_balance(game["sid"], game["amount"])
    try:
        await game["message"].edit(embed=_mn_embed(
            game, f"⌛ No mine count chosen — <@{game['creator_id']}>'s bet of **{game['amount']:,}** was refunded.",
            discord.Color.greyple()))
        await game["message"].clear_reactions()
    except Exception as e:
        print(f"[mines] pick expire: {e}")

async def _mn_finish(game: dict, kind: str, interaction: discord.Interaction | None = None):
    """kind: 'cash' | 'idle' | 'perfect' (all pay out the pot) or 'lost'."""
    _mn_cleanup(game)
    k = len(game["revealed"])

    payout = 0
    if kind != "lost":
        payout = _mn_pot(game)
        await adjust_balance(game["sid"], payout)
    net = payout - game["amount"]

    if kind == "lost":
        status = f"💥 **BOOM!** You hit a mine and lose **{game['amount']:,}**."
        color = discord.Color.red()
    else:
        prefix = {"cash": "💰 **Cashed out**", "idle": "⌛ **Idle — auto cashed out**",
                  "perfect": "🏆 **Board cleared — cashed out**"}[kind]
        status = (f"{prefix} **{payout:,}** after {k} safe tile{'s' if k != 1 else ''} "
                  f"({_mn_fmt(_mn_mult(game['mines'], k))}, net {net:+,}).")
        color = discord.Color.green() if net > 0 else discord.Color.light_grey()

    _mn_reveal_board(game, lost=(kind == "lost"))
    embed = _mn_embed(game, final_status=status, color=color)
    game["view"].stop()
    try:
        if interaction is not None and not interaction.response.is_done():
            await interaction.response.edit_message(embed=embed, view=game["view"])
        else:
            await game["message"].edit(embed=embed, view=game["view"])
        await game["message"].clear_reactions()
    except Exception as e:
        print(f"[mines] finish: {e}")

async def _mn_click(game: dict, button: MineButton, interaction: discord.Interaction):
    if interaction.user.id != game["creator_id"]:
        await interaction.response.send_message("❌ This isn't your minefield.", ephemeral=True)
        return
    if game["state"] != "active" or game["busy"] or button.idx in game["revealed"]:
        await interaction.response.defer()
        return

    game["busy"] = True
    try:
        if button.idx in game["mine_set"]:
            await _mn_finish(game, "lost", interaction)
            return

        game["revealed"].add(button.idx)
        _mn_style_safe(button)
        if len(game["revealed"]) >= MN_SIZE - game["mines"]:
            await _mn_finish(game, "perfect", interaction)
            return

        await interaction.response.edit_message(embed=_mn_embed(game), view=game["view"])
        _mn_reset_timer(game)
    finally:
        if game["state"] == "active":
            game["busy"] = False

@bot.command(name="mines", aliases=["minefield"])
async def mines_command(ctx, amount: int):
    """!mines <amount> (alias !minefield) — play the minefield."""
    if amount <= 0:
        await ctx.send("❌ Amount must be a positive whole number.")
        return
    sid = steam_id_for_discord(ctx.author.id)
    if not sid:
        await ctx.send("❌ You don't have a linked tracked player, so you have no balance.")
        return
    if ctx.author.id in MINES_ACTIVE:
        await ctx.send("❌ You already have a minefield going — finish it first.")
        return

    MINES_ACTIVE.add(ctx.author.id)   # claim the slot before any await
    if await adjust_balance(sid, -amount) is None:
        MINES_ACTIVE.discard(ctx.author.id)
        await ctx.send(f"❌ Not enough balance — you have **{PLAYER_BALANCE.get(sid, 0):,}**.")
        return

    game = {
        "creator_id": ctx.author.id, "sid": sid, "amount": amount,
        "state": "pick", "busy": False, "message": None, "task": None, "view": None,
        "mines": None, "mine_set": set(), "revealed": set(),
        "pick_ts": int(datetime.now(timezone.utc).timestamp()) + MN_PICK_TIMEOUT,
    }
    try:
        msg = await ctx.send(embed=_mn_embed(game))
        game["message"] = msg
        MINES[msg.id] = game
        for e in (*MN_NUM_EMOJI, MN_CANCEL):
            await msg.add_reaction(e)
    except Exception as e:
        # Couldn't post the game — give the bet back.
        print(f"[mines] open failed: {e}")
        MINES.pop(getattr(game["message"], "id", None), None)
        MINES_ACTIVE.discard(ctx.author.id)
        await adjust_balance(sid, amount)
        return
    game["task"] = asyncio.create_task(_mn_pick_expire(game))

@bot.listen('on_raw_reaction_add')
async def mines_reactions(payload):
    if payload.user_id == bot.user.id:
        return
    game = MINES.get(payload.message_id)
    if not game:
        return
    msg = game["message"]
    try:
        await msg.remove_reaction(payload.emoji, discord.Object(id=payload.user_id))
    except Exception:
        pass

    if payload.user_id != game["creator_id"]:
        return
    if game["busy"] or game["state"] not in ("pick", "active"):
        return
    emoji = str(payload.emoji)

    # ── Stage 1: choose the number of mines ───────────────────────
    if game["state"] == "pick":
        if emoji == MN_CANCEL:
            game["busy"] = True
            _mn_cleanup(game)
            await adjust_balance(game["sid"], game["amount"])
            try:
                await msg.edit(embed=_mn_embed(game, "🚫 Cancelled — bet refunded.", discord.Color.greyple()))
                await msg.clear_reactions()
            except Exception as e:
                print(f"[mines] cancel: {e}")
        elif emoji in MN_NUM_EMOJI:
            game["busy"] = True
            game["mines"] = MN_NUM_EMOJI.index(emoji) + 1
            game["mine_set"] = set(random.sample(range(MN_SIZE), game["mines"]))
            game["view"] = MinesView(game)
            game["state"] = "active"
            try:
                await msg.clear_reactions()
                await msg.edit(embed=_mn_embed(game), view=game["view"])
                await msg.add_reaction(MN_CASH)
            except Exception as e:
                print(f"[mines] start: {e}")
            _mn_reset_timer(game)   # replaces the pick-expiry timer
            game["busy"] = False
        return

    # ── Stage 2: cash out ─────────────────────────────────────────
    if emoji == MN_CASH:
        game["busy"] = True
        await _mn_finish(game, "cash")

# =================================================================
# 10.95 HELP
# =================================================================
# !help / !commands show a different message depending on the channel.
# (Testing/maintenance commands such as !weeklyrecap, !testgetmatch,
# !syncranks, !catchup, !getmatch and !lastmatch are intentionally not listed.)

HELP_GAMBLING = """🎰 **Gambling Commands**

💰 **Points**
`!balance [@user]` (`!bal`) — check your points or someone else's
`!transfer <amount> @user` — send points to another player

Earn points every Sunday: +1 per kill, +5 per top frag, +50 for the best kills/game (min. 3 games).

🎲 **Games**
`!coinflip <amount>` (`!cf`) — pick heads or tails, then another player takes the other side or you flip vs the bot
`!higherlower <amount>` (`!hilo`) — guess higher/lower on the next card; each correct guess raises the multiplier, ✅ cash out any time
`!blackjack <amount>` (`!bj`) — 🃏 hit · ✋ stand · 💰 double down; blackjack pays 2:1
`!roulette <amount> <red|black|green>` (`!rl`) — everyone in the channel bets on one shared wheel; red/black pay 1:1, green pays 14:1, and a 0 (green) beats red/black
`!russianroulette <amount>` (`!rr`, `!russian`) — 🔫 duel another player to the death, or play vs the revolver with rising payouts
`!minefield <amount>` (`!mines`) — pick 1-9 mines with a reaction, then click tiles on a 5×5 grid; every safe tile raises your payout, react ✅ to cash out

ℹ️ Bets are taken when you place them, payouts are rounded down to whole points, and you need a linked account to play."""

HELP_LEETIFY = """📊 **Tracking Players**

`!addplayer <steam64id> [@discord_user] <display name>` — start tracking a player (the @mention is optional, but needed for rank roles and the casino)
`!removeplayer <steam64id>` — stop tracking a player
`!players` — list everyone being tracked

Your Steam64 ID is the 17-digit number starting with 7656… (find it on steamid.io)."""

HELP_BOT_CHANNEL = """🤖 **Bot Commands**

📊 **Stats**
`!players` — list tracked players
`!stats <steam64id>` — show a player's stat card

💰 **Economy**
`!balance [@user]` (`!bal`) — check your points
`!transfer <amount> @user` — send points to another player

🎰 Games: see #gambling-commands
📋 Tracking players: see #leetify"""

HELP_BY_CHANNEL = {
    "gambling-commands": HELP_GAMBLING,
    "leetify":           HELP_LEETIFY,
    "bot-channel":       HELP_BOT_CHANNEL,
}

@bot.command(name="help", aliases=["commands"])
async def help_command(ctx):
    """!help / !commands — show the commands that are relevant for this channel."""
    text = HELP_BY_CHANNEL.get(getattr(ctx.channel, "name", ""))
    if text is None:
        text = "Use `!help` in #gambling-commands, #leetify or #bot-channel."
    await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

# =================================================================
# 11. ON READY
# =================================================================

@bot.event
async def on_ready():
    global TRACKED_PLAYERS, PLAYER_DISCORD_IDS, PLAYER_RANK_BRACKET, PLAYER_BALANCE
    print(f"✅ Logged in as {bot.user}")
    TRACKED_PLAYERS, PLAYER_DISCORD_IDS, PLAYER_RANK_BRACKET, PLAYER_BALANCE = await asyncio.to_thread(db_load_tracked)
    print(f"📋 Loaded {len(TRACKED_PLAYERS)} tracked player(s) from Supabase "
          f"({len(PLAYER_DISCORD_IDS)} linked to Discord).")

    # Catch up on any matches (including Faceit games) that happened while the
    # bot was offline, and re-sync rank roles, before starting the regular
    # periodic checker. (Same flow the nightly midnight_catchup_task runs.)
    for guild in bot.guilds:
        await run_automatic_catchup(guild)

    check_leetify_stats.start()
    reset_poll_at_3am.start()
    weekly_recap_task.start()
    midnight_catchup_task.start()

# =================================================================
# 12. RUN
# =================================================================

keep_alive()
bot.run(os.environ.get("DISCORD_TOKEN"))
