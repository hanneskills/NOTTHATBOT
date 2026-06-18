import os
import re
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

    name         = data.get("name", steam_id)
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
        e2.set_footer(text="LTF = Leetify rating ×100 · K = Kills · D = Deaths")
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
        steam_id = resolve_vanity_url(vanity)
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
        data = fetch_profile(steam_id)
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

        profile_matches = fetch_profile_matches(steam_id)
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

        embed = discord.Embed(
            title=f"🎯  {map_name}  —  CT {s_ct} : {s_t} T",
            description=(
                f"📅 {game_date}  ·  "
                f"[View on Leetify](https://leetify.com/app/match-details/{match_id})"
            ),
            color=discord.Color.gold()
        )

        all_stats  = sorted(
            match_data.get("stats", []),
            key=lambda p: p.get("leetify_rating", 0) or 0,
            reverse=True
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
                name       = TRACKED_PLAYERS.get(sid) or p.get("name") or sid
                display    = (name[:13] + "⭐" if is_tracked else name[:14]).ljust(16)
                k      = p.get("total_kills", 0)
                d      = p.get("total_deaths", 0)
                damage = p.get("total_damage", 0)
                rounds = p.get("rounds_count", 1)
                adr    = round(damage / rounds, 1) if rounds else 0
                rating = p.get("leetify_rating", None)
                hs_pct = round((p.get("total_hs_kills", 0) / k * 100)) if k else 0
                rtg    = f"{rating * 100:.1f}" if rating is not None else "—"
                lines.append("`{:<16} {:>3} {:>3} {:>5} {:>3}% {:>8}`".format(
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


@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY or not TRACKED_PLAYERS:
        return

    seen_this_tick = set()

    for steam_id in list(TRACKED_PLAYERS.keys()):
        try:
            res = requests.get(
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
                    match_data = fetch_full_match(latest_id)
                    if match_data:
                        embed = build_match_embed(match_data)
                        if embed:
                            for guild in bot.guilds:
                                channel = discord.utils.get(guild.text_channels, name="leetify")
                                if channel:
                                    await channel.send(embed=embed)

        except Exception as e:
            print(f"[check_leetify_stats] {steam_id}: {e}")


# =================================================================
# 9. COMMANDS
# =================================================================

@bot.command(name="addplayer")
async def add_player(ctx, steam_id: str, *, display_name: str):
    """!addplayer <steam64id> <display name>"""
    if not STEAMID64_RE.fullmatch(steam_id):
        await ctx.send("❌ Invalid Steam64 ID (17 digits starting with 7656119...).")
        return
    TRACKED_PLAYERS[steam_id] = display_name
    ok = db_add_player(steam_id, display_name)
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
    ok = db_remove_player(steam_id)
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
    res = requests.get(
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
    match_data = fetch_full_match(match_id)
    if not match_data:
        await ctx.send("Could not fetch full match data.")
        return
    embed = build_match_embed(match_data)
    if embed:
        await ctx.send(embed=embed)
    else:
        await ctx.send("Could not parse match data.")

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
        data = fetch_profile(steam_id)
        if not data:
            await ctx.reply(
                f"❌ No Leetify data for `{steam_id}`.\n"
                "ℹ️ This player may not be registered on Leetify — the public API only returns stats for registered users."
            )
            return
        profile_matches = fetch_profile_matches(steam_id)
        embeds = build_profile_embeds(data, steam_id, profile_matches)
        await ctx.reply(embeds=embeds)

# =================================================================
# 10. ON READY
# =================================================================

@bot.event
async def on_ready():
    global TRACKED_PLAYERS
    print(f"✅ Logged in as {bot.user}")
    TRACKED_PLAYERS = db_load_tracked()
    print(f"📋 Loaded {len(TRACKED_PLAYERS)} tracked player(s) from Supabase.")
    check_leetify_stats.start()
    reset_poll_at_3am.start()

# =================================================================
# 11. RUN
# =================================================================

keep_alive()
bot.run(os.environ.get("DISCORD_TOKEN"))
