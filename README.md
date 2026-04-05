# Discord Bot

A Discord bot for server activity tracking, League of Legends checks, SoundCloud voice playback, and a timed jumpscare mode.

## Features

- Activity tracking for messages and time spent in voice channels
- Leaderboard and rank progression based on server activity
- League of Legends integration through the Riot API
- SoundCloud playback in voice channels
- Music queue, skip, loop, and stop controls
- Automatic voice disconnect after 5 minutes of music inactivity
- Optional timed jumpscare mode for occupied voice channels

## Commands

### Activity

| Command | Description |
|---------|-------------|
| `/leaderboard` | Show the server activity leaderboard |
| `/ranks` | Show the activity rank table |
| `/setcountdown` | Save a new countdown |
| `/countdown` | Show active countdowns |

### Music

| Command | Description |
|---------|-------------|
| `/play` | Play a SoundCloud track or add it to the queue |
| `/stop` | Stop playback, clear the queue, and keep the bot in voice |
| `/skip` | Skip the currently playing track |
| `/loop` | Loop the current track |
| `/stoplooping` | Disable looping for the current track |

Music behavior:

- New `/play` requests are queued when something is already playing
- If nothing else starts after the last song ends, the bot disconnects after 5 minutes
- SoundCloud is the recommended music source

### Jumpscare

| Command | Description |
|---------|-------------|
| `/jumpscare on` | Enable the timed jumpscare mode for the server |
| `/jumpscare off` | Disable the timed jumpscare mode for the server |

Jumpscare behavior:

- If enabled and at least one non-bot user is in a voice channel, the bot may join once every 20 minutes
- It plays the configured jumpscare sound for 8 seconds and disconnects
- Jumpscare is always lower priority than normal music playback

### League of Legends

| Command | Description |
|---------|-------------|
| `/lol` | Show ranked stats and winrate for a player |
| `/ingame` | Check whether a player is currently in a live game |
| `/lastgame` | Show details of the latest match |
| `/matchhistory` | Show the last 5 matches |
| `/kda` | Show average KDA and winrate for recent matches |
| `/addprofile` | Add a tracked LoL profile to the database |
| `/removeprofile` | Remove a tracked LoL profile |
| `/teamlol` | Show ranked data for every saved LoL profile |
| `/kontrolajizdenek` | Check which saved LoL profiles are currently in game |

All LoL commands accept `jmeno` (Riot name), `tag` (Riot tag without `#`), and optional `region` (default: `euw1`) where applicable.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Discord bot token |
| `DATABASE_URL` | PostgreSQL connection string |
| `RIOT_API_KEY` | Riot API key |

## Deployment

The project is set up to run on Railway with Docker.

The container installs:

- Python 3.11
- `ffmpeg`
- `nodejs`
- Python dependencies from `requirements.txt`

## Main Dependencies

- `discord.py[voice]`
- `aiohttp`
- `psycopg2-binary`
- `yt-dlp`
- `PyNaCl`

## Notes

- SoundCloud playback uses voice playback support from `discord.py`
- Direct playback features depend on external providers and may be affected by provider-side changes
- Riot features require a valid API key and saved player profiles for team-wide checks
