import discord
from discord import app_commands
import time
import psycopg2
from config import DISCORD_TOKEN, DATABASE_URL

# Připojení k DB
conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cursor = conn.cursor()

# Vytvoření tabulky pokud neexistuje
cursor.execute("""
CREATE TABLE IF NOT EXISTS user_stats (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    messages INTEGER DEFAULT 0,
    voice_seconds INTEGER DEFAULT 0,
    voice_join_time INTEGER DEFAULT NULL
)
""")

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True


class MyClient(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()


bot = MyClient()


@bot.event
async def on_ready():
    print(f"Bot je online jako {bot.user}")


# MESSAGE TRACKING
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = str(message.author.id)
    username = message.author.name

    cursor.execute(
        """
    INSERT INTO user_stats (user_id, username, messages)
    VALUES (%s, %s, 1)
    ON CONFLICT (user_id)
    DO UPDATE SET messages = user_stats.messages + 1, username = EXCLUDED.username
    """,
        (user_id, username),
    )

    # podpora starého příkazu !leaderboard
    if message.content == "!leaderboard":
        text = build_leaderboard()
        await message.channel.send(text)


# /leaderboard
@bot.tree.command(name="leaderboard", description="Zobraz žebříček aktivních uživatelů")
async def leaderboard(interaction: discord.Interaction):
    text = build_leaderboard()
    await interaction.response.send_message(text)


RANK_TITLES = {
    1: "👑 Mistr KADIČ 👑",
    2: "🤡 Wannabe Mistr kadič 🤡",
    3: "🚽 Kaďet 🚽",
    4: "💩 Srágora 💩",
    5: "💩 Majsneros 💩",
    6: "💩 Liboros 💩",
    7: "💩 Protržená prdel 💩",
}


def build_leaderboard():
    cursor.execute("""
    SELECT username, messages, voice_seconds
    FROM user_stats
    ORDER BY (messages + voice_seconds/60) DESC
    LIMIT 7
    """)
    rows = cursor.fetchall()
    text = "🏆Pracovní docházka 🏆\n\n"
    for i, row in enumerate(rows, 1):
        hours = row[2] // 3600
        minutes = (row[2] % 3600) // 60
        title = RANK_TITLES.get(i, str(i))
        text += (
            f"{title} — {row[0]} | **{row[1]} msgs** | **{hours}h {minutes}m voice**\n"
        )
    return text


# VOICE TRACKING
@bot.event
async def on_voice_state_update(member, before, after):
    user_id = str(member.id)
    username = member.name

    # join
    if before.channel is None and after.channel is not None:
        cursor.execute(
            """
        INSERT INTO user_stats (user_id, username, messages, voice_seconds, voice_join_time)
        VALUES (%s, %s, 0, 0, %s)
        ON CONFLICT (user_id)
        DO UPDATE SET voice_join_time = EXCLUDED.voice_join_time, username = EXCLUDED.username
        """,
            (user_id, username, int(time.time())),
        )

    # leave
    if before.channel is not None and after.channel is None:
        cursor.execute(
            """
        SELECT voice_join_time FROM user_stats
        WHERE user_id=%s
        """,
            (user_id,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            duration = int(time.time()) - row[0]
            cursor.execute(
                """
            UPDATE user_stats
            SET voice_seconds = voice_seconds + %s,
                voice_join_time = NULL
            WHERE user_id=%s
            """,
                (duration, user_id),
            )


bot.run(DISCORD_TOKEN)
