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

# --- HELPER FUNCTIONS ---
def extract_steam_id(input_str):
    """Extracts 17-digit Steam64ID from a URL or returns the string if it's already an ID."""
    match = re.search(r'(\d{17})', input_str)
    return match.group(1) if match else input_str

def process_match_data(match_data):
    if not isinstance(match_data, dict): return None

    map_name = match_data.get("map_name", "Unknown").replace("de_", "").title()
    team_scores = match_data.get('team_scores', [])
    ct_score = next((s['score'] for s in team_scores if s['faction'] == 'CT'), 0)
    t_score = next((s['score'] for s in team_scores if s['faction'] == 'T'), 0)
    
    embed = discord.Embed(
        title=f"🏆 Match Leaderboard: {map_name}",
        description=f"Final Score: **CT {ct_score} - {t_score} T**\n[View on Leetify](https://leetify.com/app/match-details/{match_data.get('id')})",
        color=discord.Color.gold()
    )
    
    # Sort by kills
    stats = sorted(match_data.get("stats", []), key=lambda x: x.get("kills", 0), reverse=True)
    
    board = ""
    for p in stats:
        name = p.get("name", "Unknown")
        k, d = p.get("kills", 0), p.get("deaths", 0)
        adr = round(p.get("adr", 0), 0)
        aim = round(p.get("aim", 0), 1)
        util = round(p.get("utility", 0), 1)
        board += f"**{name}**: {k}/{d} | ADR: {adr} | Aim: {aim} | Util: {util}\n"
    
    embed.add_field(name="Player Stats", value=board or "No stats found.", inline=False)
    return embed

# --- TASKS & COMMANDS ---
@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY: return
    headers = {"_leetify_key": LEETIFY_API_KEY}
    for steam_id, player_name in TRACKED_PLAYERS.items():
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
        await ctx.send("Usage: `!stats <Steam64ID_or_ProfileURL>`")
        return

    steam_id = extract_steam_id(input_str)
    headers = {"_leetify_key": LEETIFY_API_KEY}
    await ctx.send(f"📊 Querying data for: `{steam_id}`...")
    
    try:
        res = requests.get(f"https://api-public.cs-prod.leetify.com/v3/profile/matches", headers=headers, params={"steam64_id": steam_id})
        if res.status_code != 200:
            await ctx.send("❌ Error fetching stats. Ensure profile is public.")
            return
            
        matches = res.json()[:5]
        total_aim, total_util, total_adr, count = 0, 0, 0, 0
        outcomes = []

        for m in matches:
            p_stat = next((s for s in m.get("stats", []) if str(s.get("steam64_id")) == str(steam_id)), None)
            if p_stat:
                total_aim += p_stat.get("aim", 0)
                total_util += p_stat.get("utility", 0)
                total_adr += p_stat.get("adr", 0)
                winner = m.get("winner")
                outcomes.append("W" if p_stat.get("team") == winner else "L")
                count += 1

        if count == 0: await ctx.send("No match data found."); return

        embed = discord.Embed(title=f"📈 Stats for {steam_id}", color=discord.Color.blue())
        embed.add_field(name="Avg Aim / Util / ADR", value=f"{round(total_aim/count,1)} / {round(total_util/count,1)} / {round(total_adr/count,0)}", inline=False)
        embed.add_field(name="Last 5 Games", value=" | ".join(outcomes), inline=False)
        await ctx.send(embed=embed)
    except Exception as e:
        await ctx.send(f"Error: {e}")

@bot.command(name="testmatch")
async def test_match_command(ctx):
    if not LEETIFY_API_KEY: return
    first_id = list(TRACKED_PLAYERS.keys())[0]
    res = requests.get("https://api-public.cs-prod.leetify.com/v3/profile/matches", headers={"_leetify_key": LEETIFY_API_KEY}, params={"steam64_id": first_id})
    if res.status_code == 200 and res.json():
        await ctx.send(embed=process_match_data(res.json()[0]))
