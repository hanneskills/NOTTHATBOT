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
    "76561198722789242": "Hanneskills",
}

ROLE_NAME = "gamer"
LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')

# Local memory to track the last match ID we saw for each player so we don't repeat posts
last_seen_matches = {}

@bot.event
async def on_ready():
    print(f'⚡ Bot is online and vibing as {bot.user}')
    # Start the automated 2-minute Leetify checker
    check_leetify_stats.start()


# --- HELPER FUNCTION: PARSE & CONSTRUCT EMBED FROM MATCH DATA ---
def process_match_data(match_id, match_data):
    """Shared core parser that structures Leetify match JSON data into a clean Discord Embed."""
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
                    embed = process_match_data(match_id, match_data)
                    
                    if embed:
                        # Scan all available servers for a text channel named exactly 'leetify'
                        for guild in bot.guilds:
                            channel = discord.utils.get(guild.text_channels, name="leetify")
                            if channel:
                                await channel.send(embed=embed)
                
                last_seen_matches[steam_id] = match_id

        except Exception as e:
            print(f"Error updating Leetify stats for {player_name}: {e}")


# --- NEW CUSTOM COMMAND: !stats [name] ---
@bot.command(name="stats")
async def player_stats_command(ctx, name: str = None):
    """Fetches career averages (Aim, Util, Leetify rating) for a tracked player."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ Leetify API key is missing from environment variables.")
        return
        
    if not name:
        await ctx.send(f"Provide a name. Example: `!stats Hanneskills` (Tracked: {', '.join(TRACKED_PLAYERS.values())})")
        return

    # Cross-reference the argument name with tracked Steam IDs
    steam_id = next((sid for sid, p_name in TRACKED_PLAYERS.items() if p_name.lower() == name.lower()), None)
    if not steam_id:
        await ctx.send(f"❌ `{name}` isn't in your tracked configuration profile layout.")
        return

    headers = {"Authorization": f"Bearer {LEETIFY_API_KEY}"}
    
    try:
        url = f"https://api-public.cs-prod.leetify.com/api/v1/players/{steam_id}"
        res = requests.get(url, headers=headers)
        
        if res.status_code != 200:
            await ctx.send(f"⚠️ Unable to query Leetify overview endpoints for {name}.")
            return
            
        data = res.json()
        recent_ratings = data.get("recentGameRatings", {})
        
        embed = discord.Embed(
            title=f"📊 General Leetify Profile: {name}",
            url=f"https://leetify.com/app/profile/{steam_id}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Leetify Rating", value=f"`{round(recent_ratings.get('leetify', 0), 2)}`", inline=True)
        embed.add_field(name="Aim Rating", value=f"`{round(recent_ratings.get('aim', 0), 1)}`", inline=True)
        embed.add_field(name="Utility Rating", value=f"`{round(recent_ratings.get('utility', 0), 1)}`", inline=True)
        
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"Error handling profile data request processing: {e}")


# --- NEW CUSTOM COMMAND: !testmatch ---
@bot.command(name="testmatch")
async def test_match_command(ctx):
    """Force-pulls the absolute last match played by the first tracked player to test layout output."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ Leetify API Key missing.")
        return

    first_steam_id = list(TRACKED_PLAYERS.keys())[0]
    first_name = TRACKED_PLAYERS[first_steam_id]
    
    await ctx.send(f"🔎 Scanning data pipelines for {first_name}'s last recorded match data...")
    headers = {"Authorization": f"Bearer {LEETIFY_API_KEY}"}
    
    try:
        url = f"https://api-public.cs-prod.leetify.com/api/v1/players/{first_steam_id}/matches"
        res = requests.get(url, headers=headers)
        
        if res.status_code != 200 or not res.json():
            await ctx.send("⚠️ Failed fetching history array pipelines from Leetify.")
            return
            
        latest_match_id = res.json()[0].get("matchId")
        
        detail_url = f"https://api-public.cs-prod.leetify.com/api/v1/matches/{latest_match_id}"
        detail_res = requests.get(detail_url, headers=headers)
        
        if detail_res.status_code == 200:
            embed = process_match_data(latest_match_id, detail_res.json())
            if embed:
                await ctx.send(content="✅ **Pipeline Verification Complete! Here is how game outcomes will render:**", embed=embed)
                return
                
        await ctx.send("❌ Found a match record, but no configured tracking profile match-ups inside it.")
            
    except Exception as e:
        await ctx.send(f"Pipeline Test Error Encountered: {e}")


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
