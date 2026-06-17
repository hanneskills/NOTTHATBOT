import os
import discord
import requests
import re
from discord.ext import commands, tasks
from threading import Thread
from flask import Flask

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

active_signups = {}
ROLE_NAME = "gamer"

# =================================================================
# 3. SUPABASE SETUP
# =================================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")  # https://xxxx.supabase.co  (no trailing slash)
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")  # eyJ... anon/public key

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
            headers=get_supabase_headers(),
            timeout=10
        )
        if res.status_code == 200:
            return {row["steam_id"]: row["display_name"] for row in res.json()}
        else:
            print(f"[Supabase] Failed to load players: {res.status_code} {res.text}")
            return {}
    except Exception as e:
        print(f"[Supabase] db_load_tracked error: {e}")
        return {}

def db_add_player(steam_id, display_name):
    try:
        res = requests.post(
            f"{SUPABASE_URL}/rest/v1/tracked_players",
            headers={**get_supabase_headers(), "Prefer": "resolution=merge-duplicates"},
            json={"steam_id": steam_id, "display_name": display_name},
            timeout=10
        )
        return res.status_code in (200, 201)
    except Exception as e:
        print(f"[Supabase] db_add_player error: {e}")
        return False

def db_remove_player(steam_id):
    try:
        res = requests.delete(
            f"{SUPABASE_URL}/rest/v1/tracked_players?steam_id=eq.{steam_id}",
            headers=get_supabase_headers(),
            timeout=10
        )
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"[Supabase] db_remove_player error: {e}")
        return False

# =================================================================
# 4. DISCORD FEATURES (Signups, Reactions, Voice Roles)
# =================================================================

@bot.listen('on_message')
async def handle_game_signups(message):
    if message.author == bot.user: return
    if message.content.startswith("!"): return
    content = message.content.lower()
    if "game" in content or "playing" in content:
        embed = discord.Embed(
            title="🎮 Who's playing tonight?",
            description="Click the **✅** reaction below to join the squad!",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Players Joined:", value="*No one yet...*", inline=False)
        signup_message = await message.channel.send(embed=embed)
        await signup_message.add_reaction("✅")
        active_signups[signup_message.id] = set()

@bot.event
async def on_reaction_add(reaction, user):
    if user == bot.user: return
    if reaction.message.id in active_signups and str(reaction.emoji) == "✅":
        player_ids = active_signups[reaction.message.id]
        if user.id not in player_ids:
            player_ids.add(user.id)
            await update_signup_embed(reaction.message, player_ids)

@bot.event
async def on_reaction_remove(reaction, user):
    if user == bot.user: return
    if reaction.message.id in active_signups and str(reaction.emoji) == "✅":
        player_ids = active_signups[reaction.message.id]
        if user.id in player_ids:
            player_ids.remove(user.id)
            await update_signup_embed(reaction.message, player_ids)

async def update_signup_embed(message, player_ids):
    embed = message.embeds[0]
    player_mentions = "\n".join([f"• <@{uid}>" for uid in player_ids]) if player_ids else "*No one yet...*"
    embed.set_field_at(0, name="Players Joined:", value=player_mentions, inline=False)
    await message.edit(embed=embed)

@bot.event
async def on_voice_state_update(member, before, after):
    gamer_role = discord.utils.get(member.guild.roles, name=ROLE_NAME)
    if not gamer_role: return
    if before.channel is None and after.channel is not None:
        await member.add_roles(gamer_role)
    elif before.channel is not None and after.channel is None:
        await member.remove_roles(gamer_role)

# =================================================================
# 5. LEETIFY INTEGRATION
# =================================================================

LEETIFY_API_KEY   = os.environ.get('LEETIFY_API_KEY')
LEETIFY_HEADERS   = {"_leetify_key": LEETIFY_API_KEY} if LEETIFY_API_KEY else {}
LEETIFY_BASE      = "https://api-public.cs-prod.leetify.com"

STEAMID64_RE      = re.compile(r'\b(7656119\d{10})\b')
last_seen_matches = {}
TRACKED_PLAYERS   = {}  # loaded from Supabase on startup


def fetch_full_match(match_id):
    """Fetch all 10 players from /v2/matches/{id}."""
    try:
        res = requests.get(
            f"{LEETIFY_BASE}/v2/matches/{match_id}",
            headers=LEETIFY_HEADERS,
            timeout=10
        )
        if res.status_code == 200:
            return res.json()
        else:
            print(f"[fetch_full_match] Status {res.status_code} for match {match_id}")
            return None
    except Exception as e:
        print(f"[fetch_full_match] Error: {e}")
        return None


def build_match_embed(match_data):
    """Build a full 10-player leaderboard embed, with tracked players starred."""
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
                f"📅 {game_date}\n"
                f"[View full match on Leetify](https://leetify.com/app/match-details/{match_id})"
            ),
            color=discord.Color.gold()
        )

        # Sort all players by Leetify rating descending
        all_stats = sorted(
            match_data.get("stats", []),
            key=lambda p: p.get("leetify_rating", 0) or 0,
            reverse=True
        )

        ct_rows = []
        t_rows  = []

        for p in all_stats:
            sid        = str(p.get("steam64_id", ""))
            is_tracked = sid in TRACKED_PLAYERS
            name       = TRACKED_PLAYERS.get(sid) or p.get("name") or sid

            k          = p.get("total_kills", 0)
            d          = p.get("total_deaths", 0)
            damage     = p.get("total_damage", 0)
            rounds     = p.get("rounds_count", 1)
            adr        = round(damage / rounds, 1) if rounds else 0
            rating     = p.get("leetify_rating", None)
            hs_pct     = round((p.get("total_hs_kills", 0) / k * 100)) if k else 0
            rating_str = f"{rating * 100:.2f}" if rating is not None else "—"

            prefix = "⭐ " if is_tracked else "　 "
            row = f"{prefix}**{name}** — K/D: `{k}/{d}` · ADR: `{adr}` · HS%: `{hs_pct}%` · Rating: `{rating_str}`"

            team = p.get("initial_team_number")
            if team == 3:
                ct_rows.append(row)
            else:
                t_rows.append(row)

        if ct_rows:
            embed.add_field(name="🔵 CT Side", value="\n".join(ct_rows), inline=False)
        if t_rows:
            embed.add_field(name="🟡 T Side", value="\n".join(t_rows), inline=False)

        return embed

    except Exception as e:
        print(f"[build_match_embed] {e}")
        return None


