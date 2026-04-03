import discord
from discord import app_commands
from pathlib import Path
import time
import asyncio
from collections import deque
from datetime import datetime
from time import monotonic
import psycopg2
import aiohttp
import yt_dlp
from config import (
    DISCORD_TOKEN,
    DATABASE_URL,
    RIOT_API_KEY,
    YOUTUBE_COOKIES,
    YOUTUBE_GVS_PO_TOKEN,
    YOUTUBE_PLAYER_PO_TOKEN,
)

FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

YTDL_OPTIONS = {
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_args": {"youtube": {"player_client": ["mweb", "web"]}},
}

IDLE_DISCONNECT_SECONDS = 300
JUMPSCARE_INTERVAL_SECONDS = 1200
JUMPSCARE_DURATION_SECONDS = 8
JUMPSCARE_URL = "https://soundcloud.com/theabandonedtrustman/golden-freddy-jumpscare-sound?si=2d01cfeda3704e3cb8d49720ca354a01&utm_source=clipboard&utm_medium=text&utm_campaign=social_sharing"
COOKIE_FILE_PATH = Path("/tmp/youtube-cookies.txt")


def configure_ytdl_cookies():
    if not YOUTUBE_COOKIES.strip():
        return

    cookie_text = YOUTUBE_COOKIES.strip()
    if not cookie_text.startswith("# Netscape HTTP Cookie File"):
        cookie_text = "# Netscape HTTP Cookie File\n" + cookie_text

    COOKIE_FILE_PATH.write_text(cookie_text + "\n", encoding="utf-8")
    YTDL_OPTIONS["cookiefile"] = str(COOKIE_FILE_PATH)
    print("YouTube cookies configured for yt-dlp.")


def configure_ytdl_po_tokens():
    youtube_args = YTDL_OPTIONS.setdefault("extractor_args", {}).setdefault("youtube", {})
    po_tokens = []

    if YOUTUBE_GVS_PO_TOKEN.strip():
        po_tokens.append(f"mweb.gvs+{YOUTUBE_GVS_PO_TOKEN.strip()}")

    if YOUTUBE_PLAYER_PO_TOKEN.strip():
        po_tokens.append(f"mweb.player+{YOUTUBE_PLAYER_PO_TOKEN.strip()}")

    if po_tokens:
        youtube_args["po_token"] = po_tokens
        youtube_args["player_client"] = ["mweb", "web"]
        print("YouTube PO tokens configured for yt-dlp.")


configure_ytdl_cookies()
configure_ytdl_po_tokens()


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

