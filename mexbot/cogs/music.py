import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import threading
from collections import deque
from pytubefix import Search, YouTube

FFMPEG_EXE = os.getenv("FFMPEG_PATH", "ffmpeg")
# YouTube often blocks cloud server IPs. If /play stops finding songs on Railway,
# set YOUTUBE_PROXY to a residential proxy URL (http://user:pass@host:port).
YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY")
YT_KWARGS = {"proxies": {"http": YOUTUBE_PROXY, "https": YOUTUBE_PROXY}} if YOUTUBE_PROXY else {}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}
if YOUTUBE_PROXY:
    # Stream URLs can be tied to the IP that fetched them, so FFmpeg goes through the proxy too.
    FFMPEG_OPTS["before_options"] += f" -http_proxy {YOUTUBE_PROXY}"

class GuildState:
    def __init__(self):
        self.queue: deque = deque()
        self.current: dict | None = None
        self.loop: bool = False
        self.volume: float = 0.5


def fetch_track(query: str) -> dict | None:
    print(f"[fetch_track] Called with: {query}")
    try:
        if query.startswith("http"):
            yt = YouTube(query, **YT_KWARGS)
            stream = yt.streams.get_audio_only()
            print(f"[fetch_track] Found: {yt.title}")
            return {
                "url": stream.url,
                "title": yt.title,
                "webpage_url": query,
                "duration": yt.length or 0,
                "thumbnail": yt.thumbnail_url or "",
                "uploader": yt.author or "Unknown",
            }
        else:
            results = Search(query, **YT_KWARGS)
            video = results.videos[0]
            print(f"[fetch_track] Found: {video.title}")
            stream = video.streams.get_audio_only()
            print(f"[fetch_track] Got stream URL")
            return {
                "url": stream.url,
                "title": video.title,
                "webpage_url": video.watch_url,
                "duration": video.length or 0,
                "thumbnail": video.thumbnail_url or "",
                "uploader": video.author or "Unknown",
            }
    except Exception as e:
        print(f"[fetch_track] Error: {e}")
        return None


async def fetch_track_async(query: str) -> dict | None:
    loop = asyncio.get_event_loop()
    future = loop.create_future()

    def run():
        try:
            result = fetch_track(query)
            loop.call_soon_threadsafe(future.set_result, result)
        except Exception as e:
            loop.call_soon_threadsafe(future.set_exception, e)

    t = threading.Thread(target=run, daemon=True)
    t.start()

    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=30.0)
    except asyncio.TimeoutError:
        print("[fetch_track_async] Timed out!")
        return None


def fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def now_playing_embed(track: dict, guild_state: GuildState) -> discord.Embed:
    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**[{track['title']}]({track['webpage_url']})**",
        color=0xFF0000,
    )
    embed.add_field(name="Duration", value=fmt_duration(track["duration"]), inline=True)
    embed.add_field(name="Uploader", value=track["uploader"], inline=True)
    embed.add_field(name="Loop", value="✅ On" if guild_state.loop else "❌ Off", inline=True)
    embed.add_field(name="Queue", value=f"{len(guild_state.queue)} song(s) up next", inline=True)
    if track["thumbnail"]:
        embed.set_thumbnail(url=track["thumbnail"])
    embed.set_footer(text="Use /queue to see upcoming tracks")
    return embed


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}

    def get_state(self, guild_id: int) -> GuildState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildState()
        return self.states[guild_id]

    def play_next(self, interaction_or_ctx, voice_client: discord.VoiceClient, state: GuildState):
        print(f"[play_next] Called, queue size: {len(state.queue)}")
        if state.loop and state.current:
            track = state.current
        elif state.queue:
            track = state.queue.popleft()
            state.current = track
        else:
            state.current = None
            print("[play_next] Queue empty, stopping.")
            return

        print(f"[play_next] Playing: {track['title']}")

        try:
            source = discord.FFmpegPCMAudio(track["url"], executable=FFMPEG_EXE, **FFMPEG_OPTS)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)
        except Exception as e:
            print(f"[play_next] Error creating source: {e}")
            return

        def after(err):
            if err:
                print(f"[Playback] Error: {err}")
            else:
                print(f"[Playback] Finished playing")
            state.current = None
            self.play_next(interaction_or_ctx, voice_client, state)

        try:
            voice_client.play(source, after=after)
            print(f"[play_next] Playback started!")
        except Exception as e:
            print(f"[play_next] Error starting playback: {e}")

    @app_commands.command(name="play", description="Play a song from YouTube (name or URL)")
    @app_commands.describe(query="Song name or YouTube URL")
    async def play(self, interaction: discord.Interaction, query: str):
        print(f"[play] Command received: {query}")
        await interaction.response.defer(thinking=True)
        print(f"[play] Deferred response")

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await interaction.followup.send("❌ You must be in a voice channel!", ephemeral=True)

        vc_channel = interaction.user.voice.channel
        guild = interaction.guild
        state = self.get_state(guild.id)

        if guild.voice_client is None:
            print(f"[play] Connecting to voice channel")
            try:
                voice_client = await asyncio.wait_for(vc_channel.connect(), timeout=10.0)
                print(f"[play] Connected!")
            except asyncio.TimeoutError:
                return await interaction.followup.send("❌ Could not connect to voice channel.", ephemeral=True)
        else:
            voice_client = guild.voice_client
            print(f"[play] Already in voice channel")
            if voice_client.channel != vc_channel:
                await voice_client.move_to(vc_channel)

        print(f"[play] Fetching track...")
        track = await fetch_track_async(query)
        print(f"[play] Track fetched: {track['title'] if track else None}")

        if not track:
            return await interaction.followup.send("❌ Could not find that song.", ephemeral=True)

        if voice_client.is_playing() or voice_client.is_paused():
            state.queue.append(track)
            embed = discord.Embed(
                title="➕ Added to Queue",
                description=f"**[{track['title']}]({track['webpage_url']})**",
                color=0x1DB954,
            )
            embed.add_field(name="Duration", value=fmt_duration(track["duration"]), inline=True)
            embed.add_field(name="Position", value=f"#{len(state.queue)}", inline=True)
            if track["thumbnail"]:
                embed.set_thumbnail(url=track["thumbnail"])
            return await interaction.followup.send(embed=embed)

        state.current = track
        source = discord.FFmpegPCMAudio(track["url"], executable=FFMPEG_EXE, **FFMPEG_OPTS)
        source = discord.PCMVolumeTransformer(source, volume=state.volume)

        def after(err):
            if err:
                print(f"[Playback] Error: {err}")
            else:
                print(f"[Playback] Finished, checking queue...")
            state.current = None
            self.play_next(interaction, voice_client, state)

        voice_client.play(source, after=after)
        print("[play] Playback started!")
        await interaction.followup.send(embed=now_playing_embed(track, state))

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if not vc or not vc.is_playing():
            return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        vc.stop()
        await interaction.response.send_message("⏭️ Skipped!")

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Paused.")
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume the paused song")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        state = self.get_state(interaction.guild.id)
        state.queue.clear()
        state.current = None
        state.loop = False
        if vc:
            vc.stop()
            await vc.disconnect()
        await interaction.response.send_message("⏹️ Stopped and disconnected.")

    @app_commands.command(name="nowplaying", description="Show the current song")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        if not state.current:
            return await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
        await interaction.response.send_message(embed=now_playing_embed(state.current, state))

    @app_commands.command(name="queue", description="Show the song queue")
    async def queue(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        embed = discord.Embed(title="📋 Queue", color=0xFF0000)

        if state.current:
            embed.add_field(
                name="🎵 Now Playing",
                value=f"[{state.current['title']}]({state.current['webpage_url']}) `{fmt_duration(state.current['duration'])}`",
                inline=False,
            )

        if state.queue:
            lines = []
            for i, t in enumerate(list(state.queue)[:10], 1):
                lines.append(f"`{i}.` [{t['title']}]({t['webpage_url']}) `{fmt_duration(t['duration'])}`")
            if len(state.queue) > 10:
                lines.append(f"*...and {len(state.queue) - 10} more*")
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Up Next", value="Queue is empty.", inline=False)

        embed.set_footer(text=f"Loop: {'✅ On' if state.loop else '❌ Off'} • Volume: {int(state.volume * 100)}%")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="loop", description="Toggle loop for the current song")
    async def loop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        state.loop = not state.loop
        status = "✅ enabled" if state.loop else "❌ disabled"
        await interaction.response.send_message(f"🔁 Loop {status}.")

    @app_commands.command(name="volume", description="Set volume (0–100)")
    @app_commands.describe(level="Volume level from 0 to 100")
    async def volume(self, interaction: discord.Interaction, level: int):
        if not 0 <= level <= 100:
            return await interaction.response.send_message("❌ Volume must be between 0 and 100.", ephemeral=True)
        state = self.get_state(interaction.guild.id)
        state.volume = level / 100
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = state.volume
        await interaction.response.send_message(f"🔊 Volume set to **{level}%**.")

    @app_commands.command(name="remove", description="Remove a song from the queue by position")
    @app_commands.describe(position="Position in the queue (1 = next up)")
    async def remove(self, interaction: discord.Interaction, position: int):
        state = self.get_state(interaction.guild.id)
        q = list(state.queue)
        if not q or position < 1 or position > len(q):
            return await interaction.response.send_message("❌ Invalid position.", ephemeral=True)
        removed = q.pop(position - 1)
        state.queue = deque(q)
        await interaction.response.send_message(f"🗑️ Removed **{removed['title']}** from the queue.")

    @app_commands.command(name="clear", description="Clear the entire queue")
    async def clear(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild.id)
        state.queue.clear()
        await interaction.response.send_message("🗑️ Queue cleared.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
