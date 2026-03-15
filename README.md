# Discord Activity Bot

A Discord bot that tracks user activity and provides server statistics.

## Features

- **Activity Tracking** — automatically tracks messages and voice channel time for every user
- **Leaderboard** — ranked leaderboard sorted by combined voice time and message count
- **Rank System** — users earn ranks based on total voice hours (Bronze → Silver → Gold → Platinum → Diamond → Master → Challenger)
- **League of Legends** — look up any player's ranked stats and winrate via Riot API
- **Countdown** — live countdown timer to any future date

## Commands

| Command | Description |
|---------|-------------|
| `/leaderboard` | Show the server activity leaderboard |
| `/ranks` | Show the rank table and required hours |
| `/lol` | Look up a player's LoL rank and winrate |
| `/countdown` | Start a live countdown to a date |

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
| `RIOT_API_KEY` | Riot Games API key (optional) |

### Requirements

```
discord.py
psycopg2-binary
python-dotenv
aiohttp
```

## Hosting

The bot is designed to run on [Railway](https://railway.app) with a PostgreSQL database.
