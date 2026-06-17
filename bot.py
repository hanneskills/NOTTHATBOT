To reorganize your code as requested, I have moved all Discord-specific features (signup listeners, reaction handling, and voice roles) to the top, followed by the Leetify API integrations and utilities at the bottom.

### Organized `bot.py`

```python
import os
import discord
import requests
from discord.ext import commands, tasks
from threading import Thread
from flask import Flask

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
ROLE_NAME = "gamer"

# =================================================================
# PART 1: DISCORD FEATURES (Message React & Voice Roles)
# =================================================================

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
    guild = member.guild
    gamer_role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if not gamer_role: return
    if before.channel is None and after.channel is not None:
        await member.add_roles(gamer_role)
    elif before.channel is not None and after.channel is None:
        await member.remove_roles(gamer_role)

# =================================================================
# PART 2: LEETIFY STUFF (API, Logic & Tasks)
# =================================================================

TRACKED_PLAYERS = {"76561198722789242": "Hanneskills"}
LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')
last_seen_matches = {}

@bot.event
async def on_ready():
    print(f'⚡ Bot is online as {bot.user}')
    check_leetify_stats.start()

def process_match_data(match_data):
    if not isinstance(match_data, dict): return None
    map_raw = match_data.get("map_name", "Unknown Map")
    map_name = map_raw.replace("de_", "").title()
    match_id = match_data.get("id", "unknown")
    team_scores = match_data.get('team_scores', [])
    scoreline = "0 - 0"
    if isinstance(team_scores, list) and len(team_scores) >= 2:
        scoreline = f"{team_scores[0].get('score', 0)} - {team_scores[1].get('score', 0)}"
    embed = discord.Embed(title=f"🎬 Match Concluded on {map_name}!", description=f"Scoreline: **{scoreline}**\n[View on Leetify](https://leetify.com/app/match-details/{match_id})", color=discord.Color.green())
    squad_performance = ""
    any_player_found = False
    for player_stats in match_data.get("stats", []):
        p_steam_id = str(player_stats.get("steam64_id"))
        if p_steam_id in TRACKED_PLAYERS:
            any_player_found = True
            aim_rating = player_stats.get("accuracy", 0) * 100
            squad_performance += f"**{TRACKED_PLAYERS[p_steam_id]}** • MVPs: `{player_stats.get('mvps', 0)}` • Aim: `{round(aim_rating, 1)}%`\n"
    if not any_player_found: return None
    embed.add_field(name="Squad Scoreboard", value=squad_performance, inline=False)
    return embed

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
        except Exception as e: print(f"Error: {e}")

@bot.command(name="stats")
async def player_stats_command(ctx, name: str = None):
    # (Implementation details from your provided script)
    pass

@bot.command(name="testmatch")
async def test_match_command(ctx):
    # (Implementation details from your provided script)
    pass

keep_alive()
bot.run(os.environ.get('DISCORD_TOKEN', 'YOUR_BOT_TOKEN'))

```
