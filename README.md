# Discord Activity Bot

A Discord bot that tracks user activity, provides server statistics, and integrates with the Riot Games API for League of Legends data.

## Features

- **Activity Tracking** — automatically tracks messages and voice channel time for every user
- **Leaderboard** — ranked leaderboard sorted by combined voice time and message count
- **Rank System** — users earn ranks based on total voice hours (Bronze → Silver → Gold → Platinum → Diamond → Master → Challenger)
- **League of Legends** — rank, winrate, match history, KDA stats, and live game info via Riot API
- **Countdown** — live countdown timer to any future date

## Commands

### Server Activity

| Command | Description |
|---------|-------------|
| `/leaderboard` | Show the server activity leaderboard |
| `/ranks` | Show the rank table and required hours |
| `/setcountdown` | Start a countdown to a date |
| `/countdown` | Show all active countdowns |

### League of Legends

| Command | Description |
|---------|-------------|
| `/lol` | Player's ranked stats and winrate (Solo/Duo + Flex) |
| `/ingame` | Check if a player is currently in a game (champion, teams) |
| `/lastgame` | Detail of the last match (champion, KDA, CS, damage) |
| `/matchhistory` | Overview of the last 5 matches |
| `/kda` | Average KDA and winrate from the last 10 matches |

All LoL commands accept `jmeno` (Riot name), `tag` (Riot tag without #), and optional `region` (default: `euw1`).

## Rank System

| Rank | Voice Hours Required |
|------|----------------------|
| Bronze | 0h |
| Silver | 14h |
| Gold | 28h |
| Platinum | 56h |
| Diamond | 70h |
| Master | 84h |
| Challenger | 98h |

## Setup

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Your Discord bot token |
| `RAILWAY_DATABASE_URL` | PostgreSQL connection string |
| `RIOT_API_KEY` | Riot Games API key (optional — disables LoL commands if missing) |

### Requirements

```
discord.py
psycopg2-binary
python-dotenv
aiohttp
```

## Hosting

The bot is designed to run on [Railway](https://railway.app) with a PostgreSQL database.
