import os
import re
import unicodedata
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
bot = commands.Bot(command_prefix="!", intents=intents)

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
    try:
        res = requests.get(
            f"{SUPABASE_URL}/rest/v1/tracked_players?select=steam_id,display_name",
            headers=get_supabase_headers(), timeout=10
        )
        if res.status_code == 200:
            return {row["steam_id"]: row["display_name"] for row in res.json()}
        print(f"[Supabase] Failed to load: {res.status_code} {res.text}")
        return {}
    except Exception as e:
        print(f"[Supabase] db_load_tracked error: {e}")
        return {}

def db_add_player(steam_id, display_name):
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/tracked_players",
            headers={**get_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
            json={"steam_id": steam_id, "display_name": display_name}, timeout=10
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
    Looks for 'in 2 hours', 'in 20 minutes', 'in 1.5 hours'.
    Returns a UTC datetime if found, else None.
    """
    match = re.search(r'in\s+(\d+(?:\.\d+)?)\s*(hour|hr|minute|min)s?', text.lower())
    if not match:
        return None
    amount = float(match.group(1))
    unit   = match.group(2)
    delta  = timedelta(hours=amount) if unit in ("hour", "hr") else timedelta(minutes=amount)
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

    content = message.content.lower()
    if not ("game" in content or "playing" in content):
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
        return res.json() if res.status_code == 200 else None
    except Exception as e:
        print(f"[fetch_profile_matches] {e}")
        return None


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
    recent       = data.get("recent_matches", [])

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


def build_match_embed(match_data: dict) -> discord.Embed | None:
    """
    Scoreboard-style embed with monospace table.
    NAME             K   D   ADR  HS%  RTG
    """
    try:
        map_name  = match_data.get("map_name", "Unknown").replace("de_", "").title()
        match_id  = match_data.get("id", "")
        game_date = (match_data.get("finished_at") or match_data.get("game_finished_at") or "")[:10]

        team_scores = match_data.get("team_scores", [])
        s_ct = next((s.get("score", 0) for s in team_scores if s.get("team_number") == 3), 0)
        s_t  = next((s.get("score", 0) for s in team_scores if s.get("team_number") == 2), 0)

        # Determine result from tracked players' perspective.
        # Find which team number the majority of tracked players were on.
        all_stats = sorted(
            match_data.get("stats", []),
            key=lambda p: p.get("total_kills", 0) or 0,
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

            matches = res.json()
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

        except Exception as e:
            print(f"[check_leetify_stats] {steam_id}: {e}")


MAX_CATCHUP_MATCHES_PER_PLAYER = 15

async def catch_up_missed_matches(guild: discord.Guild, max_per_player: int = MAX_CATCHUP_MATCHES_PER_PLAYER) -> int:
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
    caught up on.

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
            if not mid:
                continue
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
                    deaths  = int(parts[-4])
                    kills   = int(parts[-5])
                    name    = " ".join(parts[:-5]).rstrip("⭐").strip()
                    players.append({
                        "name":   name,
                        "kills":  kills,
                        "deaths": deaths,
                        "rating": rating,   # already ×100 (displayed value)
                        "team":   team_label,
                    })
                except (ValueError, IndexError):
                    continue

        return {
            "map":     map_name,
            "score_ct": s_ct,
            "score_t":  s_t,
            "players":  players,
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


async def build_weekly_recap(channel: discord.TextChannel, weeks_ago: int = 0) -> list[discord.Embed] | None:
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
            if parsed:
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

        # Only count tracked players for leaderboard
        tracked_names = set(TRACKED_PLAYERS.values())
        tracked_in_match = [
            p for p in players
            if p["name"].rstrip("⭐").strip() in tracked_names
        ]
        if not tracked_in_match:
            tracked_in_match = players

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
                    player_stats[n] = {"kills": 0, "deaths": 0, "games": 0, "topfrags": 0, "bottomfrags": 0}
                player_stats[n]["kills"]  += p["kills"]
                player_stats[n]["deaths"] += p["deaths"]
                player_stats[n]["games"]  += 1
                k = p.get("kills")
                if k is not None and k == most_kills:
                    player_stats[n]["topfrags"] += 1
                if k is not None and k == fewest_kills:
                    player_stats[n]["bottomfrags"] += 1

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

        maps_embed.add_field(name="🗺️ Maps Played", value="\n".join(map_lines), inline=False)

    maps_embed.set_footer(text=footer_text)

    embeds = [maps_embed]

    if player_stats:
        # ── Message 2: Kill leaderboard ────────────────────────────
        kills_embed = discord.Embed(
            title="🔫 Weekly Kill Leaderboard",
            color=discord.Color.red()
        )

        sorted_by_kills = sorted(
            player_stats.items(),
            key=lambda x: x[1]["kills"],
            reverse=True
        )

        header = f"`{_pad('PLAYER', 16)}{_rpad('KILLS', 6)}{_rpad('GAMES', 6)}{_rpad('K/G', 6)}`"
        lb_lines = [header]
        for name, s in sorted_by_kills:
            kpg = f"{s['kills'] / s['games']:.1f}" if s["games"] > 0 else "0.0"
            lb_lines.append(
                f"`{_pad(name, 16)}{_rpad(s['kills'], 6)}{_rpad(s['games'], 6)}{_rpad(kpg, 6)}`"
            )

        kills_embed.add_field(name="Kills — most to least", value="\n".join(lb_lines), inline=False)
        kills_embed.set_footer(text=footer_text)
        embeds.append(kills_embed)

        # ── Message 3: Top / bottom frag leaderboard ───────────────
        frags_embed = discord.Embed(
            title="🎯 Top & Bottom Frags",
            color=discord.Color.gold()
        )

        sorted_top = sorted(
            (p for p in player_stats.items() if p[1]["topfrags"] > 0),
            key=lambda x: x[1]["topfrags"],
            reverse=True
        )
        sorted_bottom = sorted(
            (p for p in player_stats.items() if p[1]["bottomfrags"] > 0),
            key=lambda x: x[1]["bottomfrags"],
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
            frags_embed.add_field(name="👑 Most Top Frags", value="\n".join(top_lines), inline=False)

        if sorted_bottom:
            header = f"`{_pad('PLAYER', 16)}{_rpad('×', 4)}{_rpad('GAMES', 6)}{_rpad('%', 5)}`"
            bottom_lines = [header]
            for name, s in sorted_bottom:
                pct = f"{round(s['bottomfrags'] / s['games'] * 100)}%" if s["games"] > 0 else "-"
                bottom_lines.append(
                    f"`{_pad(name, 16)}{_rpad(s['bottomfrags'], 4)}{_rpad(s['games'], 6)}{_rpad(pct, 5)}`"
                )
            frags_embed.add_field(name="💀 Most Bottom Frags", value="\n".join(bottom_lines), inline=False)

        frags_embed.set_footer(text=footer_text)
        embeds.append(frags_embed)

    return embeds


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
            embeds = await build_weekly_recap(leetify_channel)
            if embeds:
                for e in embeds:
                    await weekly_channel.send(embed=e)
            else:
                await weekly_channel.send("📅 Weekly recap: no matches tracked this week.")
        except Exception as e:
            print(f"[weekly_recap_task] {guild.name}: {e}")


# =================================================================
# 10. COMMANDS
# =================================================================

@bot.command(name="addplayer")
async def add_player(ctx, steam_id: str, *, display_name: str):
    """!addplayer <steam64id> <display name>"""
    if not STEAMID64_RE.fullmatch(steam_id):
        await ctx.send("❌ Invalid Steam64 ID (17 digits starting with 7656119...).")
        return
    TRACKED_PLAYERS[steam_id] = display_name
    ok = await asyncio.to_thread(db_add_player, steam_id, display_name)
    if ok:
        await ctx.send(f"✅ Now tracking **{display_name}** (`{steam_id}`).")
    else:
        await ctx.send("⚠️ Saved in memory but Supabase write failed — check credentials.")

@bot.command(name="removeplayer")
async def remove_player(ctx, steam_id: str):
    """!removeplayer <steam64id>"""
    if steam_id not in TRACKED_PLAYERS:
        await ctx.send("That Steam ID isn't being tracked.")
        return
    name = TRACKED_PLAYERS.pop(steam_id)
    ok = await asyncio.to_thread(db_remove_player, steam_id)
    if ok:
        await ctx.send(f"🗑️ Removed **{name}** from tracking.")
    else:
        await ctx.send(f"Removed **{name}** from memory but Supabase delete failed.")

@bot.command(name="players")
async def list_players(ctx):
    """!players — list all tracked players"""
    if not TRACKED_PLAYERS:
        await ctx.send("No players tracked yet. Use `!addplayer <steam64id> <name>`.")
        return
    lines = [f"• **{name}** — `{sid}`" for sid, name in TRACKED_PLAYERS.items()]
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
    matches = res.json()
    if not matches:
        await ctx.send("No matches found.")
        return
    match_id   = matches[0].get("id")
    match_data = await asyncio.to_thread(fetch_full_match, match_id)
    if not match_data:
        await ctx.send("Could not fetch full match data.")
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
    """!catchup — check every tracked player's recent matches (including Faceit games
    synced through Leetify) and post any that are missing from #leetify, e.g. games
    that happened while the bot was down."""
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
        posted = await catch_up_missed_matches(ctx.guild)
    if posted:
        await ctx.send(f"✅ Caught up — posted {posted} missing match{'es' if posted != 1 else ''} in {leetify_channel.mention}.")
    else:
        await ctx.send("👍 Nothing missing — all recent matches are already posted.")

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
            for e in embeds:
                await weekly_channel.send(embed=e)
            if ctx.channel != weekly_channel:
                await ctx.send(f"✅ Weekly recap ({when_text}) posted in {weekly_channel.mention}.")
        else:
            await ctx.send(f"📅 No matches found in `#leetify` for {when_text}.")


# =================================================================
# 11. ON READY
# =================================================================

@bot.event
async def on_ready():
    global TRACKED_PLAYERS
    print(f"✅ Logged in as {bot.user}")
    TRACKED_PLAYERS = await asyncio.to_thread(db_load_tracked)
    print(f"📋 Loaded {len(TRACKED_PLAYERS)} tracked player(s) from Supabase.")

    # Catch up on any matches (including Faceit games) that happened while the
    # bot was offline, before starting the regular periodic checker.
    for guild in bot.guilds:
        try:
            posted = await catch_up_missed_matches(guild)
            if posted:
                print(f"[catch_up_missed_matches] Posted {posted} missed match(es) in {guild.name}.")
        except Exception as e:
            print(f"[catch_up_missed_matches] {guild.name}: {e}")

    check_leetify_stats.start()
    reset_poll_at_3am.start()
    weekly_recap_task.start()

# =================================================================
# 12. RUN
# =================================================================

keep_alive()
bot.run(os.environ.get("DISCORD_TOKEN"))
