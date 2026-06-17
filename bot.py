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
# 2. LEETIFY INTEGRATION & STATS
# =================================================================

import re

# --- CONFIGURATION ---
TRACKED_PLAYERS = {
    "76561198722789242": "Hanneskills",
}
LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')
last_seen_matches = {}

@bot.event
async def on_ready():
    print(f'⚡ Bot is online as {bot.user}')
    if not check_leetify_stats.is_running():
        check_leetify_stats.start()

# --- HELPER FUNCTIONS ---
def extract_steam_id(input_str):
    if input_str.isdigit() and len(input_str) == 17:
        return input_str
    match = re.search(r'(\d{17})', input_str)
    return match.group(1) if match else input_str

def process_match_data(match_data):
    if not isinstance(match_data, dict): return None

    map_name = match_data.get("map_name", "Unknown").replace("de_", "").title()
    team_scores = match_data.get('team_scores', [])
    
    # Use team_number (2 or 3) instead of 'faction'
    # Team 2 is usually T, Team 3 is usually CT
    score_2 = next((s['score'] for s in team_scores if s.get('team_number') == 2), 0)
    score_3 = next((s['score'] for s in team_scores if s.get('team_number') == 3), 0)
    
    embed = discord.Embed(
        title=f"🏆 Match Leaderboard: {map_name}",
        description=f"Final Score: **CT {score_3} - {score_2} T**\n[View on Leetify](https://leetify.com/app/match-details/{match_data.get('id')})",
        color=discord.Color.gold()
    )
    
    stats = sorted(match_data.get("stats", []), key=lambda x: x.get("total_kills", 0), reverse=True)
    
    board = ""
    for p in stats:
        name = p.get("name", "Unknown")
        k = p.get("total_kills", 0)
        d = p.get("total_deaths", 0)
        # Calculate ADR safely
        rounds = p.get("rounds_count", 1)
        adr = round(p.get("total_damage", 0) / (rounds if rounds > 0 else 1), 0)
        rating = round(p.get("leetify_rating", 0), 2)
        board += f"**{name}**: {k}/{d} | ADR: {adr} | Rating: {rating}\n"
    
    embed.add_field(name="Player Stats", value=board[:1024] or "No stats found.", inline=False)
    return embed

# --- TASKS & COMMANDS ---
@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY: return
    headers = {"_leetify_key": LEETIFY_API_KEY}
    for steam_id in TRACKED_PLAYERS.keys():
        try:
            res = requests.get("https://api-public.cs-prod.leetify.com/v3/profile/matches", headers=headers, params={"steam64_id": steam_id})
            if res.status_code != 200: continue
            matches = res.json()
            if not matches: continue
            latest = matches[0]
            if steam_id not in last_seen_matches: last_seen_matches[steam_id] = latest.get("id")
            elif latest.get("id") != last_seen_matches[steam_id]:
                embed = process_match_data(latest)
                if embed:
                    for guild in bot.guilds:
                        channel = discord.utils.get(guild.text_channels, name="leetify")
                        if channel: await channel.send(embed=embed)
                last_seen_matches[steam_id] = latest.get("id")
        except Exception as e: print(f"Error updating Leetify: {e}")

@bot.command(name="stats")
async def player_stats_command(ctx, input_str: str = None):
    if not input_str:
        await ctx.send("Usage: `!stats <Steam64ID>`")
        return

    steam_id = extract_steam_id(input_str)
    headers = {"_leetify_key": LEETIFY_API_KEY}
    res = requests.get("https://api-public.cs-prod.leetify.com/v3/profile/matches", headers=headers, params={"steam64_id": steam_id})
    
    if res.status_code != 200:
        await ctx.send("❌ Error fetching stats. Ensure profile is public.")
        return
            
    matches = res.json()[:5]
    if not matches: await ctx.send("No match data found."); return

    embed = discord.Embed(title=f"📈 Stats for {steam_id}", color=discord.Color.blue())
    outcomes = []
    for m in matches:
        p_stat = next((s for s in m.get("stats", []) if str(s.get("steam64_id")) == str(steam_id)), None)
        if p_stat:
            outcomes.append("W" if p_stat.get("team") == m.get("winner") else "L")
            
    embed.add_field(name="Last 5 Games", value=" | ".join(outcomes) if outcomes else "No data")
    await ctx.send(embed=embed)

@bot.command(name="testmatch")
async def test_match_command(ctx):
    if not LEETIFY_API_KEY: return
    first_id = list(TRACKED_PLAYERS.keys())[0]
    res = requests.get("https://api-public.cs-prod.leetify.com/v3/profile/matches", headers={"_leetify_key": LEETIFY_API_KEY}, params={"steam64_id": first_id})
    if res.status_code == 200 and res.json():
        await ctx.send(embed=process_match_data(res.json()[0]))
