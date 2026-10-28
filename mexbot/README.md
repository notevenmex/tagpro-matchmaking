# MexBot

MexBot is a YouTube music bot for Discord (discord.py + pytubefix + FFmpeg). It
plays audio in voice channels through slash commands: `/play`, `/skip`,
`/pause`, `/resume`, `/stop`, `/nowplaying`, `/queue`, `/loop`, `/volume`,
`/remove`, `/clear`.

It is independent of the Ranked PUGs site: it has its own token, keeps its
queue in memory, and never talks to the site or its database. It lives in this
repo so it can be deployed as a second service in the same Railway project.

## Run it 24/7 on Railway

MexBot runs as an always-on worker: no web port, no health check, one replica
(two copies would mean two gateway sessions fighting over voice).

1. In the Railway project that hosts tagpropugs.com, click **New → GitHub Repo**
   and pick `notevenmex/tagpro-matchmaking` again. Rename the service to `mexbot`.
2. In the service's **Settings**:
   - **Root Directory**: `/mexbot`
   - **Config-as-code → Railway config file**: `/mexbot/railway.json`
     (Railway reads this path from the repo root, not from the root directory).
     That file builds `mexbot/Dockerfile`, runs 1 replica, restarts on any exit,
     and only redeploys when files under `mexbot/` change.
   - Leave **Networking** alone: don't generate a domain, the bot serves no HTTP.
3. In the service's **Variables**, add:

   | Variable | Value |
   |---|---|
   | `DISCORD_TOKEN` | MexBot's bot token (the one in `C:\MexBot\.env`) |
   | `MEXBOT_GUILD_ID` | Optional. A server ID to sync slash commands to instantly |
   | `YOUTUBE_PROXY` | Optional. See "If /play can't find songs" below |

4. Deploy. The logs should show `✅ Logged in as MexBot#3910`,
   `✅ Opus loaded: True` and `✅ Synced 11 slash command(s)`.
5. Stop the copy running on your PC, or both will be logged in at once.

The Docker image is Python 3.11 with `ffmpeg` and `libopus0` installed, so none
of the Windows files (`ffmpeg.exe`, `libopus.dll`) are needed. Never commit
`.env` or `cookies.txt`.

## If /play can't find songs

YouTube often blocks requests from cloud servers ("Sign in to confirm you're
not a bot"), which works fine from a home PC. If `[fetch_track] Error` shows up
in the Railway logs, set `YOUTUBE_PROXY` to a residential proxy URL such as
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