cursor.execute("""
    CREATE TABLE IF NOT EXISTS lol_profiles (
        id SERIAL PRIMARY KEY,
        label TEXT NOT NULL,
        riot_name TEXT NOT NULL,
        tag TEXT NOT NULL,
        region TEXT NOT NULL DEFAULT 'euw1'
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
        self.music_queues = {}
        self.current_tracks = {}
        self.looped_guilds = set()
        self.music_idle_deadlines = {}
        self.jumpscare_enabled_guilds = set()
        self.last_jumpscare_at = {}
        self.voice_automation_task = None

    async def setup_hook(self):
        if self.voice_automation_task is None:
            self.voice_automation_task = asyncio.create_task(voice_automation_loop())


bot = MyClient()
jumpscare_group = app_commands.Group(
    name="jumpscare", description="Zapne nebo vypne pravidelny voice jumpscare"
)
bot.tree.add_command(jumpscare_group)


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


async def extract_audio_info(query):
    normalized_query = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    loop = asyncio.get_running_loop()
    option_sets = [
        YTDL_OPTIONS,
        {
            **YTDL_OPTIONS,
            "extractor_args": {"youtube": {"player_client": ["web"]}},
        },
        {
            **YTDL_OPTIONS,
            "extractor_args": {"youtube": {"player_client": ["android"]}},
        },
        {
            **YTDL_OPTIONS,
            "extractor_args": {"youtube": {"player_client": ["tv", "web"]}},
        },
    ]

    last_error = None
    for options in option_sets:
        try:
            return await loop.run_in_executor(
                None,
                lambda current_options=options: yt_dlp.YoutubeDL(
                    current_options
                ).extract_info(normalized_query, download=False),
            )
        except yt_dlp.utils.DownloadError as exc:
            last_error = exc
            print(f"/play yt-dlp fallback failed: {exc}")

    if last_error:
        raise last_error

    raise RuntimeError("yt-dlp could not extract audio info.")


async def log_extraction_diagnostics(query):
    normalized_query = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    loop = asyncio.get_running_loop()
    diagnostic_options = {
        **YTDL_OPTIONS,
        "quiet": False,
        "verbose": True,
        "simulate": True,
        "listformats": True,
    }

    def run_diagnostics():
        try:
            yt_dlp.YoutubeDL(diagnostic_options).extract_info(
                normalized_query, download=False
            )
        except Exception as exc:
            print(f"/play diagnostics failed: {exc!r}")

    await loop.run_in_executor(None, run_diagnostics)


def build_playback_error_message(query: str, exc: Exception):
    message = str(exc)
    lowered_message = message.lower()
    lowered_query = query.lower()
    is_soundcloud = "soundcloud.com" in lowered_query

    if is_soundcloud:
        return f"SoundCloud prehravani selhalo: `{exc}`"

    if "requested format is not available" in lowered_message:
        return (
            "Tohle YouTube video je momentalne blokovane YouTube ochranou a bot z nej nedostal prehratelny audio stream. "
            "Zkus jiny YouTube link nebo radsi SoundCloud."
        )

    if "sign in to confirm you’re not a bot" in lowered_message or "sign in to confirm you're not a bot" in lowered_message:
        return (
            "YouTube chce potvrzeni proti botum. Zkus jiny YouTube link nebo pouzij SoundCloud."
        )

    return f"Prehravani selhalo: `{exc}`"


def select_audio_stream(info):
    formats = info.get("formats") or []
    audio_formats = [
        fmt
        for fmt in formats
        if fmt.get("url")
        and fmt.get("acodec") not in (None, "none")
        and fmt.get("vcodec") == "none"
    ]

    if audio_formats:
        audio_formats.sort(
            key=lambda fmt: (
                fmt.get("abr") or 0,
                fmt.get("tbr") or 0,
                fmt.get("asr") or 0,
            ),
            reverse=True,
        )
        return audio_formats[0]["url"]

    direct_url = info.get("url")
    if direct_url:
        return direct_url

    requested_formats = info.get("requested_formats") or []
    for fmt in requested_formats:
        if fmt.get("url") and fmt.get("acodec") not in (None, "none"):
            return fmt["url"]

    return None


async def ensure_voice_client(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send(
            "Musis byt ve voice roomce, aby bot mohl prehravat.",
            ephemeral=True,
        )
        return None

    channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client
    current_track = bot.current_tracks.get(interaction.guild_id)
    queue = get_guild_queue(interaction.guild_id)

    if voice_client and voice_client.is_playing() and not current_track and not queue:
        voice_client.stop()
        await voice_client.disconnect()
        voice_client = None

    if voice_client and voice_client.channel != channel:
        await voice_client.move_to(channel)
        return voice_client

    if voice_client:
        return voice_client

    return await asyncio.wait_for(channel.connect(), timeout=15)


def get_guild_queue(guild_id: int):
    return bot.music_queues.setdefault(guild_id, deque())


def clear_music_idle_deadline(guild_id: int):
    bot.music_idle_deadlines.pop(guild_id, None)


def arm_music_idle_deadline(guild_id: int):
    bot.music_idle_deadlines[guild_id] = monotonic() + IDLE_DISCONNECT_SECONDS


def is_music_active(guild_id: int):
    guild = bot.get_guild(guild_id)
    voice_client = guild.voice_client if guild else None
    queue = get_guild_queue(guild_id)
    return bool(
        voice_client
        and (voice_client.is_playing() or voice_client.is_paused() or queue or bot.current_tracks.get(guild_id))
    )


def get_occupied_voice_channel(guild: discord.Guild):
    for channel in guild.voice_channels:
        if any(not member.bot for member in channel.members):
            return channel
    return None


async def load_jumpscare_track():
    info = await extract_audio_info(JUMPSCARE_URL)
    if "entries" in info:
        info = next((entry for entry in info["entries"] if entry), None)

    if not info:
        raise RuntimeError("Jumpscare audio nebylo nalezeno.")

    stream_url = select_audio_stream(info)
    if not stream_url:
        raise RuntimeError("Jumpscare audio nema prehratelny stream.")

    return {
        "title": info.get("title", "Golden Freddy Jumpscare"),
        "stream_url": stream_url,
        "webpage_url": info.get("webpage_url", JUMPSCARE_URL),
    }


async def run_jumpscare(guild: discord.Guild, channel: discord.VoiceChannel):
    voice_client = None
    try:
        track = await load_jumpscare_track()
        voice_client = await asyncio.wait_for(channel.connect(), timeout=15)
        source = discord.FFmpegPCMAudio(track["stream_url"], **FFMPEG_OPTIONS)
        voice_client.play(source)
        print(f"/jumpscare started in guild {guild.id} channel {channel.id}")
        await asyncio.sleep(JUMPSCARE_DURATION_SECONDS)
    except Exception as exc:
        print(f"/jumpscare failed in guild {guild.id}: {exc!r}")
    finally:
        if voice_client:
            try:
                if voice_client.is_playing() or voice_client.is_paused():
                    voice_client.stop()
                await voice_client.disconnect()
            except Exception as exc:
                print(f"/jumpscare disconnect failed in guild {guild.id}: {exc!r}")


async def voice_automation_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = monotonic()

            for guild in bot.guilds:
                guild_id = guild.id
                voice_client = guild.voice_client
                queue = get_guild_queue(guild_id)
                deadline = bot.music_idle_deadlines.get(guild_id)

                if (
                    voice_client
                    and deadline is not None
                    and now >= deadline
                    and not voice_client.is_playing()
                    and not voice_client.is_paused()
                    and not queue
                ):
                    bot.music_idle_deadlines.pop(guild_id, None)
                    bot.current_tracks.pop(guild_id, None)
                    bot.looped_guilds.discard(guild_id)
                    await voice_client.disconnect()
                    print(f"/idle disconnect triggered for guild {guild_id}")
                    continue

                if guild_id not in bot.jumpscare_enabled_guilds:
                    continue

                if voice_client:
                    continue

                if is_music_active(guild_id):
                    continue

                last_run = bot.last_jumpscare_at.get(guild_id, 0)
                if now - last_run < JUMPSCARE_INTERVAL_SECONDS:
                    continue

                channel = get_occupied_voice_channel(guild)
                if not channel:
                    continue

                bot.last_jumpscare_at[guild_id] = now
                await run_jumpscare(guild, channel)
        except Exception as exc:
            print(f"/voice automation loop failed: {exc!r}")

        await asyncio.sleep(15)


async def start_track(interaction: discord.Interaction, voice_client, track):
    guild_id = interaction.guild_id
    clear_music_idle_deadline(guild_id)
    source = discord.FFmpegPCMAudio(track["stream_url"], **FFMPEG_OPTIONS)

    def after_playback(error):
        if error:
            print(f"/play after callback failed: {error!r}")
        bot.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(play_next_in_queue(guild_id))
        )

    bot.current_tracks[guild_id] = track
    voice_client.play(source, after=after_playback)
    print(f"/play started: {track['title']}")


async def play_next_in_queue(guild_id: int):
    queue = get_guild_queue(guild_id)
    voice_client = bot.get_guild(guild_id).voice_client if bot.get_guild(guild_id) else None

    if not voice_client:
        bot.current_tracks.pop(guild_id, None)
        return

    if voice_client.is_playing() or voice_client.is_paused():
        return

    current_track = bot.current_tracks.get(guild_id)
    if guild_id in bot.looped_guilds and current_track:
        await start_track(current_track["interaction"], voice_client, current_track)
        return

    if not queue:
        bot.current_tracks.pop(guild_id, None)
        arm_music_idle_deadline(guild_id)
        return

    next_track = queue.popleft()
    await start_track(next_track["interaction"], voice_client, next_track)


@bot.tree.command(name="play", description="Prehraje audio z YouTube do voice roomky")
@app_commands.describe(query="YouTube nebo SoundCloud odkaz, pripadne nazev videa")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    print(f"/play requested by {interaction.user} with query: {query}")

    try:
        voice_client = await ensure_voice_client(interaction)
        if voice_client is None:
            return
        clear_music_idle_deadline(interaction.guild_id)

        print(f"/play voice ready in guild {interaction.guild_id}")
        info = await asyncio.wait_for(extract_audio_info(query), timeout=25)
        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)

        if not info:
            await interaction.followup.send(
                "Nepodarilo se najit zadne prehratelne video.",
                ephemeral=True,
            )
            return

        stream_url = select_audio_stream(info)
        title = info.get("title", query)
        webpage_url = info.get("webpage_url", query)
        print(
            f"/play extracted title={title!r} extractor={info.get('extractor_key')} formats={len(info.get('formats') or [])}"
        )

        if not stream_url:
            await interaction.followup.send(
                "Nepodarilo se ziskat audio stream z YouTube.",
                ephemeral=True,
            )
            return

        track = {
            "title": title,
            "stream_url": stream_url,
            "webpage_url": webpage_url,
            "requested_by": interaction.user.display_name,
            "interaction": interaction,
        }

        queue = get_guild_queue(interaction.guild_id)
        if voice_client.is_playing() or voice_client.is_paused():
            queue.append(track)
            await interaction.followup.send(
                f"Pridano do queue na pozici **{len(queue)}**: **{title}**\n<{webpage_url}>"
            )
            return

        await start_track(interaction, voice_client, track)
        await interaction.followup.send(f"Prehravam **{title}**\n<{webpage_url}>")
    except asyncio.TimeoutError:
        print(f"/play timeout for query: {query}")
        await interaction.followup.send(
            "Prehravani vyprselo. Bot se nestihl pripojit nebo nacist YouTube audio.",
            ephemeral=True,
        )
    except Exception as exc:
        print(f"/play failed: {exc!r}")
        await log_extraction_diagnostics(query)
        await interaction.followup.send(
            build_playback_error_message(query, exc),
            ephemeral=True,
        )


@bot.tree.command(name="stop", description="Zastavi prehravani a vymaze queue")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if not voice_client:
        await interaction.response.send_message(
            "Bot neni pripojeny do zadne voice roomky.",
            ephemeral=True,
        )
        return

    queue = get_guild_queue(interaction.guild_id)
    queue.clear()
    bot.current_tracks.pop(interaction.guild_id, None)
    bot.looped_guilds.discard(interaction.guild_id)
    arm_music_idle_deadline(interaction.guild_id)

    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()

    await interaction.response.send_message("Prehravani zastaveno, queue smazana, bot zustava ve voice.")


@bot.tree.command(name="skip", description="Preskoci aktualni pisnicku")
async def skip_track(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if not voice_client or not (voice_client.is_playing() or voice_client.is_paused()):
        await interaction.response.send_message(
            "Ted nic nehraje, neni co preskocit.",
            ephemeral=True,
        )
        return

    bot.looped_guilds.discard(interaction.guild_id)
    voice_client.stop()
    queue = get_guild_queue(interaction.guild_id)
    if queue:
        await interaction.response.send_message(
            f"Preskakuju aktualni pisnicku. V queue zbyva **{len(queue)}** polozek."
        )
    else:
        await interaction.response.send_message(
            "Aktualni pisnicka byla preskocena. Queue je prazdna."
        )


@bot.tree.command(name="loop", description="Zapne loop pro aktualni pisnicku")
async def loop_track(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    current_track = bot.current_tracks.get(interaction.guild_id)
    if not voice_client or not current_track:
        await interaction.response.send_message(
            "Ted neni zadna aktualni pisnicka, kterou by slo loopovat.",
            ephemeral=True,
        )
        return

    bot.looped_guilds.add(interaction.guild_id)
    await interaction.response.send_message(
        f"Loop zapnuty pro **{current_track['title']}**."
    )


@bot.tree.command(name="stoplooping", description="Vypne loop aktualni pisnicky")
async def stop_looping(interaction: discord.Interaction):
    if interaction.guild_id not in bot.looped_guilds:
        await interaction.response.send_message(
            "Loop uz je vypnuty.",
            ephemeral=True,
        )
        return

    bot.looped_guilds.discard(interaction.guild_id)
    await interaction.response.send_message("Loop vypnuty.")


@jumpscare_group.command(name="on", description="Zapne voice jumpscare kazdych 20 minut")
async def jumpscare_on(interaction: discord.Interaction):
    bot.jumpscare_enabled_guilds.add(interaction.guild_id)
    bot.last_jumpscare_at.setdefault(interaction.guild_id, 0)
    await interaction.response.send_message(
        "Jumpscare zapnuty. Kdyz bude nekdo ve voice a bot nebude resit hudbu, muze se jednou za 20 minut pripojit, pustit zvuk a po 8 sekundach se odpojit."
    )


@jumpscare_group.command(name="off", description="Vypne voice jumpscare")
async def jumpscare_off(interaction: discord.Interaction):
    bot.jumpscare_enabled_guilds.discard(interaction.guild_id)
    bot.last_jumpscare_at.pop(interaction.guild_id, None)
    await interaction.response.send_message("Jumpscare vypnuty.")


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


# ── LoL profiles ─────────────────────────────────────────────────────────────

@bot.tree.command(name="addprofile", description="Přidej LoL profil do sledovaných")
@app_commands.describe(
    label="Přezdívka v Discordu (např. Kuba)",
    jmeno="Riot jméno (např. Faker)",
    tag="Riot tag bez # (např. EUW)",
    region="Server (výchozí: euw1)",
)
async def addprofile(
    interaction: discord.Interaction,
    label: str,
    jmeno: str,
    tag: str,
    region: str = "euw1",
):
    cursor.execute(
        "INSERT INTO lol_profiles (label, riot_name, tag, region) VALUES (%s, %s, %s, %s)",
        (label, jmeno, tag, region.lower()),
    )
    await interaction.response.send_message(
        f"✅ Profil **{label}** (`{jmeno}#{tag}` / {region.upper()}) přidán!"
    )


@bot.tree.command(name="removeprofile", description="Odstraň LoL profil ze sledovaných")
@app_commands.describe(label="Přezdívka profilu který chceš smazat")
async def removeprofile(interaction: discord.Interaction, label: str):
    cursor.execute("DELETE FROM lol_profiles WHERE label = %s", (label,))
    if cursor.rowcount == 0:
        await interaction.response.send_message(
            f"❌ Profil **{label}** nenalezen.", ephemeral=True
        )
    else:
        await interaction.response.send_message(f"🗑️ Profil **{label}** odstraněn.")


@bot.tree.command(name="teamlol", description="Zobraz ranked stats všech uložených profilů")
async def teamlol(interaction: discord.Interaction):
    if not RIOT_API_KEY:
        await interaction.response.send_message("❌ RIOT_API_KEY není nastaven.", ephemeral=True)
        return

    cursor.execute("SELECT label, riot_name, tag, region FROM lol_profiles ORDER BY id ASC")
    profiles = cursor.fetchall()

    if not profiles:
        await interaction.response.send_message(
            "📭 Žádné profily. Přidej je pomocí `/addprofile`."
        )
        return

    await interaction.response.defer()
    headers = {"X-Riot-Token": RIOT_API_KEY}

    TIER_ORDER = {
        "CHALLENGER": 9, "GRANDMASTER": 8, "MASTER": 7,
        "DIAMOND": 6, "EMERALD": 5, "PLATINUM": 4,
        "GOLD": 3, "SILVER": 2, "BRONZE": 1, "IRON": 0,
    }
    DIVISION_ORDER = {"I": 4, "II": 3, "III": 2, "IV": 1}

    results = []

    async with aiohttp.ClientSession() as session:
        for label, riot_name, tag, region in profiles:
            routing = get_routing(region)
            puuid, err = await fetch_puuid(session, riot_name, tag, routing, headers)

            if err:
                results.append({"label": label, "riot_name": riot_name, "tag": tag, "error": err, "sort": -1})
                continue

            ranked_url = f"https://{region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
            async with session.get(ranked_url, headers=headers) as resp:
                if resp.status != 200:
                    results.append({"label": label, "riot_name": riot_name, "tag": tag, "error": str(resp.status), "sort": -1})
                    continue
                entries = await resp.json()

            solo = next((e for e in entries if e["queueType"] == "RANKED_SOLO_5x5"), None)

            if solo:
                sort_val = (
                    TIER_ORDER.get(solo["tier"], 0) * 10000
                    + DIVISION_ORDER.get(solo["rank"], 0) * 1000
                    + solo["leaguePoints"]
                )
                results.append({"label": label, "solo": solo, "sort": sort_val})
            else:
                results.append({"label": label, "riot_name": riot_name, "tag": tag, "solo": None, "sort": -1})

    results.sort(key=lambda x: x["sort"], reverse=True)

    text = "📊 **Team LoL Rankings**\n\n"
    for i, r in enumerate(results, 1):
        if r.get("error"):
            err_msg = "nenalezen" if r["error"] == "not_found" else f"chyba ({r['error']})"
            text += f"**#{i} {r['label']}** (`{r['riot_name']}#{r['tag']}`) — ❌ {err_msg}\n\n"
        elif r.get("solo"):
            solo = r["solo"]
            wins, losses = solo["wins"], solo["losses"]
            winrate = round(wins / (wins + losses) * 100, 1)
            emoji = RANK_EMOJIS.get(solo["tier"], "")
            text += (
                f"**#{i} {r['label']}** — {emoji} {solo['tier']} {solo['rank']} {solo['leaguePoints']} LP\n"
                f"   ✅ {wins}W / ❌ {losses}L — winrate **{winrate}%**\n\n"
            )
        else:
            text += f"**#{i} {r['label']}** (`{r['riot_name']}#{r['tag']}`) — ⚙️ Unranked\n\n"

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
