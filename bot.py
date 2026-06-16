import os
import discord
from discord.ext import commands
from threading import Thread
from flask import Flask

# --- MINI WEB SERVER TO KEEP IT ALIVE ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"

def run_web_server():
    # Runs on port 8080, which free hosts look for
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_web_server)
    t.start()

# --- YOUR ACTUAL DISCORD BOT ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
active_signups = {}

@bot.event
async def on_ready():
    print(f'⚡ Bot is online and vibing as {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    content = message.content.lower()
    if "game" in content or "playing" in content:
        embed = discord.Embed(
            title="🎮 Who's playing tonight?", 
            description="Click the **✅** reaction below to join the squad!",
            color=discord.Color.blurple()
        )
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

ROLE_NAME = "gamer"

@bot.event
async def on_voice_state_update(member, before, after):
    guild = member.guild
    gamer_role = discord.utils.get(guild.roles, name=ROLE_NAME)
    if not gamer_role: return
    
    # ➕ If they weren't in a VC before, but are now -> Give role
    if before.channel is None and after.channel is not None:
        await member.add_roles(gamer_role)
        
    # ➖ If they WERE in a VC before, but aren't now -> Remove role
    elif before.channel is not None and after.channel is None:
        await member.remove_roles(gamer_role)

# Start the web server right before the bot runs
keep_alive()

# Crucial: On cloud hosts, we use an Environment Variable for safety, but you can paste your token here if you want
TOKEN = os.environ.get('DISCORD_TOKEN', 'YOUR_BOT_TOKEN')
bot.run(TOKEN)
