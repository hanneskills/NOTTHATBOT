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
    print(f'⚡ Bot is online and aligned with Leetify layout as {bot.user}')
    check_leetify_stats.start()


# --- HELPER FUNCTION: PARSE & CONSTRUCT EMBED FROM MATCH DATA ---
def process_match_data(match_data):
    if not isinstance(match_data, dict):
        return None

    # FIXED: Aligned with map_name from Screenshot_20260617-005301.png
    map_raw = match_data.get("map_name", "Unknown Map")
    map_name = map_raw.replace("de_", "").title()
    match_id = match_data.get("id", "unknown")
    
    # FIXED: Extracting team scores based on the array layout in the screenshot
    team_scores = match_data.get('team_scores', [])
    scoreline = "0 - 0"
    if isinstance(team_scores, list) and len(team_scores) >= 2:
        scoreline = f"{team_scores[0].get('score', 0)} - {team_scores[1].get('score', 0)}"
    
    embed = discord.Embed(
        title=f"🎬 Match Concluded on {map_name}!",
        description=f"Scoreline: **{scoreline}**\n[View full breakdown on Leetify](https://leetify.com/app/match-details/{match_id})",
        color=discord.Color.green()
    )
    
    squad_performance = ""
    any_player_found = False
    
    # FIXED: Parsing player data using the 'stats' array and 'steam64_id' from the screenshot
    for player_stats in match_data.get("stats", []):
        p_steam_id = str(player_stats.get("steam64_id"))
        
        if p_steam_id in TRACKED_PLAYERS:
            any_player_found = True
            p_name = TRACKED_PLAYERS[p_steam_id]
            
            # Grabbing standard metrics visible in the response body snippet
            aim_rating = player_stats.get("accuracy", 0) * 100 # Converting fractional accuracy to display cleanly
            mvps = player_stats.get("mvps", 0)
            leetify_rating = player_stats.get("proxim", 0) # Using visible rating key metrics
            
            squad_performance += (
                f"**{p_name}** • MVPs: `{mvps}` • Aim Accuracy: `{round(aim_rating, 1)}%`\n"
                f"└ *Leetify Metric Index:* `{round(leetify_rating, 2)}`\n\n"
            )
            
    if not any_player_found:
        return None
        
    embed.add_field(name="Squad Scoreboard", value=squad_performance, inline=False)
    return embed


