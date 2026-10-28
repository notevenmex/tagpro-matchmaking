import discord
from discord.ext import commands
import asyncio
import ctypes.util
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
# Optional: sync slash commands to one server instantly instead of waiting on global sync.
GUILD_ID = os.getenv("MEXBOT_GUILD_ID")

# Windows needs an explicit DLL path; on Linux (Railway) libopus0 is installed by the Dockerfile.
OPUS_PATH = os.getenv("OPUS_PATH") or ctypes.util.find_library("opus")
if not discord.opus.is_loaded() and OPUS_PATH:
    discord.opus.load_opus(OPUS_PATH)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"✅ Opus loaded: {discord.opus.is_loaded()}", flush=True)
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
        else:
            synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)", flush=True)
    except Exception as e:
        print(f"❌ Failed to sync commands: {e}", flush=True)

async def main():
    if not TOKEN:
        raise SystemExit("❌ DISCORD_TOKEN is not set")
    async with bot:
        await bot.load_extension("cogs.music")
        await bot.start(TOKEN)

asyncio.run(main())
