import discord
from discord import app_commands
import time
import asyncio
from datetime import datetime
import psycopg2
import aiohttp
from config import DISCORD_TOKEN, DATABASE_URL, RIOT_API_KEY


# DB connection
conn = psycopg2.connect(DATABASE_URL, sslmode="require")
conn.autocommit = True
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_stats (
        user_id TEXT PRIMARY KEY,
        username TEXT NOT NULL,
        messages INTEGER DEFAULT 0,
        voice_seconds INTEGER DEFAULT 0,
        voice_join_time INTEGER DEFAULT NULL
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS countdowns (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        target_ts BIGINT NOT NULL,
        created_by TEXT NOT NULL
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
    for guild in bot.guilds:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Příkazy synkovány do: {guild.name}")
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()


# ── Activity tracking ────────────────────────────────────────────────────────

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

    # self-deaf / undeaf
    if (
        before.channel is not None
        and after.channel is not None
        and before.channel == after.channel
    ):
        deaf_now = after.self_deaf or after.deaf
        deaf_before = before.self_deaf or before.deaf

        if deaf_before != deaf_now:
            if deaf_now:
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
            else:
                cursor.execute(
                    """
                    INSERT INTO user_stats (user_id, username, messages, voice_seconds, voice_join_time)
                    VALUES (%s, %s, 0, 0, %s)
                    ON CONFLICT (user_id)
                    DO UPDATE SET voice_join_time = EXCLUDED.voice_join_time, username = EXCLUDED.username
                    """,
                    (user_id, username, int(time.time())),
                )


# ── Leaderboard ──────────────────────────────────────────────────────────────

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
    text = "🏆 Pracovní docházka 🏆\n\n"
    for i, row in enumerate(rows, 1):
        username, messages, voice_secs = row
        hours = voice_secs // 3600
        minutes = (voice_secs % 3600) // 60
        rank = get_rank(voice_secs)
        text += f"**#{i}** {rank} — {username} | **{messages} msgs** | **{hours}h {minutes}m voice**\n"
    return text


@bot.tree.command(name="leaderboard", description="Zobraz žebříček aktivních uživatelů")
async def leaderboard(interaction: discord.Interaction):
    text = build_leaderboard()
    await interaction.response.send_message(text)


@bot.tree.command(name="ranks", description="Zobraz tabulku ranků a potřebné hodiny")
async def ranks(interaction: discord.Interaction):
    text = (
        "🏅 **Tabulka ranků**\n\n"
        "🟫 **Bronze** — 0 h\n"
        "⬜ **Silver** — 14 h\n"
        "🟨 **Gold** — 28 h\n"
        "🟩 **Platinum** — 56 h\n"
        "🟦 **Diamond** — 70 h\n"
        "🟪 **Master** — 84 h\n"
        "👑 **Challenger** — 98 h\n"
    )
    await interaction.response.send_message(text)


# ── Riot API helpers ─────────────────────────────────────────────────────────

champion_cache = {}

QUEUE_NAMES = {
    420: "Solo/Duo",
    440: "Flex",
    450: "ARAM",
    400: "Normal Draft",
    430: "Normal Blind",
    900: "URF",
    1020: "One for All",
    1300: "Nexus Blitz",
    1400: "Ultimate Spellbook",
    0: "Custom",
}

RANK_EMOJIS = {
    "IRON": "⚙️",
    "BRONZE": "🥉",
    "SILVER": "🥈",
    "GOLD": "🥇",
    "PLATINUM": "🪙",
    "EMERALD": "💚",
    "DIAMOND": "💎",
    "MASTER": "🔮",
    "GRANDMASTER": "🔴",
    "CHALLENGER": "🔷",
}


def get_routing(region):
    if region in ("euw1", "eun1", "tr1", "ru"):
        return "europe"
    if region in ("na1", "br1", "la1", "la2"):
        return "americas"
    return "asia"


async def fetch_puuid(session, jmeno, tag, routing, headers):
    url = f"https://{routing}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{jmeno}/{tag}"
    async with session.get(url, headers=headers) as resp:
        if resp.status == 404:
            return None, "not_found"
        if resp.status != 200:
            return None, str(resp.status)
        data = await resp.json()
        return data["puuid"], None


async def load_champion_cache(session):
    global champion_cache
    if champion_cache:
        return
    try:
        async with session.get("https://ddragon.leagueoflegends.com/api/versions.json") as resp:
            versions = await resp.json()
            version = versions[0]
        async with session.get(
            f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"
        ) as resp:
            data = await resp.json()
            for champ in data["data"].values():
                champion_cache[int(champ["key"])] = champ["name"]
    except Exception:
        pass


def riot_check(func):
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if not RIOT_API_KEY:
            await interaction.response.send_message(
                "❌ RIOT_API_KEY není nastaven.", ephemeral=True
            )
            return
        await func(interaction, *args, **kwargs)
    return wrapper


# ── Riot commands ─────────────────────────────────────────────────────────────

@bot.tree.command(name="lol", description="Zkontroluj LoL rank a winrate hráče")
@app_commands.describe(
    jmeno="Riot jméno (např. Faker)",
    tag="Riot tag bez # (např. EUW)",
    region="Server (výchozí: euw1)",
)
async def lol(interaction: discord.Interaction, jmeno: str, tag: str, region: str = "euw1"):
    if not RIOT_API_KEY:
        await interaction.response.send_message("❌ RIOT_API_KEY není nastaven.", ephemeral=True)
        return

    await interaction.response.defer()
    headers = {"X-Riot-Token": RIOT_API_KEY}
    region = region.lower()
    routing = get_routing(region)

    async with aiohttp.ClientSession() as session:
        puuid, err = await fetch_puuid(session, jmeno, tag, routing, headers)
        if err == "not_found":
            await interaction.followup.send(f"❌ Hráč **{jmeno}#{tag}** nenalezen.")
            return
        if err:
            await interaction.followup.send(f"❌ Chyba Riot API ({err}).")
            return

        ranked_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        async with session.get(ranked_url, headers=headers) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"❌ Chyba ranked API ({resp.status}).")
                return
            entries = await resp.json()

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


@bot.tree.command(name="ingame", description="Zkontroluj jestli hráč právě hraje")
@app_commands.describe(
    jmeno="Riot jméno (např. Faker)",
    tag="Riot tag bez # (např. EUW)",
    region="Server (výchozí: euw1)",
)
async def ingame(interaction: discord.Interaction, jmeno: str, tag: str, region: str = "euw1"):
    if not RIOT_API_KEY:
        await interaction.response.send_message("❌ RIOT_API_KEY není nastaven.", ephemeral=True)
        return

    await interaction.response.defer()
    headers = {"X-Riot-Token": RIOT_API_KEY}
    region = region.lower()
    routing = get_routing(region)

    async with aiohttp.ClientSession() as session:
        puuid, err = await fetch_puuid(session, jmeno, tag, routing, headers)
        if err == "not_found":
            await interaction.followup.send(f"❌ Hráč **{jmeno}#{tag}** nenalezen.")
            return
        if err:
            await interaction.followup.send(f"❌ Chyba API ({err}).")
            return

        await load_champion_cache(session)

        spectator_url = f"https://{region}.api.riotgames.com/lol/spectator/v5/active-games/by-summoner/{puuid}"
        async with session.get(spectator_url, headers=headers) as resp:
            if resp.status == 404:
                await interaction.followup.send(f"💤 **{jmeno}#{tag}** momentálně nehraje.")
                return
            if resp.status != 200:
                await interaction.followup.send(f"❌ Chyba Spectator API ({resp.status}).")
                return
            game = await resp.json()

    queue = QUEUE_NAMES.get(game.get("gameQueueConfigId", 0), "Unknown")
    duration = game.get("gameLength", 0) // 60
    participants = game.get("participants", [])
    our = next((p for p in participants if p["puuid"] == puuid), None)
    our_champ = champion_cache.get(our["championId"], f"ID:{our['championId']}") if our else "?"

    text = f"🎮 **{jmeno}#{tag}** právě hraje!\n\n"
    text += f"🗺️ **{queue}** | ⏱️ {duration} min\n"
    text += f"🦸 **Champion:** {our_champ}\n\n"

    team1 = [p for p in participants if p["teamId"] == 100]
    team2 = [p for p in participants if p["teamId"] == 200]

    text += "🔵 **Team 1:**\n"
    for p in team1:
        champ = champion_cache.get(p["championId"], f"ID:{p['championId']}")
        marker = " ◀" if p["puuid"] == puuid else ""
        text += f"  {champ} — {p.get('riotId', '?')}{marker}\n"

    text += "\n🔴 **Team 2:**\n"
    for p in team2:
        champ = champion_cache.get(p["championId"], f"ID:{p['championId']}")
        marker = " ◀" if p["puuid"] == puuid else ""
        text += f"  {champ} — {p.get('riotId', '?')}{marker}\n"

    await interaction.followup.send(text)


@bot.tree.command(name="lastgame", description="Zobraz detail posledního zápasu")
@app_commands.describe(
    jmeno="Riot jméno (např. Faker)",
    tag="Riot tag bez # (např. EUW)",
    region="Server (výchozí: euw1)",
)
async def lastgame(interaction: discord.Interaction, jmeno: str, tag: str, region: str = "euw1"):
    if not RIOT_API_KEY:
        await interaction.response.send_message("❌ RIOT_API_KEY není nastaven.", ephemeral=True)
        return

    await interaction.response.defer()
    headers = {"X-Riot-Token": RIOT_API_KEY}
    region = region.lower()
    routing = get_routing(region)

    async with aiohttp.ClientSession() as session:
        puuid, err = await fetch_puuid(session, jmeno, tag, routing, headers)
        if err == "not_found":
            await interaction.followup.send(f"❌ Hráč **{jmeno}#{tag}** nenalezen.")
            return
        if err:
            await interaction.followup.send(f"❌ Chyba API ({err}).")
            return

        ids_url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count=1"
        async with session.get(ids_url, headers=headers) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"❌ Chyba Match API ({resp.status}).")
                return
            match_ids = await resp.json()
            if not match_ids:
                await interaction.followup.send(f"**{jmeno}#{tag}** nemá žádné záznamy zápasů.")
                return

        match_url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{match_ids[0]}"
        async with session.get(match_url, headers=headers) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"❌ Chyba při načítání zápasu ({resp.status}).")
                return
            match = await resp.json()

    p = next((x for x in match["info"]["participants"] if x["puuid"] == puuid), None)
    if not p:
        await interaction.followup.send("❌ Hráč v zápasu nenalezen.")
        return

    queue = QUEUE_NAMES.get(match["info"]["queueId"], "Unknown")
    duration = match["info"]["gameDuration"] // 60
    win = "✅ Win" if p["win"] else "❌ Loss"
    kda_str = f"{p['kills']}/{p['deaths']}/{p['assists']}"
    cs = p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0)
    cs_per_min = round(cs / max(match["info"]["gameDuration"] / 60, 1), 1)
    damage = p.get("totalDamageDealtToChampions", 0)

    text = f"📋 **Poslední zápas — {jmeno}#{tag}**\n\n"
    text += f"{win} | 🗺️ {queue} | ⏱️ {duration} min\n"
    text += f"🦸 **{p['championName']}**\n"
    text += f"⚔️ **KDA:** {kda_str}\n"
    text += f"🌾 **CS:** {cs} ({cs_per_min}/min)\n"
    text += f"💥 **Damage:** {damage:,}\n"

    await interaction.followup.send(text)


