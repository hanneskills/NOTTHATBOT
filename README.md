# CS2 Squad Bot 🎮

A Discord bot for CS2 (Counter-Strike 2) groups that automates game night sign-ups and turns your server into a live scoreboard for everyone's matches. It watches for tracked players' games via [Leetify](https://leetify.com), posts clean match embeds automatically, and runs a weekly recap of who's been grinding.

## Features

### 🗳️ Smart "who's playing" polls
Just talk naturally in your signup channel — mention "game" or "playing" and the bot spins up a reaction poll automatically.
- Detects time phrases like *"in 2 hours"* or *"in 30 minutes"* and shows an adaptive Discord timestamp (`<t:...:R>`)
- React ✅ to join, ❌ to leave, 🗑️ to tear down the poll
- Keeps the reaction list clean by auto-removing user reactions after tallying them
- Resets automatically every night at 03:00 UTC

### 🎯 Live match tracking (Leetify integration)
- Add Steam64 IDs to a tracked list; the bot polls Leetify every couple of minutes and posts a formatted scoreboard embed the moment a new match finishes
- Also picks up Faceit matches synced through Leetify
- Catches up on anything missed while the bot was offline
- Duplicate-safe — won't repost a match that's already in the channel

### 📊 Player stat cards
Pull up anyone's current profile on demand: aim rating, utility rating, reaction time, Leetify rating, current and peak Premier rank, plus their last five matches.

### 📅 Weekly recap
A scheduled leaderboard-style summary of the week's matches, posted automatically to a dedicated channel.

### 🔊 Voice-based role assignment
Automatically grants a `gamer` role to anyone who joins a voice channel, and removes it when they leave.

### 🛡️ Display-name sanitization
Player names come from Steam, which means anyone can (accidentally or not) break a Discord monospace table with backticks, right-to-left script, or zero-width characters. Names are validated before being dropped into a scoreboard, and visual-width-aware padding keeps columns aligned even with wide-character names.

## Commands

| Command | Description |
|---|---|
| `!addplayer <steam64id> <name>` | Start tracking a player's matches |
| `!removeplayer <steam64id>` | Stop tracking a player |
| `!players` | List everyone currently tracked |
| `!stats <steam64id>` | Show a player's stat card |
| `!lastmatch <steam64id>` | Force-post someone's most recent match |
| `!getmatch <matchid or leetify url>` | Force-post a specific match (e.g. one that wasn't auto-posted) |
| `!catchup` | Check all tracked players for matches missing from the channel and post them |
| `!weeklyrecap` | Manually trigger the weekly recap |

## Tech stack

- **[discord.py](https://github.com/Rapptz/discord.py)** — bot framework
- **[Leetify Public API](https://leetify.com)** — CS2/Faceit match and profile data
- **[Supabase](https://supabase.com)** — persistent storage for tracked players
- **Flask** — a minimal keep-alive web server for always-on hosting (e.g. Render)

## Setup

1. Clone the repo and install dependencies:
   ```bash
   pip install discord.py requests flask
   ```
2. Set the following environment variables:

   | Variable | Required | Purpose |
   |---|---|---|
   | `DISCORD_TOKEN` | ✅ | Your bot's Discord token |
   | `LEETIFY_API_KEY` | ✅ | Leetify Public API key |
   | `SUPABASE_URL` | ✅ | Supabase project URL |
   | `SUPABASE_KEY` | ✅ | Supabase service key |
   | `STEAM_API_KEY` | optional | Enables resolving Steam vanity URLs to Steam64 IDs |

3. In your server, create text channels named `#leetify` (match scoreboards) and `#weekly` (weekly recap), plus a `gamer` role for voice tracking.
4. Run the bot:
   ```bash
   python bot.py
   ```

## Notes

- Built to run on free-tier hosts like Render — the built-in Flask server keeps the service pinged and alive.
- Designed for a single active poll per server at a time.