# --- Auto-detect Steam ID pasted in any channel ---
@bot.listen('on_message')
async def handle_steamid_lookup(message):
    if message.author == bot.user: return
    if message.content.startswith("!"): return

    match = STEAMID64_RE.search(message.content)
    if not match: return

    steam_id = match.group(1)

    if not LEETIFY_API_KEY:
        await message.channel.send("⚠️ `LEETIFY_API_KEY` is not set.")
        return

    async with message.channel.typing():
        try:
            # Step 1: get the latest match ID for this player
            res = requests.get(
                f"{LEETIFY_BASE}/v3/profile/matches",
                headers=LEETIFY_HEADERS,
                params={"steam64_id": steam_id},
                timeout=10
            )
            if res.status_code != 200:
                await message.channel.send(f"❌ Leetify returned `{res.status_code}` for that Steam ID.")
                return

            matches = res.json()
            if not matches:
                await message.channel.send("No recent matches found for that Steam ID.")
                return

            # Step 2: fetch the full 10-player match
            match_id   = matches[0].get("id")
            match_data = fetch_full_match(match_id)
            if not match_data:
                await message.channel.send("Could not fetch full match data.")
                return

            embed = build_match_embed(match_data)
            if embed:
                await message.channel.send(content=f"📊 Latest match for `{steam_id}`:", embed=embed)
            else:
                await message.channel.send("Could not parse match data.")

        except Exception as e:
            print(f"[handle_steamid_lookup] {e}")
            await message.channel.send("⚠️ Something went wrong fetching stats.")


# --- Manage tracked players ---
@bot.command(name="addplayer")
@commands.has_permissions(manage_guild=True)
async def add_player(ctx, steam_id: str, *, display_name: str):
    """!addplayer <steam64id> <display name>"""
    if not STEAMID64_RE.fullmatch(steam_id):
        await ctx.send("❌ Invalid Steam64 ID (should be 17 digits starting with 7656119...).")
        return
    TRACKED_PLAYERS[steam_id] = display_name
    ok = db_add_player(steam_id, display_name)
    if ok:
        await ctx.send(f"✅ Now tracking **{display_name}** (`{steam_id}`).")
    else:
        await ctx.send("⚠️ Saved in memory but Supabase write failed — check your credentials.")

@bot.command(name="removeplayer")
@commands.has_permissions(manage_guild=True)
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
        await ctx.send(f"Removed **{name}** from memory, but Supabase delete failed.")

@bot.command(name="players")
async def list_players(ctx):
    """!players — list all tracked players"""
    if not TRACKED_PLAYERS:
        await ctx.send("No players tracked yet. Use `!addplayer <steam64id> <name>`.")
        return
    lines = [f"• **{name}** — `{sid}`" for sid, name in TRACKED_PLAYERS.items()]
    await ctx.send("**Tracked players:**\n" + "\n".join(lines))

@bot.command(name="lastmatch")
@commands.has_permissions(manage_guild=True)
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


# --- Periodic match checker (every 2 min) ---
@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY or not TRACKED_PLAYERS:
        return

    seen_this_tick = set()  # prevent duplicate posts for the same match

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
                last_seen_matches[steam_id] = latest_id  # first run, just record
            elif latest_id != last_seen_matches[steam_id]:
                last_seen_matches[steam_id] = latest_id

                if latest_id not in seen_this_tick:
                    seen_this_tick.add(latest_id)

                    # Fetch the full 10-player match data
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
# 6. ON READY
# =================================================================

@bot.event
async def on_ready():
    global TRACKED_PLAYERS
    print(f"✅ Logged in as {bot.user}")
    TRACKED_PLAYERS = db_load_tracked()
    print(f"📋 Loaded {len(TRACKED_PLAYERS)} tracked player(s) from Supabase.")
    check_leetify_stats.start()


# =================================================================
# 7. RUN
# =================================================================

keep_alive()
bot.run(os.environ.get("DISCORD_TOKEN"))
