# Community Discord server and bot

One Discord bot does everything: it DMs captains the TagPro group link ([tagpro-groups.md](tagpro-groups.md))
and gives every match its own **Red** and **Blue** voice channels. It runs inside the website process
(started from the app's lifespan), so there's no separate worker to deploy. It only uses Discord's REST
API, so it needs no gateway connection and no privileged intents.

## Server layout

| Category | Channels | Who sees it |
|---|---|---|
| 📢 Info | `#welcome`, `#rules`, `#announcements`, `#how-to-play` | everyone, only Moderators post |
| 🎮 Ranked PUGs | `#general`, `#looking-for-pug`, `#match-results` (read-only), `#support` | everyone |
| 🔊 Voice | Waiting Room, Post-Game Lounge, Casual 1, Casual 2 | everyone |
| 🏆 Live Matches | *empty: the bot adds* `🔴 Match 12 · Red` *and* `🔵 Match 12 · Blue` *here* | everyone (or only that team with `VOICE_PRIVATE=1`) |
| 🛠 Staff | `#staff-chat`, `#bot-log` | Moderators |

Roles, top to bottom: **Admin** (you), **Ranked PUGs** (the bot's own role, added when it joins),
**Moderator** (can see and join every match channel, move, mute and time people out), **Player**.

## How team voice works

1. When the draft finishes (map veto starts), the bot creates the two team channels under 🏆 Live Matches.
   Anyone on the server can join them, so friends can listen in. Set `VOICE_PRIVATE=1` to lock each
   channel to its own team's players (plus Moderators) instead.
2. Anyone already in a voice channel on the server (the **Waiting Room** is the place to wait in the
   queue) is moved straight into their team channel. Discord doesn't let a bot pull someone into voice
   who isn't connected, so everyone also gets a DM with a link to their channel, and the match page
   shows a **Join Red/Blue voice** button.
3. When the match is over (score verified, disputed, or the lobby cancelled), the channels are deleted
   two minutes later (`VOICE_GRACE_SECONDS`). Anyone still in them is moved to the **Post-Game Lounge**
   first. A channel that lives longer than `VOICE_MAX_HOURS` (3) is removed anyway, and leftover match
   channels are cleaned up when the site restarts.

Players need a Discord account linked on the site (they sign in with Discord, so they do) and must be
members of the server. Put "voice is required in ranked matches" in `#rules`.

## Setup, step by step

### 1. Create the server

1. In Discord, click **+** (Add a Server) > **Create My Own** > **For a club or community**.
   Name it (e.g. *TagPro Ranked PUGs*).
2. **Server Settings > Enable Community** (optional, but gives you a rules screen and announcement
   channels). Turn on **Settings > Safety Setup > Verification Level: Medium** to slow down spam accounts.
3. Turn on **Developer Mode** for yourself: **User Settings > Advanced > Developer Mode**. You need it to
   copy IDs (right-click > Copy ID).
4. Right-click the server icon > **Copy Server ID**. That's `DISCORD_GUILD_ID`.

### 2. Set up the bot (same Discord app as the website login)

1. Go to <https://discord.com/developers/applications> and open the app you made for Discord login
   (or **New Application** if you haven't yet).
2. **Bot** tab: set the name and avatar, click **Reset Token**, and copy it. That's `DISCORD_BOT_TOKEN`.
   Keep it secret: put it only in Railway's variables (or your local `.env`), never in the repo or a chat.
   Leave **Public Bot** off and all **Privileged Gateway Intents** off; the bot doesn't need them.
3. Invite it to your server with this link, replacing `CLIENT_ID` with the app's **Application ID**
   (General Information tab):

   ```
   https://discord.com/oauth2/authorize?client_id=CLIENT_ID&scope=bot&permissions=321998352
   ```

   That grants: View Channels, Manage Channels, Manage Roles, Send Messages, Embed Links, Read Message
   History, Connect, Speak, Video, Use Voice Activity and Move Members. Manage Roles is needed so it can
   set each match channel's permissions.

### 3. Create the channels and roles

Either run the setup script once, which creates everything in the table above that's missing (it never
deletes or changes what you already have) and prints the IDs you need:

```bash
# from the repo folder, with the requirements installed
DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... python -m app.discord_setup
```

On Windows PowerShell:

```powershell
$env:DISCORD_BOT_TOKEN="..."; $env:DISCORD_GUILD_ID="..."; python -m app.discord_setup
```

Or make them by hand, and copy the IDs with right-click > Copy ID:

- Category **🏆 Live Matches**: a normal category everyone can see. Its ID is `VOICE_CATEGORY_ID`.
- Voice channel **Post-Game Lounge**: its ID is `VOICE_LOBBY_CHANNEL_ID` (optional).
- Role **Moderator**: its ID is `VOICE_STAFF_ROLE_ID` (optional).

Then in **Server Settings > Roles**, drag the bot's role (**Ranked PUGs** or the bot's name) above
**Moderator** and **Player**, and give yourself and your mods the Moderator role.

### 4. Turn it on

Add to the site's environment (Railway > your service > Variables):

```
DISCORD_BOT_TOKEN=...
DISCORD_GUILD_ID=...
VOICE_CHANNELS=1
VOICE_CATEGORY_ID=...
VOICE_LOBBY_CHANNEL_ID=...
VOICE_STAFF_ROLE_ID=...
```

Redeploy. On startup the log says nothing if it's fine; if a variable is missing it logs
`VOICE_CHANNELS=1 but ... isn't set`. To test, play a match with "Fill with bots" while you sit in the
Waiting Room: you should be moved into your team's channel when the draft ends.

### 5. Write the rules

Fill `#welcome`, `#rules` and `#how-to-play` with the site link (<https://tagpropugs.com>), how to
queue, that team voice is required, how captains report scores, and how to dispute a result.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `VOICE_CHANNELS` | `0` | `1` turns team voice channels on |
| `DISCORD_BOT_TOKEN` | | the bot's token (shared with the captain DMs) |
| `DISCORD_GUILD_ID` | | the community server's ID |
| `VOICE_CATEGORY_ID` | | category the match channels go in |
| `VOICE_LOBBY_CHANNEL_ID` | | voice channel players are moved to when the match channels close |
| `VOICE_STAFF_ROLE_ID` | | role that can always join match channels, even private ones |
| `VOICE_GRACE_SECONDS` | `120` | how long the channels stay after the match ends |
| `VOICE_MAX_HOURS` | `3` | channels older than this are removed even if the match never ended |
| `VOICE_DMS` | `1` | `0` stops the "join your team's voice" DMs |
| `VOICE_PRIVATE` | `0` | `1` lets only that team's players (and `VOICE_STAFF_ROLE_ID`) join each channel |

## Not done yet

- The bot doesn't check that players actually joined voice. Discord's REST API can tell (it already
  checks who's in voice to move them), so a "voice no-show" warning for captains or mods would be a
  small follow-up.
- Nothing is posted to `#match-results` yet; that would hook into captain score reporting.
- If the site runs as more than one process, each would run its own bot. Keep one uvicorn worker, as
  [play.py](../app/play.py) already requires.
