import os
import discord
import requests
import re
from discord.ext import commands, tasks
from threading import Thread
from flask import Flask

# =================================================================
# 1. WEB SERVER & BOT SETUP
# =================================================================
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
LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')

# =================================================================
# 2. UTILITY FUNCTIONS
# =================================================================
def extract_steam_id(input_str):
    match = re.search(r'(\d{17})', input_str)
    return match.group(1) if match else input_str

def process_match_data(match_data):
    """Generates a full leaderboard embed."""
    map_name = match_data.get("map_name", "Unknown").replace("de_", "").title()
    team_scores = match_data.get('team_scores', [])
    # Scores logic
    ct_score = next((s['score'] for s in team_scores if s['faction'] == 'CT'), 0)
    t_score = next((s['score'] for s in team_scores if s['faction'] == 'T'), 0)
    
    embed = discord.Embed(title=f"🏆 Match Leaderboard: {map_name}", 
                          description=f"Final Score: **CT {ct_score} - {t_score} T**", 
                          color=discord.Color.gold())
    
    stats = sorted(match_data.get("stats", []), key=lambda x: x.get("kills", 0), reverse=True)
    
    # Create the leaderboard string
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

# =================================================================
# 3. COMMANDS: STATS & LEADERBOARDS
# =================================================================
@bot.command(name="stats")
async def player_stats(ctx, input_str: str):
    steam_id = extract_steam_id(input_str)
    headers = {"_leetify_key": LEETIFY_API_KEY}
    res = requests.get(f"https://api-public.cs-prod.leetify.com/v3/profile/matches?steam64_id={steam_id}", headers=headers)
    
    if res.status_code == 200:
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
        
        embed = discord.Embed(title=f"📊 Form Summary: {steam_id}", color=discord.Color.blue())
        embed.add_field(name="Avg Aim / Util / ADR", value=f"{round(total_aim/count,1)} / {round(total_util/count,1)} / {round(total_adr/count,0)}")
        embed.add_field(name="Last 5 Games", value=" | ".join(outcomes))
        await ctx.send(embed=embed)

@bot.command(name="match")
async def get_match(ctx, match_id: str):
    headers = {"_leetify_key": LEETIFY_API_KEY}
    res = requests.get(f"https://api-public.cs-prod.leetify.com/v2/matches/{match_id}", headers=headers)
    if res.status_code == 200:
        await ctx.send(embed=process_match_data(res.json()))

# =================================================================
# 4. DISCORD FEATURES (POLLS & VOICE ROLES)
# =================================================================
# Trigger for "Who's playing?"
@bot.listen('on_message')
async def handle_game_signups(message):
    if message.author == bot.user: return
    if "game" in message.content.lower() or "playing" in message.content.lower():
        embed = discord.Embed(title="🎮 Who's playing?", description="React with ✅ to join.")
        msg = await message.channel.send(embed=embed)
        await msg.add_reaction("✅")

# Auto-Role System
@bot.event
async def on_voice_state_update(member, before, after):
    role = discord.utils.get(member.guild.roles, name="gamer")
    if not role: return
    if before.channel is None and after.channel is not None:
        await member.add_roles(role)
    elif before.channel is not None and after.channel is None:
        await member.remove_roles(role)

# =================================================================
# 5. INITIALIZATION
# =================================================================
keep_alive()
bot.run(os.environ['DISCORD_TOKEN'])
