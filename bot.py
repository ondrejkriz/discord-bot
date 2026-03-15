import discord
from discord import app_commands
import time
import asyncio
from datetime import datetime
import psycopg2
import aiohttp
from config import DISCORD_TOKEN, DATABASE_URL, RIOT_API_KEY

# Připojení k DB
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
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
        pass


bot = MyClient()


@bot.event
async def on_ready():
    print(f"Bot je online jako {bot.user}")
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Příkazy synkovány do: {guild.name}")


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


def get_rank(voice_seconds):
    hours = voice_seconds / 3600
    if hours >= 98:
        return "👑 Challenger"
    elif hours >= 84:
        return "🟪 Master"
    elif hours >= 70:
        return "🟦 Diamond"
    elif hours >= 56:
        return "🟩 Platinum"
    elif hours >= 28:
        return "🟨 Gold"
    elif hours >= 14:
        return "⬜ Silver"
    else:
        return "🟫 Bronze"


def build_leaderboard():
    cursor.execute("""
    SELECT username, messages, voice_seconds
    FROM user_stats
    ORDER BY (messages + voice_seconds/60) DESC
    """)
    rows = cursor.fetchall()
    text = "🏆 LEADERBOARD 🏆\n\n"
    for i, row in enumerate(rows,1):
        username, messages, voice_secs = row
        hours = voice_secs // 3600
        minutes = (voice_secs % 3600) // 60
        rank = get_rank(voice_secs)
        text += f"**#{i}** {rank} — {username} | **{messages} msgs** | **{hours}h {minutes}m voice**\n"
    return text


# /ranks
@bot.tree.command(name="ranks", description="Zobraz tabulku ranků a potřebné hodiny")
async def ranks(interaction: discord.Interaction):
    text = (
        "🏅 **Ranks**\n\n"
        "🟫 **Bronze** — 0 h\n"
        "⬜ **Silver** — 14 h\n"
        "🟨 **Gold** — 28 h\n"
        "🟩 **Platinum** — 56 h\n"
        "🟦 **Diamond** — 70 h\n"
        "🟪 **Master** — 84 h\n"
        "👑 **Challenger** — 98 h\n"
    )
    await interaction.response.send_message(text)