@bot.tree.command(name="matchhistory", description="Zobraz historii posledních 5 zápasů")
@app_commands.describe(
    jmeno="Riot jméno (např. Faker)",
    tag="Riot tag bez # (např. EUW)",
    region="Server (výchozí: euw1)",
)
async def matchhistory(interaction: discord.Interaction, jmeno: str, tag: str, region: str = "euw1"):
    if not RIOT_API_KEY:
        await interaction.response.send_message("❌ RIOT_API_KEY není nastaven.", ephemeral=True)
        return

    await interaction.response.defer()
    headers = {"X-Riot-Token": RIOT_API_KEY}
    region = region.lower()
    routing = get_routing(region)

    async with aiohttp.ClientSession() as session:
        puuid, err = await fetch_puuid(session, jmeno, tag, routing, headers)
        if err == "not_found":
            await interaction.followup.send(f"❌ Hráč **{jmeno}#{tag}** nenalezen.")
            return
        if err:
            await interaction.followup.send(f"❌ Chyba API ({err}).")
            return

        ids_url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count=5"
        async with session.get(ids_url, headers=headers) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"❌ Chyba Match API ({resp.status}).")
                return
            match_ids = await resp.json()
            if not match_ids:
                await interaction.followup.send(f"**{jmeno}#{tag}** nemá žádné záznamy zápasů.")
                return

        matches = []
        for mid in match_ids:
            async with session.get(
                f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}",
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    matches.append(await resp.json())

    text = f"📜 **Match History — {jmeno}#{tag}**\n\n"
    for i, match in enumerate(matches, 1):
        p = next((x for x in match["info"]["participants"] if x["puuid"] == puuid), None)
        if not p:
            continue
        queue = QUEUE_NAMES.get(match["info"]["queueId"], "Unknown")
        duration = match["info"]["gameDuration"] // 60
        result = "✅" if p["win"] else "❌"
        kda_str = f"{p['kills']}/{p['deaths']}/{p['assists']}"
        text += f"**#{i}** {result} **{p['championName']}** | {kda_str} | {queue} | {duration}min\n"

    await interaction.followup.send(text)


