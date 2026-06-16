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

# --- ⚙️ CONFIGURATION (CHANGE THESE) ---
# Replace with your squad's 17-digit Steam64 IDs and their names
TRACKED_PLAYERS = {
    "76561198000000001": "S1mple",
    "76561198000000002": "ZywOo",
    "76561198000000003": "m0NESY",
}

# Replace with your actual Discord Text Channel ID where stats should post
STATS_CHANNEL_ID = 123456789012345678  

ROLE_NAME = "gamer"
LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')

# Local memory to track the last match ID we saw for each player so we don't repeat posts
last_seen_matches = {}

@bot.event
async def on_ready():
    print(f'⚡ Bot is online and vibing as {bot.user}')
    # Start the automated 2-minute Leetify checker
    check_leetify_stats.start()


# --- FEATURE 1: LEETIFY MATCH REPORT WRAPPER ---

@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY:
        print("⚠️ Leetify API Key missing from Environment Variables.")
        return

    headers = {"Authorization": f"Bearer {LEETIFY_API_KEY}"}
    games_to_report = {}

    for steam_id, player_name in TRACKED_PLAYERS.items():
        try:
            # Request player's match history
            url = f"https://api-public.cs-prod.leetify.com/api/v1/players/{steam_id}/matches"
            response = requests.get(url, headers=headers)
            if response.status_code != 200: continue
                
            matches = response.json()
            if not matches: continue

            latest_match = matches[0]
            match_id = latest_match.get("matchId")

            # Seed data on first boot so it doesn't dump old history
            if steam_id not in last_seen_matches:
                last_seen_matches[steam_id] = match_id
                continue

            # Process if it's a completely new match
            if match_id != last_seen_matches[steam_id]:
                detail_url = f"https://api-public.cs-prod.leetify.com/api/v1/matches/{match_id}"
                detail_res = requests.get(detail_url, headers=headers)
                
                if detail_res.status_code == 200:
                    match_data = detail_res.json()
                    player_stats = next((p for p in match_data.get("playerStats", []) if str(p.get("steamId")) == str(steam_id)), None)
                    
                    if player_stats:
                        ratings = player_stats.get("ratings", {})
                        
                        if match_id not in games_to_report:
                            games_to_report[match_id] = {
                                "mapName": match_data.get("mapName", "Unknown Map"),
                                "result": f"{match_data.get('teamScores', {}).get('ct', 0)} - {match_data.get('teamScores', {}).get('t', 0)}",
                                "players": []
                            }
                        
                        games_to_report[match_id]["players"].append({
                            "name": player_name,
                            "kills": player_stats.get("kills", 0),
                            "deaths": player_stats.get("deaths", 1),
                            "adr": round(player_stats.get("adr", 0), 1),
                            "aim": round(ratings.get("aim", 0), 1),
                            "utility": round(ratings.get("utility", 0), 1),
                            "leetifyRating": round(player_stats.get("leetifyRating", 0), 2)
                        })
                
                last_seen_matches[steam_id] = match_id

        except Exception as e:
            print(f"Error updating Leetify stats for {player_name}: {e}")

    # Build and broadcast grouped stats to Discord
    channel = bot.get_channel(STATS_CHANNEL_ID)
    if channel and games_to_report:
        for match_id, game in games_to_report.items():
            embed = discord.Embed(
                title=f"🎬 Match Concluded on {game['mapName'].title()}!",
                description=f"Scoreline: **{game['result']}**\n[View full breakdown on Leetify](https://leetify.com/app/match-details/{match_id})",
                color=discord.Color.green()
            )
            
            squad_performance = ""
            for p in game["players"]:
                squad_performance += (
                    f"**{p['name']}** • K/D: `{p['kills']}/{p['deaths']}` • ADR: `{p['adr']}`\n"
                    f"└ *Aim:* `{p['aim']}` | *Util:* `{p['utility']}` | *Leetify:* `{p['leetifyRating']}`\n\n"
                )
            
            embed.add_field(name="Squad Scoreboard", value=squad_performance, inline=False)
            await channel.send(embed=embed)


# --- FEATURE 2: THE "WHO'S PLAYING" SIGNUP ---

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    content = message.content.lower()
    if "game" in content or "playing" in content:
        embed = discord.Embed(title="🎮 Who's playing tonight?", description="Click the **✅** reaction below to join the squad!", color=discord.Color.blurple())
        embed.add_field(name="Players Joined:", value="*No one yet...*", inline=False)
        signup_message = await message.channel.send(embed=embed)
        await signup_message.add_reaction("✅")
        active_signups[signup_message.id] = set()
    await bot.process_commands(message)

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


# --- FEATURE 3: AUTO GAMER ROLE FOR VOICE CHAT ---

@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    gamer_role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if not gamer_role: return
    if before.channel is None and after.channel is not None:
        await member.add_roles(gamer_role)
    elif before.channel is not None and after.channel is None:
        await member.remove_roles(gamer_role)

# --- START THE ENGINES ---
keep_alive()
TOKEN = os.environ.get('DISCORD_TOKEN', 'YOUR_BOT_TOKEN')
bot.run(TOKEN)
