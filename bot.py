import os
import discord
import requests
import re
from discord.ext import commands, tasks
from threading import Thread
from flask import Flask

# =================================================================
# 1. DISCORD FEATURES (Signups, Reactions, Voice Roles)
# =================================================================

# --- MINI WEB SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
def run_web_server(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run_web_server).start()

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
active_signups = {}
ROLE_NAME = "gamer"

@bot.listen('on_message')
async def handle_game_signups(message):
    if message.author == bot.user: return
    content = message.content.lower()
    if "game" in content or "playing" in content:
        embed = discord.Embed(title="🎮 Who's playing tonight?", description="Click the **✅** reaction below to join the squad!", color=discord.Color.blurple())
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
    player_mentions = "\n".join([f"• <@{user_id}>" for user_id in player_ids]) if player_ids else "*No one yet...*"
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
# LEETIFY INTEGRATION & STATS
# =================================================================

import json

LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')
LEETIFY_HEADERS = {"_leetify_key": LEETIFY_API_KEY} if LEETIFY_API_KEY else {}
LEETIFY_BASE = "https://api-public.cs-prod.leetify.com"

# Persisted tracked players: { steam64_id: display_name }
TRACKED_FILE = "tracked_players.json"
last_seen_matches = {}

def load_tracked():
    if os.path.exists(TRACKED_FILE):
        with open(TRACKED_FILE) as f:
            return json.load(f)
    return {}

def save_tracked(data):
    with open(TRACKED_FILE, "w") as f:
        json.dump(data, f)

TRACKED_PLAYERS = load_tracked()

# --- Steam ID validator (accepts 17-digit numeric IDs only) ---
STEAMID64_RE = re.compile(r'\b(7656119\d{10})\b')


def build_match_embed(match_data, tracked_only=True):
    """
    Build a Discord Embed from a Leetify match dict.
    tracked_only=True  → only show players in TRACKED_PLAYERS
    tracked_only=False → show all players in the match
    """
    try:
        map_name  = match_data.get("map_name", "Unknown").replace("de_", "").title()
        match_id  = match_data.get("id", "")
        game_date = match_data.get("game_finished_at", "")[:10] if match_data.get("game_finished_at") else ""

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

        stats = match_data.get("stats", [])
        rows  = []

        for p in stats:
            sid  = str(p.get("steam64_id", ""))
            name = TRACKED_PLAYERS.get(sid) if tracked_only else None
            if tracked_only and name is None:
                continue                          # skip non-tracked players
            if not tracked_only:
                name = p.get("name") or p.get("steam64_id", "Unknown")

            k    = p.get("total_kills",   0)
            d    = p.get("total_deaths",  0)
            adr  = p.get("adr",           0)      # average damage per round
            aim  = p.get("aim_rating",    None)   # Leetify aim rating
            util = p.get("utility_rating",None)   # Leetify utility rating

            aim_str  = f"{aim:.1f}"  if aim  is not None else "—"
            util_str = f"{util:.1f}" if util is not None else "—"
            adr_str  = f"{adr:.1f}"  if isinstance(adr, float) else str(adr)

            rows.append(
                f"**{name}**\n"
                f"  K/D: `{k}/{d}` · ADR: `{adr_str}` · Aim: `{aim_str}` · Util: `{util_str}`"
            )

        if rows:
            label = "Squad Performance" if tracked_only else "Player Stats"
            embed.add_field(name=label, value="\n".join(rows), inline=False)
        elif tracked_only:
            embed.add_field(name="Squad Performance", value="*None of your tracked players were in this match.*", inline=False)

        return embed

    except Exception as e:
        print(f"[build_match_embed] Error: {e}")
        return None


# --- Auto-detect Steam ID pasted in any channel ---
@bot.listen('on_message')
async def handle_steamid_lookup(message):
    if message.author == bot.user:
        return

    match = STEAMID64_RE.search(message.content)
    if not match:
        return

    steam_id = match.group(1)

    if not LEETIFY_API_KEY:
        await message.channel.send("⚠️ `LEETIFY_API_KEY` is not set in environment variables.")
        return

    async with message.channel.typing():
        try:
            # Fetch the player's recent matches
            res = requests.get(
                f"{LEETIFY_BASE}/v3/profile/matches",
                headers=LEETIFY_HEADERS,
                params={"steam64_id": steam_id},
                timeout=10
            )
            if res.status_code != 200:
                await message.channel.send(f"❌ Leetify returned status `{res.status_code}` for that Steam ID.")
                return

            matches = res.json()
            if not matches:
                await message.channel.send("No recent matches found for that Steam ID.")
                return

            latest = matches[0]
            embed  = build_match_embed(latest, tracked_only=False)

            if embed:
                await message.channel.send(
                    content=f"📊 Latest match for `{steam_id}`:",
                    embed=embed
                )
            else:
                await message.channel.send("Could not parse match data.")

        except Exception as e:
            print(f"[handle_steamid_lookup] {e}")
            await message.channel.send("⚠️ Something went wrong fetching stats from Leetify.")


# --- Manage tracked players ---
@bot.command(name="addplayer")
@commands.has_permissions(manage_guild=True)
async def add_player(ctx, steam_id: str, *, display_name: str):
    """!addplayer <steam64id> <display name>  — start tracking a player."""
    if not STEAMID64_RE.fullmatch(steam_id):
        await ctx.send("❌ That doesn't look like a valid Steam64 ID (17-digit number starting with 7656119...).")
        return
    TRACKED_PLAYERS[steam_id] = display_name
    save_tracked(TRACKED_PLAYERS)
    await ctx.send(f"✅ Now tracking **{display_name}** (`{steam_id}`).")

@bot.command(name="removeplayer")
@commands.has_permissions(manage_guild=True)
async def remove_player(ctx, steam_id: str):
    """!removeplayer <steam64id>  — stop tracking a player."""
    if steam_id in TRACKED_PLAYERS:
        name = TRACKED_PLAYERS.pop(steam_id)
        save_tracked(TRACKED_PLAYERS)
        await ctx.send(f"🗑️ Removed **{name}** from tracking.")
    else:
        await ctx.send("That Steam ID isn't in the tracked list.")

@bot.command(name="players")
async def list_players(ctx):
    """!players  — list all tracked players."""
    if not TRACKED_PLAYERS:
        await ctx.send("No players are being tracked yet. Use `!addplayer <steam64id> <name>`.")
        return
    lines = [f"• **{name}** — `{sid}`" for sid, name in TRACKED_PLAYERS.items()]
    await ctx.send("**Tracked players:**\n" + "\n".join(lines))


# --- Periodic match checker (every 2 min) ---
@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY or not TRACKED_PLAYERS:
        return

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
                # First time seeing this player — just record, don't post
                last_seen_matches[steam_id] = latest_id
            elif latest_id != last_seen_matches[steam_id]:
                embed = build_match_embed(latest, tracked_only=True)
                if embed:
                    for guild in bot.guilds:
                        channel = discord.utils.get(guild.text_channels, name="leetify")
                        if channel:
                            await channel.send(embed=embed)
                last_seen_matches[steam_id] = latest_id

        except Exception as e:
            print(f"[check_leetify_stats] {steam_id}: {e}")


# --- Start the loop when the bot is ready ---
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    check_leetify_stats.start()