@bot.tree.command(name="kda", description="Zobraz průměrné KDA z posledních 10 zápasů")
@app_commands.describe(
    jmeno="Riot jméno (např. Faker)",
    tag="Riot tag bez # (např. EUW)",
    region="Server (výchozí: euw1)",
)
async def kda(interaction: discord.Interaction, jmeno: str, tag: str, region: str = "euw1"):
    if not RIOT_API_KEY:
        await interaction.response.send_message("❌ RIOT_API_KEY není nastaven.", ephemeral=True)
        return

    await interaction.response.defer()
    headers = {"X-Riot-Token": RIOT_API_KEY}
    region = region.lower()
    routing = get_routing(region)

    async with aiohttp.ClientSession() as session:
        puuid, err = await fetch_puuid(session, jmeno, tag, routing, headers)
        if err == "not_found":
            await interaction.followup.send(f"❌ Hráč **{jmeno}#{tag}** nenalezen.")
            return
        if err:
            await interaction.followup.send(f"❌ Chyba API ({err}).")
            return

        ids_url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?count=10"
        async with session.get(ids_url, headers=headers) as resp:
            if resp.status != 200:
                await interaction.followup.send(f"❌ Chyba Match API ({resp.status}).")
                return
            match_ids = await resp.json()
            if not match_ids:
                await interaction.followup.send(f"**{jmeno}#{tag}** nemá žádné záznamy zápasů.")
                return

        kills_t = deaths_t = assists_t = wins = count = 0
        for mid in match_ids:
            async with session.get(
                f"https://{routing}.api.riotgames.com/lol/match/v5/matches/{mid}",
                headers=headers,
            ) as resp:
                if resp.status != 200:
                    continue
                match = await resp.json()
                p = next((x for x in match["info"]["participants"] if x["puuid"] == puuid), None)
                if not p:
                    continue
                kills_t += p["kills"]
                deaths_t += p["deaths"]
                assists_t += p["assists"]
                if p["win"]:
                    wins += 1
                count += 1

    if count == 0:
        await interaction.followup.send("❌ Nepodařilo se načíst zápasy.")
        return

    avg_k = round(kills_t / count, 1)
    avg_d = round(deaths_t / count, 1)
    avg_a = round(assists_t / count, 1)
    ratio = round((kills_t + assists_t) / max(deaths_t, 1), 2)
    winrate = round(wins / count * 100, 1)

    text = f"📊 **KDA — {jmeno}#{tag}** (posledních {count} zápasů)\n\n"
    text += f"⚔️ **Avg KDA:** {avg_k} / {avg_d} / {avg_a}\n"
    text += f"📈 **KDA Ratio:** {ratio}\n"
    text += f"✅ **Winrate:** {winrate}% ({wins}W / {count - wins}L)\n"

    await interaction.followup.send(text)