# VOICE TRACKING
@bot.event
async def on_voice_state_update(member, before, after):
    user_id = str(member.id)
    username = member.name

    # join
    if before.channel is None and after.channel is not None:
        if not after.self_deaf and not after.deaf:
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
            "SELECT voice_join_time FROM user_stats WHERE user_id=%s", (user_id,)
        )
        row = cursor.fetchone()
        if row and row[0]:
            duration = int(time.time()) - row[0]
            cursor.execute(
                """
            UPDATE user_stats
            SET voice_seconds = voice_seconds + %s, voice_join_time = NULL
            WHERE user_id=%s
            """,
                (duration, user_id),
            )

    # self-deaf / undeaf (bez změny kanálu)
    if (
        before.channel is not None
        and after.channel is not None
        and before.channel == after.channel
    ):
        deaf_now = after.self_deaf or after.deaf
        deaf_before = before.self_deaf or before.deaf
        if deaf_before != deaf_now:
            if deaf_now:
                # Nasadil hluchotu → zastav timer
                cursor.execute(
                    "SELECT voice_join_time FROM user_stats WHERE user_id=%s",
                    (user_id,),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    duration = int(time.time()) - row[0]
                    cursor.execute(
                        """
                    UPDATE user_stats
                    SET voice_seconds = voice_seconds + %s, voice_join_time = NULL
                    WHERE user_id=%s
                    """,
                        (duration, user_id),
                    )
            else:
                # Sundal hluchotu → spusť timer
                cursor.execute(
                    """
                INSERT INTO user_stats (user_id, username, messages, voice_seconds, voice_join_time)
                VALUES (%s, %s, 0, 0, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET voice_join_time = EXCLUDED.voice_join_time, username = EXCLUDED.username
                """,
                    (user_id, username, int(time.time())),
                )

# /lol
@bot.tree.command(name="lol", description="Zkontroluj LoL rank a winrate hráče")
@app_commands.describe(
    jmeno="Riot jméno (např. Faker)",
    tag="Riot tag bez # (např. EUW)",
    region="Server (výchozí: euw1)"
)
async def lol(
    interaction: discord.Interaction,
    jmeno: str,
    tag: str,
    region: str = "euw1"
):
    await interaction.response.defer()

    headers = {"X-Riot-Token": RIOT_API_KEY}
    region = region.lower()
    routing = "europe" if region in ("euw1", "eun1", "tr1", "ru") else "americas" if region in ("na1", "br1", "la1", "la2") else "asia"

    async with aiohttp.ClientSession() as session:
        # 1. PUUID podle Riot ID
        account_url = f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{jmeno}/{tag}"
        async with session.get(account_url, headers=headers) as resp:
            if resp.status == 404:
                await interaction.followup.send(f"❌ Hráč **{jmeno}#{tag}** nenalezen.")
                return
            if resp.status != 200:
                await interaction.followup.send(f"❌ Chyba Riot API ({resp.status}).")
                return
            account = await resp.json()
            puuid = account["puuid"]

        # 2. Ranked data přímo přes PUUID
        ranked_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        async with session.get(ranked_url, headers=headers) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"❌ Nepodařilo se načíst ranked data ({resp.status}).")
                return
            entries = await resp.json()

    RANK_EMOJIS = {
        "IRON": "⚙️", "BRONZE": "🥉", "SILVER": "🥈", "GOLD": "🥇",
        "PLATINUM": "🪙", "EMERALD": "💚", "DIAMOND": "💎",
        "MASTER": "🔮", "GRANDMASTER": "🔴", "CHALLENGER": "🔷"
    }

    # Najdi Solo/Duo queue
    solo = next((e for e in entries if e["queueType"] == "RANKED_SOLO_5x5"), None)
    flex = next((e for e in entries if e["queueType"] == "RANKED_FLEX_SR"), None)

    text = f"📊 **{jmeno}#{tag}** — {region.upper()}\n\n"

    if solo:
        wins, losses = solo["wins"], solo["losses"]
        winrate = round(wins / (wins + losses) * 100, 1)
        emoji = RANK_EMOJIS.get(solo["tier"], "")
        text += f"{emoji} **Solo/Duo:** {solo['tier']} {solo['rank']} — {solo['leaguePoints']} LP\n"
        text += f"   ✅ {wins}W / ❌ {losses}L — winrate **{winrate}%**\n\n"
    else:
        text += "⚙️ **Solo/Duo:** Unranked\n\n"

    if flex:
        wins, losses = flex["wins"], flex["losses"]
        winrate = round(wins / (wins + losses) * 100, 1)
        emoji = RANK_EMOJIS.get(flex["tier"], "")
        text += f"{emoji} **Flex:** {flex['tier']} {flex['rank']} — {flex['leaguePoints']} LP\n"
        text += f"   ✅ {wins}W / ❌ {losses}L — winrate **{winrate}%**\n"
    else:
        text += "⚙️ **Flex:** Unranked\n"

    await interaction.followup.send(text)


# /countdown
@bot.tree.command(name="countdown", description="Odpočet do určitého data")
@app_commands.describe(
    name="Název odpočtu (např. Vánoce)",
    year="Rok (např. 2026)",
    month="Měsíc (1-12)",
    day="Den (1-31)",
    hour="Hodina (0-23)",
    minute="Minuta (0-59)",
)
async def countdown(
    interaction: discord.Interaction,
    name: str,
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
):
    try:
        target_date = datetime(year, month, day, hour, minute)
        now = datetime.now()

        if target_date <= now:
            await interaction.response.send_message(
                "Datum musí být v budoucnosti!", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"⏱️ {name}: {target_date.strftime('%d.%m.%Y %H:%M')}"
        )
        message = await interaction.original_response()

        while True:
            now = datetime.now()
            if now >= target_date:
                await message.edit(content=f"✅ {name} - Čas nastal!")
                break

            diff = target_date - now
            days = diff.days
            hours = diff.seconds // 3600
            minutes = (diff.seconds % 3600) // 60
            seconds = diff.seconds % 60

            await message.edit(
                content=f"⏱️ {name}: {days}d {hours}h {minutes}m {seconds}s"
            )
            await asyncio.sleep(1)

    except ValueError:
        await interaction.response.send_message("Neplatné datum!", ephemeral=True)



bot.run(DISCORD_TOKEN)