# --- FEATURE 1: LEETIFY AUTOMATED MATCH REPORT BACKGROUND LOOP ---
@tasks.loop(minutes=2)
async def check_leetify_stats():
    if not LEETIFY_API_KEY: return

    headers = {"_leetify_key": LEETIFY_API_KEY}

    for steam_id, player_name in TRACKED_PLAYERS.items():
        try:
            url = f"https://api-public.cs-prod.leetify.com/v3/profile/matches"
            # FIXED: query parameter set to steam64_id per screenshot requirements
            params = {"steam64_id": steam_id}
            response = requests.get(url, headers=headers, params=params)
            if response.status_code != 200: continue
                
            matches = response.json()
            if not isinstance(matches, list) or not matches: continue

            # The top element is the latest match record
            latest_match = matches[0]
            match_id = latest_match.get("id")

            if steam_id not in last_seen_matches:
                last_seen_matches[steam_id] = match_id
                continue

            if match_id != last_seen_matches[steam_id]:
                # Since the profile history response already contains the full statistics array,
                # we can parse it directly without needing a secondary v2 API call!
                embed = process_match_data(latest_match)
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
    """Calculates form averages from recent games visible in the history array."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ Leetify API key is missing from Render.")
        return
        
    if not name:
        await ctx.send("Provide a name. Example: `!stats Hanneskills`")
        return

    steam_id = next((sid for sid, p_name in TRACKED_PLAYERS.items() if p_name.lower() == name.lower()), None)
    if not steam_id:
        await ctx.send(f"❌ `{name}` isn't in your tracked config list.")
        return

    headers = {"_leetify_key": LEETIFY_API_KEY}
    await ctx.send(f"📊 Querying latest match arrays for **{name}**...")
    
    try:
        url = f"https://api-public.cs-prod.leetify.com/v3/profile/matches"
        params = {"steam64_id": steam_id}
        res = requests.get(url, headers=headers, params=params)
        
        if res.status_code != 200:
            await ctx.send(f"⚠️ Leetify blocked the request. (Error code: `{res.status_code}`).")
            return
            
        matches = res.json()
        if not isinstance(matches, list) or not matches:
            await ctx.send(f"❌ No games found on Leetify for {name}.")
            return

        sample_size = min(len(matches), 5)
        recent_games = matches[:sample_size]
        
        total_accuracy, total_mvps, games_calculated = 0, 0, 0

        for match in recent_games:
            for p_stat in match.get("stats", []):
                if str(p_stat.get("steam64_id")) == steam_id:
                    total_accuracy += p_stat.get("accuracy", 0)
                    total_mvps += p_stat.get("mvps", 0)
                    games_calculated += 1

        if games_calculated == 0:
            await ctx.send("⚠️ Stats found, but couldn't isolate your user ID values inside them.")
            return

        embed = discord.Embed(
            title=f"📈 Performance Form Summary: {name}",
            description=f"Averages calculated over last `{games_calculated}` matches.",
            url=f"https://leetify.com/app/profile/{steam_id}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Avg Accuracy", value=f"`{round((total_accuracy / games_calculated) * 100, 1)}%`", inline=True)
        embed.add_field(name="Total MVPs (Form Block)", value=f"`{total_mvps}`", inline=True)
        await ctx.send(embed=embed)
        
    except Exception as e:
        await ctx.send(f"Error processing stats command: {e}")


# --- CUSTOM COMMAND: !testmatch ---
@bot.command(name="testmatch")
async def test_match_command(ctx):
    """Force-pulls the absolute last match from the history payload to verify layout output."""
    if not LEETIFY_API_KEY:
        await ctx.send("⚠️ Leetify API Key missing from Render.")
        return

    first_steam_id = list(TRACKED_PLAYERS.keys())[0]
    first_name = TRACKED_PLAYERS[first_steam_id]
    
    await ctx.send(f"🔎 Scanning data pipelines for {first_name}'s last recorded match data...")
    headers = {"_leetify_key": LEETIFY_API_KEY}
    
    try:
        url = f"https://api-public.cs-prod.leetify.com/v3/profile/matches"
        params = {"steam64_id": first_steam_id}
        res = requests.get(url, headers=headers, params=params)
        
        if res.status_code != 200:
            await ctx.send(f"⚠️ Leetify blocked the history request. (Error code: `{res.status_code}`).")
            return
            
        matches = res.json()
        if not isinstance(matches, list) or not matches:
            await ctx.send("❌ No match records found inside the payload list structure.")
            return
            
        # Select the top match structure seen in the screenshot response body
        latest_match_payload = matches[0]
        
        embed = process_match_data(latest_match_payload)
        if embed:
            await ctx.send(content="✅ **Pipeline Verification Complete! Here is how your data renders:**", embed=embed)
            return
                
        await ctx.send("❌ Found a match record, but player metrics formatting mismatch occurred.")
            
    except Exception as e:
        await ctx.send(f"Pipeline Test Error Encountered: {e}")


# --- THE "WHO'S PLAYING" SIGNUP LISTEN TRIGGER ---
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


# --- AUTO GAMER ROLE FOR VOICE CHAT ---
@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    gamer_role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if not gamer_role: return
    if before.channel is None and after.channel is not None:
        await member.add_roles(gamer_role)
    elif before.channel is not None and after.channel is None:
        await member.remove_roles(gamer_role)

keep_alive()
TOKEN = os.environ.get('DISCORD_TOKEN', 'YOUR_BOT_TOKEN')
bot.run(TOKEN)
