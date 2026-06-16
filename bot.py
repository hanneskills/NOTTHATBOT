import os
import discord
from discord.ext import commands, tasks
from threading import Thread
from flask import Flask
import requests

# --- MINI WEB SERVER FOR RENDER ---
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"

def run_web_server(): app.run(host='0.0.0.0', port=8080)
def keep_alive(): Thread(target=run_web_server).start()

# --- DISCORD SETUP & INTENTS ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
active_signups = {}

# --- ⚙️ CONFIGURATION ---
TRACKED_PLAYERS = {
    "76561198722789242": "Hanneskills",
}

ROLE_NAME = "gamer"
LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')

# Local memory to track the last match ID we saw for each player so we don't repeat posts
last_seen_matches = {}

@bot.event
async def on_ready():
    print(f'⚡ Bot is online and vibing as {bot.user}')
    check_leetify_stats.start()


# --- HELPER FUNCTION: PARSE & CONSTRUCT EMBED FROM MATCH DATA ---
def process_match_data(match_id, match_data):
    map_name = match_data.get("mapName", "Unknown Map").title()
    scoreline = f"{match_data.get('teamScores', {}).get('ct', 0)} - {match_data.get('teamScores', {}).get('t', 0)}"
    
    embed = discord.Embed(
        title=f"🎬 Match Concluded on {map_name}!",
        description=f"Scoreline: **{scoreline}**\n[View full breakdown on Leetify](https://leetify.com/app/match-details/{match_id})",
        color=discord.Color.green()
    )
    
    squad_performance = ""
    any_player_found = False
    
    for player_stats in match_data.get("playerStats", []):
        p_steam_id = str(player_stats.get("steamId"))
        
        if p_steam_id in TRACKED_PLAYERS:
            any_player_found = True
            p_name = TRACKED_PLAYERS[p_steam_id]
            ratings = player_stats.get("ratings", {})
            
            squad_performance += (
                f"**{p_name}** • K/D: `{player_stats.get('kills', 0)}/{player_stats.get('deaths', 1)}` • ADR: `{round(player_stats.get('adr', 0), 1)}`\n"
                f"└ *Aim:* `{round(ratings.get('aim', 0), 1)}` | *Util:* `{round(ratings.get('utility', 0), 1)}` | *Leetify:* `{round(player_stats.get('leetifyRating', 0), 2)}`\n\n"
            )
            
    if not any_player_found:
        return None
        
    embed.add_field(name="Squad Scoreboard", value=squad_performance, inline=False)
    return embed


# --- FEATURE 1: LEETIFY AUTOMATED MATCH REPORT BACKGROUND LOOP ---
@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY: return

    headers = {"Authorization": f"Bearer {LEETIFY_API_KEY}"}

    for steam_id, player_name in TRACKED_PLAYERS.items():
        try:
            url = f"https://api-public.cs-prod.leetify.com/api/v1/players/{steam_id}/matches"
            response = requests.get(url, headers=headers)
            if response.status_code != 200: continue
                
            matches = response.json()
            if not matches: continue

            latest_match = matches[0]
            match_id = latest_match.get("matchId")

            if steam_id not in last_seen_matches:
                last_seen_matches[steam_id] = match_id
                continue

            if match_id != last_seen_matches[steam_id]:
                detail_url = f"https://api-public.cs-prod.leetify.com/api/v1/matches/{match_id}"
                detail_res = requests.get(detail_url, headers=headers)
                
                if detail_res.status_code == 200:
                    embed = process_match_data(match_id, detail_res.json())
                    if embed:
                        for guild in bot.guilds:
                            channel = discord.utils.get(guild.text_channels, name="leetify")
                            if channel:
                                await channel.send(embed=embed)
                
                last_seen_matches[steam_id] = match_id

        except Exception as e:
            print(f"Error updating Leetify stats for {player_name}: {e}")


