# MexBot

MexBot is a YouTube music bot for Discord (discord.py + pytubefix + FFmpeg). It
plays audio in voice channels through slash commands: `/play`, `/skip`,
`/pause`, `/resume`, `/stop`, `/nowplaying`, `/queue`, `/loop`, `/volume`,
`/remove`, `/clear`.

It is independent of the Ranked PUGs site's code: it keeps its queue in
memory and never talks to the site or its database. It does use the same
Discord app (and so the same bot token) as the site; the site only makes REST
calls, so MexBot holding the gateway connection doesn't conflict with it.

## Run it 24/7 on Render

MexBot runs as a Render **Background Worker**: no web port, no health check,
one instance (two copies would mean two gateway sessions fighting over voice).

1. In Render, click **New → Background Worker**, pick
   `notevenmex/tagpro-matchmaking`, and set:
   - **Name:** `mexbot`
   - **Region:** Ohio (same as the site)
   - **Language:** Docker
   - **Root Directory:** `mexbot`
   - **Dockerfile Path:** `./Dockerfile`
   - **Instance type:** the $7 one (0.5 CPU, 512 MB)
2. Under **Environment**, add:

   | Variable | Value |
   |---|---|
   | `DISCORD_TOKEN` | The bot token (same as the site's `DISCORD_BOT_TOKEN`) |
   | `MEXBOT_GUILD_ID` | Optional. A server ID to sync slash commands to instantly |
   | `YOUTUBE_PROXY` | Optional. See "If /play can't find songs" below |

3. Deploy. The logs should show `✅ Logged in as MexBot#3910`,
   `✅ Opus loaded: True` and `✅ Synced 11 slash command(s)`.
4. Keep it at one instance, and stop the copy running on your PC, or two
   copies will be logged in at once.

Because the Root Directory is `mexbot`, Render only redeploys the worker when
files under `mexbot/` change. If the site's `render.yaml` Blueprint gets a
MexBot block, step 1 happens automatically: open the Blueprint and click **Sync**.

The Docker image is Python 3.11 with `ffmpeg` and `libopus0` installed, so none
of the Windows files (`ffmpeg.exe`, `libopus.dll`) are needed. Never commit
`.env` or `cookies.txt`.

## If /play can't find songs

YouTube often blocks requests from cloud servers ("Sign in to confirm you're
not a bot"), which works fine from a home PC. If `[fetch_track] Error` shows up
in the Render logs, set `YOUTUBE_PROXY` to a residential proxy URL such as
`http://user:pass@host:port`. Both the song lookup and FFmpeg's download then go
through it. Updating `pytubefix` in `requirements.txt` also fixes many
breakages after YouTube changes.

## Run it locally

```
cd mexbot
pip install -r requirements.txt
python bot.py
```

Put `DISCORD_TOKEN=...` in `mexbot/.env` (git-ignored). On Windows, set
`OPUS_PATH` to the `libopus-0.x64.dll` that ships with discord.py and have
FFmpeg on PATH (or set `FFMPEG_PATH`). Python 3.11 is recommended; 3.14 broke
PyNaCl voice support.
