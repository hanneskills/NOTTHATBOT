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

# Ensure these are defined at the top level of your script
# LEETIFY_API_KEY = os.environ.get('LEETIFY_API_KEY')
# last_seen_matches = {}

def process_match_data(match_data):
    """Parses Leetify match JSON and returns a formatted Discord Embed."""
    try:
        if not isinstance(match_data, dict): return None

        map_name = match_data.get("map_name", "Unknown").replace("de_", "").title()
        match_id = match_data.get("id")
        team_scores = match_data.get('team_scores', [])
        
        # Safely extract scores by team_number (2=T, 3=CT)
        s3 = next((s.get('score', 0) for s in team_scores if s.get('team_number') == 3), 0)
        s2 = next((s.get('score', 0) for s in team_scores if s.get('team_number') == 2), 0)
        
        embed = discord.Embed(
            title=f"🏆 Match Report: {map_name}",
            description=f"Score: **CT {s3} - {s2} T**\n[View on Leetify](https://leetify.com/app/match-details/{match_id})",
            color=discord.Color.gold()
        )
        
        # Format stats for tracked players only
        stats = match_data.get("stats", [])
        perf_text = ""
        for p in stats:
            steam_id = str(p.get("steam64_id"))
            if steam_id in TRACKED_PLAYERS:
                name = TRACKED_PLAYERS[steam_id]
                k = p.get("total_kills", 0)
                d = p.get("total_deaths", 0)
                perf_text += f"**{name}**: {k}/{d} K/D\n"
        
        if perf_text:
            embed.add_field(name="Squad Performance", value=perf_text, inline=False)
        return embed
    except Exception as e:
        print(f"Error processing match data: {e}")
        return None

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
            latest_id = latest.get("id")
            
            if steam_id not in last_seen_matches:
                last_seen_matches[steam_id] = latest_id
            elif latest_id != last_seen_matches[steam_id]:
                embed = process_match_data(latest)
                if embed:
                    for guild in bot.guilds:
                        channel = discord.utils.get(guild.text_channels, name="leetify")
                        if channel: await channel.send(embed=embed)
                last_seen_matches[steam_id] = latest_id
        except Exception as e:
            print(f"Leetify Loop Error: {e}")

# Remember to call check_leetify_stats.start() inside on_ready()
        await ctx.send(embed=process_match_data(res.json()[0]))