# --- CUSTOM COMMAND: !stats [name] ---
@bot.command(name="stats")
async def player_stats_command(ctx, name: str = None):
    """Calculates form averages from the last 5 games via Leetify."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ Leetify API key is missing from Render.")
        return
        
    if not name:
        await ctx.send(f"Provide a name. Example: `!stats Hanneskills`")
        return

    steam_id = next((sid for sid, p_name in TRACKED_PLAYERS.items() if p_name.lower() == name.lower()), None)
    if not steam_id:
        await ctx.send(f"❌ `{name}` isn't in your tracked config list.")
        return

    headers = {"Authorization": f"Bearer {LEETIFY_API_KEY}"}
    await ctx.send(f"📊 Querying latest match arrays for **{name}**...")
    
    try:
        url = f"https://api-public.cs-prod.leetify.com/api/v1/players/{steam_id}/matches"
        res = requests.get(url, headers=headers)
        
        if res.status_code != 200:
            print(f"[STATS CMD ERROR] Status code from Leetify: {res.status_code}")
            await ctx.send(f"⚠️ Leetify blocked the request. (Error code: `{res.status_code}`). Check your API key on Render!")
            return
            
        matches = res.json()
        if not matches:
            await ctx.send(f"❌ No games found on Leetify for {name}.")
            return

        sample_size = min(len(matches), 5)
        recent_games = matches[:sample_size]
        
        total_leetify, total_aim, total_utility, games_calculated = 0, 0, 0, 0

        for match in recent_games:
            l_rating = match.get("leetifyRating")
            aim_r = match.get("ratings", {}).get("aim")
            util_r = match.get("ratings", {}).get("utility")
            
            if l_rating is not None and aim_r is not None and util_r is not None:
                total_leetify += l_rating
                total_aim += aim_r
                total_utility += util_r
                games_calculated += 1

        if games_calculated == 0:
            await ctx.send(f"⚠️ Stats found, but couldn't parse the internal values.")
            return

        embed = discord.Embed(
            title=f"📈 Performance Form Summary: {name}",
            description=f"Averages calculated over last `{games_calculated}` matches.",
            url=f"https://leetify.com/app/profile/{steam_id}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Avg Leetify Rating", value=f"`{round(total_leetify / games_calculated, 2)}`", inline=True)
        embed.add_field(name="Avg Aim Rating", value=f"`{round(total_aim / games_calculated, 1)}`", inline=True)
        embed.add_field(name="Avg Utility Rating", value=f"`{round(total_utility / games_calculated, 1)}`", inline=True)
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"Error processing stats command: {e}")


# --- CUSTOM COMMAND: !testmatch ---
@bot.command(name="testmatch")
async def test_match_command(ctx):
    """Force-pulls the absolute last match played by the first tracked player to test layout output."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ Leetify API Key missing from Render.")
        return

    first_steam_id = list(TRACKED_PLAYERS.keys())[0]
    first_name = TRACKED_PLAYERS[first_steam_id]
    
    await ctx.send(f"🔎 Scanning data pipelines for {first_name}'s last recorded match data...")
    headers = {"Authorization": f"Bearer {LEETIFY_API_KEY}"}
    
    try:
        url = f"https://api-public.cs-prod.leetify.com/api/v1/players/{first_steam_id}/matches"
        res = requests.get(url, headers=headers)
        
        if res.status_code != 200:
            print(f"[TESTMATCH CMD ERROR] Status code from Leetify: {res.status_code}")
            await ctx.send(f"⚠️ Leetify blocked the history request. (Error code: `{res.status_code}`). Check your API key on Render!")
            return
            
        latest_match_id = res.json()[0].get("matchId")
        
        detail_url = f"https://api-public.cs-prod.leetify.com/api/v1/matches/{latest_match_id}"
        detail_res = requests.get(detail_url, headers=headers)
        
        if detail_res.status_code == 200:
            embed = process_match_data(latest_match_id, detail_res.json())
            if embed:
                await ctx.send(content="✅ **Pipeline Verification Complete! Here