# ── Countdown ────────────────────────────────────────────────────────────────

@bot.tree.command(name="setcountdown", description="Nastav nový odpočet a ulož ho")
@app_commands.describe(
    name="Název odpočtu (např. Vánoce)",
    year="Rok (např. 2026)",
    month="Měsíc (1-12)",
    day="Den (1-31)",
    hour="Hodina (0-23)",
    minute="Minuta (0-59)",
)
async def setcountdown(
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
                "❌ Datum musí být v budoucnosti!", ephemeral=True
            )
            return

        target_ts = int(target_date.timestamp())
        created_by = interaction.user.name

        cursor.execute(
            "INSERT INTO countdowns (name, target_ts, created_by) VALUES (%s, %s, %s)",
            (name, target_ts, created_by),
        )

        diff = target_date - now
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60

        await interaction.response.send_message(
            f"✅ Odpočet **{name}** uložen!\n"
            f"📅 Cíl: {target_date.strftime('%d.%m.%Y %H:%M')}\n"
            f"⏳ Zbývá: {days}d {hours}h {minutes}m"
        )

    except ValueError:
        await interaction.response.send_message("❌ Neplatné datum!", ephemeral=True)


@bot.tree.command(name="countdown", description="Zobraz všechny aktivní odpočty")
async def countdown(interaction: discord.Interaction):
    cursor.execute(
        "SELECT id, name, target_ts, created_by FROM countdowns ORDER BY target_ts ASC"
    )
    rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message("📭 Žádné aktivní odpočty.")
        return

    now_ts = int(datetime.now().timestamp())
    finished = []
    text = "⏱️ **Aktivní odpočty**\n\n"

    for row in rows:
        cd_id, name, target_ts, created_by = row
        remaining = target_ts - now_ts

        if remaining <= 0:
            finished.append(cd_id)
            text += f"✅ **{name}** — hotovo! *(přidal {created_by})*\n"
        else:
            days = remaining // 86400
            hours = (remaining % 86400) // 3600
            minutes = (remaining % 3600) // 60
            seconds = remaining % 60
            target_str = datetime.fromtimestamp(target_ts).strftime("%d.%m.%Y %H:%M")
            text += (
                f"⏳ **{name}** — {days}d {hours}h {minutes}m {seconds}s\n"
                f"   📅 {target_str} *(přidal {created_by})*\n\n"
            )

    if finished:
        cursor.execute(
            f"DELETE FROM countdowns WHERE id = ANY(%s)", (finished,)
        )

    await interaction.response.send_message(text)


bot.run(DISCORD_TOKEN)
