import os

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
DATABASE_SSLMODE = os.environ.get("DATABASE_SSLMODE", "prefer")
RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY", "")
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")
YOUTUBE_GVS_PO_TOKEN = os.environ.get("YOUTUBE_GVS_PO_TOKEN", "")
YOUTUBE_PLAYER_PO_TOKEN = os.environ.get("YOUTUBE_PLAYER_PO_TOKEN", "")
