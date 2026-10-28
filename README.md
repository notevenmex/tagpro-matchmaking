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

## API

| Method | Path | Who |
|---|---|---|
| GET | `/api/season`, `/api/leaderboard`, `/api/maps`, `/api/players`, `/api/players/{name}` | anyone |
| GET | `/api/me` | anyone |
| POST | `/api/register` | logged in, not yet registered |
| PUT | `/api/maps/{id}/vote` `{score: -5..10 or null}` | registered players |
| POST | `/api/matches` | admins |

## Tests

```bash
pytest -q
```

## Deploying

Any host that runs a Python web app works (Railway, Render, Fly.io, a VPS). Use Postgres via
`DATABASE_URL`, set `HTTPS_ONLY=1`, keep `DEV_LOGIN=0`, and start with
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
