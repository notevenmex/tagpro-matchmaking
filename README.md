# Ranked PUGs

A remake of the old TagPro Ranked PUGs site: Top 100 leaderboard, map voting, Discord login,
and admin match reporting with team Elo ratings. FastAPI + SQLAlchemy backend; the front-end is
one HTML file in `static/` served by the same app.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # then edit it (see below)
set -a; source .env; set +a   # Windows PowerShell: load the vars another way, or use python-dotenv
python -m app.seed            # season 4 + the archived players and maps
uvicorn app.main:app --reload
```

Open http://localhost:8000.

**Without Discord set up yet:** put `DEV_LOGIN=1` in `.env`, then visit
`http://localhost:8000/auth/dev-login?discord_id=me&name=Miguel` to sign in as a test user.
Add `me` to `ADMIN_DISCORD_IDS` to see the Report tab.

## Set up Discord login

1. Go to https://discord.com/developers/applications and create an application.
2. Under **OAuth2**, copy the Client ID and Client Secret into `.env`.
3. Add the redirect URL `http://localhost:8000/auth/callback` (and your real domain's
   `/auth/callback` when you deploy), and set `DISCORD_REDIRECT_URI` to match.
4. Put your own Discord user ID in `ADMIN_DISCORD_IDS` (Discord settings → Advanced →
   Developer Mode, then right-click your name → Copy User ID).

## How it works

- **Ratings** (`app/rating.py`): team Elo on the average of each side; everyone on a team moves
  the same amount. Each player has a separate rating per position per season. Elo is shown as
  "points" (`(elo − 1400) / 10`) so numbers look like the original site. Players need 10 games
  to appear on the Top 100. Change `K`, `MIN_GAMES` and `points()` to tune it.
- **Map chance**: each map's vote total (floored at 0) divided by the total across all maps.
- **Archived data**: the seed loads the players and votes from the June 2017 Wayback snapshot.
  Those players have no Discord linked. To give one to its real owner, set `discord_id` on that
  row after they log in once.
- **Matches** are entered by admins on the Report tab for now.
- **Matchmaking** (`app/play.py`): a queue of 8 pops a ready check (30 s; anyone who misses or
  declines is locked out for 5 min and everyone else goes back in the queue with their place).
  The two highest-rated players captain; the lower-rated one is red and picks first, in
  1-2-2-1 order. Five maps are drawn by vote chance and captains ban in turn, blue first, down to
  one. Then all 8 vote on a server and the red captain pastes the TagPro group link. Timeouts
  auto-pick the best player left and auto-ban a random map. The site shows each step as a pop-up
  and gets live updates over the `/ws` WebSocket. Run a single worker: the lock and socket list
  live in memory. Timers and sizes can be changed with the env vars in `.env.example`.
- **Testing a match alone:** with `DEV_LOGIN=1`, join the queue and press **Fill with bots**.
  Archived players fill the other 7 spots and accept, pick and ban on their own.

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/season`, `/api/leaderboard`, `/api/maps`, `/api/players`, `/api/players/{name}` | anyone |
| GET | `/api/me` | anyone |
| POST | `/api/register` | logged in, not yet registered |
| PUT | `/api/maps/{id}/vote` `{score: -5..10 or null}` | registered players |
| POST | `/api/matches` | admins |
| GET | `/api/play` (queue, your lobby), `/api/lobbies/{id}` | anyone |
| POST / DELETE | `/api/queue` (join / leave) | registered players |
| POST | `/api/lobbies/{id}/accept`, `/decline`, `/pick {name}`, `/ban {map_id}`, `/server {server}`, `/group {link}` | players in that lobby |
| POST | `/api/lobbies/{id}/cancel` | admins |
| WS | `/ws` sends `{"type": "update"}` whenever matchmaking changes | anyone |

## Auto group bot

Once a match is drafted (map and server chosen), a bot can create the TagPro group and DM both
captains the link, so nobody has to set it up by hand:

1. `pip install -r requirements-bot.txt && playwright install chromium`
2. Create a bot in the Discord Developer Portal (the same app as login works), invite it to the
   community server, and put its token in `DISCORD_BOT_TOKEN`. Bots can only DM people who share a
   server with them.
3. Set `AUTO_GROUP=1`, and `TAGPRO_NOKISS_SETTING` once you've confirmed the no-kiss option's name.

The group is private, with standard competitive settings, the drafted map and the voted server. The
bot waits in the group until a captain joins, makes them leader, and leaves. If creation fails a
few times, the captains get a DM asking the red captain to make the group and paste the link on the
match page. Research notes, settings and known risks: [docs/tagpro-groups.md](docs/tagpro-groups.md).

## Team voice channels

Voice is required in matches. When the draft ends, the same Discord bot creates **Red** and **Blue**
voice channels (open to anyone, or team-only with `VOICE_PRIVATE=1`), moves anyone already in voice on
the server into theirs, DMs everyone the link, and deletes the channels after the match. Set
`VOICE_CHANNELS=1`, `DISCORD_GUILD_ID` and `VOICE_CATEGORY_ID`. The full server layout, bot invite
link and step-by-step setup (including `python -m app.discord_setup`, which builds the channels for
you) are in [docs/discord-server.md](docs/discord-server.md).

## Tests

```bash
pytest -q
```

## Deploying

Any host that runs a Python web app works (Railway, Render, Fly.io, a VPS). Use Postgres via
`DATABASE_URL`, set `HTTPS_ONLY=1`, keep `DEV_LOGIN=0`, and start with
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
